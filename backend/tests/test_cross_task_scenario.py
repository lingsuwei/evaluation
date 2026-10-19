"""跨任务场景子步骤矩阵（Phase A）。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base, EvaluationTask
from services.cross_task_cockpit_service import CrossTaskCockpitError
from services.cross_task_scenario_service import build_cross_task_scenario_matrix
from tests.scenario_test_helpers import seed_two_tasks_with_item_template

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
async def test_cross_task_scenario_matrix(mem_session):
    await seed_two_tasks_with_item_template(mem_session)
    payload = await build_cross_task_scenario_matrix(
        mem_session, ["t_s1", "t_s2"]
    )
    assert payload["comparison_mode"] == "cross_task_scenario"
    assert len(payload["members"]) == 2
    assert len(payload["questions"]) >= 1
    row = payload["questions"][0]
    assert row["cells"]["t_s1"] is not None
    assert row["cells"]["t_s2"] is not None
    assert row["cells"]["t_s1"]["composite_score"] > 0
    summaries = payload.get("outcome_summaries")
    assert summaries is not None
    assert len(summaries["rows"]) == 2
    assert summaries["baseline_task_id"] == "t_s1"
    baseline_row = next(r for r in summaries["rows"] if r["is_baseline"])
    assert baseline_row["member_id"] == "t_s1"
    other = next(r for r in summaries["rows"] if not r["is_baseline"])
    assert other["vs_baseline"] is not None

    perf = payload.get("performance_summaries")
    assert perf is not None
    assert len(perf["rows"]) == 2
    assert all("llm_call_count" in r for r in perf["rows"])

    steps = payload.get("step_performance_summary")
    assert steps is not None


@pytest.mark.asyncio
async def test_cross_task_scenario_rejects_mixed_dataset(mem_session):
    await seed_two_tasks_with_item_template(mem_session)
    mem_session.add(
        EvaluationTask(
            id="t_other",
            dataset_id="ds_other",
            status="completed",
            agent_id="ag_s1",
            completed_at=datetime.utcnow(),
        )
    )
    await mem_session.commit()
    with pytest.raises(CrossTaskCockpitError, match="同一评测集"):
        await build_cross_task_scenario_matrix(mem_session, ["t_s1", "t_other"])
