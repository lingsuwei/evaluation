"""Agent 注册时预置 onto-agent 默认识别过程点（Phase1，仅 Agent 过程点，不含场景子步骤）。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Agent, ProcessCheckpointTemplate
from logger import log
from services.agent_identity import default_logical_agent_id
from services.checkpoint_match_rules import MatchRulesValidationError, validate_match_rules
from services.checkpoint_reference_service import get_onto_agent_baseline_specs

_TEMPLATE_SET = "onto-agent-default-baseline"


def is_onto_agent_inference_config(config: Optional[Dict[str, Any]]) -> bool:
    """推断是否为 onto-agent 类推理端点（/agentic/*）。"""
    if not config or not isinstance(config, dict):
        return False
    endpoint = str(config.get("endpoint") or "").lower()
    url = str(config.get("url") or "").lower()
    if "/agentic" in endpoint:
        return True
    if "/agentic" in url:
        return True
    return False


async def _ensure_agent_logical_id(session: AsyncSession, agent: Agent) -> str:
    lid = (agent.logical_agent_id or "").strip()
    if not lid:
        lid = default_logical_agent_id(agent)
        agent.logical_agent_id = lid
        await session.flush()
    return lid


async def logical_agent_has_checkpoints(session: AsyncSession, logical_agent_id: str) -> bool:
    result = await session.execute(
        select(ProcessCheckpointTemplate.id)
        .where(ProcessCheckpointTemplate.logical_agent_id == logical_agent_id)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def seed_onto_agent_baseline_checkpoints(
    session: AsyncSession,
    agent: Agent,
    *,
    enabled: bool = True,
) -> Dict[str, Any]:
    """
    为 Agent 所属 logical_agent 写入 5 条 onto-agent 必达过程点。
    若该 logical_agent 已有过程点或未启用 seed，则跳过。
    """
    if not enabled:
        return {"created": 0, "skipped": True, "reason": "disabled"}

    inference_cfg = None
    if agent.inference_config:
        try:
            inference_cfg = json.loads(agent.inference_config)
        except json.JSONDecodeError:
            inference_cfg = None

    if not is_onto_agent_inference_config(inference_cfg):
        return {"created": 0, "skipped": True, "reason": "not_onto_agent_endpoint"}

    logical_agent_id = await _ensure_agent_logical_id(session, agent)

    if await logical_agent_has_checkpoints(session, logical_agent_id):
        return {"created": 0, "skipped": True, "reason": "checkpoints_exist"}

    existing_names = set(
        (
            await session.execute(
                select(ProcessCheckpointTemplate.name).where(
                    ProcessCheckpointTemplate.logical_agent_id == logical_agent_id
                )
            )
        )
        .scalars()
        .all()
    )

    created = 0
    now = datetime.utcnow()
    for spec in get_onto_agent_baseline_specs():
        name = str(spec.get("name") or "").strip()
        if not name or name in existing_names:
            continue
        try:
            rules = validate_match_rules(spec.get("match_rules") or {})
        except MatchRulesValidationError as e:
            log("WARNING", "CHECKPOINT_SEED", f"跳过无效规则模板 {name}: {e}")
            continue

        row = ProcessCheckpointTemplate(
            id=f"cp_{uuid.uuid4().hex[:12]}",
            logical_agent_id=logical_agent_id,
            name=name,
            description=spec.get("description"),
            order=spec.get("order"),
            enforce_order=bool(spec.get("enforce_order", False)),
            required=bool(spec.get("required", True)),
            match_rules=json.dumps(rules, ensure_ascii=False),
            score_weight=float(spec.get("score_weight") or 1.0),
            match_mode=str(spec.get("match_mode") or "rule"),
            template_set_name=_TEMPLATE_SET,
            enabled=bool(spec.get("enabled", True)),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        existing_names.add(name)
        created += 1

    if created:
        await session.flush()
        log(
            "INFO",
            "CHECKPOINT_SEED",
            "预置 onto-agent 过程点",
            agent_id=agent.id,
            logical_agent_id=logical_agent_id,
            created=created,
        )

    return {"created": created, "skipped": created == 0, "reason": None if created else "no_templates"}
