"""过程点效能归因（P1）单测。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import (
    AgentRun,
    Base,
    CheckpointResult,
    ProcessCheckpointTemplate,
    RunSpanNode,
)
from services.checkpoint_cost_attribution_service import build_checkpoint_cost_attribution
from tests.test_checkpoint_result_service import _seed_checkpoint_data

pytestmark = pytest.mark.p3


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
async def test_checkpoint_cost_attribution_joins_span(mem_session):
    await _seed_checkpoint_data(mem_session)
    mem_session.add_all(
        [
            RunSpanNode(
                id="sn_o1",
                agent_run_id="run_cp1",
                observation_id="o1",
                step_index=0,
                sibling_order=0,
                node_type="GENERATION",
                name="plan",
                latency_ms=1200,
                tokens=800,
            ),
            RunSpanNode(
                id="sn_o2",
                agent_run_id="run_cp1",
                observation_id="o2",
                step_index=1,
                sibling_order=0,
                node_type="TOOL",
                name="kb",
                latency_ms=450,
                tokens=200,
            ),
        ]
    )
    await mem_session.commit()

    out = await build_checkpoint_cost_attribution(mem_session, "t_cp")
    assert out is not None
    assert out["logical_agent_id"] == "logical-a"
    assert len(out["rows"]) == 2
    plan = next(r for r in out["rows"] if r["checkpoint_id"] == "tpl_plan")
    tool = next(r for r in out["rows"] if r["checkpoint_id"] == "tpl_tool")
    assert plan["hit_rate"] == 1.0
    assert plan["avg_latency_ms"] == 1200.0
    assert plan["avg_tokens"] == 800.0
    assert tool["avg_latency_ms"] == 450.0


@pytest.mark.asyncio
async def test_checkpoint_cost_attribution_none_without_templates(mem_session):
    mem_session.add(
        AgentRun(
            id="run_empty",
            task_id="t_empty",
            dataset_id="d1",
            question_id=0,
            agent_id="ag1",
            logical_agent_id="no-templates",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    await mem_session.commit()
    assert await build_checkpoint_cost_attribution(mem_session, "t_empty") is None
