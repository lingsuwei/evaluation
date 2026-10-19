"""P5-E：Builder L1 映射端到端。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, RunSpanNode
from services.eval_context import build_eval_context
from services.runtime_fact_builder import build_facts_for_trace

pytestmark = pytest.mark.p5


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
async def test_build_facts_maps_l1_fields_to_span_nodes(mem_session):
    run = AgentRun(
        id="run_l1_e2e",
        origin="evaluation",
        eval_context=build_eval_context(
            task_id="t1", dataset_id="d1", question_id=0, run_index=0
        ),
        task_id="t1",
        dataset_id="d1",
        question_id=0,
        agent_id="ag1",
        status="success",
        started_at=datetime.utcnow(),
        trace_id="trace_l1",
        agent_snapshot="{}",
        input_snapshot={"question": "q"},
        trace_sync_status="pending_sync",
    )
    mem_session.add(run)
    await mem_session.commit()

    trace = {
        "id": "trace_l1",
        "observations": [
            {
                "id": "obs_m",
                "type": "SPAN",
                "metadata": {
                    "agent.tool.name": "search",
                    "agent.is_deterministic_io": True,
                    "agent.mock_response_payload": {"result": "cached"},
                },
            },
        ],
    }
    await build_facts_for_trace(mem_session, run, trace)
    await mem_session.commit()

    span = (
        await mem_session.execute(
            select(RunSpanNode).where(RunSpanNode.agent_run_id == "run_l1_e2e")
        )
    ).scalar_one()
    assert span.is_deterministic_io is True
    assert span.mock_response_payload == {"result": "cached"}
