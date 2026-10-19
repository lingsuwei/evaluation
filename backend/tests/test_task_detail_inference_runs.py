"""任务详情 inference_runs_by_question 单测。"""
from __future__ import annotations

from datetime import datetime, timedelta

from database import AgentRun, EvaluationResult
from services.task_detail_inference_runs import (
    build_inference_runs_by_question,
    model_answer_from_run,
)


def _token_breakdown(run):
    return None, None


def test_build_inference_runs_prefers_trace_latency():
    now = datetime.utcnow()
    runs = [
        AgentRun(
            id="r1",
            origin="evaluation",
            task_id="t1",
            dataset_id="d1",
            question_id=0,
            run_index=0,
            agent_id="ag1",
            status="success",
            started_at=now,
            ended_at=now + timedelta(seconds=1),
            output_snapshot={"model_answer": "ans1"},
            agent_snapshot="{}",
        ),
    ]
    results = [
        EvaluationResult(
            task_id="t1",
            question_id=0,
            model="runtime",
            metric_name="inference_latency",
            score=1.0,
            threshold=1.0,
            passed=True,
            agent_run_id="r1",
            inference_duration_ms=2000,
        ),
    ]
    grouped = build_inference_runs_by_question(
        runs,
        results,
        token_breakdown_fn=_token_breakdown,
        trace_latency_by_run={"r1": 3500},
    )
    assert grouped["0"][0]["inference_duration_ms"] == 3500


def test_build_inference_runs_by_question_orders_by_run_index():
    now = datetime.utcnow()
    runs = [
        AgentRun(
            id="r2",
            origin="evaluation",
            task_id="t1",
            dataset_id="d1",
            question_id=0,
            run_index=2,
            agent_id="ag1",
            status="success",
            started_at=now,
            output_snapshot={"model_answer": "ans3"},
            agent_snapshot="{}",
        ),
        AgentRun(
            id="r0",
            origin="evaluation",
            task_id="t1",
            dataset_id="d1",
            question_id=0,
            run_index=0,
            agent_id="ag1",
            status="success",
            started_at=now,
            output_snapshot={"model_answer": "ans1"},
            agent_snapshot="{}",
        ),
        AgentRun(
            id="r1",
            origin="evaluation",
            task_id="t1",
            dataset_id="d1",
            question_id=0,
            run_index=1,
            agent_id="ag1",
            status="success",
            started_at=now,
            output_snapshot={"model_answer": "ans2"},
            agent_snapshot="{}",
        ),
    ]
    results = [
        EvaluationResult(
            task_id="t1",
            question_id=0,
            model="runtime",
            metric_name="inference_latency",
            score=1.0,
            threshold=1.0,
            passed=True,
            agent_run_id="r1",
            inference_duration_ms=2000,
        ),
    ]
    grouped = build_inference_runs_by_question(runs, results, token_breakdown_fn=_token_breakdown)
    assert len(grouped["0"]) == 3
    assert [x["run_index"] for x in grouped["0"]] == [0, 1, 2]
    assert grouped["0"][0]["agent_answer"] == "ans1"
    assert grouped["0"][0]["is_primary"] is True
    assert grouped["0"][1]["inference_duration_ms"] == 2000
    assert grouped["0"][2]["agent_answer"] == "ans3"


def test_model_answer_from_run_error():
    run = AgentRun(
        id="rx",
        origin="evaluation",
        agent_id="ag1",
        status="failed",
        started_at=datetime.utcnow(),
        error_message="timeout",
        agent_snapshot="{}",
    )
    assert "timeout" in model_answer_from_run(run)
