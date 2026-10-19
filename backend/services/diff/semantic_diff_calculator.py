"""semantic_diff：Semantic 层对比（P5 Tier 0/1）。"""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import RunSpanNode
from services.diff.base import DiffCalculator
from services.diff.diff_types import SCHEMA_VERSION, SEMANTIC_DIFF
from services.diff.semantic_judge import build_rule_based_judge_summary
from services.diff.semantic_semantics import (
    index_semantics_by_aggregate_key,
    merge_semantic_diff,
)
from services.semantic_node_service import (
    load_semantic_index_by_aggregate_key,
    materialize_semantic_nodes_for_run,
)


class SemanticDiffCalculator(DiffCalculator):
    diff_type = SEMANTIC_DIFF
    schema_version = SCHEMA_VERSION

    async def compute(
        self,
        session: AsyncSession,
        *,
        left_run_id: str,
        right_run_id: str,
    ) -> Dict[str, Any]:
        await materialize_semantic_nodes_for_run(session, left_run_id)
        await materialize_semantic_nodes_for_run(session, right_run_id)
        left_idx = await load_semantic_index_by_aggregate_key(session, left_run_id)
        right_idx = await load_semantic_index_by_aggregate_key(session, right_run_id)
        tier = "1"
        if not left_idx and not right_idx:
            tier = "0"
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
            left_idx = index_semantics_by_aggregate_key(left_spans)
            right_idx = index_semantics_by_aggregate_key(right_spans)
        payload = merge_semantic_diff(left_idx, right_idx)
        payload["tier"] = tier
        payload["judge_summary"] = build_rule_based_judge_summary(payload)
        return payload


def compact_semantic_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """semantic_diff 紧凑摘要（任务报表 / 实验 highlights）。"""
    return {
        "tier": payload.get("tier"),
        "role_mismatch_count": len(payload.get("role_mismatch_keys") or []),
        "summary_mismatch_count": len(payload.get("summary_mismatch_keys") or []),
        "judge_summary": payload.get("judge_summary"),
    }
