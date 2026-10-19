"""跨任务（跨版本）单题 Step 序列对比（P1）。

选定 question_key 后，在基线任务与对比任务中各取代表性 Run（首个 success，
否则首个 Run），复用同任务多轮已有的 build_run_step_compare 做 Step 对齐 diff，
与前端共享 StepSequenceDiffView 组件渲染。

跨任务场景下，基线任务自身的多轮稳定性（round_stability / structure_summary）
无对比语义，这里清空，仅保留 aligned_steps 与左右 Run 元数据 + Trace。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun
from services.cross_task_question_runtime_service import _representative_run
from services.task_performance_report_service import (
    _load_task_runs,
    build_run_step_compare,
)
from services.task_scenario_matrix_service import question_key_from_run


async def _representative_run_for_question(
    session: AsyncSession, task_id: str, question_key: str
) -> Optional[AgentRun]:
    runs: List[AgentRun] = await _load_task_runs(session, task_id)
    matched = [r for r in runs if question_key_from_run(r) == question_key]
    return _representative_run(matched)


async def build_cross_task_step_compare(
    session: AsyncSession,
    *,
    question_key: str,
    baseline_task_id: str,
    compare_task_id: str,
) -> Optional[Dict[str, Any]]:
    """基线版本代表 Run vs 对比版本代表 Run 的 Step 对齐 diff。"""
    qk = (question_key or "").strip()
    if not qk or not baseline_task_id or not compare_task_id:
        return None

    left = await _representative_run_for_question(session, baseline_task_id, qk)
    right = await _representative_run_for_question(session, compare_task_id, qk)
    if left is None or right is None:
        return None

    payload = await build_run_step_compare(
        session,
        left_run_id=left.id,
        right_run_id=right.id,
    )
    if payload is None:
        return None

    # 跨任务无同任务多轮稳定性语义，去掉以免误导
    payload["round_stability"] = {"columns": [], "rows": []}
    payload["structure_summary"] = None
    payload["question_key"] = qk
    payload["baseline_task_id"] = baseline_task_id
    payload["compare_task_id"] = compare_task_id
    return payload
