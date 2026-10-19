"""被测 Agent 推理模型快照与展示工具。"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional


def _first_text(data: Dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    for key in keys:
        val = data.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return None


def _nested_dict(data: Dict[str, Any], keys: Iterable[str]) -> Dict[str, Any]:
    for key in keys:
        val = data.get(key)
        if isinstance(val, dict):
            return val
    return {}


def _first_text_from_candidates(
    candidates: Iterable[Optional[Dict[str, Any]]],
    keys: Iterable[str],
) -> Optional[str]:
    for item in candidates:
        if not item:
            continue
        val = _first_text(item, keys)
        if val:
            return val
    return None


def parse_inference_model_from_config(config: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """从 Agent inference_config 中提取被测推理模型信息。"""
    cfg = config or {}
    payload = _nested_dict(cfg, ("payload_template", "payload", "body", "request_body"))
    model_cfg = _nested_dict(cfg, ("model_config", "llm", "llm_config", "model"))
    context_cfg = _nested_dict(payload, ("context",))
    candidates = _flatten_candidates([cfg, model_cfg, payload, context_cfg])
    return _extract_model_from_candidates(candidates)


def parse_inference_model_from_agent_response(
    result: Optional[Dict[str, Any]],
) -> Dict[str, Optional[str]]:
    """从 onto-agent /agentic/runs/sync 等响应中提取被测推理模型。"""
    if not isinstance(result, dict):
        return {}
    candidates: list[Dict[str, Any]] = [result]
    # 常见嵌套层级
    for key in (
        "metadata", "stats", "run_stats", "trace", "usage",
        "inference_model", "model_config", "llm", "llm_config",
        "runtime_info", "agent_config",
    ):
        nested = result.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    trace = result.get("trace")
    if isinstance(trace, dict):
        candidates.append(trace)
        meta = trace.get("metadata")
        if isinstance(meta, dict):
            candidates.append(meta)

    provider = _first_text_from_candidates(
        candidates,
        (
            "model_provider",
            "provider",
            "llm_provider",
            "vendor",
            "model_vendor",
        ),
    )
    name = _first_text_from_candidates(
        candidates,
        (
            "model",
            "model_name",
            "modelName",
            "llm_model",
            "llm_model_name",
            "deployment",
            "deployment_name",
        ),
    )
    version = _first_text_from_candidates(
        candidates,
        (
            "model_version",
            "modelVersion",
            "version",
            "model_revision",
            "revision",
        ),
    )
    model_id = _first_text_from_candidates(
        candidates,
        ("model_id", "modelId", "deployment_id", "deploymentId", "model_profile_id"),
    )
    return {
        "provider": provider,
        "name": name,
        "version": version,
        "model_id": model_id,
    }


def _flatten_candidates(candidates: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    """展平候选列表：包含每个 dict 及其一级子 dict（处理嵌套模型配置）。"""
    result: list[Dict[str, Any]] = []
    seen: set[int] = set()
    for d in candidates:
        if not d or id(d) in seen:
            continue
        seen.add(id(d))
        result.append(d)
        for v in d.values():
            if isinstance(v, dict) and id(v) not in seen:
                seen.add(id(v))
                result.append(v)
    return result


def _extract_model_from_candidates(
    candidates: list[Dict[str, Any]],
) -> Dict[str, Optional[str]]:
    """从一组候选 dict 中提取模型信息（供 trace 级 metadata 等场景复用）。"""
    provider = _first_text_from_candidates(
        candidates,
        ("model_provider", "provider", "llm_provider", "vendor", "model_vendor"),
    )
    name = _first_text_from_candidates(
        candidates,
        ("model", "model_name", "modelName", "llm_model", "llm_model_name",
         "deployment", "deployment_name"),
    )
    version = _first_text_from_candidates(
        candidates,
        ("model_version", "modelVersion", "version", "model_revision", "revision"),
    )
    model_id = _first_text_from_candidates(
        candidates,
        ("model_id", "modelId", "deployment_id", "deploymentId", "model_profile_id"),
    )
    return {
        "provider": provider,
        "name": name,
        "version": version,
        "model_id": model_id,
    }


def merge_inference_model_info(
    primary: Dict[str, Optional[str]],
    secondary: Dict[str, Optional[str]],
) -> Dict[str, Optional[str]]:
    """合并模型信息：primary 优先，secondary 补空。"""
    merged = dict(primary or {})
    for key in ("provider", "name", "version", "model_id"):
        if not (merged.get(key) or "").strip():
            val = (secondary or {}).get(key)
            if val and str(val).strip():
                merged[key] = str(val).strip()
    return merged


def inference_model_label(info: Dict[str, Optional[str]]) -> Optional[str]:
    name = (info.get("name") or info.get("model_id") or "").strip()
    # 过滤掉无意义的占位值
    if not name or name.lower() in ("unknown", "none", "null", "undefined", ""):
        return None
    provider = (info.get("provider") or "").strip()
    if provider.lower() in ("unknown", "none", "null", "undefined", ""):
        provider = ""
    version = (info.get("version") or "").strip()
    if version.lower() in ("unknown", "none", "null", "undefined", ""):
        version = ""
    label = f"{provider} · {name}" if provider else name
    return f"{label} · {version}" if version and version not in name else label


def apply_inference_model_to_run(
    run: Any,
    info: Dict[str, Optional[str]],
    *,
    overwrite: bool = False,
) -> None:
    """将推理模型快照写入 AgentRun 列；overwrite=True 时以运行时响应为准覆盖。"""
    if not inference_model_label(info):
        return
    mapping = {
        "provider": "inference_model_provider",
        "name": "inference_model_name",
        "version": "inference_model_version",
        "model_id": "inference_model_id",
    }
    for src, col in mapping.items():
        val = (info.get(src) or "").strip()
        if not val:
            continue
        current = getattr(run, col, None)
        if not overwrite and current and str(current).strip():
            continue
        setattr(run, col, val)


_VALID_AGENT_MODES = frozenset({"quick", "thinking", "multi_agent"})
_AGENT_MODE_LABELS = {
    "quick": "快速",
    "thinking": "深度思考",
    "multi_agent": "多 Agent",
}


def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "on", "yes"}:
            return True
        if text in {"0", "false", "off", "no"}:
            return False
    return bool(value)


def parse_execution_mode_from_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从 Agent inference_config / payload context 提取 agent_mode。"""
    cfg = config or {}
    payload = _nested_dict(cfg, ("payload_template", "payload", "body", "request_body"))
    context_cfg = _nested_dict(payload, ("context",))
    candidates = [context_cfg, payload, cfg]

    agent_mode = _first_text_from_candidates(
        candidates,
        ("agent_mode", "execution_mode"),
    )
    if agent_mode:
        agent_mode = agent_mode.strip().lower()
        if agent_mode not in _VALID_AGENT_MODES:
            agent_mode = None

    deep_thinking_raw = None
    for item in candidates:
        if not item:
            continue
        if "deep_thinking_enabled" in item:
            deep_thinking_raw = item.get("deep_thinking_enabled")
            break

    deep_thinking = _coerce_bool(deep_thinking_raw)
    if agent_mode is None and deep_thinking is not None:
        agent_mode = "multi_agent" if deep_thinking else "quick"

    result: Dict[str, Any] = {}
    if agent_mode:
        result["agent_mode"] = agent_mode
    if deep_thinking is not None:
        result["deep_thinking_enabled"] = deep_thinking
    elif agent_mode in ("thinking", "multi_agent"):
        result["deep_thinking_enabled"] = True
    elif agent_mode == "quick":
        result["deep_thinking_enabled"] = False
    return result


