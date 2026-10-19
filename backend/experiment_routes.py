"""Experiment API（P3-B）。"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from services.experiment_service import (
    ExperimentError,
    get_experiment_detail,
    get_experiment_for_task,
    get_or_create_experiment_for_task,
    run_version_experiment,
)
from services.version_cockpit_service import build_version_cockpit

experiment_router = APIRouter(prefix="/experiments", tags=["Experiments"])


class CreateExperimentRequest(BaseModel):
    name: Optional[str] = None
    baseline_agent_id: Optional[str] = None


class RunExperimentRequest(BaseModel):
    diff_types: Optional[List[str]] = Field(
        default=None,
        description="省略时默认 P3 四类 + semantic_diff + structural_diff",
    )


@experiment_router.post("/task/{task_id}", deprecated=True)
async def create_experiment_for_task(
    task_id: str,
    body: CreateExperimentRequest,
    session: AsyncSession = Depends(get_session),
):
    """[已废弃] 请使用 POST /api/scenario-eval/from-tasks。"""
    exp = await get_or_create_experiment_for_task(
        session,
        task_id,
        name=body.name,
        baseline_agent_id=body.baseline_agent_id,
    )
    await session.commit()
    detail = await get_experiment_detail(session, exp.id)
    return detail


@experiment_router.get("/task/{task_id}", deprecated=True)
async def get_task_experiment(
    task_id: str,
    session: AsyncSession = Depends(get_session),
):
    """[已废弃] 请使用 POST /api/scenario-eval/from-tasks 跨任务对比。"""
    detail = await get_experiment_for_task(session, task_id)
    if not detail:
        raise HTTPException(404, "experiment not found for task")
    return detail


@experiment_router.get("/task/{task_id}/cockpit", deprecated=True)
async def get_task_version_cockpit(
    task_id: str,
    question_id: Optional[int] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """[已废弃] Outcome 驾驶舱；请使用 GET /api/scenario-eval/tasks/{task_id}/matrix。"""
    return await build_version_cockpit(session, task_id, question_id=question_id)


@experiment_router.get("/{experiment_id}")
async def get_experiment_by_id(
    experiment_id: str,
    session: AsyncSession = Depends(get_session),
):
    detail = await get_experiment_detail(session, experiment_id)
    if not detail:
        raise HTTPException(404, "experiment not found")
    return detail


@experiment_router.post("/{experiment_id}/run", deprecated=True)
async def post_run_experiment(
    experiment_id: str,
    body: RunExperimentRequest,
    session: AsyncSession = Depends(get_session),
):
    """[已废弃] 请使用 POST /api/scenario-eval/from-tasks。"""
    try:
        return await run_version_experiment(
            session, experiment_id, diff_types=body.diff_types
        )
    except ExperimentError as e:
        raise HTTPException(400, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
