"""
MemberTraceSlicer — 按 agent.member.name 切 RunSpanNode 树为 N 棵子树。

M-W2-3: per-member 统计物化 → run_metrics（dims={"member_name":..., "role":...}）
M-W2-3 (future): per-member DeepEval agentic 指标（step_efficiency / plan_adherence / plan_quality）

设计参照：
- materialize_call_stats_for_run（per-span 聚合 → upsert_fn → run_metrics）
- build_trace_dict（agentic.py — 构造 DeepEval trace dict）
- batch_evaluate_checkpoints_for_run（批量评估范式）
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import RunSpanNode

LOGGER = logging.getLogger("evaluation.member_trace_slicer")


def slice_by_member(
    span_nodes: Sequence[RunSpanNode],
) -> Dict[str, List[RunSpanNode]]:
    """按 agent.member.name 切 span 树为 N 棵子树。

    无 agent.member.name 的节点（Leader 观测）不纳入任何子树。
    """
    slices: Dict[str, List[RunSpanNode]] = {}
    for node in span_nodes:
        attrs = node.attributes if isinstance(node.attributes, dict) else {}
        member_name = attrs.get("agent.member.name")
        if member_name and str(member_name).strip():
            slices.setdefault(str(member_name), []).append(node)
    return slices


def get_member_role(nodes: List[RunSpanNode]) -> str:
    """从子树节点中提取 member role（取第一个非空值）。"""
    for node in nodes:
        attrs = node.attributes if isinstance(node.attributes, dict) else {}
        role = attrs.get("agent.role")
        if role and str(role).strip():
            return str(role)
    return "member"


def build_member_trace_dict(
    member_nodes: List[RunSpanNode],
    task_input: str = "",
    output: str = "",
) -> Dict[str, Any]:
    """构造 DeepEval 兼容 trace dict（{name, input, output, children}）。

    借鉴 agentic.py build_trace_dict 思路，但用 onto-eval 物化树替换单 Agent 扁平 children。
    供未来 AgenticMetricsEvaluator 消费。
    """
    children: List[Dict[str, Any]] = []
    for node in member_nodes:
        attrs = node.attributes if isinstance(node.attributes, dict) else {}
        tool_name = attrs.get("agent.tool.name") or node.name or "unknown"
        child: Dict[str, Any] = {
            "name": tool_name,
            "input": {"inputParameters": attrs},
            "output": node.summary_text or "",
        }
        if node.latency_ms is not None:
            child["latency_ms"] = node.latency_ms
        children.append(child)

    return {
        "name": "agent",
        "input": {"input": task_input},
        "output": output,
        "children": children,
    }


def _summarize_member_stats(
    nodes: List[RunSpanNode],
) -> Dict[str, Any]:
    """从子树节点聚合 per-member 统计。"""
    tool_count = 0
    llm_count = 0
    team_event_count = 0
    total_latency_ms: Optional[float] = None

    for node in nodes:
        attrs = node.attributes if isinstance(node.attributes, dict) else {}
        kind = attrs.get("agent.step.kind", "")
        node_type = (node.node_type or "").lower()

        if kind == "tool" or node_type == "tool":
            tool_count += 1
        elif kind == "llm" or node_type == "generation":
            llm_count += 1
        elif kind == "team_tool":
            team_event_count += 1

        if node.latency_ms is not None:
            total_latency_ms = (total_latency_ms or 0) + node.latency_ms

    return {
        "tool_count": tool_count,
        "llm_count": llm_count,
        "team_event_count": team_event_count,
        "total_latency_ms": total_latency_ms,
        "span_count": len(nodes),
    }


async def materialize_member_stats(
    session: AsyncSession,
    agent_run_id: str,
    nodes: Sequence[RunSpanNode],
    upsert_fn: Callable,
) -> Dict[str, Any]:
    """per-member 统计物化 → run_metrics。

    入参参照 materialize_call_stats_for_run 范式：
    (session, agent_run_id, nodes, upsert_fn) → 聚合 → upsert_fn → run_metrics。
    """
    member_slices = slice_by_member(nodes)
    if not member_slices:
        return {}

    results: Dict[str, Any] = {}

    for member_name, member_nodes in member_slices.items():
        role = get_member_role(member_nodes)
        stats = _summarize_member_stats(member_nodes)
        dims = {"member_name": member_name, "role": role}

        # Per-member runtime 指标（source="runtime"，无需 LLM）
        await upsert_fn(
            session,
            agent_run_id,
            "runtime.member.tool_count",
            value_num=float(stats["tool_count"]),
            dims=dims,
        )
        await upsert_fn(
            session,
            agent_run_id,
            "runtime.member.llm_count",
            value_num=float(stats["llm_count"]),
            dims=dims,
        )
        await upsert_fn(
            session,
            agent_run_id,
            "runtime.member.team_event_count",
            value_num=float(stats["team_event_count"]),
            dims=dims,
        )
        if stats["total_latency_ms"] is not None:
            await upsert_fn(
                session,
                agent_run_id,
                "runtime.member.total_latency_ms",
                value_num=float(stats["total_latency_ms"]),
                dims=dims,
            )
        await upsert_fn(
            session,
            agent_run_id,
            "runtime.member.span_count",
            value_num=float(stats["span_count"]),
            dims=dims,
        )

        results[member_name] = {"role": role, **stats}

    LOGGER.info(
        "MEMBER_TRACE_SLICER: %d members materialized for run %s",
        len(member_slices),
        agent_run_id,
    )
    return results
