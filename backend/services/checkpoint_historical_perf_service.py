"""过程点历史效能参考（P2）：跨任务聚合 checkpoint + Span，供配置页展示 P75/P90。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, CheckpointResult, ProcessCheckpointTemplate, RunSpanNode
from services.checkpoint_batch_service import load_enabled_templates
from services.checkpoint_cost_attribution_service import _matched_obs_id
from services.runtime_target_suggestion_service import _percentile


async def build_checkpoint_historical_performance(
    session: AsyncSession,
    logical_agent_id: str,
    *,
    dataset_id: Optional[str] = None,
    sample_limit: int = 500,
) -> Optional[Dict[str, Any]]:
    """
    按 logical_agent_id（可选 dataset_id）聚合历史 Run 的过程点命中 Span 耗时/Tokens P75/P90。
    """
    lid = (logical_agent_id or "").strip()
    if not lid:
        return None

    templates = await load_enabled_templates(session, lid)
    if not templates:
        return None

    query = (
        select(AgentRun)
        .where(
            AgentRun.logical_agent_id == lid,
            AgentRun.status == "success",
            AgentRun.origin == "evaluation",
        )
        .order_by(AgentRun.started_at.desc())
        .limit(sample_limit)
    )
    if dataset_id:
        query = query.where(AgentRun.dataset_id == dataset_id)

    runs = (await session.execute(query)).scalars().all()
    if not runs:
        return {
            "logical_agent_id": lid,
            "dataset_id": dataset_id,
            "sample_count": 0,
            "rows": [],
        }

    run_ids = [r.id for r in runs]
    cp_rows = (
        await session.execute(
            select(CheckpointResult).where(CheckpointResult.agent_run_id.in_(run_ids))
        )
    ).scalars().all()
    span_rows = (
        await session.execute(
            select(RunSpanNode).where(RunSpanNode.agent_run_id.in_(run_ids))
        )
    ).scalars().all()
    span_by_obs: Dict[Tuple[str, str], RunSpanNode] = {}
    for node in span_rows:
        if node.observation_id:
            span_by_obs[(node.agent_run_id, node.observation_id)] = node

    tpl_map = {t.id: t for t in templates}
    agg: Dict[str, Dict[str, Any]] = {
        t.id: {
            "checkpoint_id": t.id,
            "name": t.name,
            "order": t.order,
            "latency_vals": [],
            "token_vals": [],
            "hit_count": 0,
            "evaluated_runs": 0,
        }
        for t in templates
    }

    seen: set[Tuple[str, str]] = set()
    for row in cp_rows:
        if row.checkpoint_id not in tpl_map:
            continue
        key = (row.agent_run_id, row.checkpoint_id)
        if key in seen:
            continue
        seen.add(key)
        bucket = agg[row.checkpoint_id]
        bucket["evaluated_runs"] += 1
        evidence = row.evidence if isinstance(row.evidence, dict) else {}
        if evidence.get("hit", True):
            bucket["hit_count"] += 1
        obs_id = _matched_obs_id(evidence)
        if obs_id:
            node = span_by_obs.get((row.agent_run_id, obs_id))
            if node:
                if node.latency_ms is not None:
                    bucket["latency_vals"].append(int(node.latency_ms))
                if node.tokens is not None:
                    bucket["token_vals"].append(int(node.tokens))

    rows_out: List[Dict[str, Any]] = []
    for t in templates:
        b = agg[t.id]
        lat = b["latency_vals"]
        tok = b["token_vals"]
        ev = b["evaluated_runs"]
        rows_out.append(
            {
                "checkpoint_id": t.id,
                "name": t.name,
                "order": t.order,
                "evaluated_runs": ev,
                "hit_rate": round(b["hit_count"] / ev, 4) if ev else None,
                "latency_ms": {
                    "p75": round(_percentile([float(x) for x in lat], 75) or 0, 1)
                    if lat
                    else None,
                    "p90": round(_percentile([float(x) for x in lat], 90) or 0, 1)
                    if lat
                    else None,
                    "sample_count": len(lat),
                },
                "tokens": {
                    "p75": round(_percentile([float(x) for x in tok], 75) or 0, 1)
                    if tok
                    else None,
                    "p90": round(_percentile([float(x) for x in tok], 90) or 0, 1)
                    if tok
                    else None,
                    "sample_count": len(tok),
                },
            }
        )

    return {
        "logical_agent_id": lid,
        "dataset_id": dataset_id,
        "sample_count": len(runs),
        "rows": rows_out,
    }
