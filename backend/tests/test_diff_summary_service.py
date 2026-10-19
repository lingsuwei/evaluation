"""diff_summary_service 单测。"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base
from services.diff_summary_service import build_task_experiment_report, get_task_diff_highlights
from services.run_comparison_service import compare_runs
from tests.test_p3_diff_calculators import _seed_runs

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


@pytest.mark.p5
@pytest.mark.asyncio
async def test_task_diff_highlights_with_semantic(mem_session):
    await _seed_runs(mem_session)
    await compare_runs(
        mem_session,
        left_run_id="run_left",
        right_run_id="run_right",
        diff_types=["semantic_diff"],
    )
    highlights = await get_task_diff_highlights(mem_session, "t1")
    assert len(highlights) == 1
    assert highlights[0]["semantic"]["summary_mismatch_count"] == 1
    assert "judge_summary" in highlights[0]["semantic"]


@pytest.mark.p5
@pytest.mark.asyncio
async def test_task_diff_highlights_with_structural(mem_session):
    from database import RunSpanNode

    await _seed_runs(mem_session)
    span = await mem_session.get(RunSpanNode, "sn_l2")
    span.parent_id = "sn_l1"
    await mem_session.commit()
    await compare_runs(
        mem_session,
        left_run_id="run_left",
        right_run_id="run_right",
        diff_types=["structural_diff"],
    )
    highlights = await get_task_diff_highlights(mem_session, "t1")
    assert "structural" in highlights[0]
    assert "summary" in highlights[0]["structural"]


@pytest.mark.asyncio
async def test_task_diff_highlights(mem_session):
    await _seed_runs(mem_session)
    await compare_runs(
        mem_session, left_run_id="run_left", right_run_id="run_right"
    )
    highlights = await get_task_diff_highlights(mem_session, "t1")
    assert len(highlights) == 1
    assert highlights[0]["outcome"]["delta"] == pytest.approx(0.2)
    assert highlights[0]["cost"]["tokens_delta"] == -20


@pytest.mark.asyncio
async def test_build_task_experiment_report(mem_session):
    await _seed_runs(mem_session)
    report = await build_task_experiment_report(mem_session, "t1")
    assert report["comparison_count"] == 0
    assert report["experiment"] is None
