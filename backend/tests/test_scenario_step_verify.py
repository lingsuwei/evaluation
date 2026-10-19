"""场景子步骤 Trace 绑定试跑单测。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base, Dataset
from services.dataset_item_scenario_service import upsert_item_template
from services.scenario_step_verify_service import (
    _evaluate_step_binding,
    batch_preview_scenario_steps_for_item,
    preview_scenario_step_on_trace,
)
from services.dataset_item_scenario_service import upsert_item_template

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


def test_evaluate_step_binding_matches_answer_span():
    scenario_key = "ditem_item1"
    step_def = {
        "step_key": "S1",
        "name": "回答",
        "gt_expected": {"keywords": ["结论"]},
        "match_rules": {
            "trace_binding": {
                "span_name_patterns": ["answer", "llm:main"],
                "output_keywords": ["结论"],
            }
        },
    }
    observations = [
        {"id": "obs_root", "name": "agent_run", "type": "SPAN", "output": ""},
        {
            "id": "obs_gen",
            "name": "answer",
            "type": "GENERATION",
            "output": "最终结论如下",
        },
    ]
    ev = _evaluate_step_binding(
        scenario_key=scenario_key,
        step_def=step_def,
        observations=observations,
    )
    assert ev["span_bound"] is True
    assert ev["perf_ready"] is True
    assert ev["matched_observation_name"] == "answer"


def test_evaluate_step_binding_default_patterns_when_unbound():
    """空 match_rules 时，per-item 场景会回退到默认 span_name_patterns（llm:main_iteration 等）。

    生产逻辑（scenario_step_trace_service.get_step_trace_binding）：空 match_rules 不再视为 unbound，
    而是返回默认 patterns，bind_step_observation 会用默认 patterns + heuristic fallback 绑定 observation。
    """
    scenario_key = "ditem_item1"
    step_def = {
        "step_key": "S1",
        "name": "工具步",
        "gt_expected": {"keywords": ["检索"]},
        "match_rules": {},
    }
    observations = [
        {"id": "obs_gen", "name": "answer", "type": "GENERATION", "output": "ok"},
    ]
    ev = _evaluate_step_binding(
        scenario_key=scenario_key,
        step_def=step_def,
        observations=observations,
    )
    # 默认 patterns 启用 → observation 被绑定（heuristic fallback）
    assert ev["span_bound"] is True
    # 返回默认 span_name_patterns
    assert ev["span_name_patterns"] == ["llm:main_iteration", "llm:main", "tool:"]


@pytest.mark.asyncio
async def test_batch_preview_scenario_steps_for_item(monkeypatch, mem_session):
    ds = Dataset(
        id="ds_t",
        name="test",
        data=json.dumps([{"question": "q1"}], ensure_ascii=False),
        item_count=1,
    )
    mem_session.add(ds)
    await mem_session.flush()

    ditem_id = "ditem_test1"
    await upsert_item_template(
        mem_session,
        dataset_id="ds_t",
        dataset_item_id=ditem_id,
        name="Q1 template",
        steps=[
            {
                "step_key": "S1",
                "name": "回答",
                "step_order": 1,
                "weight": 1.0,
                "required": True,
                "enabled": True,
                "gt_expected": {"keywords": ["结论"]},
                "match_rules": {
                    "trace_binding": {
                        "span_name_patterns": ["answer"],
                        "output_keywords": ["结论"],
                    }
                },
            }
        ],
    )
    await mem_session.commit()

    async def _fake_load(session, *, trace_id, project, source, agent_name, client_for_project=None):
        obs = [
            {"id": "obs_gen", "name": "answer", "type": "GENERATION", "output": "结论"},
        ]
        return obs, "fact_layer", "proj", ["answer"]

    monkeypatch.setattr(
        "services.scenario_step_verify_service._load_trace_observations",
        _fake_load,
    )

    payload = await batch_preview_scenario_steps_for_item(
        mem_session,
        dataset_item_id=ditem_id,
        trace_id="trace_abc",
        agent_name="test-agent",
    )
    assert payload["bound_count"] == 1
    assert payload["all_enabled_bound"] is True
    assert payload["results"][0]["span_bound"] is True


# ── LLM 汇聚试跑单测 ────────────────────────────────────────────────


def test_evaluate_step_binding_with_llm_aggregate_match_rules():
    """eval_config.mode=llm_aggregate 时，_evaluate_step_binding 仍正常返回绑定结果
    （LLM 汇聚在 preview/batch 层调用，不影响 binding 层）。"""
    step_def = {
        "step_key": "S1",
        "name": "态势研判",
        "gt_expected": {"keywords": ["0.727"]},
        "match_rules": {
            "trace_binding": {
                "span_name_patterns": ["llm:main"],
                "output_keywords": ["海况"],
            },
            "eval_config": {
                "mode": "llm_aggregate",
                "aggregate_prompt": "请提取海况关键参数",
            },
        },
    }
    observations = [
        {"id": "obs1", "name": "llm:main", "type": "GENERATION", "output": "海况系数 0.727"},
    ]
    ev = _evaluate_step_binding(
        scenario_key="ditem_test1",
        step_def=step_def,
        observations=observations,
    )
    assert ev["span_bound"] is True
    assert "0.727" in ev.get("output_preview", "")


@pytest.mark.asyncio
async def test_preview_scenario_step_on_trace_llm_aggregate(monkeypatch, mem_session):
    """preview_scenario_step_on_trace 在 llm_aggregate 模式下调用 LLM 汇聚并返回预判关键词命中。"""
    ds = Dataset(
        id="ds_llm_agg",
        name="test-llm-agg",
        data=json.dumps([{"question": "q1"}], ensure_ascii=False),
        item_count=1,
    )
    mem_session.add(ds)
    await mem_session.flush()

    ditem_id = "ditem_llm_agg"
    await upsert_item_template(
        mem_session,
        dataset_id="ds_llm_agg",
        dataset_item_id=ditem_id,
        name="LLM Agg Test",
        steps=[
            {
                "step_key": "S1",
                "name": "态势研判",
                "step_order": 1,
                "weight": 1.0,
                "required": True,
                "enabled": True,
                "gt_expected": {"keywords": ["0.727", "225"]},
                "match_rules": {
                    "trace_binding": {
                        "span_name_patterns": ["answer"],
                        "output_keywords": ["海况"],
                    },
                    "eval_config": {
                        "mode": "llm_aggregate",
                        "aggregate_prompt": "请提取海况关键参数",
                    },
                },
            }
        ],
    )
    await mem_session.commit()

    async def _fake_load(session, *, trace_id, project, source, agent_name, client_for_project=None):
        obs = [
            {"id": "obs_gen", "name": "answer", "type": "GENERATION", "output": "海况系数 0.727 威胁方向 225°"},
        ]
        return obs, "fact_layer", "proj", ["answer"]

    monkeypatch.setattr(
        "services.scenario_step_verify_service._load_trace_observations",
        _fake_load,
    )

    async def _fake_llm_aggregate(step_output, aggregate_prompt, llm_model=None):
        return "海况系数 0.727，威胁方向 225°"

    monkeypatch.setattr(
        "services.scenario_step_verify_service.llm_aggregate_step_output",
        _fake_llm_aggregate,
    )

    result = await preview_scenario_step_on_trace(
        mem_session,
        dataset_item_id=ditem_id,
        trace_id="trace_agg_001",
        agent_name="test-agent",
        step_key="S1",
        name="态势研判",
        gt_expected={"keywords": ["0.727", "225"]},
        match_rules={
            "trace_binding": {
                "span_name_patterns": ["answer"],
                "output_keywords": ["海况"],
            },
            "eval_config": {
                "mode": "llm_aggregate",
                "aggregate_prompt": "请提取海况关键参数",
            },
        },
    )
    assert result["eval_mode"] == "llm_aggregate"
    assert "0.727" in result.get("aggregated_answer_preview", "")
    assert "0.727" in " ".join(result.get("aggregate_keyword_hits", []))


@pytest.mark.asyncio
async def test_preview_scenario_step_on_trace_llm_aggregate_no_output(monkeypatch, mem_session):
    """LLM 汇聚模式下 span 绑定无 output 时，不调用 LLM，返回空的汇聚预览。"""
    ds = Dataset(
        id="ds_no_out",
        name="test-no-output",
        data=json.dumps([{"question": "q1"}], ensure_ascii=False),
        item_count=1,
    )
    mem_session.add(ds)
    await mem_session.flush()

    ditem_id = "ditem_no_out"
    await upsert_item_template(
        mem_session,
        dataset_id="ds_no_out",
        dataset_item_id=ditem_id,
        name="No Output Test",
        steps=[
            {
                "step_key": "S1",
                "name": "测试步骤",
                "step_order": 1,
                "weight": 1.0,
                "required": True,
                "enabled": True,
                "gt_expected": {"keywords": ["结论"]},
                "match_rules": {
                    "eval_config": {
                        "mode": "llm_aggregate",
                        "aggregate_prompt": "请汇总",
                    },
                },
            }
        ],
    )
    await mem_session.commit()

    # 无匹配 observation → span_bound=False, output 为空
    async def _fake_load(session, *, trace_id, project, source, agent_name, client_for_project=None):
        return [], "none", None, []

    monkeypatch.setattr(
        "services.scenario_step_verify_service._load_trace_observations",
        _fake_load,
    )

    llm_called = False

    async def _fake_llm_aggregate(step_output, aggregate_prompt, llm_model=None):
        nonlocal llm_called
        llm_called = True
        return "不应被调用"

    monkeypatch.setattr(
        "services.scenario_step_verify_service.llm_aggregate_step_output",
        _fake_llm_aggregate,
    )

    result = await preview_scenario_step_on_trace(
        mem_session,
        dataset_item_id=ditem_id,
        trace_id="trace_no_out",
        agent_name="test-agent",
        step_key="S1",
        name="测试步骤",
        gt_expected={"keywords": ["结论"]},
        match_rules={
            "eval_config": {
                "mode": "llm_aggregate",
                "aggregate_prompt": "请汇总",
            },
        },
    )
    # 无 span → 不会走到 LLM 汇聚，但 eval_mode 仍标记 llm_aggregate
    assert result.get("eval_mode") == "llm_aggregate"
    assert result.get("aggregated_answer_preview", "") == ""
    assert not llm_called


@pytest.mark.asyncio
async def test_batch_preview_with_llm_aggregate(monkeypatch, mem_session):
    """batch_preview 中含 llm_aggregate 步骤时，结果中包含汇聚信息。"""
    ds = Dataset(
        id="ds_batch_agg",
        name="test-batch-agg",
        data=json.dumps([{"question": "q1"}], ensure_ascii=False),
        item_count=1,
    )
    mem_session.add(ds)
    await mem_session.flush()

    ditem_id = "ditem_batch_agg"
    await upsert_item_template(
        mem_session,
        dataset_id="ds_batch_agg",
        dataset_item_id=ditem_id,
        name="Batch LLM Agg",
        steps=[
            {
                "step_key": "S1",
                "name": "常规匹配",
                "step_order": 1,
                "weight": 0.5,
                "required": True,
                "enabled": True,
                "gt_expected": {"keywords": ["结论"]},
                "match_rules": {
                    "trace_binding": {
                        "span_name_patterns": ["answer"],
                        "output_keywords": ["结论"],
                    },
                },
            },
            {
                "step_key": "S2",
                "name": "LLM汇聚步",
                "step_order": 2,
                "weight": 0.5,
                "required": True,
                "enabled": True,
                "gt_expected": {"keywords": ["0.727"]},
                "match_rules": {
                    "trace_binding": {
                        "span_name_patterns": ["llm:main"],
                        "output_keywords": ["海况"],
                    },
                    "eval_config": {
                        "mode": "llm_aggregate",
                        "aggregate_prompt": "请提取海况关键参数",
                    },
                },
            },
        ],
    )
    await mem_session.commit()

    async def _fake_load(session, *, trace_id, project, source, agent_name, client_for_project=None):
        obs = [
            {"id": "obs1", "name": "answer", "type": "GENERATION", "output": "结论如下"},
            {"id": "obs2", "name": "llm:main", "type": "GENERATION", "output": "海况系数 0.727"},
        ]
        return obs, "fact_layer", "proj", ["answer", "llm:main"]

    monkeypatch.setattr(
        "services.scenario_step_verify_service._load_trace_observations",
        _fake_load,
    )

    async def _fake_llm_aggregate(step_output, aggregate_prompt, llm_model=None):
        return "海况系数 0.727 汇总"

    monkeypatch.setattr(
        "services.scenario_step_verify_service.llm_aggregate_step_output",
        _fake_llm_aggregate,
    )

    payload = await batch_preview_scenario_steps_for_item(
        mem_session,
        dataset_item_id=ditem_id,
        trace_id="trace_batch_agg",
        agent_name="test-agent",
    )
    assert payload["bound_count"] == 2

    # S1 常规步骤无 eval_mode
    s1 = payload["results"][0]
    assert "eval_mode" not in s1 or s1.get("eval_mode") != "llm_aggregate"

    # S2 LLM 汇聚步骤有汇聚信息
    s2 = payload["results"][1]
    assert s2.get("eval_mode") == "llm_aggregate"
    assert "0.727" in s2.get("aggregated_answer_preview", "")
