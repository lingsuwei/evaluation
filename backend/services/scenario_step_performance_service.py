"""场景子步骤运行效能汇总（P2）：矩阵 step 列级均时延/Tokens。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import RunSpanNode, ScenarioEvalStep, ScenarioStepSpanRef
from services.observation_latency import resolve_span_node_latency_ms


def _resolve_step_span_node(
    *,
    run_id: str,
    step: Dict[str, Any],
    span_by_run_obs: Dict[Tuple[str, str], RunSpanNode],
    span_by_run_node_id: Dict[Tuple[str, str], RunSpanNode],
    ref_by_run_step_key: Dict[Tuple[str, str], ScenarioStepSpanRef],
    ref_by_run_step_id: Dict[Tuple[str, str], ScenarioStepSpanRef],
) -> Optional[RunSpanNode]:
    """按 span_ref / trace_binding 定位子步骤对应 Span 节点。"""
    step_key = str(step.get("step_key") or "").strip()
    step_id = str(step.get("step_id") or "").strip()

    ref = None
    if step_id:
        ref = ref_by_run_step_id.get((run_id, step_id))
    if ref is None and step_key:
        ref = ref_by_run_step_key.get((run_id, step_key))

    if ref is not None:
        if ref.span_node_id:
            node = span_by_run_node_id.get((run_id, ref.span_node_id))
            if node is not None:
                return node
        if ref.observation_id:
            node = span_by_run_obs.get((run_id, str(ref.observation_id)))
            if node is not None:
                return node

    tb = step.get("trace_binding") if isinstance(step.get("trace_binding"), dict) else {}
    evidence = step.get("evidence") if isinstance(step.get("evidence"), dict) else {}
    obs_id = tb.get("observation_id") or evidence.get("observation_id")
    if obs_id:
        return span_by_run_obs.get((run_id, str(obs_id)))

    span_node_id = tb.get("span_node_id") or evidence.get("span_node_id")
    if span_node_id:
        return span_by_run_node_id.get((run_id, str(span_node_id)))

    return None


async def build_scenario_step_performance_summary(
    session: AsyncSession,
    question_rows: List[Dict[str, Any]],
    member_ids: List[str],
) -> List[Dict[str, Any]]:
    """
    从矩阵题目行的 cells.steps 关联已绑定 Span 节点，按 step_key 汇总均时延/Tokens。

    时延/Tokens 取自 RunSpanNode（含 started_at/ended_at 补全），
    不再将整 Run 总耗时均摊到各子步骤。
    """
    run_ids: set[str] = set()
    step_keys: Dict[str, str] = {}

    for row in question_rows:
        cells = row.get("cells") or {}
        for mid in member_ids:
            cell = cells.get(mid)
            if not cell:
                continue
            rid = cell.get("agent_run_id")
            if rid:
                run_ids.add(rid)
            for step in cell.get("steps") or []:
                sk = step.get("step_key")
                if sk:
                    step_keys[sk] = step.get("name") or sk

    if not run_ids or not step_keys:
        return []

    run_id_list = list(run_ids)
    span_nodes = (
        await session.execute(
            select(RunSpanNode).where(RunSpanNode.agent_run_id.in_(run_id_list))
        )
    ).scalars().all()

    span_by_run_obs: Dict[Tuple[str, str], RunSpanNode] = {}
    span_by_run_node_id: Dict[Tuple[str, str], RunSpanNode] = {}
    for node in span_nodes:
        span_by_run_node_id[(node.agent_run_id, node.id)] = node
        if node.observation_id:
            span_by_run_obs[(node.agent_run_id, str(node.observation_id))] = node

    ref_rows = (
        await session.execute(
            select(ScenarioStepSpanRef, ScenarioEvalStep)
            .join(
                ScenarioEvalStep,
                ScenarioEvalStep.id == ScenarioStepSpanRef.step_id,
            )
            .where(ScenarioStepSpanRef.agent_run_id.in_(run_id_list))
        )
    ).all()

    ref_by_run_step_key: Dict[Tuple[str, str], ScenarioStepSpanRef] = {}
    ref_by_run_step_id: Dict[Tuple[str, str], ScenarioStepSpanRef] = {}
    for ref, step in ref_rows:
        ref_by_run_step_id[(ref.agent_run_id, ref.step_id)] = ref
        if step.step_key:
            ref_by_run_step_key[(ref.agent_run_id, step.step_key)] = ref

    agg_lat: Dict[str, List[float]] = defaultdict(list)
    agg_tok: Dict[str, List[float]] = defaultdict(list)

    for row in question_rows:
        cells = row.get("cells") or {}
        for mid in member_ids:
            cell = cells.get(mid)
            if not cell:
                continue
            rid = cell.get("agent_run_id")
            if not rid:
                continue
            for step in cell.get("steps") or []:
                sk = step.get("step_key")
                if not sk:
                    continue
                node = _resolve_step_span_node(
                    run_id=rid,
                    step=step,
                    span_by_run_obs=span_by_run_obs,
                    span_by_run_node_id=span_by_run_node_id,
                    ref_by_run_step_key=ref_by_run_step_key,
                    ref_by_run_step_id=ref_by_run_step_id,
                )
                if node is None:
                    continue
                lat = resolve_span_node_latency_ms(
                    latency_ms=node.latency_ms,
                    started_at=node.started_at,
                    ended_at=node.ended_at,
                )
                if lat is not None:
                    agg_lat[sk].append(float(lat))
                if node.tokens is not None:
                    agg_tok[sk].append(float(node.tokens))

    out: List[Dict[str, Any]] = []
    for sk, name in sorted(step_keys.items(), key=lambda x: x[0]):
        lats = agg_lat.get(sk) or []
        toks = agg_tok.get(sk) or []
        out.append(
            {
                "step_key": sk,
                "name": name,
                "avg_latency_ms": round(sum(lats) / len(lats), 1) if lats else None,
                "avg_tokens": round(sum(toks) / len(toks), 1) if toks else None,
                "sample_count": max(len(lats), len(toks)),
            }
        )
    return out
