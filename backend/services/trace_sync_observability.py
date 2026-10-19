"""
Trace Sync 队列可观测性（P2 I-05）：pending 数、失败率、失败类别分布。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import TraceSyncJob
from services.trace_sync_failure_category import derive_category_from_error_str


@dataclass
class TraceSyncQueueStats:
    pending: int
    running: int
    success: int
    failed: int
    total: int
    failure_categories: Dict[str, int] = field(default_factory=dict)

    @property
    def failure_rate(self) -> float:
        done = self.success + self.failed
        return (self.failed / done) if done else 0.0

    def as_dict(self) -> Dict[str, float | int]:
        return {
            "pending": self.pending,
            "running": self.running,
            "success": self.success,
            "failed": self.failed,
            "total": self.total,
            "failure_rate": round(self.failure_rate, 4),
            "failure_categories": dict(self.failure_categories),
        }


async def get_trace_sync_queue_stats(session: AsyncSession) -> TraceSyncQueueStats:
    """按 job status 聚合 + 失败类别分布（failure_category 为 NULL 的用 last_error lazy 推导）。"""
    rows = (
        await session.execute(
            select(TraceSyncJob.status, func.count())
            .group_by(TraceSyncJob.status)
        )
    ).all()
    counts = {str(status or "unknown"): int(n) for status, n in rows}
    pending = counts.get("pending", 0)
    running = counts.get("running", 0)
    success = counts.get("success", 0)
    failed = counts.get("failed", 0)
    total = sum(counts.values())

    failure_categories: Dict[str, int] = {}
    if failed > 0:
        failed_rows = (
            await session.execute(
                select(TraceSyncJob.failure_category, TraceSyncJob.last_error)
                .where(TraceSyncJob.status == "failed")
            )
        ).all()
        for category, last_error in failed_rows:
            cat = category or derive_category_from_error_str(last_error or "")
            failure_categories[cat] = failure_categories.get(cat, 0) + 1

    return TraceSyncQueueStats(
        pending=pending,
        running=running,
        success=success,
        failed=failed,
        total=total,
        failure_categories=failure_categories,
    )
