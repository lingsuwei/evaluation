"""inference_duration_service 单测。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, EvaluationResult, RunMetric
from services.inference_duration_service import (
    batch_load_trace_latency_ms,
    duration_ms_from_run_interval,
    load_token_breakdown_for_run,
    reconcile_inference_latency_for_run,
    reconcile_tokens_total_for_run,
    resolve_agent_e2e_duration_ms,
)
from services.runtime_fact_builder import build_facts_for_trace


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _run(**kwargs) -> AgentRun:
    now = datetime.utcnow()
    defaults = dict(
        id="run_lat",
        task_id="t1",
        dataset_id="ds1",
        question_id=0,
        run_index=0,
        agent_id="ag1",
        status="success",
        started_at=now,
        ended_at=now + timedelta(seconds=2),
        agent_snapshot="{}",
        created_at=now,
    )
    defaults.update(kwargs)
    return AgentRun(**defaults)


def test_resolve_prefers_trace_over_interval_and_snapshot():
    run = _run(output_snapshot={"duration_ms": 500})
    assert resolve_agent_e2e_duration_ms(run, trace_latency_ms=1200) == 1200


def test_resolve_falls_back_to_run_interval():
    run = _run()
    assert duration_ms_from_run_interval(run) == 2000
    assert resolve_agent_e2e_duration_ms(run) == 2000


def test_resolve_falls_back_to_snapshot():
    run = _run(started_at=None, ended_at=None, output_snapshot={"duration_ms": 888})
    assert resolve_agent_e2e_duration_ms(run) == 888


@pytest.mark.asyncio
async def test_batch_load_trace_latency_ms(mem_session):
    run = _run(id="run_metric")
    mem_session.add(run)
    mem_session.add(
        RunMetric(
            id="m1",
            agent_run_id="run_metric",
            metric_key="runtime.latency_ms",
            source="runtime",
            value_num=3210.0,
        )
    )
    await mem_session.flush()
    loaded = await batch_load_trace_latency_ms(mem_session, ["run_metric"])
    assert loaded["run_metric"] == 3210


@pytest.mark.asyncio
async def test_reconcile_updates_evaluation_result(mem_session):
    from sqlalchemy import select

    run = _run(id="run_rec", trace_id="tr_rec")
    mem_session.add(run)
    mem_session.add(
        EvaluationResult(
            task_id="t1",
            question_id=0,
            model="runtime",
            metric_name="inference_latency",
            score=0.0,
            threshold=5000.0,
            passed=False,
            eval_method="runtime",
            agent_run_id="run_rec",
            inference_duration_ms=900,
        )
    )
    await mem_session.flush()

    await reconcile_inference_latency_for_run(mem_session, run, 1500)
    await mem_session.commit()

    row = (
        await mem_session.execute(
            select(EvaluationResult).where(EvaluationResult.agent_run_id == "run_rec")
        )
    ).scalar_one()
    assert row.inference_duration_ms == 1500
    assert row.passed is True


@pytest.mark.asyncio
async def test_reconcile_tokens_updates_evaluation_result(mem_session):
    from sqlalchemy import select

    run = _run(id="run_tok_rec", trace_id="tr_tok", tokens_total=420)
    mem_session.add(run)
    mem_session.add(
        EvaluationResult(
            task_id="t1",
            question_id=0,
            model="runtime",
            metric_name="tokens_total",
            score=0.0,
            threshold=500.0,
            passed=False,
            eval_method="runtime",
            agent_run_id="run_tok_rec",
        )
    )
    await mem_session.flush()

    await reconcile_tokens_total_for_run(mem_session, run, 420)
    await mem_session.commit()

    row = (
        await mem_session.execute(
            select(EvaluationResult).where(EvaluationResult.agent_run_id == "run_tok_rec")
        )
    ).scalar_one()
    assert row.passed is True
    assert row.avg_score == 1.0


@pytest.mark.asyncio
async def test_load_token_breakdown_prefers_run_metrics(mem_session):
    run = _run(
        id="run_bd",
        output_snapshot={"tokens_llm": 99, "usage": {"total_tokens": 99}},
    )
    mem_session.add(run)
    mem_session.add(
        RunMetric(
            id="m_llm",
            agent_run_id="run_bd",
            metric_key="runtime.tokens_llm",
            source="runtime",
            value_num=200.0,
        )
    )
    mem_session.add(
        RunMetric(
            id="m_tool",
            agent_run_id="run_bd",
            metric_key="runtime.tokens_tool",
            source="runtime",
            value_num=15.0,
        )
    )
    await mem_session.flush()

    llm, tool = await load_token_breakdown_for_run(mem_session, "run_bd")
    assert llm == 200
    assert tool == 15


@pytest.mark.asyncio
async def test_build_facts_triggers_tokens_reconcile(mem_session):
    from sqlalchemy import select

    run = _run(id="run_fact_tok", trace_id="tr_fact_tok", tokens_total=None)
    mem_session.add(run)
    mem_session.add(
        EvaluationResult(
            task_id="t1",
            question_id=0,
            model="runtime",
            metric_name="tokens_total",
            score=0.0,
            threshold=1000.0,
            passed=False,
            eval_method="runtime",
            agent_run_id="run_fact_tok",
        )
    )
    await mem_session.flush()

    trace = {
        "id": "tr_fact_tok",
        "observations": [
            {"id": "o1", "type": "GENERATION", "usage": {"total": 300}},
            {"id": "o2", "type": "TOOL", "usage": {"total": 20}},
        ],
    }
    await build_facts_for_trace(mem_session, run, trace)
    await mem_session.commit()

    refreshed_run = (
        await mem_session.execute(select(AgentRun).where(AgentRun.id == "run_fact_tok"))
    ).scalar_one()
    assert refreshed_run.tokens_total == 320

    row = (
        await mem_session.execute(
            select(EvaluationResult).where(EvaluationResult.agent_run_id == "run_fact_tok")
        )
    ).scalar_one()
    assert row.passed is True


@pytest.mark.asyncio
async def test_build_facts_triggers_latency_reconcile(mem_session):
    from sqlalchemy import select

    run = _run(id="run_fact", trace_id="tr_fact", tokens_total=None)
    mem_session.add(run)
    mem_session.add(
        EvaluationResult(
            task_id="t1",
            question_id=0,
            model="runtime",
            metric_name="inference_latency",
            score=0.0,
            threshold=10000.0,
            passed=False,
            eval_method="runtime",
            agent_run_id="run_fact",
            inference_duration_ms=800,
        )
    )
    await mem_session.flush()

    trace = {"id": "tr_fact", "latency": 2200, "observations": []}
    await build_facts_for_trace(mem_session, run, trace)
    await mem_session.commit()

    row = (
        await mem_session.execute(
            select(EvaluationResult).where(EvaluationResult.agent_run_id == "run_fact")
        )
    ).scalar_one()
    assert row.inference_duration_ms == 2200
