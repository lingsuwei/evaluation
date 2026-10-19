"""Semantic diff 规则摘要（P5-B4 Tier 2 占位：无 LLM 的规则 Judge）。"""
from __future__ import annotations

from typing import Any, Dict, List


def build_rule_based_judge_summary(payload: Dict[str, Any]) -> str:
    """根据 mismatch 集生成可读摘要（非 LLM）。"""
    parts: List[str] = []
    role_keys = payload.get("role_mismatch_keys") or []
    summary_keys = payload.get("summary_mismatch_keys") or []
    only_left = payload.get("only_left_aggregate_keys") or []
    only_right = payload.get("only_right_aggregate_keys") or []
    if role_keys:
        parts.append(f"角色不一致：{', '.join(role_keys)}")
    if summary_keys:
        parts.append(f"摘要不一致：{', '.join(summary_keys)}")
    if only_left:
        parts.append(f"仅左 Run：{', '.join(only_left)}")
    if only_right:
        parts.append(f"仅右 Run：{', '.join(only_right)}")
    if not parts:
        align = payload.get("align_aggregate_keys") or []
        if align:
            return f"已对齐 {len(align)} 个 aggregate_key，语义字段一致。"
        return "无可用语义节点。"
    return "；".join(parts)
