"""AgentRun Span Tree / Checkpoint API（P4）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from services.checkpoint_result_service import build_run_checkpoint_report
from services.span_tree_service import build_span_tree_for_run

run_router = APIRouter(prefix="/runs", tags=["Runs"])


@run_router.get("/{run_id}/checkpoints")
async def get_run_checkpoints(
    run_id: str,
    session: AsyncSession = Depends(get_session),
):
    """单 Run 过程点合规明细（按模板 order 排序）。"""
    report = await build_run_checkpoint_report(session, run_id)
    if not report:
        raise HTTPException(404, "run not found")
    return report


@run_router.get("/{run_id}/span-tree")
async def get_run_span_tree(
    run_id: str,
    session: AsyncSession = Depends(get_session),
):
    tree = await build_span_tree_for_run(session, run_id)
    if not tree:
        raise HTTPException(404, "run or span tree not found")
    return tree