def parse_execution_mode_from_agent_response(
    result: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """从 onto-agent /agentic/runs/sync 响应中提取 agent_mode / deep_thinking。"""
    if not isinstance(result, dict):
        return {}

    candidates: list[Dict[str, Any]] = [result]
    for key in ("metadata", "context", "stats", "run_stats", "trace"):
        nested = result.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    trace = result.get("trace")
    if isinstance(trace, dict):
        meta = trace.get("metadata")
        if isinstance(meta, dict):
            candidates.append(meta)

    agent_mode = _first_text_from_candidates(
        candidates,
        ("agent_mode", "execution_mode"),
    )
    if agent_mode:
        agent_mode = agent_mode.strip().lower()
        if agent_mode not in _VALID_AGENT_MODES:
            agent_mode = None

    deep_thinking = None
    for item in candidates:
        if not item or "deep_thinking_enabled" not in item:
            continue
        deep_thinking = _coerce_bool(item.get("deep_thinking_enabled"))
        break

    if agent_mode is None and deep_thinking is not None:
        agent_mode = "multi_agent" if deep_thinking else "quick"
    if deep_thinking is None and agent_mode:
        deep_thinking = agent_mode in ("thinking", "multi_agent")

    parsed: Dict[str, Any] = {}
    if agent_mode:
        parsed["agent_mode"] = agent_mode
    if deep_thinking is not None:
        parsed["deep_thinking_enabled"] = deep_thinking
    return parsed


def merge_execution_mode_info(
    primary: Dict[str, Any],
    secondary: Dict[str, Any],
) -> Dict[str, Any]:
    merged = dict(secondary or {})
    merged.update({k: v for k, v in (primary or {}).items() if v is not None})
    return merged


def execution_mode_label(info: Dict[str, Any]) -> Optional[str]:
    agent_mode = str(info.get("agent_mode") or "").strip().lower()
    if agent_mode in _AGENT_MODE_LABELS:
        return _AGENT_MODE_LABELS[agent_mode]
    if info.get("deep_thinking_enabled") is True:
        return _AGENT_MODE_LABELS["thinking"]
    if info.get("deep_thinking_enabled") is False:
        return _AGENT_MODE_LABELS["quick"]
    return None


def execution_mode_info_from_run(run: Any) -> Dict[str, Any]:
    snapshot = getattr(run, "output_snapshot", None)
    if isinstance(snapshot, dict):
        mode = snapshot.get("execution_mode")
        if isinstance(mode, dict) and execution_mode_label(mode):
            return mode

    snapshot = getattr(run, "input_snapshot", None)
    if isinstance(snapshot, dict):
        request_ctx = snapshot.get("request_context")
        if isinstance(request_ctx, dict):
            fallback = parse_execution_mode_from_config({"payload_template": {"context": request_ctx}})
            if execution_mode_label(fallback):
                return fallback
    return {}


def summarize_execution_modes_from_runs(runs: Iterable[Any]) -> Dict[str, Any]:
    seen: Dict[str, Dict[str, Any]] = {}
    for run in runs:
        info = execution_mode_info_from_run(run)
        label = execution_mode_label(info)
        if label:
            seen[label] = info
    labels = sorted(seen.keys())
    if not labels:
        display = "未记录"
    elif len(labels) == 1:
        display = labels[0]
    else:
        display = f"{len(labels)} 种模式"
    modes = [seen[label] for label in labels]
    deep_thinking = None
    if len(modes) == 1:
        deep_thinking = modes[0].get("deep_thinking_enabled")
    return {
        "display": display,
        "count": len(labels),
        "labels": labels,
        "modes": modes,
        "deep_thinking_enabled": deep_thinking,
    }


def _observation_usage_tokens(obs: Dict[str, Any]) -> int:
    usage = obs.get("usage")
    if not isinstance(usage, dict):
        return 0
    for key in (
        "total",
        "totalTokens",
        "total_tokens",
        "totalTokenCount",
        "total_token_count",
    ):
        try:
            if usage.get(key) is not None:
                return int(usage[key])
        except (TypeError, ValueError):
            continue
    try:
        prompt = (
            usage.get("promptTokens")
            or usage.get("prompt_tokens")
            or usage.get("inputTokens")
            or usage.get("input_tokens")
            or usage.get("input")
            or 0
        )
        completion = (
            usage.get("completionTokens")
            or usage.get("completion_tokens")
            or usage.get("outputTokens")
            or usage.get("output_tokens")
            or usage.get("output")
            or 0
        )
        return int(prompt or 0) + int(completion or 0)
    except (TypeError, ValueError):
        return 0


def is_llm_observation(obs: Dict[str, Any]) -> bool:
    """判断 observation 是否代表 LLM 推理（用于从 trace 提取被测模型）。"""
    obs_type = (obs.get("type") or "").lower()
    if obs_type in ("generation", "llm"):
        return True
    name = (obs.get("name") or "").lower()
    return name.startswith("llm:") or name in ("agent-inference", "llm:main", "llm:reasoning")


def parse_inference_model_from_observation(
    obs: Dict[str, Any],
) -> Dict[str, Optional[str]]:
    """从 Langfuse observation 提取被测推理模型（顶层 model + metadata）。"""
    candidates: list[Dict[str, Any]] = [obs]
    meta = obs.get("metadata")
    if isinstance(meta, dict):
        candidates.append(meta)
    # Langfuse GENERATION 可能把 model 参数放在 modelParameters 或 usage 内
    for sub in ("modelParameters", "usage", "input"):
        val = obs.get(sub)
        if isinstance(val, dict):
            candidates.append(val)

    provider = _first_text_from_candidates(
        candidates,
        (
            "model_provider",
            "provider",
            "llm_provider",
            "vendor",
            "model_vendor",
        ),
    )
    name = _first_text_from_candidates(
        candidates,
        (
            "model",
            "model_name",
            "modelName",
            "llm_model",
            "llm_model_name",
            "deployment",
            "deployment_name",
        ),
    )
    version = _first_text_from_candidates(
        candidates,
        (
            "model_version",
            "modelVersion",
            "version",
            "model_revision",
            "revision",
        ),
    )
    model_id = _first_text_from_candidates(
        candidates,
        ("model_id", "modelId", "deployment_id", "deploymentId", "model_profile_id"),
    )
    return {
        "provider": provider,
        "name": name,
        "version": version,
        "model_id": model_id,
    }


def _llm_observation_rank(obs: Dict[str, Any]) -> tuple[int, int]:
    """主模型优先级：主路径 LLM span 优先，同 tier 取 token 用量更大者。"""
    name = (obs.get("name") or "").lower()
    if "llm:main_iteration" in name or name in ("agent-inference", "llm:main"):
        tier = 0
    elif name.startswith("llm:"):
        tier = 1
    elif (obs.get("type") or "").lower() in ("generation", "llm"):
        tier = 2
    else:
        tier = 99
    return tier, -_observation_usage_tokens(obs)


def summarize_inference_models_from_trace(
    trace: Dict[str, Any],
) -> list[Dict[str, Optional[str]]]:
    """从 trace 汇总去重后的推理模型列表。"""
    observations = trace.get("observations") or []
    if not isinstance(observations, list):
        return []

    seen: Dict[str, Dict[str, Optional[str]]] = {}
    for obs in observations:
        if not isinstance(obs, dict) or not is_llm_observation(obs):
            continue
        info = parse_inference_model_from_observation(obs)
        label = inference_model_label(info)
        if label:
            seen[label] = info
    return [seen[label] for label in sorted(seen.keys())]


def pick_primary_inference_model_from_trace(
    trace: Dict[str, Any],
) -> Dict[str, Optional[str]]:
    """从 trace 选取单 run 主推理模型（写入 agent_runs 头表）。

    优先级：LLM observation → 任意 observation → trace 级 metadata。
    """
    observations = trace.get("observations") or []
    if not isinstance(observations, list):
        observations = []

    # 第一优先级：LLM 类 observation（type=GENERATION 或 name 以 llm: 开头）
    ranked: list[tuple[tuple[int, int], Dict[str, Optional[str]]]] = []
    for obs in observations:
        if not isinstance(obs, dict) or not is_llm_observation(obs):
            continue
        info = parse_inference_model_from_observation(obs)
        if inference_model_label(info):
            ranked.append((_llm_observation_rank(obs), info))

    if ranked:
        ranked.sort(key=lambda item: item[0])
        return ranked[0][1]

    # 第二优先级：非 LLM observation 也可能携带模型信息（tool span 的 metadata）
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        info = parse_inference_model_from_observation(obs)
        if inference_model_label(info):
            return info

    # 第三优先级：trace 级 metadata / input / output
    trace_candidates: list[Dict[str, Any]] = []
    for key in ("metadata", "input", "output", "tags"):
        val = trace.get(key)
        if isinstance(val, dict):
            trace_candidates.append(val)
    info = _extract_model_from_candidates(trace_candidates)
    if inference_model_label(info):
        return info

    return {}


def inference_model_info_from_run(run: Any) -> Dict[str, Optional[str]]:
    info = {
        "provider": getattr(run, "inference_model_provider", None),
        "name": getattr(run, "inference_model_name", None),
        "version": getattr(run, "inference_model_version", None),
        "model_id": getattr(run, "inference_model_id", None),
    }
    if inference_model_label(info):
        return info

    snapshot = getattr(run, "agent_snapshot", None)
    if snapshot:
        try:
            data = json.loads(snapshot)
            if isinstance(data, dict):
                fallback = {
                    "provider": data.get("inference_model_provider") or data.get("provider"),
                    "name": data.get("inference_model_name") or data.get("model"),
                    "version": data.get("inference_model_version") or data.get("model_version"),
                    "model_id": data.get("inference_model_id") or data.get("model_id"),
                }
                if inference_model_label(fallback):
                    return fallback
        except (json.JSONDecodeError, TypeError):
            pass
    return info


def summarize_inference_models_from_runs(runs: Iterable[Any]) -> Dict[str, Any]:
    seen: Dict[str, Dict[str, Optional[str]]] = {}
    for run in runs:
        info = inference_model_info_from_run(run)
        label = inference_model_label(info)
        if label:
            seen[label] = info
    labels = sorted(seen.keys())
    if not labels:
        display = "未记录"
    elif len(labels) == 1:
        display = labels[0]
    else:
        display = f"{len(labels)} 个推理模型"
    return {
        "display": display,
        "count": len(labels),
        "labels": labels,
        "models": [seen[label] for label in labels],
    }
