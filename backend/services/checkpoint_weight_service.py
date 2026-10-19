"""Agent 过程点权重汇总与归一化（对齐场景子步骤 C-2）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import ProcessCheckpointTemplate
from services.agent_identity import resolve_logical_agent_id_by_name


def enabled_checkpoint_weight_summary(
    templates: List[ProcessCheckpointTemplate],
) -> Dict[str, Any]:
    enabled = [t for t in templates if t.enabled]
    total = sum(float(t.score_weight or 0) for t in enabled)
    return {
        "enabled_count": len(enabled),
        "total_count": len(templates),
        "weight_sum": round(total, 4),
        "normalized": len(enabled) > 0 and abs(total - 1.0) <= 0.01,
    }


async def normalize_checkpoint_weights_for_agent(
    session: AsyncSession,
    *,
    agent_name: str,
) -> Dict[str, Any]:
    """将指定 Agent 启用过程点的 score_weight 归一化为合计 1.0。"""
    lid = await resolve_logical_agent_id_by_name(session, agent_name.strip())
    rows = (
        await session.execute(
            select(ProcessCheckpointTemplate).where(
                ProcessCheckpointTemplate.logical_agent_id == lid
            )
        )
    ).scalars().all()
    templates = list(rows)
    enabled = [t for t in templates if t.enabled]
    if not enabled:
        raise ValueError("没有启用的过程点可归一化")

    total = sum(float(t.score_weight or 0) for t in enabled)
    now = datetime.utcnow()
    if total <= 0:
        share = round(1.0 / len(enabled), 4)
        for t in enabled:
            t.score_weight = share
            t.updated_at = now
    else:
        for t in enabled:
            t.score_weight = round(float(t.score_weight or 0) / total, 4)
            t.updated_at = now

    await session.flush()
    summary = enabled_checkpoint_weight_summary(templates)
    return {
        "agent_name": agent_name.strip(),
        "logical_agent_id": lid,
        "weight_summary": summary,
    }
