"""RunComparison / RunDiff API（P3）。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from services.cross_task_cockpit_service import (
    CrossTaskCockpitError,
    build_cross_task_cockpit,
)
from services.run_comparison_service import (
    RunComparisonError,
    compare_runs,
    get_comparison,
    list_comparisons_for_task,
)
from services.span_tree_service import build_dual_span_trees_for_comparison

comparison_router = APIRouter(prefix="/comparisons", tags=["Comparisons"])


class CrossTaskCompareRequest(BaseModel):
    task_ids: List[str] = Field(..., min_length=2, max_length=6)
    baseline_task_id: Optional[str] = None
    question_key: Optional[str] = None
    right_task_id: Optional[str] = Field(
        default=None,
        description="Diff 对比列（非基线任务 ID）；默认取 task_ids 中第一个非基线任务",
    )


class CompareRunsRequest(BaseModel):
    left_run_id: str
    right_run_id: str
    comparison_scope: str = Field(default="manual")
    diff_types: Optional[List[str]] = None
    meta: Optional[Dict[str, Any]] = None


@comparison_router.post("/from-tasks", deprecated=True)
async def post_cross_task_cockpit(
    body: CrossTaskCompareRequest,
    session: AsyncSession = Depends(get_session),
):
    """[已废弃] 跨历史 Outcome 驾驶舱；请使用 POST /api/scenario-eval/from-tasks。"""
    try:
        return await build_cross_task_cockpit(
            session,
            body.task_ids,
            baseline_task_id=body.baseline_task_id,
            question_key=body.question_key,
            right_task_id=body.right_task_id,
        )
    except CrossTaskCockpitError as e:
        raise HTTPException(400, str(e)) from e


@comparison_router.post("/compare")
async def post_compare_runs(
    body: CompareRunsRequest,
    session: AsyncSession = Depends(get_session),
):
    try:
        return await compare_runs(
            session,
            left_run_id=body.left_run_id,
            right_run_id=body.right_run_id,
            comparison_scope=body.comparison_scope,
            diff_types=body.diff_types,
            meta=body.meta,
        )
    except RunComparisonError as e:
        raise HTTPException(400, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@comparison_router.get("/{comparison_id}/span-trees")
async def get_comparison_span_trees(
    comparison_id: str,
    session: AsyncSession = Depends(get_session),
):
    data = await build_dual_span_trees_for_comparison(session, comparison_id)
    if not data:
        raise HTTPException(404, "comparison or span trees not found")
    return data


@comparison_router.get("/{comparison_id}")
async def get_comparison_detail(
    comparison_id: str,
    session: AsyncSession = Depends(get_session),
):
    row = await get_comparison(session, comparison_id)
    if not row:
        raise HTTPException(404, "comparison not found")
    return row


@comparison_router.get("")
async def list_comparisons(
    task_id: str = Query(...),
    question_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    return await list_comparisons_for_task(
        session, task_id, question_id=question_id, limit=limit
    )
