"""Observation / Span 逐步耗时解析与补全（Langfuse 秒/毫秒兼容）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

_LATENCY_MS_KEYS = ("latencyMs", "latency_ms", "durationMs", "duration_ms", "elapsedMs", "elapsed_ms")
_LATENCY_RAW_KEYS = ("latency", "calculatedLatency", "duration", "elapsed")


def parse_observation_datetime(value: Any) -> Optional[datetime]:
    """解析 Langfuse / onto-agent observation 时间字段为 naive UTC datetime。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def latency_ms_from_interval(
    start: Any,
    end: Any,
) -> Optional[int]:
    """由起止时间推算毫秒耗时。"""
    start_dt = parse_observation_datetime(start)
    end_dt = parse_observation_datetime(end)
    if start_dt is None or end_dt is None or end_dt < start_dt:
        return None
    return max(0, int((end_dt - start_dt).total_seconds() * 1000))


def _coerce_non_negative_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        ms = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return ms if ms >= 0 else None


def _latency_from_raw_value(raw: Any) -> Optional[int]:
    """
    解析 observation 上的 latency 类字段。

    - latencyMs / duration_ms 等显式毫秒字段：直接取整
    - latency / duration 等：小数视为秒；>=1000 视为毫秒；否则按秒×1000（Langfuse 默认）
    """
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val < 0:
        return None
    if val == 0:
        return 0
    if abs(val - round(val)) > 1e-9:
        return max(0, int(round(val * 1000)))
    if val >= 1000:
        return int(val)
    return max(0, int(round(val * 1000)))


def resolve_observation_latency_ms(obs: dict) -> Optional[int]:
    """从 Langfuse / Push trace observation 解析逐步耗时（毫秒）。"""
    if not isinstance(obs, dict):
        return None

    start = obs.get("startTime") or obs.get("start_time")
    end = obs.get("endTime") or obs.get("end_time")
    from_interval = latency_ms_from_interval(start, end)
    if from_interval is not None:
        return from_interval

    for key in _LATENCY_MS_KEYS:
        if obs.get(key) is not None:
            ms = _coerce_non_negative_ms(obs.get(key))
            if ms is not None:
                return ms

    for key in _LATENCY_RAW_KEYS:
        if obs.get(key) is not None:
            ms = _latency_from_raw_value(obs.get(key))
            if ms is not None:
                return ms

    return None


def resolve_span_node_latency_ms(
    *,
    latency_ms: Any = None,
    started_at: Any = None,
    ended_at: Any = None,
) -> Optional[int]:
    """读取 Span 节点时补全 latency_ms：优先起止时间，其次已存字段。"""
    from_interval = latency_ms_from_interval(started_at, ended_at)
    if from_interval is not None:
        return from_interval
    stored = _coerce_non_negative_ms(latency_ms)
    if stored is not None and stored > 0:
        return stored
    if stored == 0 and from_interval is None:
        return 0
    return stored
