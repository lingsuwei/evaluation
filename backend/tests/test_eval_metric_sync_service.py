"""评测分 → run_metrics 同步单测（P2 F-02）。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database import AgentRun, Base, EvaluationResult, RunMetric
from services.eval_context import build_eval_context
from services.eval_metric_sync_service import eval_metric_key, sync_evaluation_result_to_run_metrics


@pytest.fixture
async def mem_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def test_eval_metric_key_normalizes():
    assert eval_metric_key("Faithfulness") == "eval.faithfulness"
    assert eval_metric_key("correctness") == "eval.correctness"
    assert eval_metric_key("Metric Name") == "eval.metric_name"


@pytest.mark.asyncio
async def test_sync_evaluation_result_writes_run_metric(mem_session):
    run = AgentRun(
        id="run_eval_sync",
        origin="evaluation",
        eval_context=build_eval_context(
            task_id="t1", dataset_id="d1", question_id=0, run_index=0
        ),
        task_id="t1",
        dataset_id="d1",
        question_id=0,
        run_index=0,
        agent_id="ag1",
        status="success",
        started_at=datetime.utcnow(),
        agent_snapshot="{}",
    )
    mem_session.add(run)
    await mem_session.flush()

    result = EvaluationResult(
        task_id="t1",
        question_id=0,
        model="glm",
        metric_name="correctness",
        score=0.85,
        avg_score=0.85,
        threshold=0.7,
        passed=True,
        agent_run_id="run_eval_sync",
        eval_method="llm",
    )
    mem_session.add(result)
    await mem_session.flush()

    ok = await sync_evaluation_result_to_run_metrics(mem_session, result)
    await mem_session.commit()

    assert ok is True
    rows = (
        await mem_session.execute(
            select(RunMetric).where(RunMetric.agent_run_id == "run_eval_sync")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].metric_key == "eval.correctness"
    assert rows[0].source == "evaluation"
    assert rows[0].value_num == pytest.approx(0.85)
    assert rows[0].value_json["passed"] is True


@pytest.mark.asyncio
async def test_sync_skips_without_agent_run_id(mem_session):
    result = EvaluationResult(
        task_id="t1",
        question_id=0,
        model="glm",
        metric_name="correctness",
        score=1.0,
        threshold=0.5,
        passed=True,
    )
    mem_session.add(result)
    await mem_session.flush()

    ok = await sync_evaluation_result_to_run_metrics(mem_session, result)
    assert ok is False


@pytest.mark.asyncio
async def test_sync_runtime_tokens_total_uses_agent_run_raw_value(mem_session):
    run = AgentRun(
        id="run_tok_sync",
        origin="evaluation",
        eval_context=build_eval_context(
            task_id="t1", dataset_id="d1", question_id=0, run_index=0
        ),
        task_id="t1",
        dataset_id="d1",
        question_id=0,
        run_index=0,
        agent_id="ag1",
        status="success",
        started_at=datetime.utcnow(),
        agent_snapshot="{}",
        tokens_total=622975,
    )
    mem_session.add(run)
    await mem_session.flush()

    result = EvaluationResult(
        task_id="t1",
        question_id=0,
        model="glm",
        metric_name="tokens_total",
        score=0.0,
        avg_score=0.0,
        threshold=100000.0,
        passed=False,
        agent_run_id="run_tok_sync",
        eval_method="runtime",
        inference_duration_ms=143650,
    )
    mem_session.add(result)
    await mem_session.flush()

    ok = await sync_evaluation_result_to_run_metrics(mem_session, result)
    await mem_session.commit()

    assert ok is True
    rows = (
        await mem_session.execute(
            select(RunMetric).where(RunMetric.agent_run_id == "run_tok_sync")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].metric_key == "eval.tokens_total"
    assert rows[0].value_num == pytest.approx(622975.0)
    assert rows[0].value_json["efficiency_score"] == pytest.approx(0.0)

    llm_rows = (
        await mem_session.execute(
            select(RunMetric).where(
                RunMetric.agent_run_id == "run_tok_sync",
                RunMetric.metric_key == "eval.tokens_llm",
            )
        )
    ).scalars().all()
    assert len(llm_rows) == 0


@pytest.mark.asyncio
async def test_sync_runtime_tokens_total_writes_submetrics_from_run_metrics(mem_session):
    run = AgentRun(
        id="run_tok_sub",
        origin="evaluation",
        eval_context=build_eval_context(
            task_id="t1", dataset_id="d1", question_id=0, run_index=0
        ),
        task_id="t1",
        dataset_id="d1",
        question_id=0,
        run_index=0,
        agent_id="ag1",
        status="success",
        started_at=datetime.utcnow(),
        agent_snapshot="{}",
        tokens_total=500,
        output_snapshot={
            "status": "success",
            "tokens_llm": 480,
            "usage": {"total_tokens": 500, "tokens_llm": 480},
        },
    )
    mem_session.add(run)
    mem_session.add(
        RunMetric(
            id="rm_llm",
            agent_run_id="run_tok_sub",
            metric_key="runtime.tokens_llm",
            source="runtime",
            value_num=410.0,
        )
    )
    mem_session.add(
        RunMetric(
            id="rm_tool",
            agent_run_id="run_tok_sub",
            metric_key="runtime.tokens_tool",
            source="runtime",
            value_num=25.0,
        )
    )
    await mem_session.flush()

    result = EvaluationResult(
        task_id="t1",
        question_id=0,
        model="glm",
        metric_name="tokens_total",
        score=1.0,
        avg_score=1.0,
        threshold=100000.0,
        passed=True,
        agent_run_id="run_tok_sub",
        eval_method="runtime",
    )
    mem_session.add(result)
    await mem_session.flush()

    await sync_evaluation_result_to_run_metrics(mem_session, result)
    await mem_session.commit()

    llm = (
        await mem_session.execute(
            select(RunMetric).where(
                RunMetric.metric_key == "eval.tokens_llm",
                RunMetric.agent_run_id == "run_tok_sub",
            )
        )
    ).scalar_one()
    tool = (
        await mem_session.execute(
            select(RunMetric).where(
                RunMetric.metric_key == "eval.tokens_tool",
                RunMetric.agent_run_id == "run_tok_sub",
            )
        )
    ).scalar_one()
    assert llm.value_num == pytest.approx(410.0)
    assert tool.value_num == pytest.approx(25.0)


@pytest.mark.asyncio
async def test_sync_runtime_tokens_total_writes_submetrics_from_snapshot(mem_session):
    run = AgentRun(
        id="run_tok_sub",
        origin="evaluation",
        eval_context=build_eval_context(
            task_id="t1", dataset_id="d1", question_id=0, run_index=0
        ),
        task_id="t1",
        dataset_id="d1",
        question_id=0,
        run_index=0,
        agent_id="ag1",
        status="success",
        started_at=datetime.utcnow(),
        agent_snapshot="{}",
        tokens_total=500,
        output_snapshot={
            "status": "success",
            "tokens_llm": 480,
            "usage": {"total_tokens": 500, "tokens_llm": 480},
        },
    )
    mem_session.add(run)
    await mem_session.flush()

    result = EvaluationResult(
        task_id="t1",
        question_id=0,
        model="glm",
        metric_name="tokens_total",
        score=1.0,
        avg_score=1.0,
        threshold=100000.0,
        passed=True,
        agent_run_id="run_tok_sub",
        eval_method="runtime",
    )
    mem_session.add(result)
    await mem_session.flush()

    await sync_evaluation_result_to_run_metrics(mem_session, result)
    await mem_session.commit()

    llm = (
        await mem_session.execute(
            select(RunMetric).where(
                RunMetric.metric_key == "eval.tokens_llm",
                RunMetric.agent_run_id == "run_tok_sub",
            )
        )
    ).scalar_one()
    assert llm.value_num == pytest.approx(480.0)


@pytest.mark.asyncio
async def test_sync_runtime_inference_latency_uses_raw_ms(mem_session):
    run = AgentRun(
        id="run_lat_sync",
        origin="evaluation",
        eval_context=build_eval_context(
            task_id="t1", dataset_id="d1", question_id=1, run_index=0
        ),
        task_id="t1",
        dataset_id="d1",
        question_id=1,
        run_index=0,
        agent_id="ag1",
        status="success",
        started_at=datetime.utcnow(),
        agent_snapshot="{}",
    )
    mem_session.add(run)
    await mem_session.flush()

    result = EvaluationResult(
        task_id="t1",
        question_id=1,
        model="glm",
        metric_name="inference_latency",
        score=1.0,
        avg_score=1.0,
        threshold=180000.0,
        passed=True,
        agent_run_id="run_lat_sync",
        eval_method="runtime",
        inference_duration_ms=89426,
    )
    mem_session.add(result)
    await mem_session.flush()

    ok = await sync_evaluation_result_to_run_metrics(mem_session, result)
    await mem_session.commit()

    assert ok is True
    row = (
        await mem_session.execute(
            select(RunMetric).where(RunMetric.agent_run_id == "run_lat_sync")
        )
    ).scalar_one()
    assert row.metric_key == "eval.inference_latency"
    assert row.value_num == pytest.approx(89426.0)
    assert row.value_json["efficiency_score"] == pytest.approx(1.0)
