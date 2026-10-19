"""workflow_lift_diff：工作流 vs 单 Agent 提升量（M-W3-3）。

参照 BenchAgent 范式：对比工作流 Agent（left）相对于单 Agent 基线（right）的提升量。
lift = (left - right) / right * 100%，正值表示优于基线。

维度：
- outcome_lift：评测分数提升（higher is better）
- cost_lift：Token 成本节省率（lower is better → 取反使正值=节省）
- latency_lift：延迟节省率（lower is better → 取反使正值=加速）
- member_lift：成员规模差（workflow 成员数 - baseline 成员数）
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, EvaluationResult, RunSpanNode
from services.diff.base import DiffCalculator
from services.diff.diff_types import SCHEMA_VERSION, WORKFLOW_LIFT_DIFF
from services.diff.span_aggregate import run_latency_ms
from services.member_trace_slicer import slice_by_member


def _pct(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    """安全计算百分比提升，除零返回 None。"""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return round(numerator / denominator * 100, 2)


def _compute_outcome_lift(
    left_scores: Dict[str, Optional[float]],
    right_scores: Dict[str, Optional[float]],
) -> Dict[str, Any]:
    """逐指标计算 outcome lift。约定 higher-is-better。"""
    metrics: Dict[str, Any] = {}
    keys = sorted(set(left_scores) | set(right_scores))
    lifts: List[float] = []
    for key in keys:
        l = left_scores.get(key)
        r = right_scores.get(key)
        delta = (l - r) if l is not None and r is not None else None
        lift_pct = _pct(delta, r) if delta is not None else None
        if lift_pct is not None:
            lifts.append(lift_pct)
        metrics[key] = {
            "left": l,
            "right": r,
            "delta": round(delta, 6) if delta is not None else None,
            "lift_pct": lift_pct,
        }
    avg_lift = round(sum(lifts) / len(lifts), 2) if lifts else None
    return {
        "by_metric": metrics,
        "avg_lift_pct": avg_lift,
    }


def _compute_cost_lift(
    left_tokens: Optional[int], right_tokens: Optional[int]
) -> Dict[str, Any]:
    """Token 成本 lift：正值 = 节省（left < right）。"""
    delta = (right_tokens - left_tokens) if left_tokens is not None and right_tokens is not None else None
    return {
        "left_tokens": left_tokens,
        "right_tokens": right_tokens,
        "token_delta": delta,
        "cost_lift_pct": _pct(delta, right_tokens) if delta is not None else None,
    }


def _compute_latency_lift(
    left_latency: Optional[int], right_latency: Optional[int]
) -> Dict[str, Any]:
    """延迟 lift：正值 = 加速（left < right）。"""
    delta = (right_latency - left_latency) if left_latency is not None and right_latency is not None else None
    return {
        "left_latency_ms": left_latency,
        "right_latency_ms": right_latency,
        "latency_delta_ms": delta,
        "latency_lift_pct": _pct(delta, right_latency) if delta is not None else None,
    }


def _get_member_count(spans: List[RunSpanNode]) -> int:
    """从 span 树推导 member 数量。"""
    return len(slice_by_member(spans))


class WorkflowLiftDiffCalculator(DiffCalculator):
    diff_type = WORKFLOW_LIFT_DIFF
    schema_version = SCHEMA_VERSION

    async def compute(
        self,
        session: AsyncSession,
        *,
        left_run_id: str,
        right_run_id: str,
    ) -> Dict[str, Any]:
        left_run = await session.get(AgentRun, left_run_id)
        right_run = await session.get(AgentRun, right_run_id)
        if not left_run or not right_run:
            raise ValueError("left_run_id or right_run_id not found")

        # outcome
        left_scores = await self._load_scores(session, left_run_id)
        right_scores = await self._load_scores(session, right_run_id)
        outcome = _compute_outcome_lift(left_scores, right_scores)

        # cost
        cost = _compute_cost_lift(left_run.tokens_total, right_run.tokens_total)

        # latency
        left_spans = await self._load_spans(session, left_run_id)
        right_spans = await self._load_spans(session, right_run_id)
        latency = _compute_latency_lift(
            run_latency_ms(left_run),
            run_latency_ms(right_run),
        )

        # member count
        left_member_count = _get_member_count(list(left_spans))
        right_member_count = _get_member_count(list(right_spans))

        # overall improvement_percentage
        lift_components: List[float] = []
        if outcome["avg_lift_pct"] is not None:
            lift_components.append(outcome["avg_lift_pct"])
        if cost["cost_lift_pct"] is not None:
            lift_components.append(cost["cost_lift_pct"])
        if latency["latency_lift_pct"] is not None:
            lift_components.append(latency["latency_lift_pct"])
        improvement_percentage = (
            round(sum(lift_components) / len(lift_components), 2)
            if lift_components
            else None
        )

        return {
            "outcome_lift": outcome,
            "cost_lift": cost,
            "latency_lift": latency,
            "member_count": {
                "left": left_member_count,
                "right": right_member_count,
                "delta": left_member_count - right_member_count,
            },
            "improvement_percentage": improvement_percentage,
            "conclusion": self._conclusion(improvement_percentage),
        }

    async def _load_scores(
        self, session: AsyncSession, agent_run_id: str
    ) -> Dict[str, Optional[float]]:
        rows = (
            await session.execute(
                select(EvaluationResult).where(
                    EvaluationResult.agent_run_id == agent_run_id
                )
            )
        ).scalars().all()
        out: Dict[str, Optional[float]] = {}
        for row in rows:
            score = row.avg_score if row.avg_score is not None else row.score
            out[row.metric_name] = float(score) if score is not None else None
        return out

    async def _load_spans(self, session: AsyncSession, run_id: str):
        return (
            await session.execute(
                select(RunSpanNode).where(RunSpanNode.agent_run_id == run_id)
            )
        ).scalars().all()

    @staticmethod
    def _conclusion(pct: Optional[float]) -> str:
        if pct is None:
            return "insufficient_data"
        if pct > 5:
            return "workflow_lift"
        if pct < -5:
            return "workflow_regression"
        return "neutral"
