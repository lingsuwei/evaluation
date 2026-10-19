"""trace_ref_service 单测。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, AgentRunTraceRef, Base
from services.trace_ref_service import ensure_root_trace_ref, list_trace_refs_for_run

pytestmark = pytest.mark.p3


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_root_trace_ref_idempotent(mem_session):
    run = AgentRun(
        id="run_tr",
        origin="evaluation",
        task_id="t",
        agent_id="a",
        status="success",
        started_at=datetime.utcnow(),
        trace_id="trace_1",
        agent_snapshot="{}",
    )
    mem_session.add(run)
    await mem_session.commit()
    ref1 = await ensure_root_trace_ref(mem_session, run)
    ref2 = await ensure_root_trace_ref(mem_session, run)
    assert ref1 is not None
    assert ref1.id == ref2.id
    rows = await list_trace_refs_for_run(mem_session, "run_tr")
    assert len(rows) == 1
    assert rows[0].role == "root"


@pytest.mark.asyncio
async def test_ensure_root_trace_ref_no_trace(mem_session):
    run = AgentRun(
        id="run_notr",
        origin="evaluation",
        task_id="t",
        agent_id="a",
        status="success",
        started_at=datetime.utcnow(),
        agent_snapshot="{}",
    )
    mem_session.add(run)
    await mem_session.commit()
    assert await ensure_root_trace_ref(mem_session, run) is None
    assert (
        await mem_session.execute(select(AgentRunTraceRef))
    ).scalars().all() == []
