"""任务级 diff 摘要（P3-B2）。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import RunComparison, RunDiff
from services.diff.diff_types import COST_DIFF, OUTCOME_DIFF, SEMANTIC_DIFF, STRUCTURAL_DIFF
from services.diff.semantic_diff_calculator import compact_semantic_payload
from services.diff.structural_diff_calculator import compact_structural_payload
from services.experiment_service import get_experiment_for_task


def _compact_outcome(payload: Dict[str, Any]) -> Dict[str, Any]:
    metrics = payload.get("metrics") or {}
    primary = metrics.get("correctness") or next(iter(metrics.values()), {})
    return {
        "metric": "correctness" if "correctness" in metrics else (next(iter(metrics), None)),
        "left": primary.get("left") if isinstance(primary, dict) else None,
        "right": primary.get("right") if isinstance(primary, dict) else None,
        "delta": primary.get("delta") if isinstance(primary, dict) else None,
    }


def _compact_cost(payload: Dict[str, Any]) -> Dict[str, Any]:
    run_level = payload.get("run_level") or {}
    tokens = run_level.get("tokens_total") or {}
    latency = run_level.get("latency_ms") or {}
    return {
        "tokens_delta": tokens.get("delta"),
        "latency_delta_ms": latency.get("delta"),
    }


def _compact_semantic(payload: Dict[str, Any]) -> Dict[str, Any]:
    return compact_semantic_payload(payload)


async def get_task_diff_highlights(
    session: AsyncSession,
    task_id: str,
    *,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """最近对比的 outcome/cost 紧凑摘要。"""
    comparisons = (
        await session.execute(
            select(RunComparison)
            .where(RunComparison.task_id == task_id)
            .order_by(RunComparison.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    out: List[Dict[str, Any]] = []
    for cmp in comparisons:
        diffs = (
            await session.execute(
                select(RunDiff).where(RunDiff.comparison_id == cmp.id)
            )
        ).scalars().all()
        item: Dict[str, Any] = {
            "comparison_id": cmp.id,
            "question_id": cmp.question_id,
            "left_run_id": cmp.left_run_id,
            "right_run_id": cmp.right_run_id,
            "comparison_scope": cmp.comparison_scope,
        }
        for d in diffs:
            if d.diff_type == OUTCOME_DIFF:
                item["outcome"] = _compact_outcome(d.payload or {})
            elif d.diff_type == COST_DIFF:
                item["cost"] = _compact_cost(d.payload or {})
            elif d.diff_type == SEMANTIC_DIFF:
                item["semantic"] = _compact_semantic(d.payload or {})
            elif d.diff_type == STRUCTURAL_DIFF:
                item["structural"] = compact_structural_payload(d.payload or {})
        out.append(item)
    return out


async def build_task_experiment_report(
    session: AsyncSession, task_id: str
) -> Dict[str, Any]:
    """任务详情用：实验 + diff 摘要。"""
    experiment = await get_experiment_for_task(session, task_id)
    highlights = await get_task_diff_highlights(session, task_id)
    return {
        "experiment": experiment,
        "diff_highlights": highlights,
        "comparison_count": len(highlights),
    }
