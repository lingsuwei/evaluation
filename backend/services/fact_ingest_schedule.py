"""
推理结束后的摄入调度：统一入队 Trace Sync Worker 物化。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, TraceSyncJob
from services.trace_sync_service import enqueue_trace_sync_for_run


async def schedule_fact_ingest_after_run(
    session: AsyncSession,
    run: AgentRun,
) -> Optional[TraceSyncJob]:
    """推理结束后入队 Trace Sync Worker 物化。"""
    if not (run.trace_id or "").strip():
        run.trace_sync_status = "skipped"
        await session.flush()
        return None

    return await enqueue_trace_sync_for_run(session, run)
