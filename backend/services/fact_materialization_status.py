"""
任务级 Execution Fact 物化状态（P2 D-05）。

基于 agent_runs.trace_sync_status 聚合，供报表/API 判断 lag。
"""
from __future__ import annotations

from typing import Dict, List, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import TraceSyncJob

FactMaterializationStatus = Literal[
    "no_runs",
    "in_progress",
    "complete",
    "partial",
    "failed",
]

_PENDING_STATUSES = frozenset({"pending_sync", "pending", "awaiting_push"})
_TERMINAL_OK = frozenset({"synced", "skipped"})


def summarize_trace_sync_counts(agent_runs) -> Dict[str, int]:
    """按 trace_sync_status 计数。"""
    summary: Dict[str, int] = {}
    for run in agent_runs:
        key = (getattr(run, "trace_sync_status", None) or "unknown").strip() or "unknown"
        summary[key] = summary.get(key, 0) + 1
    return summary


async def summarize_trace_sync_failure_categories(
    session: AsyncSession,
    task_id: str,
) -> Dict[str, int]:
    """按 failure_category 聚合指定任务下的 failed SyncJob。

    failure_category 为 NULL 的历史 job 用 last_error 做 lazy 推导。
    """
    rows = (
        await session.execute(
            select(TraceSyncJob.failure_category, TraceSyncJob.last_error)
            .where(
                TraceSyncJob.task_id == task_id,
                TraceSyncJob.status == "failed",
            )
        )
    ).all()
    if not rows:
        return {}
    from services.trace_sync_failure_category import derive_category_from_error_str

    categories: Dict[str, int] = {}
    for category, last_error in rows:
        cat = category or derive_category_from_error_str(last_error or "")
        categories[cat] = categories.get(cat, 0) + 1
    return categories


def compute_fact_materialization_status(
    sync_summary: Dict[str, int],
    *,
    agent_run_count: int,
) -> FactMaterializationStatus:
    """
    任务级物化完成度：
    - complete：全部 run 为 synced 或 skipped（离线无 trace）
    - in_progress：存在 pending_sync / pending
    - failed：仅有 failed 且无 synced
    - partial：其余混合态
    - no_runs：无 AgentRun
    """
    if agent_run_count <= 0:
        return "no_runs"

    if any(sync_summary.get(s, 0) > 0 for s in _PENDING_STATUSES):
        return "in_progress"

    synced = sync_summary.get("synced", 0)
    skipped = sync_summary.get("skipped", 0)
    failed = sync_summary.get("failed", 0)

    if synced + skipped >= agent_run_count:
        return "complete"

    if failed > 0 and synced == 0 and skipped == 0:
        return "failed"

    return "partial"
