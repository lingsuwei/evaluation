"""过程点效能归因（P1）：checkpoint_results + run_span_nodes JOIN，不重跑 match_rules。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, CheckpointResult, ProcessCheckpointTemplate, RunSpanNode
from services.checkpoint_batch_service import load_enabled_templates
from services.eval_context import get_task_id


async def _load_runs_for_task(session: AsyncSession, task_id: str) -> List[AgentRun]:
    tid = (task_id or "").strip()
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


def _matched_obs_id(evidence: Any) -> Optional[str]:
    if not isinstance(evidence, dict):
        return None
    oid = evidence.get("matched_observation_id")
    if oid:
        return str(oid)
    ids = evidence.get("matched_ids")
    if isinstance(ids, list) and ids:
        return str(ids[0])
    return None


async def build_checkpoint_cost_attribution(
    session: AsyncSession,
    task_id: str,
    *,
    logical_agent_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    按过程点模板汇总命中率、平均耗时/Tokens（来自命中 Span 节点）。
    无模板或无 checkpoint_results 时返回 None。
    """
    runs = await _load_runs_for_task(session, task_id)
    if not runs:
        return None

    lid = (logical_agent_id or "").strip()
    if not lid and runs:
        lid = (runs[0].logical_agent_id or "").strip()
    if not lid:
        return None

    templates = await load_enabled_templates(session, lid)
    if not templates:
        return None

    tpl_map = {t.id: t for t in templates}
    run_ids = [r.id for r in runs]

    cp_rows = (
        await session.execute(
            select(CheckpointResult).where(CheckpointResult.agent_run_id.in_(run_ids))
        )
    ).scalars().all()
    if not cp_rows:
        return None

    span_rows = (
        await session.execute(
            select(RunSpanNode).where(RunSpanNode.agent_run_id.in_(run_ids))
        )
    ).scalars().all()
    span_by_obs: Dict[Tuple[str, str], RunSpanNode] = {}
    for node in span_rows:
        if node.observation_id:
            span_by_obs[(node.agent_run_id, node.observation_id)] = node

    agg: Dict[str, Dict[str, Any]] = {
        t.id: {
            "checkpoint_id": t.id,
            "name": t.name,
            "order": t.order,
            "required": bool(t.required),
            "evaluated_runs": 0,
            "hit_count": 0,
            "pass_count": 0,
            "latency_vals": [],
            "token_vals": [],
        }
        for t in templates
    }

    seen_run_cp: set[Tuple[str, str]] = set()
    for row in cp_rows:
        tpl = tpl_map.get(row.checkpoint_id)
        if not tpl:
            continue
        key = (row.agent_run_id, row.checkpoint_id)
        if key in seen_run_cp:
            continue
        seen_run_cp.add(key)

        bucket = agg[row.checkpoint_id]
        bucket["evaluated_runs"] += 1
        evidence = row.evidence if isinstance(row.evidence, dict) else {}
        hit = evidence.get("hit")
        if hit is None:
            hit = True
        if hit:
            bucket["hit_count"] += 1
        if row.passed:
            bucket["pass_count"] += 1

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
        b = agg.get(t.id)
        if not b or b["evaluated_runs"] == 0:
            continue
        ev = b["evaluated_runs"]
        lat = b["latency_vals"]
        tok = b["token_vals"]
        rows_out.append(
            {
                "checkpoint_id": t.id,
                "name": t.name,
                "order": t.order,
                "required": b["required"],
                "hit_rate": round(b["hit_count"] / ev, 4),
                "pass_rate": round(b["pass_count"] / ev, 4),
                "avg_latency_ms": round(sum(lat) / len(lat), 1) if lat else None,
                "avg_tokens": round(sum(tok) / len(tok), 1) if tok else None,
                "evaluated_runs": ev,
            }
        )

    if not rows_out:
        return None

    return {
        "logical_agent_id": lid,
        "template_count": len(templates),
        "rows": rows_out,
    }
