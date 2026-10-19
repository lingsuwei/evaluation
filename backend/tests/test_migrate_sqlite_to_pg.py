"""A-06 SQLite → PG 迁移服务单测。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import AgentRun, Base, Dataset
from services.migrate_sqlite_to_pg_service import (
    MIGRATION_MODELS,
    _filter_models,
    _model_to_mapping,
    run_migration,
    validate_migration_urls,
)


@pytest.fixture
def sqlite_source_engine(tmp_path):
    db_path = tmp_path / "source.db"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        now = datetime.utcnow()
        session.add(
            Dataset(
                id="ds_mig",
                name="Migrate DS",
                data="[]",
                item_count=0,
                created_at=now,
            )
        )
        session.add(
            AgentRun(
                id="run_mig",
                origin="evaluation",
                task_id="task_mig",
                dataset_id="ds_mig",
                question_id=0,
                agent_id="ag1",
                status="success",
                started_at=now,
                trace_id="tr_mig",
                trace_sync_status="synced",
                agent_snapshot="{}",
            )
        )
        session.commit()
    yield url
    engine.dispose()


def test_validate_migration_urls_rejects_wrong_dialects():
    with pytest.raises(ValueError, match="源库须为 SQLite"):
        validate_migration_urls(
            "postgresql+asyncpg://u@localhost/db",
            "postgresql+asyncpg://u@localhost/db",
        )
    with pytest.raises(ValueError, match="目标库须为 PostgreSQL"):
        validate_migration_urls(
            "sqlite:///a.db",
            "sqlite:///b.db",
        )


def test_filter_models_subset():
    models = _filter_models(["agent_runs", "datasets"])
    names = {m.__tablename__ for m in models}
    assert names == {"agent_runs", "datasets"}


def test_model_to_mapping_parses_json_fields():
    run = AgentRun(
        id="r1",
        origin="evaluation",
        agent_id="a",
        status="success",
        started_at=datetime.utcnow(),
        agent_snapshot="{}",
        eval_context={"task_id": "t1"},
    )
    data = _model_to_mapping(run)
    assert data["eval_context"]["task_id"] == "t1"


def test_dry_run_counts_sqlite_rows(sqlite_source_engine):
    result = run_migration(
        sqlite_source_engine,
        "",
        dry_run=True,
        tables=["datasets", "agent_runs"],
    )
    assert not result.errors
    summary = result.summary()
    assert summary["dry_run"] is True
    assert summary["source_rows"] == 2
    by_table = {t.table: t.source_count for t in result.tables}
    assert by_table["datasets"] == 1
    assert by_table["agent_runs"] == 1


def test_migration_models_order_has_agent_runs_before_children():
    names = [m.__tablename__ for m in MIGRATION_MODELS]
    assert names.index("agent_runs") < names.index("run_span_nodes")
    assert names.index("evaluation_results") < names.index("evaluation_rounds")
