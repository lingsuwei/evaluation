"""event_agg_diff：Structural 聚合层（P3 §10.3.2）。M-W3-2: 增 per-member 聚合。"""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import RunSpanNode
from services.diff.base import DiffCalculator
from services.diff.diff_types import EVENT_AGG_DIFF, SCHEMA_VERSION
from services.diff.span_aggregate import (
    aggregate_span_nodes,
    aggregate_span_nodes_by_member,
    merge_aggregate_diff,
    merge_member_aggregate_diff,
)


class EventAggDiffCalculator(DiffCalculator):
    diff_type = EVENT_AGG_DIFF
    schema_version = SCHEMA_VERSION

    async def compute(
        self,
        session: AsyncSession,
        *,
        left_run_id: str,
        right_run_id: str,
    ) -> Dict[str, Any]:
        left_spans = (
            await session.execute(
                select(RunSpanNode).where(RunSpanNode.agent_run_id == left_run_id)
            )
        ).scalars().all()
        right_spans = (
            await session.execute(
                select(RunSpanNode).where(RunSpanNode.agent_run_id == right_run_id)
            )
        ).scalars().all()

        # overall aggregate diff
        events = merge_aggregate_diff(
            aggregate_span_nodes(left_spans),
            aggregate_span_nodes(right_spans),
        )

        # M-W3-2: per-member aggregate diff
        by_member = merge_member_aggregate_diff(
            aggregate_span_nodes_by_member(left_spans),
            aggregate_span_nodes_by_member(right_spans),
        )

        return {
            "events": events,
            "by_member": by_member,
        }
