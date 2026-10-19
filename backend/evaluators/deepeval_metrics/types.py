# -*- coding: utf-8 -*-
"""DeepEval Metrics 子包的领域类型——从 agent_eval.domain 摘出的纯 dataclass，零框架依赖。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class MetricResult:
    """平台统一的指标结果容器。"""
    name: str
    value: float
    score: float | None = None  # DeepEval 0-1 评分；None=跳过/失败
    details: str = ""
    gate_threshold: float | None = None
    gate_op: str = "gte"  # gte(≥) | lte(≤)
    gate_passed: bool | None = None  # None=不门控;True=达标;False=拖垮用例


@dataclass
class ToolCallRecord:
    """一次工具调用的领域记录。"""
    name: str
    arguments: dict[str, Any]
    output: Any = None
    error: str | None = None
    latency_ms: float = 0.0
