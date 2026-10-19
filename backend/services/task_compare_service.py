"""跨任务对比摘要服务（轻量版）。

供 AI 助手 compare_tasks 工具直接调用，输出精简摘要：
- 每个任务的总分 + 四维徽章
- 相对 baseline 的总分差值
- 场景子步骤差异计数（可选，按需扩展）

不调用 build_cross_task_cockpit（重，会抛异常且返回结构复杂），
而是直接复用 compute_task_quality_scores + build_dimension_verdict_badges_for_tasks
两个批量接口，保证 LLM 上下文不膨胀。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import EvaluationTask
from services.quality_score_service import compute_task_quality_scores
from services.task_dimension_verdict_service import (
    build_dimension_verdict_badges_for_tasks,
)


async def compare_tasks_summary(
    session: AsyncSession,
    task_ids: List[str],
    baseline_task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """对比多个任务，输出精简摘要供 LLM 生成 Markdown 报告。

    Args:
        task_ids: 任务 ID 列表（2-6 个）
        baseline_task_id: 基线任务 ID，默认取第一个

    Returns:
        {
            "baseline_task_id": str,
            "members": [
                {
                    "task_id": str,
                    "status": str,
                    "is_baseline": bool,
                    "total_score": float | None,
                    "result_score": float | None,
                    "process_score": float | None,
                    "dimension_badges": {"quality": "pass", ...},
                    "delta_vs_baseline": float | None,  # 仅非 baseline 成员有
                }
            ],
            "warnings": [...],
        }
    """
    ids = [str(t).strip() for t in task_ids if t and str(t).strip()]
    if len(ids) < 2:
        return {"error": "对比至少需要 2 个任务"}
    if len(ids) > 6:
        return {"error": "单次最多对比 6 个任务"}
    # 去重保序
    seen = set()
    unique_ids = []
    for tid in ids:
        if tid not in seen:
            seen.add(tid)
            unique_ids.append(tid)
    ids = unique_ids

    tasks = (
        await session.execute(select(EvaluationTask).where(EvaluationTask.id.in_(ids)))
    ).scalars().all()
    task_by_id = {t.id: t for t in tasks}

    # 未找到的任务
    missing = [tid for tid in ids if tid not in task_by_id]
    warnings: List[str] = []
    if missing:
        warnings.append(f"未找到的任务: {', '.join(missing)}")
    # 未完成的任务不参与对比（与 cockpit 保持一致）
    incomplete = [
        tid for tid in ids
        if tid in task_by_id
        and (task_by_id[tid].status or "").lower() not in ("completed", "cancelled", "failed")
    ]
    if incomplete:
        return {
            "error": "仅支持已完成/已取消/失败的任务参与对比",
            "incomplete": incomplete,
            "completed_required": True,
        }

    valid_ids = [tid for tid in ids if tid in task_by_id and tid not in incomplete]
    if len(valid_ids) < 2:
        return {"error": "有效对比任务不足 2 个", "warnings": warnings}

    baseline_id = baseline_task_id if baseline_task_id in valid_ids else valid_ids[0]

    # 批量算质量分 + 四维徽章
    quality_scores = await compute_task_quality_scores(session, valid_ids)
    badges = await build_dimension_verdict_badges_for_tasks(
        session,
        [task_by_id[tid] for tid in valid_ids],
        quality_scores=quality_scores,
    )

    baseline_qs = quality_scores.get(baseline_id, {})
    baseline_total = _safe_float(baseline_qs.get("total_score"))

    members: List[Dict[str, Any]] = []
    for tid in valid_ids:
        qs = quality_scores.get(tid, {})
        total = _safe_float(qs.get("total_score"))
        members.append({
            "task_id": tid,
            "status": task_by_id[tid].status,
            "is_baseline": tid == baseline_id,
            "total_score": total,
            "result_score": _safe_float(qs.get("result_score")),
            "process_score": _safe_float(qs.get("process_score")),
            "dimension_badges": badges.get(tid, {}),
            "delta_vs_baseline": (
                None if tid == baseline_id or total is None or baseline_total is None
                else round(total - baseline_total, 4)
            ),
        })

    # 评测集不一致提示
    dataset_ids = {task_by_id[tid].dataset_id for tid in valid_ids if task_by_id[tid].dataset_id}
    if len(dataset_ids) > 1:
        warnings.append("所选任务的评测集不一致，对比结果仅供参考")

    return {
        "baseline_task_id": baseline_id,
        "members": members,
        "warnings": warnings,
    }


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (ValueError, TypeError):
        return None
    return round(f, 4)
