"""Tier 1 语义节点物化（P5-B2）。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import RunSemanticNode, RunSpanNode

LLM_NODE_TYPES = frozenset({"generation", "llm"})
SUMMARY_MAX_LEN = 1024


def build_tier1_reasoning_summary(span: RunSpanNode) -> Optional[str]:
    """模板摘要：优先 summary_text；llm/generation 节点生成简短描述。"""
    if span.summary_text and str(span.summary_text).strip():
        return str(span.summary_text).strip()[:SUMMARY_MAX_LEN]
    ntype = (span.node_type or "").strip().lower()
    name = (span.name or span.aggregate_key or "node").strip()
    role = (span.semantic_role or "").strip()
    if ntype in LLM_NODE_TYPES:
        prefix = role or ntype
        return f"[{prefix}] {name}"[:SUMMARY_MAX_LEN]
    if role:
        return f"[{role}] {name}"[:SUMMARY_MAX_LEN]
    return None


def _should_materialize(span: RunSpanNode) -> bool:
    key = (span.aggregate_key or "").strip()
    if not key or key == "unknown":
        return False
    return build_tier1_reasoning_summary(span) is not None


async def materialize_semantic_nodes_for_run(
    session: AsyncSession, agent_run_id: str
) -> int:
    """从 run_span_nodes 幂等 UPSERT run_semantic_nodes（Tier 1 模板）。"""
    spans = (
        await session.execute(
            select(RunSpanNode)
            .where(RunSpanNode.agent_run_id == agent_run_id)
            .order_by(RunSpanNode.step_index, RunSpanNode.sibling_order)
        )
    ).scalars().all()
    existing = (
        await session.execute(
            select(RunSemanticNode).where(RunSemanticNode.agent_run_id == agent_run_id)
        )
    ).scalars().all()
    by_obs = {
        (row.observation_id or row.run_span_node_id or row.id): row for row in existing
    }
    written = 0
    for span in spans:
        if not _should_materialize(span):
            continue
        summary = build_tier1_reasoning_summary(span)
        if not summary:
            continue
        obs_key = span.observation_id or span.id
        meta = {
            "tier": "1",
            "source": "template",
            "aggregate_key": span.aggregate_key,
            "semantic_role": span.semantic_role,
            "node_type": span.node_type,
        }
        row = by_obs.get(obs_key)
        if row:
            row.reasoning_summary = summary
            row.run_span_node_id = span.id
            row.generator_meta = meta
        else:
            row = RunSemanticNode(
                id=f"rsn_{uuid.uuid4().hex[:12]}",
                agent_run_id=agent_run_id,
                observation_id=span.observation_id,
                run_span_node_id=span.id,
                reasoning_summary=summary,
                generator_meta=meta,
                created_at=datetime.utcnow(),
            )
            session.add(row)
            by_obs[obs_key] = row
        written += 1
    if written:
        await session.flush()
    return written


async def load_semantic_index_by_aggregate_key(
    session: AsyncSession, agent_run_id: str
) -> Dict[str, Dict[str, Any]]:
    """读取 run_semantic_nodes 并按 aggregate_key 索引。"""
    semantic_rows = (
        await session.execute(
            select(RunSemanticNode).where(RunSemanticNode.agent_run_id == agent_run_id)
        )
    ).scalars().all()
    if not semantic_rows:
        return {}
    spans = (
        await session.execute(
            select(RunSpanNode).where(RunSpanNode.agent_run_id == agent_run_id)
        )
    ).scalars().all()
    spans_by_id = {s.id: s for s in spans}
    spans_by_obs = {
        s.observation_id: s for s in spans if s.observation_id
    }
    # pick representative span per aggregate_key (lowest step_index)
    rep_span_by_key: Dict[str, RunSpanNode] = {}
    for span in spans:
        key = (span.aggregate_key or "").strip()
        if not key or key == "unknown":
            continue
        prev = rep_span_by_key.get(key)
        if prev is None:
            rep_span_by_key[key] = span
            continue
        prev_idx = prev.step_index if prev.step_index is not None else 10**9
        cur_idx = span.step_index if span.step_index is not None else 10**9
        if cur_idx < prev_idx:
            rep_span_by_key[key] = span
    sem_by_span_id = {r.run_span_node_id: r for r in semantic_rows if r.run_span_node_id}
    sem_by_obs = {r.observation_id: r for r in semantic_rows if r.observation_id}
    out: Dict[str, Dict[str, Any]] = {}
    for key, span in rep_span_by_key.items():
        sem = sem_by_span_id.get(span.id) or sem_by_obs.get(span.observation_id or "")
        if not sem:
            continue
        summary = sem.reasoning_summary or span.summary_text
        out[key] = {
            "aggregate_key": key,
            "semantic_role": (span.semantic_role or "").strip() or None,
            "summary_text": (str(summary).strip() if summary else None),
            "observation_id": span.observation_id,
        }
    return out
