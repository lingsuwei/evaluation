"""P5-D structural_diff 单测。"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base, RunSpanNode
from services.diff.structural_diff_calculator import (
    StructuralDiffCalculator,
    extract_topology_edges,
    merge_structural_diff,
)
from services.run_comparison_service import compare_runs
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


def test_extract_topology_edges():
    spans = [
        RunSpanNode(
            id="root",
            agent_run_id="r",
            aggregate_key="step:plan",
            parent_id=None,
            sibling_order=0,
        ),
        RunSpanNode(
            id="child",
            agent_run_id="r",
            aggregate_key="tool:search",
            parent_id="root",
            sibling_order=0,
        ),
    ]
    edges = extract_topology_edges(spans)
    assert ("__root__", "step:plan") in edges
    assert ("step:plan", "tool:search") in edges


@pytest.mark.asyncio
async def test_structural_diff_calculator(mem_session):
    await _seed_runs(mem_session)
    # link sn_l2 under sn_l1 for left run
    span = await mem_session.get(RunSpanNode, "sn_l2")
    span.parent_id = "sn_l1"
    await mem_session.commit()
    calc = StructuralDiffCalculator()
    payload = await calc.compute(
        mem_session, left_run_id="run_left", right_run_id="run_right"
    )
    assert payload["edge_count_left"] >= 1
    assert "only_left_edges" in payload


@pytest.mark.asyncio
async def test_compare_runs_structural_diff(mem_session):
    await _seed_runs(mem_session)
    out = await compare_runs(
        mem_session,
        left_run_id="run_left",
        right_run_id="run_right",
        diff_types=["structural_diff"],
    )
    assert out["diffs"][0]["diff_type"] == "structural_diff"
