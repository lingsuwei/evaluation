"""
PG 端到端集成 smoke：Alembic schema → SQLite 迁移 → v_run_analytics 读路径。

近似生产切 PG 流程（P1 基线 + Alembic + 编排表 create_all + A-06 迁移）。

  cd backend
  set PG_ALEMBIC_SMOKE=1
  pytest tests/test_pg_integration_smoke.py -v
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from database import Base
from services.migrate_sqlite_to_pg_service import run_migration, verify_migration
from tests.pg_migrate_seed import seed_sqlite_source
from tests.pg_smoke_helpers import (
    bootstrap_p1_agent_runs,
    pg_object_exists,
    pg_smoke_enabled,
    run_alembic,
    temporary_pg_database,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgresql]


@pytest.fixture()
def pg_e2e_env(tmp_path):
    if not pg_smoke_enabled():
        pytest.skip("设置 PG_ALEMBIC_SMOKE=1 以运行 PostgreSQL 集成 smoke test")

    sqlite_path = tmp_path / "source.db"
    sqlite_url = f"sqlite:///{sqlite_path.as_posix()}"

    with temporary_pg_database("onto_eval_e2e") as pg_url:
        pg_engine = create_engine(pg_url)
        try:
            bootstrap_p1_agent_runs(pg_engine)
            run_alembic(pg_url, "upgrade", "head")
            Base.metadata.create_all(pg_engine)
        finally:
            pg_engine.dispose()

        sqlite_engine = create_engine(
            sqlite_url,
            connect_args={"check_same_thread": False},
        )
        run_id = seed_sqlite_source(sqlite_engine)
        sqlite_engine.dispose()

        yield {"pg_url": pg_url, "sqlite_url": sqlite_url, "run_id": run_id}


def test_alembic_schema_migrate_and_v_run_analytics(pg_e2e_env):
    """P1→Alembic→迁移→v_run_analytics 宽表可读。"""
    pg_url = pg_e2e_env["pg_url"]
    sqlite_url = pg_e2e_env["sqlite_url"]
    run_id = pg_e2e_env["run_id"]

    result = run_migration(sqlite_url, pg_url, batch_size=50)
    assert not result.errors, result.errors

    verify = verify_migration(sqlite_url, pg_url, sample_ids=[run_id])
    assert verify.ok, verify.mismatches

    engine = create_engine(pg_url)
    with engine.connect() as conn:
        assert pg_object_exists(conn, kind="view", name="v_run_analytics")
        row = (
            conn.execute(
                text(
                    "SELECT * FROM v_run_analytics "
                    "WHERE agent_run_id = :id AND task_id = :task_id"
                ),
                {"id": run_id, "task_id": "task_pg_smoke"},
            )
            .mappings()
            .one()
        )
        assert row["runtime_tokens_total"] == 42.0
        assert row["checkpoint_total"] == 1
        assert row["checkpoint_passed"] == 1
        assert row["tokens_total"] == 42
    engine.dispose()
