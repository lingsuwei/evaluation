"""被测推理模型解析与展示。"""
from __future__ import annotations

from services.inference_model_display import (
    apply_inference_model_to_run,
    execution_mode_label,
    is_llm_observation,
    merge_execution_mode_info,
    merge_inference_model_info,
    parse_execution_mode_from_agent_response,
    parse_execution_mode_from_config,
    parse_inference_model_from_agent_response,
    parse_inference_model_from_config,
    parse_inference_model_from_observation,
    pick_primary_inference_model_from_trace,
    summarize_inference_models_from_trace,
)


class _Run:
    inference_model_provider = None
    inference_model_name = None
    inference_model_version = None
    inference_model_id = None


def test_parse_inference_model_from_config_payload_context():
    cfg = {
        "url": "http://localhost:5055",
        "payload_template": {
            "request": "${question}",
            "context": {
                "agent_mode": "thinking",
                "model": "deepseek-chat",
                "model_provider": "DeepSeek",
            },
        },
    }
    info = parse_inference_model_from_config(cfg)
    assert info["name"] == "deepseek-chat"
    assert info["provider"] == "DeepSeek"


def test_parse_inference_model_from_agent_response_top_level():
    result = {
        "status": "completed",
        "run_id": "run-1",
        "final_answer": "ok",
        "model": "deepseek-chat",
        "model_provider": "DeepSeek",
        "model_version": "v3",
    }
    info = parse_inference_model_from_agent_response(result)
    assert info["name"] == "deepseek-chat"
    assert info["provider"] == "DeepSeek"
    assert info["version"] == "v3"


def test_parse_inference_model_from_agent_response_metadata():
    result = {
        "final_answer": "ok",
        "metadata": {"model": "glm-4.7", "model_provider": "智谱"},
    }
    info = parse_inference_model_from_agent_response(result)
    assert info["name"] == "glm-4.7"
    assert info["provider"] == "智谱"


def test_merge_response_primary_over_config():
    config = {"provider": "ConfigVendor", "name": "config-model", "version": None, "model_id": None}
    response = {"provider": "DeepSeek", "name": "deepseek-chat", "version": "v3", "model_id": "accurate"}
    merged = merge_inference_model_info(response, config)
    assert merged["name"] == "deepseek-chat"
    assert merged["provider"] == "DeepSeek"
    assert merged["version"] == "v3"


def test_merge_and_apply_only_fills_empty():
    run = _Run()
    merged = merge_inference_model_info(
        {"provider": None, "name": "from-config", "version": None, "model_id": None},
        {"provider": "DeepSeek", "name": "deepseek-chat", "version": "v3", "model_id": None},
    )
    assert merged["name"] == "from-config"
    assert merged["provider"] == "DeepSeek"

    apply_inference_model_to_run(run, merged)
    assert run.inference_model_name == "from-config"
    assert run.inference_model_provider == "DeepSeek"
    assert run.inference_model_version == "v3"


def test_parse_execution_mode_from_agent_response():
    result = {
        "final_answer": "ok",
        "agent_mode": "thinking",
        "deep_thinking_enabled": True,
        "model": "deepseek-chat",
    }
    info = parse_execution_mode_from_agent_response(result)
    assert info["agent_mode"] == "thinking"
    assert info["deep_thinking_enabled"] is True
    assert execution_mode_label(info) == "深度思考"


def test_parse_execution_mode_from_config_payload_context():
    cfg = {
        "payload_template": {
            "context": {"agent_mode": "thinking"},
        },
    }
    info = parse_execution_mode_from_config(cfg)
    assert info["agent_mode"] == "thinking"
    assert info["deep_thinking_enabled"] is True


def test_merge_execution_mode_response_primary():
    response = {"agent_mode": "multi_agent", "deep_thinking_enabled": True}
    config = {"agent_mode": "thinking", "deep_thinking_enabled": True}
    merged = merge_execution_mode_info(response, config)
    assert merged["agent_mode"] == "multi_agent"


def test_parse_inference_model_from_observation_top_level_model():
    obs = {
        "id": "o1",
        "type": "GENERATION",
        "name": "llm:main_iteration",
        "model": "deepseek-chat",
        "metadata": {"model_provider": "DeepSeek", "model_version": "v3"},
    }
    info = parse_inference_model_from_observation(obs)
    assert info["name"] == "deepseek-chat"
    assert info["provider"] == "DeepSeek"
    assert info["version"] == "v3"


def test_parse_inference_model_from_observation_metadata_only():
    obs = {
        "id": "o2",
        "type": "SPAN",
        "name": "llm:subagent_iteration",
        "metadata": {"model": "glm-4.7", "model_provider": "智谱"},
    }
    assert is_llm_observation(obs)
    info = parse_inference_model_from_observation(obs)
    assert info["name"] == "glm-4.7"
    assert info["provider"] == "智谱"


def test_summarize_inference_models_from_trace_deduplicates():
    trace = {
        "observations": [
            {
                "id": "o1",
                "type": "GENERATION",
                "name": "llm:main_iteration",
                "model": "deepseek-chat",
                "metadata": {"model_provider": "DeepSeek"},
            },
            {
                "id": "o2",
                "type": "GENERATION",
                "name": "llm:main_iteration",
                "model": "deepseek-chat",
                "metadata": {"model_provider": "DeepSeek"},
            },
            {
                "id": "o3",
                "type": "GENERATION",
                "name": "llm:subagent_iteration",
                "model": "glm-4.7",
                "metadata": {"model_provider": "智谱"},
            },
        ]
    }
    models = summarize_inference_models_from_trace(trace)
    assert len(models) == 2
    names = {m["name"] for m in models}
    assert names == {"deepseek-chat", "glm-4.7"}


def test_pick_primary_prefers_main_iteration_over_subagent():
    trace = {
        "observations": [
            {
                "id": "o1",
                "type": "GENERATION",
                "name": "llm:subagent_iteration",
                "model": "glm-4.7",
                "usage": {"total": 500},
            },
            {
                "id": "o2",
                "type": "GENERATION",
                "name": "llm:main_iteration",
                "model": "deepseek-chat",
                "usage": {"total": 50},
            },
        ]
    }
    primary = pick_primary_inference_model_from_trace(trace)
    assert primary["name"] == "deepseek-chat"
