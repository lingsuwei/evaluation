"""
Trace Sync 入队与任务查询（P1.5）。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, TraceSyncJob
from services.eval_context import get_task_id


_ACTIVE_STATUSES = ("pending", "running")


async def enqueue_trace_sync_for_run(
    session: AsyncSession,
    run: AgentRun,
) -> Optional[TraceSyncJob]:
    """
    推理结束后入队：按 agent_run_id 幂等，已有 pending/running 则跳过。
    无 trace_id 时标记 skipped，不入 Langfuse 拉取。
    """
    now = datetime.utcnow()
    if not (run.trace_id or "").strip():
        run.trace_sync_status = "skipped"
        await session.flush()
        return None

    existing = await session.execute(
        select(TraceSyncJob).where(
            TraceSyncJob.agent_run_id == run.id,
            TraceSyncJob.status.in_(_ACTIVE_STATUSES),
        )
    )
    if existing.scalar_one_or_none():
        if not run.trace_sync_status:
            run.trace_sync_status = "pending_sync"
        await session.flush()
        return None

    job = TraceSyncJob(
        id=f"tsj_{uuid.uuid4().hex[:12]}",
        agent_run_id=run.id,
        task_id=get_task_id(run) or run.task_id or "",
        trace_id=run.trace_id,
        langfuse_project=run.langfuse_project,
        status="pending",
        attempts=0,
        created_at=now,
    )
    session.add(job)
    run.trace_sync_status = "pending_sync"
    await session.flush()
    return job


async def list_trace_sync_jobs(
    session: AsyncSession,
    *,
    task_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[TraceSyncJob]:
    q = select(TraceSyncJob).order_by(TraceSyncJob.created_at.desc()).limit(limit)
    if task_id:
        q = q.where(TraceSyncJob.task_id == task_id)
    if status:
        q = q.where(TraceSyncJob.status == status)
    result = await session.execute(q)
    return list(result.scalars().all())
