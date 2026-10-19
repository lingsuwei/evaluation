"""Span 节点按 aggregate_key 聚合（cost / event_agg 共用）。M-W3-2: 增 per-member 聚合。"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

from database import RunSpanNode


def _node_member(node: RunSpanNode) -> str:
    """从节点属性提取 member 名（M-W3-2）。"""
    attrs = node.attributes if isinstance(node.attributes, dict) else {}
    m = attrs.get("agent.member.name")
    return str(m).strip() if m and str(m).strip() else "_overall"


def aggregate_span_nodes(nodes: Iterable[RunSpanNode]) -> Dict[str, Dict[str, Any]]:
    """按 aggregate_key 汇总 count / latency / tokens。"""
    agg: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        key = (node.aggregate_key or "unknown").strip() or "unknown"
        bucket = agg.setdefault(
            key,
            {
                "aggregate_key": key,
                "attribute_path": "agent.step.id",
                "count": 0,
                "latency_ms_sum": 0,
                "tokens_sum": 0,
            },
        )
        bucket["count"] += 1
        bucket["latency_ms_sum"] += int(node.latency_ms or 0)
        bucket["tokens_sum"] += int(node.tokens or 0)
    return agg


def aggregate_span_nodes_by_member(
    nodes: Iterable[RunSpanNode],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """按 member + aggregate_key 双层聚合（M-W3-2）。

    返回: {member_name: {aggregate_key: {count, latency_ms_sum, tokens_sum}}}
    """
    agg: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for node in nodes:
        member = _node_member(node)
        key = (node.aggregate_key or "unknown").strip() or "unknown"
        member_agg = agg.setdefault(member, {})
        bucket = member_agg.setdefault(
            key,
            {
                "aggregate_key": key,
                "attribute_path": "agent.step.id",
                "count": 0,
                "latency_ms_sum": 0,
                "tokens_sum": 0,
            },
        )
        bucket["count"] += 1
        bucket["latency_ms_sum"] += int(node.latency_ms or 0)
        bucket["tokens_sum"] += int(node.tokens or 0)
    return agg


def merge_member_aggregate_diff(
    left: Dict[str, Dict[str, Dict[str, Any]]],
    right: Dict[str, Dict[str, Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """按 member 合并两侧 aggregate diff（M-W3-2）。

    返回: {member_name: [diff_rows]}（成员级增减一目了然）
    """
    members = sorted(set(left) | set(right))
    out: Dict[str, List[Dict[str, Any]]] = {}
    for member in members:
        l = left.get(member, {})
        r = right.get(member, {})
        out[member] = merge_aggregate_diff(l, r)
    return out


def merge_aggregate_diff(
    left: Dict[str, Dict[str, Any]],
    right: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """合并两侧 aggregate 为 diff 行列表。"""
    keys = sorted(set(left) | set(right))
    rows: List[Dict[str, Any]] = []
    for key in keys:
        l = left.get(key, {"count": 0, "latency_ms_sum": 0, "tokens_sum": 0})
        r = right.get(key, {"count": 0, "latency_ms_sum": 0, "tokens_sum": 0})
        rows.append(
            {
                "aggregate_key": key,
                "attribute_path": l.get("attribute_path") or r.get("attribute_path") or "agent.step.id",
                "left": {
                    "count": l.get("count", 0),
                    "latency_ms_sum": l.get("latency_ms_sum", 0),
                    "tokens_sum": l.get("tokens_sum", 0),
                },
                "right": {
                    "count": r.get("count", 0),
                    "latency_ms_sum": r.get("latency_ms_sum", 0),
                    "tokens_sum": r.get("tokens_sum", 0),
                },
            }
        )
    return rows


def run_latency_ms(run) -> int | None:
    """从 agent_run 起止时间推算 latency。"""
    if run.started_at and run.ended_at:
        delta = run.ended_at - run.started_at
        return max(0, int(delta.total_seconds() * 1000))
    return None
