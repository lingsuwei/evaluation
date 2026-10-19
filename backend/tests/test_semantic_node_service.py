"""P5 semantic_node_service 单测。"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base, RunSemanticNode, RunSpanNode
from services.semantic_node_service import (
    build_tier1_reasoning_summary,
    load_semantic_index_by_aggregate_key,
    materialize_semantic_nodes_for_run,
)
from tests.test_p3_diff_calculators import _seed_runs

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


def test_build_tier1_from_summary_text():
    span = RunSpanNode(
        id="s1",
        agent_run_id="r",
        aggregate_key="step:x",
        summary_text="hello world",
        sibling_order=0,
    )
    assert build_tier1_reasoning_summary(span) == "hello world"


def test_build_tier1_llm_node():
    span = RunSpanNode(
        id="s1",
        agent_run_id="r",
        aggregate_key="step:answer",
        node_type="generation",
        name="Answer",
        semantic_role="answer",
        sibling_order=0,
    )
    assert build_tier1_reasoning_summary(span) == "[answer] Answer"


@pytest.mark.asyncio
async def test_materialize_semantic_nodes_idempotent(mem_session):
    await _seed_runs(mem_session)
    n1 = await materialize_semantic_nodes_for_run(mem_session, "run_left")
    n2 = await materialize_semantic_nodes_for_run(mem_session, "run_left")
    assert n1 == 2
    assert n2 == 2
    count = (
        await mem_session.execute(
            select(RunSemanticNode).where(RunSemanticNode.agent_run_id == "run_left")
        )
    ).scalars().all()
    assert len(count) == 2


@pytest.mark.asyncio
async def test_load_semantic_index_by_aggregate_key(mem_session):
    await _seed_runs(mem_session)
    await materialize_semantic_nodes_for_run(mem_session, "run_left")
    idx = await load_semantic_index_by_aggregate_key(mem_session, "run_left")
    assert "step:plan" in idx
    assert idx["step:plan"]["summary_text"] == "left plan summary"
