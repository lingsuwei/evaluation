"""P3 集成测试：Experiment → Compare → Task Report。"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base
from services.diff_summary_service import build_task_experiment_report
from services.experiment_service import get_or_create_experiment_for_task, run_version_experiment
from tests.test_experiment_service import _seed_multi_agent_task

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
async def test_p3_e2e_version_experiment_pipeline(mem_session):
    await _seed_multi_agent_task(mem_session)
    exp = await get_or_create_experiment_for_task(
        mem_session, "t_exp", baseline_agent_id="ag_v1"
    )
    await mem_session.commit()
    await run_version_experiment(mem_session, exp.id)
    report = await build_task_experiment_report(mem_session, "t_exp")
    assert report["experiment"] is not None
    assert report["comparison_count"] >= 1
    assert report["experiment"]["outcome_wins"]["right"] >= 1
    hl = report["diff_highlights"][0]
    assert hl["outcome"]["delta"] == pytest.approx(0.3)
