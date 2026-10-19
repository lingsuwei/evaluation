"""P3-B Experiment 服务单测。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, EvaluationResult, RunSpanNode
from services.eval_context import build_eval_context
from services.eval_context import build_eval_context
from services.experiment_service import (
    get_or_create_experiment_for_task,
    run_version_experiment,
)
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


async def _seed_multi_agent_task(session: AsyncSession):
    started = datetime.utcnow()
    session.add_all(
        [
            AgentRun(
                id="run_v_a",
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t_exp", dataset_id="d1", question_id=0, run_index=0
                ),
                task_id="t_exp",
                dataset_id="d1",
                question_id=0,
                agent_id="ag_v1",
                agent_name="Agent V1",
                status="success",
                started_at=started,
                tokens_total=100,
                agent_snapshot="{}",
            ),
            AgentRun(
                id="run_v_b",
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t_exp", dataset_id="d1", question_id=0, run_index=0
                ),
                task_id="t_exp",
                dataset_id="d1",
                question_id=0,
                agent_id="ag_v2",
                agent_name="Agent V2",
                status="success",
                started_at=started,
                tokens_total=80,
                agent_snapshot="{}",
            ),
        ]
    )
    session.add_all(
        [
            EvaluationResult(
                task_id="t_exp",
                question_id=0,
                model="m",
                metric_name="correctness",
                score=0.6,
                avg_score=0.6,
                passed=True,
                agent_run_id="run_v_a",
            ),
            EvaluationResult(
                task_id="t_exp",
                question_id=0,
                model="m",
                metric_name="correctness",
                score=0.9,
                avg_score=0.9,
                passed=True,
                agent_run_id="run_v_b",
            ),
        ]
    )
    session.add_all(
        [
            RunSpanNode(
                id="sn_va1",
                agent_run_id="run_v_a",
                observation_id="o_a1",
                aggregate_key="step:plan",
                semantic_role="planning",
                summary_text="v1 plan",
                sibling_order=0,
                step_index=0,
            ),
            RunSpanNode(
                id="sn_vb1",
                agent_run_id="run_v_b",
                observation_id="o_b1",
                aggregate_key="step:plan",
                semantic_role="planning",
                summary_text="v2 plan",
                sibling_order=0,
                step_index=0,
            ),
            RunSpanNode(
                id="sn_vb2",
                agent_run_id="run_v_b",
                observation_id="o_b2",
                aggregate_key="tool:search",
                parent_id="sn_vb1",
                sibling_order=0,
                step_index=1,
            ),
        ]
    )
    await session.commit()


@pytest.mark.asyncio
async def test_get_or_create_experiment(mem_session):
    exp1 = await get_or_create_experiment_for_task(mem_session, "t_exp", name="E1")
    await mem_session.commit()
    exp2 = await get_or_create_experiment_for_task(mem_session, "t_exp")
    assert exp1.id == exp2.id


@pytest.mark.asyncio
async def test_run_version_experiment_multi_agent(mem_session):
    await _seed_multi_agent_task(mem_session)
    exp = await get_or_create_experiment_for_task(
        mem_session, "t_exp", baseline_agent_id="ag_v1"
    )
    await mem_session.commit()
    detail = await run_version_experiment(mem_session, exp.id)
    assert detail["status"] == "completed"
    assert len(detail["summaries"]) == 1
    assert detail["summaries"][0]["outcome_winner"] == "right"
    assert detail["outcome_wins"]["right"] == 1


@pytest.mark.p5
@pytest.mark.asyncio
async def test_run_version_experiment_default_semantic_structural(mem_session):
    await _seed_multi_agent_task(mem_session)
    exp = await get_or_create_experiment_for_task(
        mem_session, "t_exp", baseline_agent_id="ag_v1"
    )
    await mem_session.commit()
    detail = await run_version_experiment(mem_session, exp.id)
    highlights = detail["summaries"][0]["highlights"]
    assert "semantic" in highlights
    assert highlights["semantic"]["summary_mismatch_count"] >= 1
    assert "structural" in highlights
    assert highlights["structural"]["same_topology"] is False


@pytest.mark.asyncio
async def test_run_version_experiment_single_agent(mem_session):
    started = datetime.utcnow()
    mem_session.add(
        AgentRun(
            id="run_only",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t1", dataset_id="d1", question_id=0, run_index=0
            ),
            task_id="t1",
            dataset_id="d1",
            question_id=0,
            agent_id="ag_solo",
            status="success",
            started_at=started,
            agent_snapshot="{}",
        )
    )
    await mem_session.commit()
    exp = await get_or_create_experiment_for_task(mem_session, "t1")
    await mem_session.commit()
    detail = await run_version_experiment(mem_session, exp.id)
    assert detail["status"] == "completed"
    assert detail["meta"]["pair_count"] == 0
