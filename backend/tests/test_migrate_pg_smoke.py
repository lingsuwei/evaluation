"""
A-06 SQLite → PostgreSQL 迁移集成 smoke（需 PG_ALEMBIC_SMOKE=1）。

  cd backend
  set PG_ALEMBIC_SMOKE=1
  pytest tests/test_migrate_pg_smoke.py -v
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from database import Base
from tests.pg_migrate_seed import seed_sqlite_source
from tests.pg_smoke_helpers import ensure_v_run_analytics_view, temporary_pg_database
from services.migrate_sqlite_to_pg_service import run_migration, verify_migration

pytestmark = [pytest.mark.integration, pytest.mark.postgresql]


def _bootstrap_pg_schema(pg_url: str) -> None:
    """目标 PG 使用 ORM create_all + v_run_analytics（与 dev init_db + Alembic 002 一致）。"""
    engine = create_engine(pg_url)
    try:
        Base.metadata.create_all(engine)
        ensure_v_run_analytics_view(engine)
    finally:
        engine.dispose()


def _assert_v_run_analytics_row(pg_url: str, run_id: str) -> None:
    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT runtime_tokens_total, checkpoint_total, checkpoint_passed "
                        "FROM v_run_analytics WHERE agent_run_id = :id"
                    ),
                    {"id": run_id},
                )
                .mappings()
                .one()
            )
            assert row["runtime_tokens_total"] == 42.0
            assert row["checkpoint_total"] == 1
            assert row["checkpoint_passed"] == 1
    finally:
        engine.dispose()


@pytest.fixture()
def pg_migrate_env(tmp_path):
    sqlite_path = tmp_path / "source.db"
    sqlite_url = f"sqlite:///{sqlite_path.as_posix()}"

    with temporary_pg_database("onto_eval_migrate") as pg_url:
        _bootstrap_pg_schema(pg_url)
        sqlite_engine = create_engine(
            sqlite_url,
            connect_args={"check_same_thread": False},
        )
        run_id = seed_sqlite_source(sqlite_engine)
        sqlite_engine.dispose()
        yield {"pg_url": pg_url, "sqlite_url": sqlite_url, "run_id": run_id}


def test_sqlite_to_pg_migration_and_verify(pg_migrate_env):
    """SQLite 源库 → PG 目标库：迁移 + 行数/抽样对账 + v_run_analytics。"""
    pg_url = pg_migrate_env["pg_url"]
    sqlite_url = pg_migrate_env["sqlite_url"]
    run_id = pg_migrate_env["run_id"]

    result = run_migration(sqlite_url, pg_url, batch_size=50)
    assert not result.errors, result.errors
    summary = result.summary()
    assert summary["inserted"] > 0
    assert summary["source_rows"] == summary["inserted"]

    verify = verify_migration(
        sqlite_url,
        pg_url,
        sample_ids=[run_id],
    )
    assert verify.ok, verify.mismatches
    _assert_v_run_analytics_row(pg_url, run_id)


def test_sqlite_to_pg_migration_idempotent(pg_migrate_env):
    """重复迁移应跳过已有主键，对账仍通过。"""
    pg_url = pg_migrate_env["pg_url"]
    sqlite_url = pg_migrate_env["sqlite_url"]
    run_id = pg_migrate_env["run_id"]

    first = run_migration(sqlite_url, pg_url)
    assert not first.errors

    second = run_migration(sqlite_url, pg_url)
    assert not second.errors
    # rowcount 在 ON CONFLICT 下可能为 0；以对账为准
    assert second.summary()["inserted"] == 0 or second.summary()["skipped"] > 0

    verify = verify_migration(sqlite_url, pg_url, sample_ids=[run_id])
    assert verify.ok, verify.mismatches
    _assert_v_run_analytics_row(pg_url, run_id)
