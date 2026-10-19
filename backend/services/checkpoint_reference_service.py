"""onto-agent 过程点参考模板（M2 识别 JSON）加载与按 Agent 初始化。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Agent, ProcessCheckpointTemplate
from logger import log
from services.agent_identity import resolve_logical_agent_id_by_name  # raises HTTPException
from services.checkpoint_match_rules import MatchRulesValidationError, validate_match_rules

_RESOURCES = Path(__file__).resolve().parent.parent / "resources" / "checkpoints"
_BASELINE_JSON = _RESOURCES / "onto-agent-baseline.json"
_REFERENCE_JSON = _RESOURCES / "onto-agent-reference.json"
_BASELINE_SET = "onto-agent-default-baseline"
_REFERENCE_SET = "onto-agent-reference"


def _load_pack(path: Path) -> Dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} 必须为 JSON 对象")
    templates = raw.get("templates")
    if not isinstance(templates, list):
        raise ValueError(f"{path.name} 缺少 templates 数组")
    return raw


def get_onto_agent_baseline_specs() -> List[Dict[str, Any]]:
    return list(_load_pack(_BASELINE_JSON).get("templates") or [])


def get_onto_agent_reference_catalog() -> Dict[str, Any]:
    """供过程点配置页展示的参考模板目录。"""
    pack = _load_pack(_REFERENCE_JSON)
    templates = pack.get("templates") or []
    return {
        "id": "onto-agent",
        "description": pack.get("description"),
        "source": pack.get("source"),
        "template_set_name": pack.get("template_set_name") or _REFERENCE_SET,
        "count": len(templates),
        "templates": [
            {
                "name": t.get("name"),
                "description": t.get("description"),
                "order": t.get("order"),
                "required": bool(t.get("required", True)),
                "score_weight": float(t.get("score_weight") or 1.0),
                "match_mode": t.get("match_mode") or "rule",
                "enabled": bool(t.get("enabled", True)),
                "enforce_order": bool(t.get("enforce_order", False)),
                "match_rules": t.get("match_rules") or {},
            }
            for t in templates
            if isinstance(t, dict) and (t.get("name") or "").strip()
        ],
    }


async def _existing_names_for_logical(
    session: AsyncSession,
    logical_agent_id: str,
) -> set[str]:
    rows = (
        await session.execute(
            select(ProcessCheckpointTemplate.name).where(
                ProcessCheckpointTemplate.logical_agent_id == logical_agent_id
            )
        )
    ).scalars().all()
    return {str(n).strip() for n in rows if n}


async def _insert_specs(
    session: AsyncSession,
    *,
    logical_agent_id: str,
    specs: List[Dict[str, Any]],
    template_set_name: str,
    only_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    existing = await _existing_names_for_logical(session, logical_agent_id)
    allow = {n.strip() for n in only_names if (n or "").strip()} if only_names else None
    created_names: List[str] = []
    skipped_names: List[str] = []
    now = datetime.utcnow()

    for spec in specs:
        name = str(spec.get("name") or "").strip()
        if not name:
            continue
        if allow is not None and name not in allow:
            continue
        if name in existing:
            skipped_names.append(name)
            continue
        try:
            rules = validate_match_rules(spec.get("match_rules") or {})
        except MatchRulesValidationError as e:
            log("WARNING", "CHECKPOINT_REF", f"跳过无效参考模板 {name}: {e}")
            skipped_names.append(name)
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
            template_set_name=template_set_name,
            enabled=bool(spec.get("enabled", True)),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        existing.add(name)
        created_names.append(name)

    if created_names:
        await session.flush()
        log(
            "INFO",
            "CHECKPOINT_REF",
            "初始化参考过程点",
            logical_agent_id=logical_agent_id,
            created=len(created_names),
        )

    return {
        "created": len(created_names),
        "created_names": created_names,
        "skipped_names": skipped_names,
        "logical_agent_id": logical_agent_id,
    }


async def seed_onto_agent_reference_for_agent_name(
    session: AsyncSession,
    *,
    agent_name: str,
    template_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """将 M2 参考模板写入指定 Agent（按 logical_agent_id，仅补缺，不覆盖已有同名）。"""
    name = (agent_name or "").strip()
    logical_agent_id = await resolve_logical_agent_id_by_name(session, name)

    pack = _load_pack(_REFERENCE_JSON)
    specs = [s for s in (pack.get("templates") or []) if isinstance(s, dict)]
    template_set = str(pack.get("template_set_name") or _REFERENCE_SET)

    result = await _insert_specs(
        session,
        logical_agent_id=logical_agent_id,
        specs=specs,
        template_set_name=template_set,
        only_names=template_names,
    )
    result["agent_name"] = name
    return result
