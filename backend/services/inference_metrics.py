"""从 onto-agent HTTP 响应解析推理时延与 Token 用量。"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Tuple


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_reported_submetric(value: Any) -> Optional[int]:
    """
    Token 分项契约（G-04）：未采集或占位 0 视为未上报，返回 None。

    onto-agent 在未实现工具 LLM 统计时常填 tokens_tool=0；不得当作实测值展示或落库。
    """
    parsed = _as_int(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _first_int(mapping: Dict[str, Any], keys: Iterable[str]) -> Optional[int]:
    for key in keys:
        parsed = _as_int(mapping.get(key))
        if parsed is not None:
            return parsed
    return None


def _extract_tokens_from_usage(usage: Any) -> Optional[int]:
    if not isinstance(usage, dict):
        return None

    total = _first_int(
        usage,
        (
            "total",
            "total_tokens",
            "totalTokens",
            "total_token_count",
            "totalTokenCount",
        ),
    )
    if total is not None:
        return total

    prompt = _first_int(
        usage,
        (
            "prompt_tokens",
            "promptTokens",
            "input_tokens",
            "inputTokens",
            "input",
            "prompt",
        ),
    )
    completion = _first_int(
        usage,
        (
            "completion_tokens",
            "completionTokens",
            "output_tokens",
            "outputTokens",
            "output",
            "completion",
        ),
    )
    if prompt is not None or completion is not None:
        return (prompt or 0) + (completion or 0)
    return None


def _iter_candidate_dicts(result: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """遍历常见响应信封，兼容 onto-agent/Langfuse 风格的嵌套返回。"""
    yield result
    for key in ("data", "result", "output", "response", "metrics", "metadata"):
        nested = result.get(key)
        if isinstance(nested, dict):
            yield nested
            for inner_key in ("data", "result", "output", "metrics", "usage"):
                inner = nested.get(inner_key)
                if isinstance(inner, dict):
                    yield inner


def extract_inference_runtime_fields(result: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """
    解析 agentic/runs/sync（及兼容字段）返回的时延与 Token。

    优先级：
    - duration_ms / elapsed_ms / latency_ms / metrics.duration_ms
    - usage.total_tokens / totalTokens；否则 prompt+completion
    - tokens_total / total_tokens / totalTokens 等顶层或常见嵌套字段
    """
    if not isinstance(result, dict):
        return None, None

    duration_ms: Optional[int] = None
    tokens: Optional[int] = None

    for candidate in _iter_candidate_dicts(result):
        if duration_ms is None:
            duration_ms = _first_int(
                candidate,
                (
                    "duration_ms",
                    "durationMs",
                    "elapsed_ms",
                    "elapsedMs",
                    "latency_ms",
                    "latencyMs",
                ),
            )
        if tokens is None:
            tokens = _first_int(
                candidate,
                (
                    "tokens_total",
                    "total_tokens",
                    "totalTokens",
                    "token_total",
                    "tokenTotal",
                ),
            )
        if tokens is None:
            tokens = _extract_tokens_from_usage(candidate.get("usage"))
        if duration_ms is not None and tokens is not None:
            break

    return duration_ms, tokens


def _find_usage_dict(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for candidate in _iter_candidate_dicts(result):
        usage = candidate.get("usage")
        if isinstance(usage, dict):
            return usage
    return None


def extract_inference_token_breakdown(
    result: Dict[str, Any],
) -> Tuple[Optional[int], Optional[int], Optional[Dict[str, Any]]]:
    """
    解析 sync 响应中的 LLM / 工具 Token 细分。

    - tokens_llm：显式字段，或由 prompt/completion 推导
    - tokens_tool：仅在上报正值时返回；占位 0 视为未上报（None）
    """
    usage = _find_usage_dict(result)
    if not usage:
        return None, None, None

    tokens_llm = _first_int(
        usage,
        (
            "tokens_llm",
            "llm_tokens",
            "llmTokens",
            "model_tokens",
            "modelTokens",
        ),
    )
    tokens_tool = normalize_reported_submetric(
        _first_int(
            usage,
            (
                "tokens_tool",
                "tool_tokens",
                "toolTokens",
                "tools_tokens",
                "toolsTokens",
            ),
        )
    )
    if tokens_llm is None:
        tokens_llm = _extract_tokens_from_usage(usage)
    return tokens_llm, tokens_tool, dict(usage)
