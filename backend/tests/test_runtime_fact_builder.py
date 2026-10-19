"""Runtime Fact Builder 单元测试（TS-BLD-*）"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base
from services.runtime_fact_builder import (
    apply_runtime_facts_from_trace,
    count_observations,
    summarize_token_breakdown_from_trace,
    summarize_tokens_from_trace,
)


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
    defaults = dict(
        id="run_bld",
        task_id="t1",
        dataset_id="ds1",
        question_id=0,
        run_index=0,
        agent_id="ag1",
        status="success",
        started_at=datetime.utcnow(),
        agent_snapshot="{}",
        created_at=datetime.utcnow(),
    )
    defaults.update(kwargs)
    return AgentRun(**defaults)


def test_summarize_empty_observations():
    assert summarize_tokens_from_trace({}) is None
    assert summarize_tokens_from_trace({"observations": []}) is None


def test_summarize_mixed_usage_keys():
    trace = {
        "observations": [
            {"usage": {"totalTokens": 20}},
            {"usage": {"promptTokens": 3, "completionTokens": 2}},
        ]
    }
    assert summarize_tokens_from_trace(trace) == 25


def test_summarize_snake_case_prompt_completion_keys():
    trace = {
        "observations": [
            {"usage": {"prompt_tokens": 11, "completion_tokens": 7}},
            {"usage": {"input_tokens": 5, "output_tokens": 4}},
        ]
    }
    assert summarize_tokens_from_trace(trace) == 27


def test_summarize_ignores_non_dict_observations():
    trace = {"observations": ["bad", {"usage": {"total": 5}}, None]}
    assert summarize_tokens_from_trace(trace) == 5


def test_count_observations_invalid_root():
    assert count_observations({"observations": "nope"}) == 0


@pytest.mark.asyncio
async def test_apply_builds_span_nodes(mem_session):
    from database import RunSpanNode
    from sqlalchemy import select

    run = _run(tokens_total=None, trace_id="tr1")
    mem_session.add(run)
    await mem_session.flush()
    trace = {
        "observations": [
            {"id": "o1", "name": "s", "type": "SPAN", "metadata": {"agent.step.id": "x"}},
        ]
    }
    out = await apply_runtime_facts_from_trace(mem_session, run, trace)
    assert out["span_nodes"] == 1
    spans = (await mem_session.execute(select(RunSpanNode))).scalars().all()
    assert len(spans) == 1
    assert spans[0].aggregate_key == "step:x"


def test_summarize_token_breakdown_splits_llm_and_tool():
    trace = {
        "observations": [
            {"type": "GENERATION", "usage": {"total": 100}},
            {"type": "TOOL", "usage": {"total": 15}},
            {"type": "SPAN", "usage": {"total": 5}},
        ]
    }
    llm, tool = summarize_token_breakdown_from_trace(trace)
    assert llm == 105
    assert tool == 15


@pytest.mark.asyncio
async def test_apply_writes_token_breakdown_run_metrics(mem_session):
    from database import RunMetric
    from sqlalchemy import select

    run = _run(
        tokens_total=None,
        trace_id="tr_tok",
        output_snapshot={
            "status": "success",
            "tokens_llm": 200,
            "usage": {"total_tokens": 200, "tokens_llm": 200},
        },
    )
    mem_session.add(run)
    await mem_session.flush()
    trace = {
        "observations": [
            {"id": "o1", "type": "GENERATION", "usage": {"total": 100}},
            {"id": "o2", "type": "TOOL", "usage": {"total": 12}, "metadata": {"agent.tool.name": "t1"}},
        ]
    }
    out = await apply_runtime_facts_from_trace(mem_session, run, trace)
    assert out["tokens_llm"] == 200
    assert out["tokens_tool"] == 12

    metrics = (await mem_session.execute(select(RunMetric))).scalars().all()
    keys = {m.metric_key: m.value_num for m in metrics}
    assert keys.get("runtime.tokens_llm") == 200.0
    assert keys.get("runtime.tokens_tool") == 12.0
    assert keys.get("runtime.tokens_total") == 212.0
    assert keys.get("runtime.llm_call_count") == 1.0
    assert keys.get("runtime.tool_call_count") == 1.0


@pytest.mark.asyncio
async def test_apply_attributes_incomplete_when_no_agent_step(mem_session):
    run = _run(trace_id="tr_inc")
    mem_session.add(run)
    await mem_session.flush()
    out = await apply_runtime_facts_from_trace(
        mem_session,
        run,
        {"observations": [{"id": "o1", "name": "gen", "type": "GENERATION"}]},
    )
    assert out["attributes_incomplete"] is True


@pytest.mark.asyncio
async def test_apply_attributes_complete_with_agent_step(mem_session):
    run = _run(trace_id="tr_ok")
    mem_session.add(run)
    await mem_session.flush()
    out = await apply_runtime_facts_from_trace(
        mem_session,
        run,
        {
            "observations": [
                {
                    "id": "o1",
                    "name": "s",
                    "type": "SPAN",
                    "metadata": {"agent.step.id": "main"},
                }
            ]
        },
    )
    assert out["attributes_incomplete"] is False


@pytest.mark.asyncio
async def test_apply_fills_null_tokens(mem_session):
    run = _run(tokens_total=None, trace_id="tr1")
    mem_session.add(run)
    await mem_session.flush()
    out = await apply_runtime_facts_from_trace(
        mem_session, run, {"observations": [{"usage": {"total": 30}}]}
    )
    assert out["tokens_total"] == 30
    assert out["observation_count"] == 1


@pytest.mark.asyncio
async def test_apply_writes_tokens_from_trace_as_single_source(mem_session):
    """COUP-C04：Builder 是 run.tokens_total 唯一写入源，直接用 trace 值覆盖。"""
    run = _run(tokens_total=None)
    mem_session.add(run)
    await mem_session.flush()
    await apply_runtime_facts_from_trace(
        mem_session, run, {"observations": [{"usage": {"total": 50}}]}
    )
    assert run.tokens_total == 50


@pytest.mark.asyncio
async def test_apply_overwrites_with_trace_tokens(mem_session):
    """Builder 幂等：重新物化时用最新 trace 值覆盖（如 trace 被更新）。"""
    run = _run(tokens_total=10)
    mem_session.add(run)
    await mem_session.flush()
    await apply_runtime_facts_from_trace(
        mem_session, run, {"observations": [{"usage": {"total": 99}}]}
    )
    assert run.tokens_total == 99


@pytest.mark.asyncio
async def test_apply_writes_inference_model_from_trace(mem_session):
    run = _run(
        trace_id="tr_model",
        inference_model_provider="ConfigVendor",
        inference_model_name="config-model",
        inference_model_version="v0",
    )
    mem_session.add(run)
    await mem_session.flush()
    trace = {
        "observations": [
            {
                "id": "o1",
                "type": "GENERATION",
                "name": "llm:main_iteration",
                "model": "deepseek-chat",
                "metadata": {"model_provider": "DeepSeek", "model_version": "v3"},
            }
        ]
    }
    await apply_runtime_facts_from_trace(mem_session, run, trace)
    assert run.inference_model_name == "deepseek-chat"
    assert run.inference_model_provider == "DeepSeek"
    assert run.inference_model_version == "v3"


@pytest.mark.asyncio
async def test_apply_skips_inference_model_when_trace_has_no_llm_model(mem_session):
    run = _run(
        trace_id="tr_no_model",
        inference_model_name="config-model",
        inference_model_provider="ConfigVendor",
    )
    mem_session.add(run)
    await mem_session.flush()
    trace = {
        "observations": [
            {"id": "o1", "type": "TOOL", "name": "tool:search", "usage": {"total": 10}},
        ]
    }
    await apply_runtime_facts_from_trace(mem_session, run, trace)
    assert run.inference_model_name == "config-model"
    assert run.inference_model_provider == "ConfigVendor"
