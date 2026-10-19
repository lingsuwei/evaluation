"""任务关联 Agent 版本摘要（历史列表 / 详情）。"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Agent, EvaluationTask
from services.evaluation_pipeline import parse_task_agent_ids


def _version_label(agent: Agent) -> str:
    """仅返回 Agent.version（历史列表「Agent版本」列，不与 agent_name 重复）。"""
    ver = (agent.version or "").strip()
    if ver:
        return ver
    tag = (getattr(agent, "version_tag", None) or "").strip()
    return tag


async def build_task_agent_summary(
    session: AsyncSession,
    task: EvaluationTask,
) -> Dict[str, Any]:
    """返回 agent_count、agent_versions、展示用短标签。"""
    ids = parse_task_agent_ids(task)
    if not ids and task.agent_id:
        ids = [task.agent_id]

    if not ids:
        return {
            "agent_count": 0,
            "agent_ids": [],
            "agent_versions": [],
            "agent_versions_label": None,
        }

    rows = await session.execute(select(Agent).where(Agent.id.in_(ids)))
    by_id = {a.id: a for a in rows.scalars().all()}
    versions: List[Dict[str, Any]] = []
    labels: List[str] = []
    for aid in ids:
        ag = by_id.get(aid)
        if not ag:
            labels.append(aid[:8])
            versions.append({"id": aid, "name": None, "version": None, "label": aid[:8]})
            continue
        lbl = _version_label(ag)
        labels.append(lbl)
        versions.append(
            {
                "id": ag.id,
                "name": ag.name,
                "version": ag.version,
                "logical_agent_id": ag.logical_agent_id,
                "label": lbl,
            }
        )

    non_empty = [lbl for lbl in labels if lbl]
    if not non_empty:
        short = None
    elif len(non_empty) == 1:
        short = non_empty[0]
    elif len(non_empty) <= 3:
        short = " · ".join(non_empty)
    else:
        short = f"{non_empty[0]} 等 {len(non_empty)} 版"

    return {
        "agent_count": len(ids),
        "agent_ids": ids,
        "agent_versions": versions,
        "agent_versions_label": short,
    }
