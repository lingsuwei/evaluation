"""cost_diff：读 agent_runs + span 聚合（P3 §10.3.1b）。"""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, RunSpanNode
from services.diff.base import DiffCalculator
from services.diff.diff_types import COST_DIFF, SCHEMA_VERSION
from services.diff.span_aggregate import aggregate_span_nodes, merge_aggregate_diff, run_latency_ms


class CostDiffCalculator(DiffCalculator):
    diff_type = COST_DIFF
    schema_version = SCHEMA_VERSION

    async def compute(
        self,
        session: AsyncSession,
        *,
        left_run_id: str,
        right_run_id: str,
    ) -> Dict[str, Any]:
        left_run = await session.get(AgentRun, left_run_id)
        right_run = await session.get(AgentRun, right_run_id)
        if not left_run or not right_run:
            raise ValueError("left_run_id or right_run_id not found")

        left_spans = await self._load_spans(session, left_run_id)
        right_spans = await self._load_spans(session, right_run_id)
        left_agg = aggregate_span_nodes(left_spans)
        right_agg = aggregate_span_nodes(right_spans)

        left_lat = run_latency_ms(left_run)
        right_lat = run_latency_ms(right_run)
        left_tokens = left_run.tokens_total
        right_tokens = right_run.tokens_total

        run_level: Dict[str, Any] = {}
        if left_lat is not None or right_lat is not None:
            run_level["latency_ms"] = {
                "left": left_lat,
                "right": right_lat,
                "delta": (right_lat - left_lat) if left_lat is not None and right_lat is not None else None,
            }
        if left_tokens is not None or right_tokens is not None:
            run_level["tokens_total"] = {
                "left": left_tokens,
                "right": right_tokens,
                "delta": (right_tokens - left_tokens)
                if left_tokens is not None and right_tokens is not None
                else None,
            }

        return {
            "run_level": run_level,
            "by_aggregate_key": merge_aggregate_diff(left_agg, right_agg),
        }

    async def _load_spans(self, session: AsyncSession, run_id: str):
        return (
            await session.execute(
                select(RunSpanNode).where(RunSpanNode.agent_run_id == run_id)
            )
        ).scalars().all()
