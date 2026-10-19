"""运行时评估因子（推理时延、Tokens 消耗等）。"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# 不走 LLM/脚本 Judge 的因子
RUNTIME_METRICS = frozenset({"inference_latency", "tokens_total"})

METRIC_DISPLAY = {
    "inference_latency": "推理时延",
    "tokens_total": "Tokens总消耗",
}


def is_runtime_metric(metric_name: str) -> bool:
    return (metric_name or "").strip() in RUNTIME_METRICS


def split_metrics(metrics: list[str]) -> Tuple[list[str], list[str]]:
    runtime = [m for m in metrics if is_runtime_metric(m)]
    qualitative = [m for m in metrics if not is_runtime_metric(m)]
    return runtime, qualitative


def build_runtime_metric_values(
    metric_name: str,
    *,
    inference_duration_ms: Optional[int],
    tokens_total: Optional[int],
) -> Tuple[Optional[float], str]:
    """返回 (原始测量值, 说明文案)。"""
    if metric_name == "inference_latency":
        if inference_duration_ms is None:
            return None, "推理时延未知（等待 Langfuse Trace 物化或 Agent 侧计时）"
        ms = max(0, int(inference_duration_ms))
        return (
            float(ms),
            f"推理时延 {ms} ms（Agent 端到端；优先 Langfuse trace 物化）",
        )
    if metric_name == "tokens_total":
        tok = tokens_total
        if tok is None:
            return None, "Tokens 总消耗未知（等待 Langfuse Trace 物化）"
        val = max(0, int(tok))
        return float(val), f"Tokens 总消耗 {val}（来自 Langfuse trace 物化汇总）"
    return None, "未知运行时因子"


def score_runtime_metric(
    metric_name: str,
    raw_value: Optional[float],
    threshold: float,
) -> Tuple[float, bool, str]:
    """
    运行时因子评分：阈值表示「目标上限」（越低越好）。
    - actual <= target：效率分 1.0，视为达标
    - target < actual < limit：按区间线性递减
    - actual >= limit：效率分 0

    注意：该分数只表达运行效率，不应混入质量总分。
    """
    if raw_value is None:
        return 0.0, False, "无有效测量值"

    target = float(threshold) if threshold else 1.0
    if target <= 0:
        target = 1.0
    limit = target * 3

    if metric_name == "inference_latency":
        passed = raw_value <= target
        normalized = _score_lower_is_better(raw_value, target, limit)
        return round(normalized, 4), passed, METRIC_DISPLAY["inference_latency"]

    if metric_name == "tokens_total":
        passed = raw_value <= target
        normalized = _score_lower_is_better(raw_value, target, limit)
        return round(normalized, 4), passed, METRIC_DISPLAY["tokens_total"]

    return 0.0, False, metric_name


def _score_lower_is_better(value: float, target: float, limit: float) -> float:
    if value <= target:
        return 1.0
    if value >= limit:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (value - target) / (limit - target)))
