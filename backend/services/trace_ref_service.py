"""AgentRun ↔ trace 映射（P3-B / C-03）。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, AgentRunTraceRef


async def ensure_root_trace_ref(
    session: AsyncSession,
    agent_run: AgentRun,
    *,
    trace_id: Optional[str] = None,
    role: str = "root",
    sort_order: int = 0,
) -> Optional[AgentRunTraceRef]:
    tid = trace_id or agent_run.trace_id
    if not tid:
        return None
    existing = (
        await session.execute(
            select(AgentRunTraceRef).where(
                AgentRunTraceRef.agent_run_id == agent_run.id,
                AgentRunTraceRef.trace_id == tid,
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    ref = AgentRunTraceRef(
        id=f"tref_{uuid.uuid4().hex[:12]}",
        agent_run_id=agent_run.id,
        trace_id=tid,
        role=role,
        sort_order=sort_order,
        created_at=datetime.utcnow(),
    )
    session.add(ref)
    await session.flush()
    return ref


async def list_trace_refs_for_run(
    session: AsyncSession, agent_run_id: str
) -> List[AgentRunTraceRef]:
    return (
        await session.execute(
            select(AgentRunTraceRef)
            .where(AgentRunTraceRef.agent_run_id == agent_run_id)
            .order_by(AgentRunTraceRef.sort_order, AgentRunTraceRef.created_at)
        )
    ).scalars().all()
