"""Agent 过程点模板 → 场景子步骤草稿（阶段 E-2）。"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import ProcessCheckpointTemplate
from services.dataset_item_scenario_service import (
    build_default_match_rules,
    upsert_item_template,
)


def _slug_step_key(name: str, order: int) -> str:
    base = re.sub(r"[^\w\u4e00-\u9fff]+", "", name.strip())[:12]
    return f"P{order}" if not base else f"P{order}_{base[:8]}"


def checkpoint_match_rules_to_scenario(raw: Any) -> Dict[str, Any]:
    """将过程点 match_rules 转为场景子步骤 match_rules（保留原规则 + trace_binding）。"""
    rules: Dict[str, Any]
    if isinstance(raw, str):
        try:
            rules = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            rules = {}
    elif isinstance(raw, dict):
        rules = dict(raw)
    else:
        rules = {}

    patterns: List[str] = []
    keywords: List[str] = []
    for item in rules.get("rules") or []:
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or "")
        val = str(item.get("value") or "").strip()
        if not val:
            continue
        if typ == "observation_name":
            patterns.append(val)
        elif typ in ("output_contains", "keyword", "content"):
            keywords.append(val)

    trace_binding: Dict[str, Any] = {
        "span_name_patterns": patterns[:6]
        or ["llm:main_iteration", "llm:main", "tool:"],
        "output_keywords": keywords[:6],
        "label": "from_checkpoint",
    }
    out = dict(rules)
    out["trace_binding"] = trace_binding
    out["imported_from"] = "process_checkpoint"
    return out


def draft_steps_from_checkpoints(
    templates: List[ProcessCheckpointTemplate],
) -> List[Dict[str, Any]]:
    """过程点模板列表 → 场景子步骤 spec（未写库）。"""
    enabled = [t for t in templates if t.enabled]
    enabled.sort(key=lambda t: (t.order if t.order is not None else 9999, t.name))
    if not enabled:
        return []
    n = len(enabled)
    default_w = round(1.0 / n, 4)
    steps: List[Dict[str, Any]] = []
    for idx, tpl in enumerate(enabled, start=1):
        order = tpl.order if tpl.order is not None else idx
        w = float(tpl.score_weight or 0)
        if w <= 0:
            w = default_w
        match_rules = checkpoint_match_rules_to_scenario(tpl.match_rules)
        steps.append(
            {
                "step_key": _slug_step_key(tpl.name, order),
                "name": tpl.name,
                "step_order": order,
                "weight": w,
                "required": bool(tpl.required),
                "enforce_order": bool(getattr(tpl, "enforce_order", False)),
                "enabled": True,
                "gt_criteria": [tpl.description] if tpl.description else [],
                "gt_expected": {"keywords": []},
                "match_rules": match_rules,
            }
        )
    total = sum(float(s["weight"]) for s in steps)
    if total > 0:
        for s in steps:
            s["weight"] = round(float(s["weight"]) / total, 4)
    return steps


async def import_checkpoints_to_item_template(
    session: AsyncSession,
    *,
    dataset_id: str,
    dataset_item_id: str,
    logical_agent_id: str,
    template_set_name: Optional[str] = None,
    replace: bool = True,
) -> Dict[str, Any]:
    q = select(ProcessCheckpointTemplate).where(
        ProcessCheckpointTemplate.logical_agent_id == logical_agent_id,
        ProcessCheckpointTemplate.enabled.is_(True),
    )
    if template_set_name:
        q = q.where(ProcessCheckpointTemplate.template_set_name == template_set_name)
    rows = (await session.execute(q.order_by(ProcessCheckpointTemplate.order))).scalars().all()
    if not rows:
        raise ValueError(f"no enabled checkpoints for agent {logical_agent_id}")

    steps = draft_steps_from_checkpoints(list(rows))
    for s in steps:
        if not (s.get("match_rules") or {}).get("trace_binding"):
            s["match_rules"] = build_default_match_rules(
                gt_expected=s.get("gt_expected"),
                name=s.get("name"),
            )

    name = f"自过程点导入 · {logical_agent_id}"
    if template_set_name:
        name += f" ({template_set_name})"
    tpl = await upsert_item_template(
        session,
        dataset_id=dataset_id,
        dataset_item_id=dataset_item_id,
        name=name,
        description="由 Agent 过程点配置导入，请校对 gt_expected 与权重",
        steps=steps,
        enabled=True,
    )
    return {
        "dataset_id": dataset_id,
        "dataset_item_id": dataset_item_id,
        "logical_agent_id": logical_agent_id,
        "template_set_name": template_set_name,
        "replaced": replace,
        "step_count": len(steps),
        "template_id": tpl.id,
        "scenario_key": tpl.scenario_key,
    }


async def import_checkpoints_to_dataset(
    session: AsyncSession,
    *,
    dataset_id: str,
    logical_agent_id: str,
    template_set_name: Optional[str] = None,
) -> Dict[str, Any]:
    """为评测集每题导入同一套过程点草稿（按题独立模板）。"""
    from services.dataset_item_scenario_service import list_dataset_item_configs

    cfg = await list_dataset_item_configs(session, dataset_id)
    imported: List[str] = []
    for item in cfg.get("items") or []:
        ditem_id = item["dataset_item_id"]
        await import_checkpoints_to_item_template(
            session,
            dataset_id=dataset_id,
            dataset_item_id=ditem_id,
            logical_agent_id=logical_agent_id,
            template_set_name=template_set_name,
            replace=True,
        )
        imported.append(ditem_id)
    return {
        "dataset_id": dataset_id,
        "logical_agent_id": logical_agent_id,
        "imported_item_count": len(imported),
        "imported_item_ids": imported,
    }
