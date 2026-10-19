"""
过程点批量评估 → checkpoint_results（P2 E-01～E-03）。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, CheckpointResult, ProcessCheckpointTemplate, RunSpanNode
from logger import log
from services.checkpoint_match_rules import (
    MatchRulesValidationError,
    evaluate_checkpoint_on_trace,
    validate_match_rules,
)

# === 模块级模板缓存 ===
# 同 logical_agent_id 的模板在任务执行期间不变，缓存避免 300 题 × 1 次查询的 N+1。
# expire_on_commit=False 保证 ORM 列属性在 session 关闭后仍可读。
_checkpoint_template_cache: Dict[str, List[ProcessCheckpointTemplate]] = {}
_checkpoint_template_cache_lock = asyncio.Lock()


def clear_checkpoint_template_cache() -> None:
    """清空模板缓存。在模板 CRUD（创建/更新/删除/重排）后调用。"""
    _checkpoint_template_cache.clear()


def span_node_to_observation(node: RunSpanNode) -> Dict[str, Any]:
    """将物化后的 span 节点转为 match_rules 可用的 observation 形态。"""
    attrs = node.attributes if isinstance(node.attributes, dict) else {}
    return {
        "id": node.observation_id or node.id,
        "name": node.name,
        "type": node.node_type,
        "metadata": attrs,
        "_step_index": node.step_index,
    }


def _order_ok_from_span_nodes(
    matched_observation_id: Optional[str],
    order: Optional[int],
    nodes: List[RunSpanNode],
) -> Optional[bool]:
    if order is None or not matched_observation_id:
        return None
    for node in nodes:
        oid = node.observation_id or node.id
        if oid == matched_observation_id:
            if node.step_index is None:
                return None
            return (node.step_index + 1) == order
    return None


def compute_checkpoint_passed(
    *,
    hit: bool,
    required: bool,
    order_ok: Optional[bool],
) -> bool:
    if required and not hit:
        return False
    if order_ok is False:
        return False
    if not required and not hit:
        return True
    return True


def evaluate_template_on_nodes(
    template: ProcessCheckpointTemplate,
    nodes: List[RunSpanNode],
) -> Dict[str, Any]:
    """对单模板在 span 节点上评估（优先 JSONPath / attributes）。"""
    if (template.match_mode or "rule") != "rule":
        return {
            "checkpoint_id": template.id,
            "checkpoint_name": template.name,
            "skipped": True,
            "skip_reason": "仅 rule 模式支持 Builder 批量",
            "hit": False,
            "passed": not bool(template.required),
            "evidence": {},
        }

    try:
        raw_rules = json.loads(template.match_rules or "{}")
        match_rules = validate_match_rules(raw_rules)
    except (json.JSONDecodeError, MatchRulesValidationError) as e:
        return {
            "checkpoint_id": template.id,
            "checkpoint_name": template.name,
            "skipped": True,
            "skip_reason": f"match_rules 无效: {e}",
            "hit": False,
            "passed": False,
            "evidence": {},
        }

    observations = [span_node_to_observation(n) for n in nodes]
    enforce_order = bool(getattr(template, "enforce_order", False))
    result = evaluate_checkpoint_on_trace(
        checkpoint_name=template.name,
        order=template.order,
        required=bool(template.required),
        match_rules=match_rules,
        observations=observations,
    )
    order_ok: Optional[bool] = None
    if enforce_order and template.order is not None and result["hit"]:
        order_ok = _order_ok_from_span_nodes(
            result.get("matched_observation_id"),
            template.order,
            nodes,
        )
        result["order_ok"] = order_ok

    passed = compute_checkpoint_passed(
        hit=bool(result["hit"]),
        required=bool(template.required),
        order_ok=order_ok,
    )
    return {
        "checkpoint_id": template.id,
        "checkpoint_name": template.name,
        "skipped": False,
        "hit": result["hit"],
        "passed": passed,
        "required": bool(template.required),
        "enforce_order": enforce_order,
        "order_ok": order_ok,
        "matched_count": result.get("matched_count", 0),
        "evidence": result.get("evidence") or {},
    }


async def load_enabled_templates(
    session: AsyncSession,
    logical_agent_id: str,
) -> List[ProcessCheckpointTemplate]:
    lid = (logical_agent_id or "").strip()
    if not lid:
        return []
    result = await session.execute(
        select(ProcessCheckpointTemplate)
        .where(
            ProcessCheckpointTemplate.logical_agent_id == lid,
            ProcessCheckpointTemplate.enabled.is_(True),
        )
        .order_by(
            ProcessCheckpointTemplate.order,
            ProcessCheckpointTemplate.name,
        )
    )
    return list(result.scalars().all())


async def load_enabled_templates_cached(
    session: AsyncSession,
    logical_agent_id: str,
) -> List[ProcessCheckpointTemplate]:
    """带模块级缓存的 load_enabled_templates。同 logical_agent_id 只查一次 DB。

    模板在任务执行期间不变；CRUD 操作后需调用 clear_checkpoint_template_cache()。
    """
    lid = (logical_agent_id or "").strip()
    if not lid:
        return []
    if lid in _checkpoint_template_cache:
        return _checkpoint_template_cache[lid]
    async with _checkpoint_template_cache_lock:
        if lid not in _checkpoint_template_cache:
            _checkpoint_template_cache[lid] = await load_enabled_templates(session, lid)
        return _checkpoint_template_cache[lid]


async def _replace_checkpoint_results(
    session: AsyncSession,
    agent_run_id: str,
    rows: List[Dict[str, Any]],
) -> int:
    await session.execute(
        delete(CheckpointResult).where(CheckpointResult.agent_run_id == agent_run_id)
    )
    for row in rows:
        session.add(
            CheckpointResult(
                id=f"cpr_{uuid.uuid4().hex[:12]}",
                agent_run_id=agent_run_id,
                checkpoint_id=row["checkpoint_id"],
                passed=bool(row["passed"]),
                evidence=row.get("evidence"),
            )
        )
    await session.flush()
    return len(rows)


async def batch_evaluate_checkpoints_for_run(
    session: AsyncSession,
    run: AgentRun,
    span_nodes: List[RunSpanNode],
    *,
    _preloaded_templates: Optional[List[ProcessCheckpointTemplate]] = None,
) -> List[Dict[str, Any]]:
    """
    按 run.logical_agent_id 加载模板，在 span 节点上批量评估并写入 checkpoint_results。
    可传入 _preloaded_templates 跳过模板查询。
    """
    if _preloaded_templates is not None:
        templates = _preloaded_templates
    else:
        templates = await load_enabled_templates_cached(session, run.logical_agent_id or "")
    if not templates:
        return []

    evaluations: List[Dict[str, Any]] = []
    persist_rows: List[Dict[str, Any]] = []
    for tpl in templates:
        ev = evaluate_template_on_nodes(tpl, span_nodes)
        evaluations.append(ev)
        if ev.get("skipped"):
            continue
        persist_rows.append(
            {
                "checkpoint_id": ev["checkpoint_id"],
                "passed": ev["passed"],
                "evidence": {
                    **(ev.get("evidence") or {}),
                    "hit": ev.get("hit"),
                    "order_ok": ev.get("order_ok"),
                    "matched_count": ev.get("matched_count"),
                    "checkpoint_name": ev.get("checkpoint_name"),
                },
            }
        )

    count = await _replace_checkpoint_results(session, run.id, persist_rows)
    log(
        "INFO",
        "CHECKPOINT_BATCH",
        "过程点批量落库",
        agent_run_id=run.id,
        logical_agent_id=run.logical_agent_id,
        templates=len(templates),
        persisted=count,
    )
    return evaluations


def warn_legacy_name_only_rules(match_rules: Dict[str, Any]) -> Optional[str]:
    """E-04 / A5：若规则仅 observation_name，提示迁移 JSONPath + eval.operation.id。"""
    rules = match_rules.get("rules") or []
    if not rules:
        return None
    has_eval_op = any(
        r.get("type") == "jsonpath"
        and "eval.operation.id" in str(r.get("path", ""))
        for r in rules
    )
    if has_eval_op:
        return None  # 已使用 eval.operation.id，无需迁移
    if all(r.get("type") == "observation_name" for r in rules):
        return (
            "建议优先使用 jsonpath（$.attributes.eval.operation.id）匹配，"
            "observation_name 仅作兼容预览"
        )
    return None


def has_v2_jsonpath_rules(match_rules: Dict[str, Any]) -> bool:
    """A5：判断 match_rules 是否包含 v2 JSONPath 规则（含 eval.operation.id）。"""
    rules = match_rules.get("rules") or []
    return any(
        r.get("type") == "jsonpath"
        and "eval.operation.id" in str(r.get("path", ""))
        for r in rules
    )
