"""
历史任务 Trace Sync / Builder 回填（P2 H-03）。

按 task_id 筛选 AgentRun，批量入队 trace_sync_jobs 并可选立即执行 Builder。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Agent, AgentRun, TraceSyncJob
from services.eval_context import get_task_id
from services.trace_sync_service import enqueue_trace_sync_for_run
from services.trace_sync_worker import process_trace_sync_job

_DEFAULT_STATUSES = ("pending_sync", "failed", "pending")
_ACTIVE_JOB_STATUSES = ("pending", "running")


@dataclass
class BackfillEnqueueStats:
    task_id: str
    matched: int = 0
    enqueued: int = 0
    reset: int = 0
    skipped_no_trace: int = 0
    skipped_synced: int = 0
    skipped_active: int = 0

    def as_dict(self) -> Dict[str, int | str]:
        return {
            "task_id": self.task_id,
            "matched": self.matched,
            "enqueued": self.enqueued,
            "reset": self.reset,
            "skipped_no_trace": self.skipped_no_trace,
            "skipped_synced": self.skipped_synced,
            "skipped_active": self.skipped_active,
        }


@dataclass
class ReResolveStats:
    """retry 端点重解析 langfuse_project 的统计。"""

    task_id: str
    matched: int = 0  # failed + trace_not_found 的 jobs 数
    re_resolved: int = 0  # langfuse_project 变化并已更新
    unchanged: int = 0  # langfuse_project 未变但已重置 attempts
    fetch_failed: int = 0  # onto-agent 不可达 / inference_config 无效
    agent_missing: int = 0  # Agent 表无对应记录

    def as_dict(self) -> Dict[str, int | str]:
        return {
            "task_id": self.task_id,
            "matched": self.matched,
            "re_resolved": self.re_resolved,
            "unchanged": self.unchanged,
            "fetch_failed": self.fetch_failed,
            "agent_missing": self.agent_missing,
        }


@dataclass
class BackfillProcessStats:
    processed: int = 0
    success: int = 0
    skipped: int = 0
    failed: int = 0
    rounds: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "processed": self.processed,
            "success": self.success,
            "skipped": self.skipped,
            "failed": self.failed,
            "rounds": self.rounds,
        }


@dataclass
class BackfillTaskResult:
    enqueue: BackfillEnqueueStats
    process: Optional[BackfillProcessStats] = None


@dataclass
class BackfillBatchResult:
    tasks: List[BackfillTaskResult] = field(default_factory=list)
    dry_run: bool = False

    def summary(self) -> Dict[str, int]:
        out = {
            "tasks": len(self.tasks),
            "matched": 0,
            "enqueued": 0,
            "reset": 0,
            "skipped_no_trace": 0,
            "skipped_synced": 0,
            "skipped_active": 0,
            "processed": 0,
            "success": 0,
            "failed": 0,
        }
        for item in self.tasks:
            eq = item.enqueue
            out["matched"] += eq.matched
            out["enqueued"] += eq.enqueued
            out["reset"] += eq.reset
            out["skipped_no_trace"] += eq.skipped_no_trace
            out["skipped_synced"] += eq.skipped_synced
            out["skipped_active"] += eq.skipped_active
            if item.process:
                out["processed"] += item.process.processed
                out["success"] += item.process.success
                out["failed"] += item.process.failed
        return out


async def _load_runs_for_task(session: AsyncSession, task_id: str) -> List[AgentRun]:
    tid = (task_id or "").strip()
    if not tid:
        return []
    result = await session.execute(
        select(AgentRun).where(
            or_(
                AgentRun.task_id == tid,
                AgentRun.eval_context["task_id"].as_string() == tid,
            )
        )
    )
    runs = list(result.scalars().all())
    if not runs:
        fallback = await session.execute(select(AgentRun))
        runs = [r for r in fallback.scalars().all() if get_task_id(r) == tid]
    return runs


def _filter_runs_by_status(
    runs: Sequence[AgentRun],
    statuses: Sequence[str],
    *,
    include_synced: bool,
) -> List[AgentRun]:
    allowed = set(statuses)
    if include_synced:
        allowed.add("synced")
    filtered: List[AgentRun] = []
    for run in runs:
        status = (run.trace_sync_status or "unknown").strip()
        if status in allowed:
            filtered.append(run)
    return filtered


async def _get_active_job(
    session: AsyncSession,
    agent_run_id: str,
) -> Optional[TraceSyncJob]:
    result = await session.execute(
        select(TraceSyncJob).where(
            TraceSyncJob.agent_run_id == agent_run_id,
            TraceSyncJob.status.in_(_ACTIVE_JOB_STATUSES),
        )
    )
    return result.scalar_one_or_none()


async def enqueue_backfill_for_run(
    session: AsyncSession,
    run: AgentRun,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> str:
    """
    为单条 Run 入队 Builder。
    返回：enqueued | reset | skipped_no_trace | skipped_synced | skipped_active
    """
    if not (run.trace_id or "").strip():
        if not dry_run and run.trace_sync_status != "skipped":
            run.trace_sync_status = "skipped"
            await session.flush()
        return "skipped_no_trace"

    if not force and (run.trace_sync_status or "") == "synced":
        return "skipped_synced"

    active_job = await _get_active_job(session, run.id)
    if active_job:
        if force:
            if dry_run:
                return "reset"
            active_job.status = "pending"
            active_job.attempts = 0
            active_job.last_error = None
            run.trace_sync_status = "pending"
            await session.flush()
            return "reset"
        return "skipped_active"

    if dry_run:
        return "enqueued"

    if force and run.trace_sync_status in ("failed", "synced", "synced_incomplete", "pending_sync", "pending", "awaiting_push"):
        run.trace_sync_status = "pending_sync"
        await session.flush()

    job = await enqueue_trace_sync_for_run(session, run)
    return "enqueued" if job else "skipped_active"


async def enqueue_backfill_for_task(
    session: AsyncSession,
    task_id: str,
    *,
    statuses: Optional[Sequence[str]] = None,
    include_synced: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> BackfillEnqueueStats:
    stats = BackfillEnqueueStats(task_id=task_id)
    status_filter = tuple(statuses or _DEFAULT_STATUSES)
    runs = _filter_runs_by_status(
        await _load_runs_for_task(session, task_id),
        status_filter,
        include_synced=include_synced,
    )
    stats.matched = len(runs)

    for run in runs:
        outcome = await enqueue_backfill_for_run(
            session,
            run,
            force=force,
            dry_run=dry_run,
        )
        if outcome == "enqueued":
            stats.enqueued += 1
        elif outcome == "reset":
            stats.reset += 1
        elif outcome == "skipped_no_trace":
            stats.skipped_no_trace += 1
        elif outcome == "skipped_synced":
            stats.skipped_synced += 1
        elif outcome == "skipped_active":
            stats.skipped_active += 1

    return stats


async def re_resolve_langfuse_project_for_failed_jobs(
    session: AsyncSession,
    task_id: str,
) -> ReResolveStats:
    """重解析 failed jobs 的 langfuse_project 并回写 Agent/AgentRun/Job 三表。

    仅处理 status="failed" 且 failure_category="trace_not_found" 的 jobs：
    通过 AgentRun.agent_id 反查 Agent.inference_config，调用 onto-agent
    observability 接口重新解析 langfuse_project。解析成功时回写三表并重置
    job 为 pending，等待 Worker 自动处理；解析失败则保持 failed。
    """
    from logger import log
    from services.agent_probe_service import (
        resolve_langfuse_project_from_inference_config,
    )

    stats = ReResolveStats(task_id=task_id)

    # 1. 查询符合条件的 failed jobs（仅 trace_not_found 类别）
    result = await session.execute(
        select(TraceSyncJob).where(
            TraceSyncJob.task_id == task_id,
            TraceSyncJob.status == "failed",
            TraceSyncJob.failure_category == "trace_not_found",
        )
    )
    jobs = list(result.scalars().all())
    stats.matched = len(jobs)
    if not jobs:
        return stats

    # 2. 按 agent_id 缓存重解析结果，避免同 agent 多 job 重复探测 onto-agent
    agent_cache: Dict[str, Optional[str]] = {}

    for job in jobs:
        try:
            # 3. 查 AgentRun 拿 agent_id
            run_result = await session.execute(
                select(AgentRun).where(AgentRun.id == job.agent_run_id)
            )
            run = run_result.scalar_one_or_none()
            if not run:
                stats.agent_missing += 1
                log(
                    "WARNING",
                    "TRACE_SYNC_RERESOLVE",
                    "AgentRun 不存在，跳过",
                    job_id=job.id,
                    agent_run_id=job.agent_run_id,
                )
                continue

            agent_id = (run.agent_id or "").strip()
            old_project = job.langfuse_project or ""

            if not agent_id:
                stats.agent_missing += 1
                log(
                    "WARNING",
                    "TRACE_SYNC_RERESOLVE",
                    "AgentRun.agent_id 为空，跳过",
                    job_id=job.id,
                    agent_run_id=run.id,
                )
                continue

            # 4. 缓存命中则复用
            if agent_id in agent_cache:
                new_project = agent_cache[agent_id]
            else:
                # 先确认 Agent 存在（区分 agent_missing 与 fetch_failed）
                agent_exists_result = await session.execute(
                    select(Agent.id).where(Agent.id == agent_id)
                )
                if not agent_exists_result.scalar_one_or_none():
                    stats.agent_missing += 1
                    agent_cache[agent_id] = None
                    log(
                        "WARNING",
                        "TRACE_SYNC_RERESOLVE",
                        "Agent 不存在，跳过",
                        agent_id=agent_id,
                        job_id=job.id,
                    )
                    continue
                new_project = await _resolve_agent_langfuse_project(
                    session, agent_id
                )
                agent_cache[agent_id] = new_project

            # 5. 解析失败：保持 failed，不重置
            if not new_project:
                stats.fetch_failed += 1
                continue

            # 6. 回写三表
            if new_project != old_project:
                # 回写 Agent 表（持久化修正，后续新 run 直接用正确项目）
                agent_result = await session.execute(
                    select(Agent).where(Agent.id == agent_id)
                )
                agent = agent_result.scalar_one_or_none()
                if agent:
                    agent.langfuse_project = new_project

                run.langfuse_project = new_project
                job.langfuse_project = new_project
                stats.re_resolved += 1
                log(
                    "INFO",
                    "TRACE_SYNC_RERESOLVE",
                    "重解析 langfuse_project 成功，回写三表",
                    job_id=job.id,
                    agent_id=agent_id,
                    old_project=old_project,
                    new_project=new_project,
                )
            else:
                stats.unchanged += 1
                log(
                    "INFO",
                    "TRACE_SYNC_RERESOLVE",
                    "langfuse_project 未变化，仅重置 attempts",
                    job_id=job.id,
                    agent_id=agent_id,
                    project=old_project,
                )

            # 7. 重置 job 状态，等待 Worker 自动处理
            job.attempts = 0
            job.last_error = None
            job.failure_category = None
            job.status = "pending"
            run.trace_sync_status = "pending_sync"
        except Exception as e:
            log(
                "ERROR",
                "TRACE_SYNC_RERESOLVE",
                "重解析异常，跳过该 job",
                job_id=job.id,
                error=str(e)[:300],
            )
            stats.fetch_failed += 1
            continue

    if stats.matched > 0:
        await session.flush()
        log(
            "INFO",
            "TRACE_SYNC_RERESOLVE",
            "批量重解析完成",
            **stats.as_dict(),
        )
    return stats


async def _resolve_agent_langfuse_project(
    session: AsyncSession,
    agent_id: str,
) -> Optional[str]:
    """从 Agent.inference_config 调 onto-agent 重解析 langfuse_project。"""
    from logger import log
    from services.agent_probe_service import (
        resolve_langfuse_project_from_inference_config,
    )

    agent_result = await session.execute(
        select(Agent).where(Agent.id == agent_id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        log(
            "WARNING",
            "TRACE_SYNC_RERESOLVE",
            "Agent 不存在",
            agent_id=agent_id,
        )
        return None

    raw_config = (agent.inference_config or "").strip()
    if not raw_config:
        log(
            "WARNING",
            "TRACE_SYNC_RERESOLVE",
            "Agent.inference_config 为空",
            agent_id=agent_id,
        )
        return None

    try:
        config = json.loads(raw_config)
    except json.JSONDecodeError as e:
        log(
            "WARNING",
            "TRACE_SYNC_RERESOLVE",
            "inference_config JSON 解析失败",
            agent_id=agent_id,
            error=str(e)[:200],
        )
        return None

    if not isinstance(config, dict) or not (config.get("url") or "").strip():
        log(
            "WARNING",
            "TRACE_SYNC_RERESOLVE",
            "inference_config 缺少 url",
            agent_id=agent_id,
        )
        return None

    try:
        resolved = await resolve_langfuse_project_from_inference_config(config)
    except Exception as e:
        log(
            "WARNING",
            "TRACE_SYNC_RERESOLVE",
            "调用 onto-agent 重解析失败",
            agent_id=agent_id,
            error=str(e)[:300],
        )
        return None

    new_project = (resolved.get("langfuse_project") or "").strip() or None
    if not new_project:
        log(
            "WARNING",
            "TRACE_SYNC_RERESOLVE",
            "onto-agent 未配置 Langfuse 或未匹配到项目",
            agent_id=agent_id,
            status=resolved.get("status"),
        )
    return new_project


async def _fetch_pending_jobs_for_task(
    session: AsyncSession,
    task_id: str,
    limit: int,
) -> List[TraceSyncJob]:
    result = await session.execute(
        select(TraceSyncJob)
        .where(
            TraceSyncJob.task_id == task_id,
            TraceSyncJob.status == "pending",
        )
        .order_by(TraceSyncJob.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def process_pending_jobs_for_task(
    session: AsyncSession,
    task_id: str,
    *,
    batch_size: int = 20,
    max_rounds: int = 50,
    dry_run: bool = False,
) -> BackfillProcessStats:
    stats = BackfillProcessStats()
    if dry_run:
        pending = await _fetch_pending_jobs_for_task(session, task_id, batch_size * max_rounds)
        stats.processed = len(pending)
        stats.rounds = 1 if pending else 0
        return stats

    for _ in range(max_rounds):
        jobs = await _fetch_pending_jobs_for_task(session, task_id, batch_size)
        if not jobs:
            break
        stats.rounds += 1
        for job in jobs:
            result = await process_trace_sync_job(session, job)
            stats.processed += 1
            status = result.get("status")
            if status == "success":
                stats.success += 1
            elif status == "skipped":
                stats.skipped += 1
            else:
                stats.failed += 1
        await session.commit()
    return stats


async def backfill_trace_sync_for_tasks(
    session: AsyncSession,
    task_ids: Sequence[str],
    *,
    statuses: Optional[Sequence[str]] = None,
    include_synced: bool = False,
    force: bool = False,
    enqueue_only: bool = False,
    batch_size: int = 20,
    max_rounds: int = 50,
    dry_run: bool = False,
) -> BackfillBatchResult:
    batch = BackfillBatchResult(dry_run=dry_run)
    for task_id in task_ids:
        tid = (task_id or "").strip()
        if not tid:
            continue
        enqueue_stats = await enqueue_backfill_for_task(
            session,
            tid,
            statuses=statuses,
            include_synced=include_synced,
            force=force,
            dry_run=dry_run,
        )
        process_stats = None
        if not enqueue_only and not dry_run:
            if enqueue_stats.enqueued or enqueue_stats.reset:
                await session.commit()
            process_stats = await process_pending_jobs_for_task(
                session,
                tid,
                batch_size=batch_size,
                max_rounds=max_rounds,
                dry_run=False,
            )
        elif not enqueue_only and dry_run:
            process_stats = await process_pending_jobs_for_task(
                session,
                tid,
                batch_size=batch_size,
                max_rounds=max_rounds,
                dry_run=True,
            )
        batch.tasks.append(
            BackfillTaskResult(enqueue=enqueue_stats, process=process_stats)
        )
    if dry_run:
        await session.rollback()
    elif enqueue_only:
        await session.commit()
    return batch
