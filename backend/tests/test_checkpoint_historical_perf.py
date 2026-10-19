"""过程点历史效能 API 单测（P2）。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, RunSpanNode
from services.checkpoint_historical_perf_service import build_checkpoint_historical_performance
from services.eval_context import build_eval_context
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
async def test_checkpoint_historical_performance_p75(mem_session):
    await _seed_checkpoint_data(mem_session)
    mem_session.add(
        AgentRun(
            id="run_hist2",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t_cp2", dataset_id="d1", question_id=1, run_index=0
            ),
            task_id="t_cp2",
            dataset_id="d1",
            question_id=1,
            agent_id="ag1",
            logical_agent_id="logical-a",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    mem_session.add_all(
        [
            RunSpanNode(
                id="sn_h1",
                agent_run_id="run_cp1",
                observation_id="o1",
                step_index=0,
                sibling_order=0,
                node_type="GENERATION",
                latency_ms=1000,
                tokens=500,
            ),
            RunSpanNode(
                id="sn_h2",
                agent_run_id="run_cp1",
                observation_id="o2",
                step_index=1,
                sibling_order=0,
                node_type="TOOL",
                latency_ms=2000,
                tokens=800,
            ),
        ]
    )
    await mem_session.commit()

    out = await build_checkpoint_historical_performance(mem_session, "logical-a")
    assert out is not None
    assert out["sample_count"] >= 1
    plan = next(r for r in out["rows"] if r["checkpoint_id"] == "tpl_plan")
    assert plan["latency_ms"]["p75"] == 1000.0
