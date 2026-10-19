"""role_roster_diff：工作流角色花名册对比（M-W3-1）。

对比两个 Run 的成员花名册：成员增减 + 角色变更。
role_roster 来源优先级：
1. agent_snapshot.workflow.role_roster（物化层回填）
2. span 树 agent.member.name + agent.role（fallback，运行时推导）
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, RunSpanNode
from services.diff.base import DiffCalculator
from services.diff.diff_types import ROLE_ROSTER_DIFF, SCHEMA_VERSION
from services.member_trace_slicer import slice_by_member, get_member_role


def _extract_roster_from_snapshot(run: AgentRun) -> Optional[List[Dict[str, str]]]:
    """从 agent_snapshot.workflow.role_roster 提取花名册。"""
    if not run.agent_snapshot:
        return None
    try:
        snap = json.loads(run.agent_snapshot)
    except (json.JSONDecodeError, TypeError):
        return None
    workflow = snap.get("workflow") if isinstance(snap, dict) else None
    if not isinstance(workflow, dict):
        return None
    roster = workflow.get("role_roster")
    if not isinstance(roster, list) or not roster:
        return None
    return [
        {
            "member_name": str(r.get("member_name", "")),
            "role": str(r.get("role", "")),
        }
        for r in roster
        if r.get("member_name")
    ]


def _extract_roster_from_spans(spans: List[RunSpanNode]) -> List[Dict[str, str]]:
    """从 span 树 agent.member.name + agent.role 推导花名册（fallback）。"""
    slices = slice_by_member(spans)
    roster: List[Dict[str, str]] = []
    for member_name, member_nodes in slices.items():
        role = get_member_role(member_nodes)
        roster.append({"member_name": member_name, "role": role})
    return sorted(roster, key=lambda r: r["member_name"])


async def _get_run_roster(
    session: AsyncSession, run_id: str
) -> List[Dict[str, str]]:
    """获取 Run 的角色花名册（snapshot 优先，span 树 fallback）。"""
    run = await session.get(AgentRun, run_id)
    if not run:
        return []
    roster = _extract_roster_from_snapshot(run)
    if roster:
        return roster
    # fallback: 从 span 树推导
    spans = (
        await session.execute(
            select(RunSpanNode).where(RunSpanNode.agent_run_id == run_id)
        )
    ).scalars().all()
    return _extract_roster_from_spans(list(spans))


def merge_roster_diff(
    left: List[Dict[str, str]], right: List[Dict[str, str]]
) -> Dict[str, Any]:
    """合并两侧花名册为 diff payload。"""
    left_map = {r["member_name"]: r["role"] for r in left}
    right_map = {r["member_name"]: r["role"] for r in right}
    left_names = set(left_map)
    right_names = set(right_map)

    only_left = sorted(left_names - right_names)
    only_right = sorted(right_names - left_names)
    shared = sorted(left_names & right_names)

    role_changes: List[Dict[str, str]] = []
    for name in shared:
        lr = left_map[name]
        rr = right_map[name]
        if lr != rr:
            role_changes.append(
                {
                    "member_name": name,
                    "left_role": lr,
                    "right_role": rr,
                }
            )

    return {
        "left_roster": left,
        "right_roster": right,
        "left_member_count": len(left),
        "right_member_count": len(right),
        "only_left": [
            {"member_name": n, "role": left_map[n]} for n in only_left
        ],
        "only_right": [
            {"member_name": n, "role": right_map[n]} for n in only_right
        ],
        "shared_members": [
            {"member_name": n, "left_role": left_map[n], "right_role": right_map[n]}
            for n in shared
        ],
        "role_changes": role_changes,
        "same_roster": not only_left and not only_right and not role_changes,
    }


class RoleRosterDiffCalculator(DiffCalculator):
    diff_type = ROLE_ROSTER_DIFF
    schema_version = SCHEMA_VERSION

    async def compute(
        self,
        session: AsyncSession,
        *,
        left_run_id: str,
        right_run_id: str,
    ) -> Dict[str, Any]:
        left_roster = await _get_run_roster(session, left_run_id)
        right_roster = await _get_run_roster(session, right_run_id)
        return merge_roster_diff(left_roster, right_roster)
