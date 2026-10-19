"""
run_span_nodes.aggregate_key 中 legacy:% 占比（P2 I-06 / C-02 可观测性）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, RunSpanNode


@dataclass
class LegacyAggregateReport:
    total_spans: int
    legacy_spans: int
    legacy_ratio: float
    task_id: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "total_spans": self.total_spans,
            "legacy_spans": self.legacy_spans,
            "legacy_ratio": round(self.legacy_ratio, 4),
            "legacy_ratio_pct": round(self.legacy_ratio * 100.0, 2),
            "task_id": self.task_id,
            "alert": self.legacy_ratio > 0.05 and self.total_spans >= 10,
        }


async def report_legacy_aggregate_ratio(
    session: AsyncSession,
    *,
    task_id: Optional[str] = None,
) -> LegacyAggregateReport:
    """统计 legacy:type:name 占比；可选按 task_id 过滤（经 agent_runs 关联）。"""
    if task_id:
        total_stmt = (
            select(func.count(RunSpanNode.id))
            .select_from(RunSpanNode)
            .join(AgentRun, RunSpanNode.agent_run_id == AgentRun.id)
            .where(AgentRun.task_id == task_id)
        )
        legacy_stmt = total_stmt.where(RunSpanNode.aggregate_key.like("legacy:%"))
    else:
        total_stmt = select(func.count(RunSpanNode.id))
        legacy_stmt = select(func.count(RunSpanNode.id)).where(
            RunSpanNode.aggregate_key.like("legacy:%")
        )

    total = int((await session.execute(total_stmt)).scalar_one())
    legacy = int((await session.execute(legacy_stmt)).scalar_one())
    ratio = (legacy / total) if total else 0.0
    return LegacyAggregateReport(
        total_spans=total,
        legacy_spans=legacy,
        legacy_ratio=ratio,
        task_id=task_id,
    )
