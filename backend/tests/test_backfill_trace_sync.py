"""H-03 backfill_trace_sync 单测。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, TraceSyncJob
from services.backfill_trace_sync_service import (
    backfill_trace_sync_for_tasks,
    enqueue_backfill_for_task,
    process_pending_jobs_for_task,
)
from services.eval_context import build_eval_context


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _make_run(
    run_id: str,
    task_id: str,
    *,
    trace_id: str = "tr_1",
    trace_sync_status: str = "pending_sync",
) -> AgentRun:
    return AgentRun(
        id=run_id,
        origin="evaluation",
        eval_context=build_eval_context(
            task_id=task_id,
            dataset_id="d1",
            question_id=0,
            run_index=0,
        ),
        task_id=task_id,
        dataset_id="d1",
        question_id=0,
        agent_id="ag1",
        status="success",
        trace_id=trace_id,
        trace_sync_status=trace_sync_status,
        started_at=datetime.utcnow(),
        agent_snapshot="{}",
    )


@pytest.mark.asyncio
async def test_enqueue_backfill_for_task(mem_session):
    mem_session.add(_make_run("run_bf1", "task_bf"))
    mem_session.add(
        _make_run("run_bf2", "task_bf", trace_id="", trace_sync_status="pending_sync")
    )
    mem_session.add(
        _make_run("run_bf3", "task_bf", trace_sync_status="synced")
    )
    await mem_session.commit()

    stats = await enqueue_backfill_for_task(mem_session, "task_bf")
    await mem_session.commit()

    assert stats.matched == 2
    assert stats.enqueued == 1
    assert stats.skipped_no_trace == 1
    assert stats.skipped_synced == 0

    jobs = (
        await mem_session.execute(
            select(TraceSyncJob).where(TraceSyncJob.task_id == "task_bf")
        )
    ).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].agent_run_id == "run_bf1"


@pytest.mark.asyncio
async def test_enqueue_backfill_force_includes_synced(mem_session):
    mem_session.add(_make_run("run_synced", "task_force", trace_sync_status="synced"))
    await mem_session.commit()

    stats = await enqueue_backfill_for_task(
        mem_session,
        "task_force",
        include_synced=True,
        force=True,
    )
    await mem_session.commit()

    assert stats.matched == 1
    assert stats.enqueued == 1


@pytest.mark.asyncio
async def test_enqueue_backfill_force_resets_active_job(mem_session):
    run = _make_run("run_reset", "task_reset", trace_sync_status="pending")
    mem_session.add(run)
    await mem_session.flush()
    mem_session.add(
        TraceSyncJob(
            id="tsj_reset",
            agent_run_id=run.id,
            task_id="task_reset",
            trace_id=run.trace_id,
            status="pending",
            attempts=3,
            last_error="old error",
            created_at=datetime.utcnow(),
        )
    )
    await mem_session.commit()

    stats = await enqueue_backfill_for_task(
        mem_session,
        "task_reset",
        force=True,
    )
    await mem_session.commit()

    assert stats.reset == 1
    job = await mem_session.get(TraceSyncJob, "tsj_reset")
    assert job is not None
    assert job.attempts == 0
    assert job.last_error is None
    assert job.status == "pending"


@pytest.mark.asyncio
async def test_backfill_dry_run_no_persist(mem_session):
    mem_session.add(_make_run("run_dry", "task_dry"))
    await mem_session.commit()

    result = await backfill_trace_sync_for_tasks(
        mem_session,
        ["task_dry"],
        dry_run=True,
    )
    assert result.summary()["enqueued"] == 1

    jobs = (
        await mem_session.execute(select(TraceSyncJob))
    ).scalars().all()
    assert jobs == []


@pytest.mark.asyncio
async def test_process_pending_jobs_for_task(mem_session):
    run = _make_run("run_proc", "task_proc", trace_sync_status="pending")
    mem_session.add(run)
    await mem_session.flush()
    mem_session.add(
        TraceSyncJob(
            id="tsj_proc",
            agent_run_id=run.id,
            task_id="task_proc",
            trace_id=run.trace_id,
            status="pending",
            attempts=0,
            created_at=datetime.utcnow(),
        )
    )
    await mem_session.commit()

    with patch(
        "services.trace_sync_worker.get_langfuse_client",
        return_value=AsyncMock(get_trace=AsyncMock(return_value={"observations": []})),
    ), patch("services.trace_sync_worker.LANGFUSE_ENABLED", True), patch(
        "services.trace_sync_worker._check_observation_stability",
        return_value=(False, "test skip", None),
    ), patch("services.trace_sync_worker.TRACE_SYNC_MIN_INGESTION_DELAY_SEC", 0):
        stats = await process_pending_jobs_for_task(mem_session, "task_proc")

    assert stats.processed == 1
    assert stats.success == 1
    refreshed = await mem_session.get(AgentRun, "run_proc")
    assert refreshed is not None
    assert refreshed.trace_sync_status == "synced"


@pytest.mark.asyncio
async def test_backfill_end_to_end_enqueue_and_process(mem_session):
    mem_session.add(_make_run("run_e2e", "task_e2e"))
    await mem_session.commit()

    with patch(
        "services.trace_sync_worker.get_langfuse_client",
        return_value=AsyncMock(get_trace=AsyncMock(return_value={"observations": []})),
    ), patch("services.trace_sync_worker.LANGFUSE_ENABLED", True), patch(
        "services.trace_sync_worker._check_observation_stability",
        return_value=(False, "test skip", None),
    ), patch("services.trace_sync_worker.TRACE_SYNC_MIN_INGESTION_DELAY_SEC", 0):
        result = await backfill_trace_sync_for_tasks(mem_session, ["task_e2e"])

    summary = result.summary()
    assert summary["enqueued"] == 1
    assert summary["success"] == 1

    run = await mem_session.get(AgentRun, "run_e2e")
    assert run is not None
    assert run.trace_sync_status == "synced"
