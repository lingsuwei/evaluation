"""跨任务版本对比驾驶舱单测。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Agent, AgentRun, Base, Dataset, EvaluationResult, EvaluationTask, RunSpanNode
from services.cross_task_cockpit_service import (
    CrossTaskCockpitError,
    build_cross_task_cockpit,
)
from services.eval_context import build_eval_context

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


async def _seed_two_tasks(session: AsyncSession):
    session.add(
        Dataset(id="ds1", name="测试集A", data='[{"question":"Q1","ground_truth":"A"}]')
    )
    session.add_all(
        [
            Agent(id="ag1", name="DemoAgent", version="1.0.0", inference_config="{}"),
            Agent(id="ag2", name="DemoAgent", version="2.0.0", inference_config="{}"),
        ]
    )
    now = datetime.utcnow()
    session.add_all(
        [
            EvaluationTask(
                id="t_v1",
                dataset_id="ds1",
                status="completed",
                agent_id="ag1",
                completed_at=now,
            ),
            EvaluationTask(
                id="t_v2",
                dataset_id="ds1",
                status="completed",
                agent_id="ag2",
                completed_at=now,
            ),
        ]
    )
    qk = "qk_same"
    session.add_all(
        [
            AgentRun(
                id="run_t1",
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t_v1",
                    dataset_id="ds1",
                    question_id=0,
                    question_key=qk,
                    dataset_item_id="d1",
                ),
                task_id="t_v1",
                dataset_id="ds1",
                question_id=0,
                agent_id="ag1",
                agent_name="DemoAgent",
                agent_version="1.0.0",
                status="success",
                started_at=now,
                tokens_total=100,
                agent_snapshot="{}",
                trace_sync_status="synced",
            ),
            AgentRun(
                id="run_t2",
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t_v2",
                    dataset_id="ds1",
                    question_id=0,
                    question_key=qk,
                    dataset_item_id="d1",
                ),
                task_id="t_v2",
                dataset_id="ds1",
                question_id=0,
                agent_id="ag2",
                agent_name="DemoAgent",
                agent_version="2.0.0",
                status="success",
                started_at=now,
                tokens_total=80,
                agent_snapshot="{}",
                trace_sync_status="synced",
            ),
        ]
    )
    session.add_all(
        [
            RunSpanNode(
                id="sn_t1",
                agent_run_id="run_t1",
                observation_id="o1",
                aggregate_key="step:plan",
                semantic_role="planning",
                summary_text="left plan",
                sibling_order=0,
            ),
            RunSpanNode(
                id="sn_t2",
                agent_run_id="run_t2",
                observation_id="o2",
                aggregate_key="step:plan",
                semantic_role="planning",
                summary_text="right plan",
                sibling_order=0,
            ),
        ]
    )
    session.add_all(
        [
            EvaluationResult(
                task_id="t_v1",
                question_id=0,
                model="m",
                metric_name="correctness",
                score=0.6,
                avg_score=0.6,
                passed=True,
                agent_run_id="run_t1",
            ),
            EvaluationResult(
                task_id="t_v2",
                question_id=0,
                model="m",
                metric_name="correctness",
                score=0.9,
                avg_score=0.9,
                passed=True,
                agent_run_id="run_t2",
            ),
        ]
    )
    await session.commit()


@pytest.mark.asyncio
async def test_cross_task_cockpit_l1(mem_session):
    await _seed_two_tasks(mem_session)
    payload = await build_cross_task_cockpit(
        mem_session, ["t_v1", "t_v2"], baseline_task_id="t_v1"
    )
    assert payload["comparison_mode"] == "cross_task"
    assert len(payload["agents"]) == 2
    assert payload["agents"][0]["is_baseline"] is True
    assert len(payload["l1"]["rows"]) == 1
    row = payload["l1"]["rows"][0]
    assert row["outcomes"]["t_v1"] == 0.6
    assert row["outcomes"]["t_v2"] == 0.9
    assert row["winner_member_id"] == "t_v2"


@pytest.mark.asyncio
async def test_cross_task_cockpit_l2_diff(mem_session):
    await _seed_two_tasks(mem_session)
    payload = await build_cross_task_cockpit(
        mem_session,
        ["t_v1", "t_v2"],
        baseline_task_id="t_v1",
        question_key="qk_same",
    )
    assert payload.get("l2") is not None
    l2 = payload["l2"]
    assert l2.get("comparison_id")
    assert l2.get("diff_pair", {}).get("right_task_id") == "t_v2"
    assert l2.get("diff_pair", {}).get("left_run_id") == "run_t1"
    assert l2.get("diff_pair", {}).get("right_run_id") == "run_t2"


@pytest.mark.asyncio
async def test_cross_task_requires_two_tasks(mem_session):
    await _seed_two_tasks(mem_session)
    with pytest.raises(CrossTaskCockpitError):
        await build_cross_task_cockpit(mem_session, ["t_v1"])
