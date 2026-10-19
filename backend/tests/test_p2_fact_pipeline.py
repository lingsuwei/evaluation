"""P2 事实物化集成测试：Builder → Span + run_metrics（I-02）。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, RunMetric, RunSpanNode
from services.eval_context import build_eval_context
from services.runtime_fact_builder import build_facts_for_trace

pytestmark = pytest.mark.p2


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_ingest_builds_span_tree_and_run_metrics(mem_session):
    run = AgentRun(
        id="run_p2_pipe",
        origin="evaluation",
        eval_context=build_eval_context(
            task_id="t_p2", dataset_id="d1", question_id=0, run_index=0
        ),
        task_id="t_p2",
        dataset_id="d1",
        question_id=0,
        run_index=0,
        agent_id="ag1",
        status="success",
        started_at=datetime.utcnow(),
        trace_id="trace_p2",
        agent_snapshot="{}",
        trace_sync_status="pending_sync",
    )
    mem_session.add(run)
    await mem_session.commit()

    trace = {
        "id": "trace_p2",
        "latency": 1200,
        "observations": [
            {
                "id": "obs_root",
                "name": "agent",
                "type": "SPAN",
                "metadata": {"agent.step.id": "main"},
            },
            {
                "id": "obs_child",
                "parentObservationId": "obs_root",
                "name": "gen",
                "type": "GENERATION",
                "usage": {"total": 55},
                "output": "answer text",
            },
        ],
    }
    facts = await build_facts_for_trace(mem_session, run, trace)
    await mem_session.commit()

    assert facts["span_nodes"] == 2

    spans = (await mem_session.execute(select(RunSpanNode))).scalars().all()
    assert len(spans) == 2
    assert {s.observation_id for s in spans} == {"obs_root", "obs_child"}

    metrics = (await mem_session.execute(select(RunMetric))).scalars().all()
    keys = {m.metric_key: m.value_num for m in metrics}
    assert keys.get("runtime.tokens_total") == 55.0
    assert keys.get("runtime.observation_count") == 2.0
    assert keys.get("runtime.latency_ms") == 1200.0

    refreshed = (
        await mem_session.execute(select(AgentRun).where(AgentRun.id == "run_p2_pipe"))
    ).scalar_one()
    assert refreshed.tokens_total == 55


@pytest.mark.asyncio
async def test_build_facts_idempotent_on_reingest(mem_session):
    run = AgentRun(
        id="run_p2_idem",
        origin="evaluation",
        eval_context=build_eval_context(
            task_id="t2", dataset_id="d1", question_id=0, run_index=0
        ),
        task_id="t2",
        dataset_id="d1",
        question_id=0,
        run_index=0,
        agent_id="ag1",
        status="success",
        started_at=datetime.utcnow(),
        trace_id="tr_idem",
        agent_snapshot="{}",
    )
    mem_session.add(run)
    await mem_session.commit()

    trace = {
        "id": "tr_idem",
        "observations": [
            {"id": "o1", "name": "s", "type": "SPAN", "usage": {"total": 7}},
        ],
    }
    await build_facts_for_trace(mem_session, run, trace)
    await mem_session.commit()
    await build_facts_for_trace(mem_session, run, trace)
    await mem_session.commit()

    spans = (await mem_session.execute(select(RunSpanNode))).scalars().all()
    metrics = (await mem_session.execute(select(RunMetric))).scalars().all()
    assert len(spans) == 1
    assert len([m for m in metrics if m.metric_key == "runtime.tokens_total"]) == 1
