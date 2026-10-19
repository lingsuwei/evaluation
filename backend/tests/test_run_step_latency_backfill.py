"""run_call_stats_service 逐步耗时兜底单测。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, EvaluationResult, EvaluationTask, RunSpanNode
from services.eval_context import build_eval_context
from services.run_call_stats_service import (
    backfill_step_sequence_latencies,
    build_step_sequence,
)
from services.task_performance_report_service import build_run_step_compare


def test_backfill_single_step_from_inference_duration():
    run = AgentRun(
        id="run_fb",
        origin="evaluation",
        eval_context=build_eval_context(
            task_id="t1", dataset_id="d1", question_id=0, run_index=0
        ),
        task_id="t1",
        agent_id="ag1",
        status="success",
        started_at=datetime(2026, 1, 1, 0, 0, 0),
        agent_snapshot="{}",
    )
    node = RunSpanNode(
        id="sn1",
        agent_run_id="run_fb",
        observation_id="obs1",
        aggregate_key="step:answer",
        semantic_role="llm",
        sibling_order=0,
        step_index=0,
        node_type="generation",
        name="answer",
        latency_ms=None,
        tokens=100,
    )
    seq = build_step_sequence([node])
    assert seq[0]["latency_ms"] is None
    filled = backfill_step_sequence_latencies(
        seq, run, inference_duration_ms=12500
    )
    assert filled[0]["latency_ms"] == 12500


def test_backfill_single_step_from_run_interval():
    start = datetime(2026, 1, 1, 0, 0, 0)
    run = AgentRun(
        id="run_int",
        origin="evaluation",
        eval_context=build_eval_context(
            task_id="t1", dataset_id="d1", question_id=0, run_index=0
        ),
        task_id="t1",
        agent_id="ag1",
        status="success",
        started_at=start,
        ended_at=start + timedelta(seconds=3),
        agent_snapshot="{}",
    )
    node = RunSpanNode(
        id="sn2",
        agent_run_id="run_int",
        observation_id="obs2",
        aggregate_key="step:answer",
        semantic_role="llm",
        sibling_order=0,
        step_index=0,
        node_type="generation",
        name="answer",
        latency_ms=None,
    )
    seq = build_step_sequence([node])
    filled = backfill_step_sequence_latencies(seq, run)
    assert filled[0]["latency_ms"] == 3000


def test_backfill_skips_multi_step():
    run = AgentRun(
        id="run_multi",
        origin="evaluation",
        eval_context=build_eval_context(
            task_id="t1", dataset_id="d1", question_id=0, run_index=0
        ),
        task_id="t1",
        agent_id="ag1",
        status="success",
        started_at=datetime(2026, 1, 1),
        ended_at=datetime(2026, 1, 1, 0, 0, 10),
        agent_snapshot="{}",
    )
    nodes = [
        RunSpanNode(
            id=f"sn{i}",
            agent_run_id="run_multi",
            observation_id=f"o{i}",
            aggregate_key=f"step:{i}",
            semantic_role="llm",
            sibling_order=i,
            step_index=i,
            node_type="generation",
            latency_ms=None,
        )
        for i in range(2)
    ]
    seq = build_step_sequence(nodes)
    filled = backfill_step_sequence_latencies(seq, run, inference_duration_ms=5000)
    assert all(s["latency_ms"] is None for s in filled)


@pytest.mark.asyncio
async def test_run_step_compare_backfills_latency():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        task = EvaluationTask(
            id="t_lat",
            dataset_id="ds1",
            agent_id="ag1",
            status="completed",
            created_at=datetime.utcnow(),
        )
        session.add(task)
        run0 = AgentRun(
            id="run_l0",
            task_id="t_lat",
            dataset_id="ds1",
            question_id=0,
            run_index=0,
            agent_id="ag1",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
            trace_id="tr0",
        )
        run1 = AgentRun(
            id="run_l1",
            task_id="t_lat",
            dataset_id="ds1",
            question_id=0,
            run_index=1,
            agent_id="ag1",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
            trace_id="tr1",
        )
        session.add_all([run0, run1])
        session.add_all(
            [
                RunSpanNode(
                    id="sn_l0",
                    agent_run_id="run_l0",
                    observation_id="obs_l0",
                    aggregate_key="step:answer",
                    semantic_role="llm",
                    sibling_order=0,
                    step_index=0,
                    node_type="generation",
                    name="answer",
                    latency_ms=None,
                    tokens=100,
                ),
                RunSpanNode(
                    id="sn_l1",
                    agent_run_id="run_l1",
                    observation_id="obs_l1",
                    aggregate_key="step:answer",
                    semantic_role="llm",
                    sibling_order=0,
                    step_index=0,
                    node_type="generation",
                    name="answer",
                    latency_ms=None,
                    tokens=200,
                ),
            ]
        )
        session.add_all(
            [
                EvaluationResult(
                    task_id="t_lat",
                    question_id=0,
                    model="runtime",
                    metric_name="inference_latency",
                    score=1.0,
                    threshold=60000,
                    passed=True,
                    eval_method="runtime",
                    agent_run_id="run_l0",
                    inference_duration_ms=8000,
                ),
                EvaluationResult(
                    task_id="t_lat",
                    question_id=0,
                    model="runtime",
                    metric_name="inference_latency",
                    score=1.0,
                    threshold=60000,
                    passed=True,
                    eval_method="runtime",
                    agent_run_id="run_l1",
                    inference_duration_ms=12000,
                ),
            ]
        )
        await session.commit()

        diff = await build_run_step_compare(
            session, left_run_id="run_l0", right_run_id="run_l1", task_id="t_lat"
        )
        assert diff is not None
        assert diff["left"]["steps"][0]["latency_ms"] == 8000
        assert diff["right"]["steps"][0]["latency_ms"] == 12000
        assert diff["aligned_steps"][0]["delta_latency_ms"] == 4000
    await engine.dispose()
