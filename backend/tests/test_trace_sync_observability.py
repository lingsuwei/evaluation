"""P2 I-05：Trace Sync 队列可观测性。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, TraceSyncJob
from services.trace_sync_observability import get_trace_sync_queue_stats


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            AgentRun(
                id="run_o1",
                origin="evaluation",
                task_id="t1",
                dataset_id="ds",
                question_id=0,
                agent_id="ag1",
                status="success",
                started_at=datetime.utcnow(),
                agent_snapshot="{}",
            )
        )
        session.add_all(
            [
                TraceSyncJob(
                    id="j1",
                    agent_run_id="run_o1",
                    task_id="t1",
                    trace_id="tr1",
                    status="pending",
                    attempts=0,
                    created_at=datetime.utcnow(),
                ),
                TraceSyncJob(
                    id="j2",
                    agent_run_id="run_o1",
                    task_id="t1",
                    trace_id="tr2",
                    status="success",
                    attempts=1,
                    created_at=datetime.utcnow(),
                ),
                TraceSyncJob(
                    id="j3",
                    agent_run_id="run_o1",
                    task_id="t1",
                    trace_id="tr3",
                    status="failed",
                    attempts=3,
                    created_at=datetime.utcnow(),
                ),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_queue_stats(mem_session):
    stats = await get_trace_sync_queue_stats(mem_session)
    assert stats.pending == 1
    assert stats.success == 1
    assert stats.failed == 1
    assert stats.total == 3
    assert stats.failure_rate == 0.5
    d = stats.as_dict()
    assert d["pending"] == 1
