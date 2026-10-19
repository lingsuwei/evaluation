"""跨任务 Run 对比 Diff（S+2：基线任务 Run vs 对比任务 Run）。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun
from services.diff.diff_types import SEMANTIC_DIFF, STRUCTURAL_DIFF
from services.diff.semantic_diff_calculator import compact_semantic_payload
from services.diff.structural_diff_calculator import compact_structural_payload
from services.run_comparison_service import RunComparisonError, compare_runs


def _summaries_from_comparison(comparison: Dict[str, Any]) -> Dict[str, Any]:
    structural_summary = None
    semantic_summary = None
    for d in comparison.get("diffs") or []:
        dtype = d.get("diff_type")
        payload = d.get("payload") or {}
        if dtype == STRUCTURAL_DIFF:
            structural_summary = compact_structural_payload(payload).get("summary")
        elif dtype == SEMANTIC_DIFF:
            semantic_summary = compact_semantic_payload(payload).get("judge_summary")
    return {
        "structural_summary": structural_summary,
        "semantic_summary": semantic_summary,
    }


async def run_cross_task_pairwise_diff(
    session: AsyncSession,
    *,
    left_run: AgentRun,
    right_run: AgentRun,
    baseline_task_id: str,
    right_task_id: str,
    question_key: str,
) -> Dict[str, Any]:
    """对比两任务的 AgentRun，返回 comparison 序列化 + 摘要。"""
    if not left_run or not right_run:
        raise RunComparisonError("缺少可对比的 AgentRun")
    comparison = await compare_runs(
        session,
        left_run_id=left_run.id,
        right_run_id=right_run.id,
        comparison_scope="cross_task",
        meta={
            "question_key": question_key,
            "baseline_task_id": baseline_task_id,
            "right_task_id": right_task_id,
        },
    )
    summaries = _summaries_from_comparison(comparison)
    return {
        "comparison_id": comparison["id"],
        **summaries,
        "diff_pair": {
            "left_task_id": baseline_task_id,
            "right_task_id": right_task_id,
            "left_run_id": left_run.id,
            "right_run_id": right_run.id,
        },
    }


def pick_right_task_id(
    task_ids: List[str],
    baseline_task_id: str,
    preferred_right_task_id: Optional[str] = None,
) -> Optional[str]:
    if preferred_right_task_id and preferred_right_task_id in task_ids:
        if preferred_right_task_id != baseline_task_id:
            return preferred_right_task_id
    for tid in task_ids:
        if tid != baseline_task_id:
            return tid
    return None


async def enrich_l2_with_cross_task_diff(
    session: AsyncSession,
    l2: Dict[str, Any],
    *,
    matrix_row: Dict[str, AgentRun],
    member_ids: List[str],
    members: List[Dict[str, Any]],
    baseline_task_id: str,
    question_key: str,
    right_task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """为跨任务 L2 写入 structural/semantic 摘要与 comparison_id。"""
    baseline_run = matrix_row.get(baseline_task_id)
    right_tid = pick_right_task_id(member_ids, baseline_task_id, right_task_id)
    if not right_tid or not baseline_run:
        return l2
    right_run = matrix_row.get(right_tid)
    if not right_run:
        l2["diff_error"] = f"任务 {right_tid} 在该题无 Run，无法生成 Diff"
        return l2
    try:
        diff_info = await run_cross_task_pairwise_diff(
            session,
            left_run=baseline_run,
            right_run=right_run,
            baseline_task_id=baseline_task_id,
            right_task_id=right_tid,
            question_key=question_key,
        )
        l2.update(diff_info)
        left_label = next(
            (m["label"] for m in members if m["member_id"] == baseline_task_id),
            baseline_task_id,
        )
        right_label = next(
            (m["label"] for m in members if m["member_id"] == right_tid),
            right_tid,
        )
        l2["diff_pair"]["left_label"] = left_label
        l2["diff_pair"]["right_label"] = right_label
    except (RunComparisonError, ValueError) as exc:
        l2["diff_error"] = str(exc)
    return l2

