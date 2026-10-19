"""场景子步骤 LLM 二次汇聚服务单测。"""
from __future__ import annotations

import pytest

from services.scenario_llm_aggregate_service import (
    collect_llm_aggregate_warnings_for_templates,
    is_llm_configured,
    llm_aggregate_step_output,
    parse_eval_config,
    validate_llm_aggregate_step,
)


# ── parse_eval_config ──────────────────────────────────────────────


def test_parse_eval_config_none():
    assert parse_eval_config(None) == {
        "mode": "keyword",
        "aggregate_prompt": "",
        "llm_model": None,
    }


def test_parse_eval_config_empty_dict():
    assert parse_eval_config({}) == {
        "mode": "keyword",
        "aggregate_prompt": "",
        "llm_model": None,
    }


def test_parse_eval_config_not_dict():
    assert parse_eval_config("invalid") == {
        "mode": "keyword",
        "aggregate_prompt": "",
        "llm_model": None,
    }


def test_parse_eval_config_eval_config_not_dict():
    """match_rules.eval_config 不是 dict 时回退默认值。"""
    result = parse_eval_config({"eval_config": "bad"})
    assert result["mode"] == "keyword"
    assert result["aggregate_prompt"] == ""


def test_parse_eval_config_keyword_mode():
    result = parse_eval_config({"eval_config": {"mode": "keyword"}})
    assert result["mode"] == "keyword"
    assert result["aggregate_prompt"] == ""
    assert result["llm_model"] is None


def test_parse_eval_config_llm_aggregate_mode():
    result = parse_eval_config(
        {
            "eval_config": {
                "mode": "llm_aggregate",
                "aggregate_prompt": "请汇总以下内容",
                "llm_model": "glm-4",
            }
        }
    )
    assert result["mode"] == "llm_aggregate"
    assert result["aggregate_prompt"] == "请汇总以下内容"
    assert result["llm_model"] == "glm-4"


def test_parse_eval_config_partial_fields():
    """缺失字段用默认值填充。"""
    result = parse_eval_config({"eval_config": {"mode": "llm_aggregate"}})
    assert result["mode"] == "llm_aggregate"
    assert result["aggregate_prompt"] == ""
    assert result["llm_model"] is None


def test_parse_eval_config_strips_prompt():
    result = parse_eval_config(
        {"eval_config": {"mode": "llm_aggregate", "aggregate_prompt": "  汇总  "}}
    )
    assert result["aggregate_prompt"] == "汇总"


def test_parse_eval_config_empty_mode_defaults_keyword():
    result = parse_eval_config({"eval_config": {"mode": ""}})
    assert result["mode"] == "keyword"


def test_is_llm_configured_false_when_no_api_key(monkeypatch):
    monkeypatch.setattr(
        "config.get_llm_config",
        lambda _key=None: {"api_key": ""},
    )
    assert is_llm_configured() is False


def test_collect_llm_aggregate_warnings_when_llm_missing(monkeypatch):
    monkeypatch.setattr(
        "config.get_llm_config",
        lambda _key=None: {"api_key": ""},
    )
    tpl = {
        "steps": [
            {
                "step_key": "S2",
                "name": "态势研判",
                "enabled": True,
                "match_rules": {
                    "eval_config": {
                        "mode": "llm_aggregate",
                        "aggregate_prompt": "请汇总",
                    }
                },
            }
        ]
    }
    warnings = collect_llm_aggregate_warnings_for_templates([tpl])
    assert len(warnings) == 1
    assert "基于Prompt评测" in warnings[0]
    assert "态势研判" in warnings[0]


def test_collect_llm_aggregate_warnings_empty_when_keyword_mode(monkeypatch):
    monkeypatch.setattr(
        "config.get_llm_config",
        lambda _key=None: {"api_key": ""},
    )
    tpl = {
        "steps": [
            {
                "step_key": "S1",
                "name": "常规",
                "enabled": True,
                "match_rules": {"eval_config": {"mode": "keyword"}},
            }
        ]
    }
    assert collect_llm_aggregate_warnings_for_templates([tpl]) == []


