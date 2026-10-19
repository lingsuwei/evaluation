"""版本对比驾驶舱读模型单测（P0-A）。"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base
from services.version_cockpit_service import build_version_cockpit
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
async def test_build_version_cockpit_l1(mem_session):
    await _seed_multi_agent_task(mem_session)
    payload = await build_version_cockpit(mem_session, "t_exp")
    assert payload["task_id"] == "t_exp"
    assert len(payload["agents"]) == 2
    assert len(payload["l1"]["rows"]) == 1
    row = payload["l1"]["rows"][0]
    assert row["winner_agent_id"] == "ag_v2"
    assert row["outcomes"]["ag_v1"] == pytest.approx(0.6)
    assert row["outcomes"]["ag_v2"] == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_build_version_cockpit_l2_l3(mem_session):
    await _seed_multi_agent_task(mem_session)
    payload = await build_version_cockpit(mem_session, "t_exp", question_id=0)
    assert payload["selected_question_id"] == 0
    assert payload["l2"]["question_id"] == 0
    factor_keys = {f["factor_key"] for f in payload["l2"]["factors"]}
    assert "correctness" in factor_keys
    assert "behavioral" in factor_keys
    assert "latency_ms" in factor_keys
    assert payload["l3"]["question_id"] == 0
