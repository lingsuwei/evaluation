"""RunComparison 编排：创建对比会话并落库 RunDiff（P3 §10.4）。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, RunComparison, RunDiff
from services.diff.diff_registry import get_calculator, resolve_diff_types


class RunComparisonError(ValueError):
    pass


async def compare_runs(
    session: AsyncSession,
    *,
    left_run_id: str,
    right_run_id: str,
    comparison_scope: str = "manual",
    diff_types: Optional[Iterable[str]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """对比两 Run，按注册表计算并持久化 RunDiff。"""
    if left_run_id == right_run_id:
        raise RunComparisonError("left_run_id and right_run_id must differ")

    left_run = await session.get(AgentRun, left_run_id)
    right_run = await session.get(AgentRun, right_run_id)
    if not left_run or not right_run:
        raise RunComparisonError("agent run not found")

    types = resolve_diff_types(diff_types)
    comparison = RunComparison(
        id=f"cmp_{uuid.uuid4().hex[:12]}",
        comparison_scope=comparison_scope,
        task_id=left_run.task_id or right_run.task_id,
        question_id=left_run.question_id if left_run.question_id is not None else right_run.question_id,
        run_index=left_run.run_index if left_run.run_index is not None else right_run.run_index,
        left_run_id=left_run_id,
        right_run_id=right_run_id,
        meta=meta,
        created_at=datetime.utcnow(),
    )
    session.add(comparison)
    await session.flush()

    diffs: List[RunDiff] = []
    for diff_type in types:
        calc = get_calculator(diff_type)
        payload_body = await calc.compute(
            session, left_run_id=left_run_id, right_run_id=right_run_id
        )
        payload = {
            "diff_type": diff_type,
            "schema_version": calc.schema_version,
            "left_run_id": left_run_id,
            "right_run_id": right_run_id,
            **payload_body,
        }
        diff = RunDiff(
            id=f"diff_{uuid.uuid4().hex[:12]}",
            comparison_id=comparison.id,
            diff_type=diff_type,
            schema_version=calc.schema_version,
            left_run_id=left_run_id,
            right_run_id=right_run_id,
            payload=payload,
            created_at=datetime.utcnow(),
        )
        session.add(diff)
        diffs.append(diff)

    await session.commit()
    return serialize_comparison(comparison, diffs)


async def get_comparison(
    session: AsyncSession, comparison_id: str
) -> Optional[Dict[str, Any]]:
    comparison = await session.get(RunComparison, comparison_id)
    if not comparison:
        return None
    diffs = (
        await session.execute(
            select(RunDiff)
            .where(RunDiff.comparison_id == comparison_id)
            .order_by(RunDiff.diff_type)
        )
    ).scalars().all()
    return serialize_comparison(comparison, diffs)


async def list_comparisons_for_task(
    session: AsyncSession,
    task_id: str,
    *,
    question_id: Optional[int] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    q = select(RunComparison).where(RunComparison.task_id == task_id)
    if question_id is not None:
        q = q.where(RunComparison.question_id == question_id)
    q = q.order_by(RunComparison.created_at.desc()).limit(limit)
    rows = (await session.execute(q)).scalars().all()
    out: List[Dict[str, Any]] = []
    for cmp in rows:
        diffs = (
            await session.execute(
                select(RunDiff).where(RunDiff.comparison_id == cmp.id)
            )
        ).scalars().all()
        out.append(serialize_comparison(cmp, diffs))
    return out


def serialize_comparison(comparison: RunComparison, diffs: List[RunDiff]) -> Dict[str, Any]:
    return {
        "id": comparison.id,
        "comparison_scope": comparison.comparison_scope,
        "task_id": comparison.task_id,
        "question_id": comparison.question_id,
        "run_index": comparison.run_index,
        "left_run_id": comparison.left_run_id,
        "right_run_id": comparison.right_run_id,
        "meta": comparison.meta,
        "created_at": comparison.created_at.isoformat() if comparison.created_at else None,
        "diffs": [
            {
                "id": d.id,
                "diff_type": d.diff_type,
                "schema_version": d.schema_version,
                "left_run_id": d.left_run_id,
                "right_run_id": d.right_run_id,
                "payload": d.payload,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in diffs
        ],
    }
