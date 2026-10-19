"""P5 semantic_diff Calculator 单测。"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base
from services.diff.diff_registry import get_calculator, resolve_diff_types
from services.diff.semantic_diff_calculator import SemanticDiffCalculator
from services.diff.semantic_semantics import index_semantics_by_aggregate_key, merge_semantic_diff
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


def test_resolve_semantic_diff_type():
    types = resolve_diff_types(["semantic_diff"])
    assert types == ["semantic_diff"]
    calc = get_calculator("semantic_diff")
    assert isinstance(calc, SemanticDiffCalculator)


def test_merge_semantic_diff_role_mismatch():
    payload = merge_semantic_diff(
        {
            "step:plan": {
                "aggregate_key": "step:plan",
                "semantic_role": "plan",
                "summary_text": "left plan",
            }
        },
        {
            "step:plan": {
                "aggregate_key": "step:plan",
                "semantic_role": "execute",
                "summary_text": "left plan",
            }
        },
    )
    assert payload["tier"] == "0"
    assert payload["role_mismatch_keys"] == ["step:plan"]
    assert payload["summary_mismatch_keys"] == []


@pytest.mark.asyncio
async def test_semantic_diff_calculator(mem_session):
    await _seed_runs(mem_session)
    calc = SemanticDiffCalculator()
    payload = await calc.compute(
        mem_session, left_run_id="run_left", right_run_id="run_right"
    )
    assert payload["tier"] == "1"
    assert "step:plan" in payload["align_aggregate_keys"]
    assert "tool:search" in payload["only_left_aggregate_keys"]
    by_key = {n["aggregate_key"]: n for n in payload["nodes"]}
    assert by_key["step:plan"]["same_role"] is True
    assert by_key["step:plan"]["same_summary"] is False
    assert "step:plan" in payload["summary_mismatch_keys"]
    assert "judge_summary" in payload
    assert "摘要不一致" in payload["judge_summary"]


@pytest.mark.asyncio
async def test_compare_runs_with_semantic_diff(mem_session):
    await _seed_runs(mem_session)
    out = await compare_runs(
        mem_session,
        left_run_id="run_left",
        right_run_id="run_right",
        diff_types=["semantic_diff"],
    )
    assert len(out["diffs"]) == 1
    assert out["diffs"][0]["diff_type"] == "semantic_diff"
    assert out["diffs"][0]["payload"]["tier"] == "1"
    assert out["diffs"][0]["payload"].get("judge_summary")


def test_index_semantics_picks_lowest_step_index():
    from database import RunSpanNode

    nodes = [
        RunSpanNode(
            id="a",
            agent_run_id="r",
            aggregate_key="step:x",
            semantic_role="late",
            step_index=2,
            sibling_order=0,
        ),
        RunSpanNode(
            id="b",
            agent_run_id="r",
            aggregate_key="step:x",
            semantic_role="early",
            step_index=0,
            sibling_order=0,
        ),
    ]
    idx = index_semantics_by_aggregate_key(nodes)
    assert idx["step:x"]["semantic_role"] == "early"
