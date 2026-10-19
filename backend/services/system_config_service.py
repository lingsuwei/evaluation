"""系统配置读写服务（运行时指标全局阈值等）。

优先从 system_configs 表读取，无值时 fallback 到 config.py 环境变量默认值。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import METRICS_CONFIG
from database import SystemConfig

# 运行时指标 metric_name → system_configs key 映射
_RUNTIME_THRESHOLD_KEYS: Dict[str, str] = {
    "inference_latency": "runtime_threshold.inference_latency_ms",
    "tokens_total": "runtime_threshold.tokens_total",
}


def _config_key_for_metric(metric_name: str) -> str:
    return _RUNTIME_THRESHOLD_KEYS.get(metric_name, f"runtime_threshold.{metric_name}")


def _parse_numeric(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(json.loads(value))
    except (json.JSONDecodeError, ValueError, TypeError):
        try:
            return float(value)
        except (ValueError, TypeError):
            return None


async def get_runtime_threshold(
    session: AsyncSession, metric_name: str
) -> Optional[float]:
    """读取单个运行时指标全局默认阈值。

    先查 system_configs 表，无值时 fallback 到 config.METRICS_CONFIG。
    """
    config_key = _config_key_for_metric(metric_name)
    row = await session.get(SystemConfig, config_key)
    if row and row.value is not None:
        parsed = _parse_numeric(row.value)
        if parsed is not None:
            return parsed
    # fallback 到环境变量默认值
    return METRICS_CONFIG.get(metric_name)


async def set_runtime_threshold(
    session: AsyncSession, metric_name: str, value: float
) -> None:
    """写入/更新单个运行时指标全局默认阈值。"""
    config_key = _config_key_for_metric(metric_name)
    row = await session.get(SystemConfig, config_key)
    serialized = json.dumps(value)
    if row:
        row.value = serialized
    else:
        row = SystemConfig(key=config_key, value=serialized)
        session.add(row)


async def get_all_runtime_thresholds(session: AsyncSession) -> Dict[str, float]:
    """读取所有运行时指标全局默认阈值，返回 {metric_name: threshold}。"""
    result: Dict[str, float] = {}
    for metric_name in _RUNTIME_THRESHOLD_KEYS:
        threshold = await get_runtime_threshold(session, metric_name)
        if threshold is not None:
            result[metric_name] = threshold
    return result


async def set_runtime_thresholds(
    session: AsyncSession, updates: Dict[str, float]
) -> None:
    """批量写入运行时指标全局默认阈值。"""
    for metric_name, value in updates.items():
        if metric_name not in _RUNTIME_THRESHOLD_KEYS:
            continue
        await set_runtime_threshold(session, metric_name, value)


# ==================== 指标参数（metric_params）====================
# 存储 key 前缀：metric_params.{metric_key}，value 为 JSON 字符串
# 供 DeepEval 声明式指标（如 json_correctness / prompt_alignment）的用户配置参数

_METRIC_PARAMS_PREFIX = "metric_params."


def _metric_params_key(metric_key: str) -> str:
    return f"{_METRIC_PARAMS_PREFIX}{metric_key}"


async def get_metric_params(session: AsyncSession, metric_key: str) -> Dict[str, Any]:
    """读取单个指标的用户配置参数。无配置返回空字典。"""
    row = await session.get(SystemConfig, _metric_params_key(metric_key))
    if row and row.value:
        try:
            parsed = json.loads(row.value)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


async def set_metric_params(
    session: AsyncSession, metric_key: str, params: Dict[str, Any]
) -> None:
    """写入/更新单个指标的用户配置参数（整体覆盖）。"""
    key = _metric_params_key(metric_key)
    serialized = json.dumps(params, ensure_ascii=False)
    row = await session.get(SystemConfig, key)
    if row:
        row.value = serialized
    else:
        row = SystemConfig(key=key, value=serialized)
        session.add(row)


async def get_all_metric_params(session: AsyncSession) -> Dict[str, Dict[str, Any]]:
    """读取所有已配置的指标参数，返回 {metric_key: {param_name: value}}。"""
    result: Dict[str, Dict[str, Any]] = {}
    stmt = select(SystemConfig).where(SystemConfig.key.like(f"{_METRIC_PARAMS_PREFIX}%"))
    rows = await session.execute(stmt)
    for row in rows.scalars():
        metric_key = row.key[len(_METRIC_PARAMS_PREFIX):]
        try:
            parsed = json.loads(row.value) if row.value else {}
            if isinstance(parsed, dict):
                result[metric_key] = parsed
            else:
                result[metric_key] = {}
        except (json.JSONDecodeError, TypeError):
            result[metric_key] = {}
    return result
