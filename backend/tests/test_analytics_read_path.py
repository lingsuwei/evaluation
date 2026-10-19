"""Analytics / agent_run 列表读路径单测（P2 F-03 / G-04）。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database import AgentRun, Base, RunMetric, RunSpanNode
from services.agent_run_trace_list import (
    build_trace_detail_from_fact_layer,
    list_traces_from_agent_runs,
    load_observations_for_preview,
)
from services.analytics_service import aggregate_task_run_metrics
from services.eval_context import build_eval_context


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


@pytest.mark.asyncio
async def test_aggregate_task_run_metrics(mem_session):
    mem_session.add(
        AgentRun(
            id="run_a1",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t_an", dataset_id="d1", question_id=0, run_index=0
            ),
            task_id="t_an",
            dataset_id="d1",
            question_id=0,
            agent_id="ag1",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    mem_session.add(
        RunMetric(
            id="m1",
            agent_run_id="run_a1",
            metric_key="eval.correctness",
            source="evaluation",
            value_num=0.8,
            dims_hash="",
        )
    )
    await mem_session.commit()

    out = await aggregate_task_run_metrics(mem_session, "t_an")
    assert out["run_count"] == 1
    assert "eval.correctness" in out["by_metric_key"]
    assert out["by_metric_key"]["eval.correctness"]["avg"] == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_list_traces_from_agent_runs(mem_session):
    mem_session.add(
        AgentRun(
            id="run_list",
            origin="evaluation",
            task_id="t_list",
            dataset_id="d1",
            question_id=0,
            agent_id="ag1",
            agent_name="Agent X",
            agent_version="v1",
            trace_id="tr_list",
            status="success",
            started_at=datetime.utcnow(),
            input_snapshot={"question": "hello"},
            agent_snapshot="{}",
        )
    )
    await mem_session.commit()

    page = await list_traces_from_agent_runs(mem_session, task_id="t_list", limit=10, page=1)
    assert page["meta"]["source"] == "agent_runs"
    assert len(page["data"]) == 1
    assert page["data"][0]["id"] == "tr_list"
    assert page["data"][0]["metadata"]["agent_name"] == "Agent X"


@pytest.mark.asyncio
async def test_load_observations_for_preview(mem_session):
    mem_session.add(
        AgentRun(
            id="run_prev",
            origin="evaluation",
            task_id="t_prev",
            question_id=0,
            agent_id="ag1",
            trace_id="tr_prev",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    mem_session.add(
        RunSpanNode(
            id="span_p",
            agent_run_id="run_prev",
            observation_id="obs_p",
            sibling_order=0,
            step_index=0,
            node_type="span",
            name="step",
        )
    )
    await mem_session.commit()

    obs = await load_observations_for_preview(mem_session, "tr_prev")
    assert obs is not None
    assert len(obs) == 1
    assert obs[0]["name"] == "step"


@pytest.mark.asyncio
async def test_build_trace_detail_from_fact_layer(mem_session):
    mem_session.add(
        AgentRun(
            id="run_det",
            origin="evaluation",
            task_id="t_det",
            question_id=0,
            agent_id="ag1",
            trace_id="tr_det",
            status="success",
            started_at=datetime.utcnow(),
            output_snapshot={"answer": "ok"},
            agent_snapshot="{}",
        )
    )
    mem_session.add(
        RunSpanNode(
            id="span_1",
            agent_run_id="run_det",
            observation_id="obs_1",
            sibling_order=0,
            step_index=0,
            node_type="generation",
            name="answer",
            attributes={"agent.step.id": "main"},
            summary_text="ok",
        )
    )
    await mem_session.commit()

    detail = await build_trace_detail_from_fact_layer(mem_session, "tr_det")
    assert detail is not None
    assert detail["metadata"]["source"] == "fact_layer"
    assert len(detail["observations"]) == 1
    assert detail["observations"][0]["name"] == "answer"
