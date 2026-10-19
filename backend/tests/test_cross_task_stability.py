"""跨任务（跨版本）单题稳定性对比（P2）。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, EvaluationResult, EvaluationTask
from services.cross_task_stability_service import build_cross_task_stability_compare
from services.eval_context import build_eval_context
from services.runtime_fact_builder import apply_runtime_facts_from_trace
from tests.scenario_test_helpers import SAMPLE_Q, seed_item_template

pytestmark = pytest.mark.p3

QK = "qk_stability"


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _run(run_id: str, task_id: str, agent_id: str, run_index: int) -> AgentRun:
    return AgentRun(
        id=run_id,
        origin="evaluation",
        eval_context=build_eval_context(
            task_id=task_id,
            dataset_id="ds_sc",
            question_id=0,
            question_key=QK,
            dataset_item_id="ditem_test01",
        ),
        task_id=task_id,
        dataset_id="ds_sc",
        question_id=0,
        run_index=run_index,
        agent_id=agent_id,
        status="success",
        started_at=datetime.utcnow(),
        agent_snapshot="{}",
        tokens_total=1000 + run_index * 200,
    )


async def _seed_multi_and_single(session: AsyncSession) -> None:
    await seed_item_template(session)
    now = datetime.utcnow()
    session.add_all(
        [
            EvaluationTask(
                id="t_a",
                dataset_id="ds_sc",
                status="completed",
                agent_id="ag_a",
                completed_at=now,
            ),
            EvaluationTask(
                id="t_b",
                dataset_id="ds_sc",
                status="completed",
                agent_id="ag_b",
                completed_at=now,
            ),
            EvaluationTask(
                id="t_single",
                dataset_id="ds_sc",
                status="completed",
                agent_id="ag_c",
                completed_at=now,
            ),
        ]
    )
    runs = [
        _run("run_a0", "t_a", "ag_a", 0),
        _run("run_a1", "t_a", "ag_a", 1),
        _run("run_b0", "t_b", "ag_b", 0),
        _run("run_b1", "t_b", "ag_b", 1),
        _run("run_c0", "t_single", "ag_c", 0),
    ]
    session.add_all(runs)
    session.add_all(
        [
            EvaluationResult(
                task_id="t_a",
                question_id=0,
                model="m",
                metric_name="inference_latency",
                score=1.0,
                passed=True,
                agent_run_id="run_a0",
                question=SAMPLE_Q,
                ground_truth="gt",
                agent_answer="a",
                inference_duration_ms=2000,
            ),
            EvaluationResult(
                task_id="t_a",
                question_id=0,
                model="m",
                metric_name="inference_latency",
                score=1.0,
                passed=True,
                agent_run_id="run_a1",
                question=SAMPLE_Q,
                ground_truth="gt",
                agent_answer="a",
                inference_duration_ms=2600,
            ),
            EvaluationResult(
                task_id="t_b",
                question_id=0,
                model="m",
                metric_name="inference_latency",
                score=1.0,
                passed=True,
                agent_run_id="run_b0",
                question=SAMPLE_Q,
                ground_truth="gt",
                agent_answer="a",
                inference_duration_ms=2100,
            ),
            EvaluationResult(
                task_id="t_b",
                question_id=0,
                model="m",
                metric_name="inference_latency",
                score=1.0,
                passed=True,
                agent_run_id="run_b1",
                question=SAMPLE_Q,
                ground_truth="gt",
                agent_answer="a",
                inference_duration_ms=2400,
            ),
        ]
    )
    await session.flush()

    trace_with_tool = {
        "observations": [
            {"id": "o1", "type": "GENERATION", "name": "plan", "metadata": {"agent.step.id": "plan"}},
            {"id": "o2", "type": "TOOL", "name": "kb", "metadata": {"agent.tool.name": "kb_search"}},
            {"id": "o3", "type": "GENERATION", "name": "answer", "metadata": {"agent.step.id": "answer"}},
        ]
    }
    trace_without_tool = {
        "observations": [
            {"id": "p1", "type": "GENERATION", "name": "plan", "metadata": {"agent.step.id": "plan"}},
            {"id": "p2", "type": "GENERATION", "name": "answer", "metadata": {"agent.step.id": "answer"}},
        ]
    }
    await apply_runtime_facts_from_trace(session, runs[0], trace_with_tool)
    await apply_runtime_facts_from_trace(session, runs[1], trace_without_tool)
    await apply_runtime_facts_from_trace(session, runs[2], trace_with_tool)
    await apply_runtime_facts_from_trace(session, runs[3], trace_without_tool)
    await apply_runtime_facts_from_trace(session, runs[4], trace_with_tool)
    await session.commit()


@pytest.mark.asyncio
async def test_cross_task_stability_compare_eligible(mem_session):
    await _seed_multi_and_single(mem_session)
    payload = await build_cross_task_stability_compare(
        mem_session,
        question_key=QK,
        baseline_task_id="t_a",
        compare_task_id="t_b",
    )
    assert payload["eligible"] is True
    assert payload["empty_reason"] is None
    assert payload["baseline"]["round_count"] == 2
    assert payload["compare"]["round_count"] == 2
    assert payload["baseline"]["structure_summary"]["headline"]
    assert len(payload["baseline"]["round_stability"]["rows"]) >= 1
    assert payload["vs_summary"] is not None


@pytest.mark.asyncio
async def test_cross_task_stability_compare_ineligible_single_round(mem_session):
    await _seed_multi_and_single(mem_session)
    payload = await build_cross_task_stability_compare(
        mem_session,
        question_key=QK,
        baseline_task_id="t_a",
        compare_task_id="t_single",
    )
    assert payload["eligible"] is False
    assert payload["baseline"] is None
    assert payload["compare"] is None
    assert payload["empty_reason"]
    assert "对比版本仅 1 轮" in payload["empty_reason"]
