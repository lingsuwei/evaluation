"""P4 span_tree_service 单测。"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base
from services.run_comparison_service import compare_runs
from services.span_tree_service import (
    build_dual_span_trees_for_comparison,
    build_nested_tree,
    build_span_tree_for_run,
    collect_aggregate_keys,
)
from tests.test_p3_diff_calculators import _seed_runs

pytestmark = pytest.mark.p4


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def test_build_nested_tree_multi_root():
    flat = [
        {"id": "a", "parent_id": None, "sibling_order": 0, "step_index": 0, "children": []},
        {"id": "b", "parent_id": "a", "sibling_order": 0, "step_index": 1, "children": []},
        {"id": "c", "parent_id": None, "sibling_order": 1, "step_index": 2, "children": []},
    ]
    tree = build_nested_tree(flat)
    assert len(tree) == 2
    assert tree[0]["id"] == "a"
    assert tree[0]["children"][0]["id"] == "b"


def test_collect_aggregate_keys():
    keys = collect_aggregate_keys(
        [
            {"aggregate_key": "step:plan"},
            {"aggregate_key": "unknown"},
            {"aggregate_key": "tool:x"},
        ]
    )
    assert keys == {"step:plan", "tool:x"}


@pytest.mark.asyncio
async def test_build_span_tree_for_run(mem_session):
    await _seed_runs(mem_session)
    tree = await build_span_tree_for_run(mem_session, "run_left")
    assert tree is not None
    assert tree["node_count"] == 2
    assert len(tree["tree"]) >= 1


@pytest.mark.asyncio
async def test_build_span_tree_not_found(mem_session):
    assert await build_span_tree_for_run(mem_session, "missing") is None


@pytest.mark.asyncio
async def test_dual_span_trees(mem_session):
    await _seed_runs(mem_session)
    cmp = await compare_runs(
        mem_session, left_run_id="run_left", right_run_id="run_right"
    )
    dual = await build_dual_span_trees_for_comparison(mem_session, cmp["id"])
    assert dual is not None
    assert dual["left"]["node_count"] == 2
    assert dual["right"]["node_count"] == 1
    assert "step:plan" in dual["align_aggregate_keys"]
    assert "tool:search" in dual["only_left_aggregate_keys"]
