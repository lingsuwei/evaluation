"""metric_diff 别名 Calculator 单测。"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base
from services.diff.metric_diff_calculator import MetricDiffCalculator
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


@pytest.mark.asyncio
async def test_metric_diff_merges_outcome_and_cost(mem_session):
    await _seed_runs(mem_session)
    payload = await MetricDiffCalculator().compute(
        mem_session, left_run_id="run_left", right_run_id="run_right"
    )
    assert "metrics" in payload
    assert "run_level" in payload
    assert payload["alias_of"] == ["outcome_diff", "cost_diff"]
