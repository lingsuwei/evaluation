"""v_run_analytics 读模型单测（P2 F-04）。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database import AgentRun, Base, CheckpointResult, RunMetric
from services.eval_context import build_eval_context
from services.run_analytics_view import (
    V_RUN_ANALYTICS_VIEW_NAME,
    fetch_run_analytics_row_for_run,
    fetch_run_analytics_rows_for_task,
)


@pytest.fixture
async def mem_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_fetch_run_analytics_rows_orm_fallback(mem_session):
    mem_session.add(
        AgentRun(
            id="run_f04",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t_f04", dataset_id="d1", question_id=1, run_index=0
            ),
            task_id="t_f04",
            dataset_id="d1",
            question_id=1,
            agent_id="ag1",
            agent_name="Agent",
            status="success",
            trace_id="tr_f04",
            tokens_total=120,
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    mem_session.add(
        RunMetric(
            id="m_rt",
            agent_run_id="run_f04",
            metric_key="runtime.latency_ms",
            source="runtime",
            value_num=350.0,
            dims_hash="",
        )
    )
    mem_session.add(
        RunMetric(
            id="m_ev",
            agent_run_id="run_f04",
            metric_key="eval.correctness",
            source="evaluation",
            value_num=0.92,
            dims_hash="",
        )
    )
    mem_session.add(
        CheckpointResult(
            id="cp1",
            agent_run_id="run_f04",
            checkpoint_id="ck1",
            passed=True,
        )
    )
    mem_session.add(
        CheckpointResult(
            id="cp2",
            agent_run_id="run_f04",
            checkpoint_id="ck2",
            passed=False,
        )
    )
    await mem_session.commit()

    rows = await fetch_run_analytics_rows_for_task(mem_session, "t_f04")
    assert len(rows) == 1
    row = rows[0]
    assert row["agent_run_id"] == "run_f04"
    assert row["task_id"] == "t_f04"
    assert row["question_id"] == 1
    assert row["runtime_latency_ms"] == pytest.approx(350.0)
    assert row["eval_scores"]["eval.correctness"] == pytest.approx(0.92)
    assert row["checkpoint_total"] == 2
    assert row["checkpoint_passed"] == 1
    assert row["tokens_total"] == 120

    single = await fetch_run_analytics_row_for_run(mem_session, "run_f04")
    assert single is not None
    assert single["trace_id"] == "tr_f04"


@pytest.mark.asyncio
async def test_fetch_run_analytics_empty_task(mem_session):
    rows = await fetch_run_analytics_rows_for_task(mem_session, "missing")
    assert rows == []


def test_view_name_constant():
    assert V_RUN_ANALYTICS_VIEW_NAME == "v_run_analytics"
