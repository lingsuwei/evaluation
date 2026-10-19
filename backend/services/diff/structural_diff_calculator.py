"""structural_diff：Span 拓扑边集对比（P5-D MVP）。M-W3-2: 增 per-member 拓扑对比。"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Set, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import RunSpanNode
from services.diff.base import DiffCalculator
from services.diff.diff_types import SCHEMA_VERSION, STRUCTURAL_DIFF
from services.diff.span_aggregate import _node_member


def _aggregate_key(node: RunSpanNode) -> str:
    key = (node.aggregate_key or "").strip()
    return key if key and key != "unknown" else "unknown"


def extract_topology_edges(spans: Iterable[RunSpanNode]) -> Set[Tuple[str, str]]:
    """父 aggregate_key → 子 aggregate_key 边集（根用 __root__）。"""
    by_id = {s.id: s for s in spans}
    edges: Set[Tuple[str, str]] = set()
    for node in spans:
        child_key = _aggregate_key(node)
        parent = by_id.get(node.parent_id) if node.parent_id else None
        parent_key = _aggregate_key(parent) if parent else "__root__"
        edges.add((parent_key, child_key))
    return edges


def extract_member_topology_edges(
    spans: Iterable[RunSpanNode],
) -> Dict[str, Set[Tuple[str, str]]]:
    """按 member 切片提取拓扑边集（M-W3-2）。

    返回: {member_name: edge_set}
    """
    spans_list = list(spans)
    by_member: Dict[str, List[RunSpanNode]] = {}
    for node in spans_list:
        member = _node_member(node)
        by_member.setdefault(member, []).append(node)

    out: Dict[str, Set[Tuple[str, str]]] = {}
    for member, member_spans in by_member.items():
        out[member] = extract_topology_edges(member_spans)
    return out


def merge_structural_diff(
    left_edges: Set[Tuple[str, str]], right_edges: Set[Tuple[str, str]]
) -> Dict[str, Any]:
    only_left = sorted(left_edges - right_edges)
    only_right = sorted(right_edges - left_edges)
    shared = sorted(left_edges & right_edges)
    return {
        "edge_count_left": len(left_edges),
        "edge_count_right": len(right_edges),
        "shared_edges": [{"parent": p, "child": c} for p, c in shared],
        "only_left_edges": [{"parent": p, "child": c} for p, c in only_left],
        "only_right_edges": [{"parent": p, "child": c} for p, c in only_right],
        "same_topology": not only_left and not only_right,
    }


def compact_structural_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """structural_diff 紧凑摘要（任务报表 / 实验 highlights）。"""
    only_left = payload.get("only_left_edges") or []
    only_right = payload.get("only_right_edges") or []
    same = bool(payload.get("same_topology"))
    parts: List[str] = []
    if same:
        parts.append("拓扑一致")
    else:
        if only_left:
            parts.append(f"仅左 {len(only_left)} 边")
        if only_right:
            parts.append(f"仅右 {len(only_right)} 边")
    return {
        "same_topology": same,
        "only_left_count": len(only_left),
        "only_right_count": len(only_right),
        "edge_count_left": payload.get("edge_count_left"),
        "edge_count_right": payload.get("edge_count_right"),
        "summary": "；".join(parts) if parts else "拓扑对比",
    }


class StructuralDiffCalculator(DiffCalculator):
    diff_type = STRUCTURAL_DIFF
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

        # overall topology diff
        result = merge_structural_diff(
            extract_topology_edges(left_spans),
            extract_topology_edges(right_spans),
        )

        # M-W3-2: per-member topology diff
        left_by_member = extract_member_topology_edges(left_spans)
        right_by_member = extract_member_topology_edges(right_spans)
        members = sorted(set(left_by_member) | set(right_by_member))
        by_member: Dict[str, Any] = {}
        for member in members:
            by_member[member] = merge_structural_diff(
                left_by_member.get(member, set()),
                right_by_member.get(member, set()),
            )
        result["by_member"] = by_member
        return result