def test_validate_llm_aggregate_step_requires_prompt_only():
    with pytest.raises(ValueError, match="基于Prompt评测"):
        validate_llm_aggregate_step(
            step_key="S1",
            name="LLM步",
            enabled=True,
            gt_expected={"keywords": ["a"]},
            match_rules={"eval_config": {"mode": "llm_aggregate", "aggregate_prompt": ""}},
        )
    validate_llm_aggregate_step(
        step_key="S1",
        name="LLM步",
        enabled=True,
        gt_expected={"keywords": []},
        match_rules={"eval_config": {"mode": "llm_aggregate", "aggregate_prompt": "汇总"}},
    )
    validate_llm_aggregate_step(
        step_key="S1",
        name="LLM步",
        enabled=True,
        gt_expected=None,
        match_rules={"eval_config": {"mode": "llm_aggregate", "aggregate_prompt": "汇总"}},
    )


def test_collect_llm_aggregate_warnings_no_warning_when_llm_configured(monkeypatch):
    """LLM 已配置 + 步骤配置完整 → 无警告（keywords 是否为空不影响警告判定）。"""
    monkeypatch.setattr(
        "config.get_llm_config",
        lambda _key=None: {"api_key": "sk-test"},
    )
    tpl = {
        "steps": [
            {
                "step_key": "S2",
                "name": "态势研判",
                "enabled": True,
                "gt_expected": {"keywords": []},
                "match_rules": {
                    "eval_config": {
                        "mode": "llm_aggregate",
                        "aggregate_prompt": "请汇总",
                    }
                },
            }
        ]
    }
    warnings = collect_llm_aggregate_warnings_for_templates([tpl])
    assert len(warnings) == 0


# ── llm_aggregate_step_output ───────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_aggregate_skips_when_api_key_missing(monkeypatch):
    monkeypatch.setattr(
        "config.get_llm_config",
        lambda _key=None: {"api_key": ""},
    )
    called = False

    async def _fake_llm(*a, **k):
        nonlocal called
        called = True
        return "不应调用"

    monkeypatch.setattr(
        "evaluators.llm_client.evaluate_with_llm",
        _fake_llm,
    )
    result = await llm_aggregate_step_output("有内容", "请汇总")
    assert result == ""
    assert not called


@pytest.mark.asyncio
async def test_llm_aggregate_empty_input():
    """空 step_output 直接返回空串，不调用 LLM。"""
    assert await llm_aggregate_step_output("", "请汇总") == ""
    assert await llm_aggregate_step_output("   ", "请汇总") == ""


@pytest.mark.asyncio
async def test_llm_aggregate_success(monkeypatch):
    """正常调用 LLM 返回汇聚文本。"""
    monkeypatch.setattr(
        "config.get_llm_config",
        lambda _key=None: {"api_key": "sk-test"},
    )
    async def _fake_llm(prompt, system_prompt=None, model_key=None, **kw):
        return "汇聚结果：海况系数 0.727"

    monkeypatch.setattr(
        "evaluators.llm_client.evaluate_with_llm",
        _fake_llm,
    )
    result = await llm_aggregate_step_output(
        step_output="海况系数 0.727，威胁方向 225°",
        aggregate_prompt="请提取海况关键参数",
    )
    assert "0.727" in result
    assert result == "汇聚结果：海况系数 0.727"


@pytest.mark.asyncio
async def test_llm_aggregate_with_model_key(monkeypatch):
    """传入 llm_model 时正确转发给 evaluate_with_llm。"""
    monkeypatch.setattr(
        "config.get_llm_config",
        lambda _key=None: {"api_key": "sk-test"},
    )
    captured = {}

    async def _fake_llm(prompt, system_prompt=None, model_key=None, **kw):
        captured["model_key"] = model_key
        return "汇总完成"

    monkeypatch.setattr(
        "evaluators.llm_client.evaluate_with_llm",
        _fake_llm,
    )
    await llm_aggregate_step_output(
        step_output="原始输出",
        aggregate_prompt="汇总",
        llm_model="glm-4",
    )
    assert captured["model_key"] == "glm-4"


@pytest.mark.asyncio
async def test_llm_aggregate_llm_returns_empty(monkeypatch):
    """LLM 返回空串时，结果为空串。"""
    async def _fake_llm(prompt, **kw):
        return ""

    monkeypatch.setattr(
        "evaluators.llm_client.evaluate_with_llm",
        _fake_llm,
    )
    result = await llm_aggregate_step_output("有内容", "汇总")
    assert result == ""


@pytest.mark.asyncio
async def test_llm_aggregate_llm_returns_none(monkeypatch):
    """LLM 返回 None 时，结果为空串（or "" 兜底）。"""
    async def _fake_llm(prompt, **kw):
        return None

    monkeypatch.setattr(
        "evaluators.llm_client.evaluate_with_llm",
        _fake_llm,
    )
    result = await llm_aggregate_step_output("有内容", "汇总")
    assert result == ""
