"""按 aggregate_key 提取语义字段（P5 Tier 0）。"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from database import RunSpanNode


def index_semantics_by_aggregate_key(
    nodes: Iterable[RunSpanNode],
) -> Dict[str, Dict[str, Any]]:
    """每个 aggregate_key 取 step_index 最小节点的 semantic_role / summary_text。"""
    indexed: Dict[str, RunSpanNode] = {}
    for node in nodes:
        key = (node.aggregate_key or "").strip()
        if not key or key == "unknown":
            continue
        prev = indexed.get(key)
        if prev is None:
            indexed[key] = node
            continue
        prev_idx = prev.step_index if prev.step_index is not None else 10**9
        cur_idx = node.step_index if node.step_index is not None else 10**9
        if cur_idx < prev_idx:
            indexed[key] = node
    out: Dict[str, Dict[str, Any]] = {}
    for key, node in indexed.items():
        out[key] = {
            "aggregate_key": key,
            "semantic_role": (node.semantic_role or "").strip() or None,
            "summary_text": (node.summary_text or "").strip() or None,
            "observation_id": node.observation_id,
        }
    return out


def merge_semantic_diff(
    left: Dict[str, Dict[str, Any]],
    right: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    left_keys = set(left)
    right_keys = set(right)
    align = sorted(left_keys & right_keys)
    only_left = sorted(left_keys - right_keys)
    only_right = sorted(right_keys - left_keys)
    nodes: List[Dict[str, Any]] = []
    role_mismatch: List[str] = []
    summary_mismatch: List[str] = []
    for key in align:
        l = left[key]
        r = right[key]
        same_role = l.get("semantic_role") == r.get("semantic_role")
        same_summary = l.get("summary_text") == r.get("summary_text")
        if not same_role:
            role_mismatch.append(key)
        if not same_summary:
            summary_mismatch.append(key)
        nodes.append(
            {
                "aggregate_key": key,
                "left": {
                    "semantic_role": l.get("semantic_role"),
                    "summary_text": l.get("summary_text"),
                },
                "right": {
                    "semantic_role": r.get("semantic_role"),
                    "summary_text": r.get("summary_text"),
                },
                "same_role": same_role,
                "same_summary": same_summary,
            }
        )
    return {
        "tier": "0",
        "nodes": nodes,
        "only_left_aggregate_keys": only_left,
        "only_right_aggregate_keys": only_right,
        "align_aggregate_keys": align,
        "role_mismatch_keys": role_mismatch,
        "summary_mismatch_keys": summary_mismatch,
    }
