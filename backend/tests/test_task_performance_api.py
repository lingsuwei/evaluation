"""task performance report API 集成测试。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, EvaluationResult, EvaluationTask, RunSpanNode
from services.runtime_fact_builder import apply_runtime_facts_from_trace
from services.task_performance_report_service import (
    build_run_step_compare,
    build_task_performance_report,
)

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
async def test_performance_report_and_step_compare(mem_session):
    task = EvaluationTask(
        id="t_perf",
        dataset_id="ds1",
        agent_id="ag1",
        status="completed",
        created_at=datetime.utcnow(),
    )
    mem_session.add(task)
    run0 = AgentRun(
        id="run_p0",
        task_id="t_perf",
        dataset_id="ds1",
        question_id=1,
        run_index=0,
        agent_id="ag1",
        status="success",
        started_at=datetime.utcnow(),
        agent_snapshot="{}",
        created_at=datetime.utcnow(),
        trace_id="tr0",
        tokens_total=8000,
    )
    run1 = AgentRun(
        id="run_p1",
        task_id="t_perf",
        dataset_id="ds1",
        question_id=1,
        run_index=1,
        agent_id="ag1",
        status="success",
        started_at=datetime.utcnow(),
        agent_snapshot="{}",
        created_at=datetime.utcnow(),
        trace_id="tr1",
        tokens_total=12000,
    )
    mem_session.add_all([run0, run1])
    mem_session.add_all(
        [
            EvaluationResult(
                task_id="t_perf",
                question_id=1,
                model="runtime",
                metric_name="inference_latency",
                score=1.0,
                threshold=60000,
                passed=True,
                eval_method="runtime",
                agent_run_id="run_p0",
                question="q1",
                ground_truth="gt",
                agent_answer="a",
                inference_duration_ms=60000,
            ),
            EvaluationResult(
                task_id="t_perf",
                question_id=1,
                model="runtime",
                metric_name="inference_latency",
                score=1.0,
                threshold=60000,
                passed=True,
                eval_method="runtime",
                agent_run_id="run_p1",
                question="q1",
                ground_truth="gt",
                agent_answer="a",
                inference_duration_ms=90000,
            ),
        ]
    )
    await mem_session.flush()

    trace0 = {
        "observations": [
            {"id": "o1", "type": "GENERATION", "name": "plan", "metadata": {"agent.step.id": "plan"}},
            {
                "id": "o2",
                "type": "TOOL",
                "name": "kb",
                "metadata": {"agent.tool.name": "kb_search"},
            },
            {"id": "o3", "type": "GENERATION", "name": "answer", "metadata": {"agent.step.id": "answer"}},
        ]
    }
    trace1 = {
        "observations": [
            {"id": "b1", "type": "GENERATION", "name": "plan", "metadata": {"agent.step.id": "plan"}},
            {"id": "b2", "type": "GENERATION", "name": "answer", "metadata": {"agent.step.id": "answer"}},
        ]
    }
    await apply_runtime_facts_from_trace(mem_session, run0, trace0)
    await apply_runtime_facts_from_trace(mem_session, run1, trace1)
    await mem_session.commit()

    report = await build_task_performance_report(mem_session, "t_perf")
    assert report is not None
    assert report["summary"]["llm_call_count"] >= 3
    assert report["summary"]["tool_call_count"] >= 1
    assert report["summary"]["multi_round_mode"] is True
    assert len(report["run_rows"]) == 2
    assert len(report["run_rows"][0].get("steps") or []) >= 1
    assert len(report["stability_questions"]) == 1
    sq = report["stability_questions"][0]
    assert sq["question_id"] == 1
    assert sq["round_count"] == 2
    assert sq["latency_min_ms"] == 60000.0
    assert sq["latency_max_ms"] == 90000.0
    assert report["stability_focus"]["question_id"] == 1

    assert "checkpoint_cost_attribution" in report
    assert "run_distribution" in report
    assert report["run_distribution"]["source"] == "task"
    assert report["run_distribution"]["sample_count"] == 2
    assert report["run_distribution"]["tokens_total"]["sample_count"] == 2
    assert "target_comparison" in report
    assert report["target_comparison"]["inference_latency_ms"]["task_p90"] is not None

    diff = await build_run_step_compare(
        mem_session, left_run_id="run_p0", right_run_id="run_p1", task_id="t_perf"
    )
    assert diff is not None
    assert diff["question_id"] == 1
    assert diff["left"]["trace_id"] == "tr0"
    assert diff["right"]["trace_id"] == "tr1"
    assert any(b["type"] == "removed" for b in diff["diff_summary"])
    assert len(diff["round_stability"]["rows"]) >= 1
