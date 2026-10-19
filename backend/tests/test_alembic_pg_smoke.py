"""
Alembic × PostgreSQL smoke test（P2 A-04 / A-05）。

前置：本地 PG 可连（如 docker compose up -d postgres），并设置 PG_ALEMBIC_SMOKE=1。

  cd backend
  set PG_ALEMBIC_SMOKE=1
  pytest tests/test_alembic_pg_smoke.py -v
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from tests.pg_smoke_helpers import (
    bootstrap_p1_agent_runs,
    pg_object_exists,
    pg_smoke_enabled,
    run_alembic,
    temporary_pg_database,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgresql]


@pytest.fixture()
def pg_alembic_db():
    if not pg_smoke_enabled():
        pytest.skip("设置 PG_ALEMBIC_SMOKE=1 以运行 PostgreSQL Alembic smoke test")

    with temporary_pg_database("onto_eval_alembic") as test_url:
        engine = create_engine(test_url)
        try:
            bootstrap_p1_agent_runs(engine)
            yield test_url
        finally:
            engine.dispose()


def test_alembic_upgrade_head_creates_p2_runtime_tables(pg_alembic_db: str):
    """upgrade head：P2 事实表 + v_run_analytics 视图（002）。"""
    run_alembic(pg_alembic_db, "upgrade", "head")

    engine = create_engine(pg_alembic_db)
    with engine.connect() as conn:
        assert pg_object_exists(conn, kind="column", name="agent_runs.input_snapshot")
        assert pg_object_exists(conn, kind="column", name="agent_runs.output_snapshot")
        for table in ("run_span_nodes", "run_metrics", "checkpoint_results"):
            assert pg_object_exists(conn, kind="table", name=table), f"缺少表 {table}"
        assert pg_object_exists(conn, kind="index", name="ix_agent_runs_eval_context_gin")
        assert pg_object_exists(conn, kind="view", name="v_run_analytics"), "缺少视图 v_run_analytics"
        assert pg_object_exists(conn, kind="table", name="run_comparisons"), "缺少表 run_comparisons"
        assert pg_object_exists(conn, kind="table", name="run_diffs"), "缺少表 run_diffs"
        assert pg_object_exists(conn, kind="table", name="experiments"), "缺少表 experiments"
        assert pg_object_exists(conn, kind="table", name="agent_run_trace_refs"), "缺少表 agent_run_trace_refs"
        assert pg_object_exists(conn, kind="table", name="run_semantic_nodes"), "缺少表 run_semantic_nodes"
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert rev == "005_p5_semantic_nodes"
    engine.dispose()


def test_alembic_upgrade_idempotent(pg_alembic_db: str):
    """重复 upgrade head 不应失败（已应用 revision 时 no-op）。"""
    run_alembic(pg_alembic_db, "upgrade", "head")
    run_alembic(pg_alembic_db, "upgrade", "head")


def test_alembic_downgrade_and_reupgrade(pg_alembic_db: str):
    """downgrade base 后表应移除，再 upgrade 可恢复。"""
    run_alembic(pg_alembic_db, "upgrade", "head")
    run_alembic(pg_alembic_db, "downgrade", "base")

    engine = create_engine(pg_alembic_db)
    with engine.connect() as conn:
        assert not pg_object_exists(conn, kind="table", name="run_metrics")
        assert not pg_object_exists(conn, kind="column", name="agent_runs.input_snapshot")
    engine.dispose()

    run_alembic(pg_alembic_db, "upgrade", "head")
    engine = create_engine(pg_alembic_db)
    with engine.connect() as conn:
        assert pg_object_exists(conn, kind="table", name="run_span_nodes")
        assert pg_object_exists(conn, kind="view", name="v_run_analytics")
    engine.dispose()
