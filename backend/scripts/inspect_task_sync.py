#!/usr/bin/env python3
"""Inspect trace_sync_status and span nodes for a task."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import DATABASE_URL
from database import AgentRun, CheckpointResult, EvaluationTask, RunSpanNode, TraceSyncJob


async def inspect(task_id: str) -> None:
    print("DATABASE_URL:", DATABASE_URL)
    engine = create_async_engine(DATABASE_URL)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        task = (
            await session.execute(
                select(EvaluationTask).where(EvaluationTask.id == task_id)
            )
        ).scalar_one_or_none()
        if not task:
            print("TASK NOT FOUND:", task_id)
            return
        print(f"task={task.id} status={task.status} agent_id={task.agent_id}")

        runs = (
            await session.execute(
                select(AgentRun)
                .where(AgentRun.task_id == task_id)
                .order_by(AgentRun.question_id, AgentRun.run_index)
            )
        ).scalars().all()
        print(f"agent_runs={len(runs)}")
        sync_counts: dict[str, int] = {}
        for run in runs:
            st = run.trace_sync_status or "unknown"
            sync_counts[st] = sync_counts.get(st, 0) + 1
            span_cnt = (
                await session.execute(
                    select(func.count())
                    .select_from(RunSpanNode)
                    .where(RunSpanNode.agent_run_id == run.id)
                )
            ).scalar()
            cp_cnt = (
                await session.execute(
                    select(func.count())
                    .select_from(CheckpointResult)
                    .where(CheckpointResult.agent_run_id == run.id)
                )
            ).scalar()
            print(
                f"  q={run.question_id} idx={run.run_index} run={run.id} "
                f"sync={run.trace_sync_status} trace={run.trace_id} "
                f"spans={span_cnt} checkpoints={cp_cnt} tokens={run.tokens_total} "
                f"logical={run.logical_agent_id} agent_name={run.agent_name}"
            )

        print("sync_summary:", sync_counts)

        jobs = (
            await session.execute(
                select(TraceSyncJob)
                .where(TraceSyncJob.task_id == task_id)
                .order_by(TraceSyncJob.created_at.desc())
                .limit(10)
            )
        ).scalars().all()
        print(f"trace_sync_jobs(recent)={len(jobs)}")
        for job in jobs:
            err = (job.last_error or "")[:160]
            print(
                f"  job={job.id} status={job.status} run={job.agent_run_id} "
                f"trace={job.trace_id} attempts={job.attempts} err={err}"
            )
    await engine.dispose()


if __name__ == "__main__":
    tid = sys.argv[1] if len(sys.argv) > 1 else "task_106e5b91"
    asyncio.run(inspect(tid))
