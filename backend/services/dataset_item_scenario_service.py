"""按评测集条目（dataset_item）管理场景子步骤模板。"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Dataset, ScenarioEvalStep, ScenarioEvalTemplate
from services.dataset_items import ensure_dataset_item_id, normalize_dataset_items
from services.dataset_scenario_service import load_dataset_items
from services.scenario_eval_service import _serialize_template, get_template_by_key
from services.scenario_model_answer_draft_service import (
    draft_steps_from_model_answer,
    enhance_step_keywords_with_llm,
)
from services.scenario_llm_aggregate_service import (
    collect_llm_aggregate_warnings_for_templates,
    is_llm_configured,
    validate_llm_aggregate_step,
)


def item_scenario_key(dataset_item_id: str) -> str:
    return f"ditem_{dataset_item_id}"


def is_per_item_scenario_key(scenario_key: Optional[str]) -> bool:
    return bool(scenario_key and str(scenario_key).startswith("ditem_"))


async def find_dataset(
    session: AsyncSession, dataset_id_or_name: str
) -> Optional[Dataset]:
    key = (dataset_id_or_name or "").strip()
    if not key:
        return None
    row = await session.get(Dataset, key)
    if row:
        return row
    rows = (await session.execute(select(Dataset))).scalars().all()
    for ds in rows:
        if (ds.name or "").strip() == key:
            return ds
        if key.lower() in (ds.name or "").lower():
            return ds
    return None


async def _get_template_row_by_item(
    session: AsyncSession, dataset_item_id: str
) -> Optional[ScenarioEvalTemplate]:
    sk = item_scenario_key(dataset_item_id)
    return (
        await session.execute(
            select(ScenarioEvalTemplate).where(
                ScenarioEvalTemplate.scenario_key == sk
            )
        )
    ).scalar_one_or_none()


async def _sync_item_scenario_key_in_dataset(
    session: AsyncSession,
    dataset: Dataset,
    dataset_item_id: str,
) -> None:
    items = load_dataset_items(dataset.data)
    changed = False
    sk = item_scenario_key(dataset_item_id)
    for item in items:
        iid = str(item.get("id") or item.get("dataset_item_id") or "").strip()
        if iid == dataset_item_id:
            if item.get("scenario_key") != sk:
                item["scenario_key"] = sk
                changed = True
            break
    if changed:
        dataset.data = json.dumps(items, ensure_ascii=False)


async def upsert_item_template(
    session: AsyncSession,
    *,
    dataset_id: str,
    dataset_item_id: str,
    name: str,
    description: Optional[str] = None,
    steps: List[Dict[str, Any]],
    enabled: bool = True,
) -> ScenarioEvalTemplate:
    sk = item_scenario_key(dataset_item_id)
    tpl = await _get_template_row_by_item(session, dataset_item_id)
    if not tpl:
        tpl = ScenarioEvalTemplate(
            id=f"scen_{uuid.uuid4().hex[:12]}",
            scenario_key=sk,
            name=name,
            description=description,
            dataset_id=dataset_id,
            dataset_item_id=dataset_item_id,
            question_match=None,
            enabled=enabled,
        )
        session.add(tpl)
        await session.flush()
    else:
        tpl.name = name
        tpl.description = description
        tpl.dataset_id = dataset_id
        tpl.dataset_item_id = dataset_item_id
        tpl.enabled = enabled
        await session.execute(
            delete(ScenarioEvalStep).where(ScenarioEvalStep.template_id == tpl.id)
        )
        await session.flush()

    for spec in steps:
        resolved_rules = ensure_step_match_rules(
            match_rules=spec.get("match_rules"),
            gt_expected=spec.get("gt_expected"),
            name=spec.get("name"),
        )
        enabled = bool(spec.get("enabled", True))
        validate_llm_aggregate_step(
            step_key=str(spec["step_key"]),
            name=str(spec.get("name") or ""),
            enabled=enabled,
            gt_expected=spec.get("gt_expected"),
            match_rules=resolved_rules,
        )
        session.add(
            ScenarioEvalStep(
                id=f"step_{tpl.id}_{spec['step_key']}",
                template_id=tpl.id,
                step_key=spec["step_key"],
                name=spec["name"],
                step_order=int(spec["step_order"]),
                weight=float(spec["weight"]),
                required=bool(spec.get("required", True)),
                enabled=enabled,
                enforce_order=bool(spec.get("enforce_order", False)),
                gt_criteria=spec.get("gt_criteria"),
                gt_expected=spec.get("gt_expected"),
                match_rules=resolved_rules,
            )
        )
    await session.flush()

    ds = await session.get(Dataset, dataset_id)
    if ds:
        await _sync_item_scenario_key_in_dataset(session, ds, dataset_item_id)
    return tpl


def build_default_match_rules(
    *,
    gt_expected: Optional[Dict[str, Any]] = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """按题子步骤默认 Trace 绑定（与 get_step_trace_binding 回落一致）。"""
    keywords: List[str] = []
    if isinstance(gt_expected, dict):
        raw = gt_expected.get("keywords") or []
        if isinstance(raw, list):
            keywords = [str(k).strip() for k in raw if str(k).strip()][:8]
    return {
        "trace_binding": {
            "span_name_patterns": ["llm:main_iteration", "llm:main", "tool:"],
            "output_keywords": keywords[:6],
            "label": name or "",
        }
    }


def ensure_step_match_rules(
    *,
    match_rules: Optional[Dict[str, Any]],
    gt_expected: Optional[Dict[str, Any]] = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """保存子步骤时补全 match_rules.trace_binding（不覆盖已有非空 binding）。"""
    base = dict(match_rules) if isinstance(match_rules, dict) else {}
    tb = base.get("trace_binding")
    if isinstance(tb, dict) and tb.get("span_name_patterns"):
        if name and not tb.get("label"):
            tb = dict(tb)
            tb["label"] = name
            base["trace_binding"] = tb
        if "eval_config" not in base:
            base["eval_config"] = {"mode": "keyword"}
        return base
    default = build_default_match_rules(gt_expected=gt_expected, name=name)
    base["trace_binding"] = default["trace_binding"]
    # ★ eval_config 默认值
    if "eval_config" not in base:
        base["eval_config"] = {"mode": "keyword"}
    return base


def enabled_weight_summary(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    enabled = [s for s in steps if s.get("enabled", True)]
    total = sum(float(s.get("weight") or 0) for s in enabled)
    return {
        "enabled_count": len(enabled),
        "total_count": len(steps),
        "weight_sum": round(total, 4),
        "normalized": len(enabled) > 0 and abs(total - 1.0) <= 0.01,
    }


async def normalize_item_step_weights(
    session: AsyncSession, dataset_item_id: str
) -> Dict[str, Any]:
    """将本题启用子步骤权重归一化为合计 1.0。"""
    tpl = await _get_template_row_by_item(session, dataset_item_id)
    if not tpl:
        raise ValueError("item template not found")
    steps = (
        await session.execute(
            select(ScenarioEvalStep).where(ScenarioEvalStep.template_id == tpl.id)
        )
    ).scalars().all()
    enabled = [s for s in steps if s.enabled]
    if not enabled:
        raise ValueError("no enabled steps to normalize")
    total = sum(float(s.weight or 0) for s in enabled)
    if total <= 0:
        share = round(1.0 / len(enabled), 4)
        for s in enabled:
            s.weight = share
    else:
        for s in enabled:
            s.weight = round(float(s.weight or 0) / total, 4)
    await session.flush()
    rows = [_step_to_dict(s) for s in sorted(steps, key=lambda x: x.step_order)]
    summary = enabled_weight_summary(rows)
    return {
        "dataset_item_id": dataset_item_id,
        "steps": rows,
        "weight_summary": summary,
    }


async def sync_dataset_item_scenario_keys(
    session: AsyncSession, dataset_id: str
) -> Dict[str, Any]:
    """将评测集 JSON 中已有模板的条目 scenario_key 同步为 ditem_*。"""
    ds = await find_dataset(session, dataset_id)
    if not ds:
        raise ValueError("dataset not found")
    items = load_dataset_items(ds.data)
    synced: List[str] = []
    for item in items:
        ditem_id = ensure_dataset_item_id(item)
        tpl_row = await _get_template_row_by_item(session, ditem_id)
        if not tpl_row:
            continue
        sk = item_scenario_key(ditem_id)
        if item.get("scenario_key") != sk:
            item["scenario_key"] = sk
            synced.append(ditem_id)
    if synced:
        ds.data = json.dumps(items, ensure_ascii=False)
        await session.flush()
    return {
        "dataset_id": ds.id,
        "synced_count": len(synced),
        "synced_item_ids": synced,
    }


async def backfill_item_step_trace_bindings(
    session: AsyncSession, dataset_item_id: str
) -> Dict[str, Any]:
    """为缺少 trace_binding 的子步骤补全 match_rules（C-1 数据修复）。"""
    tpl = await _get_template_row_by_item(session, dataset_item_id)
    if not tpl:
        raise ValueError("item template not found")
    steps = (
        await session.execute(
            select(ScenarioEvalStep).where(ScenarioEvalStep.template_id == tpl.id)
        )
    ).scalars().all()
    updated = 0
    for s in steps:
        merged = ensure_step_match_rules(
            match_rules=s.match_rules if isinstance(s.match_rules, dict) else None,
            gt_expected=s.gt_expected if isinstance(s.gt_expected, dict) else None,
            name=s.name,
        )
        if merged != s.match_rules:
            s.match_rules = merged
            updated += 1
    await session.flush()
    return {
        "dataset_item_id": dataset_item_id,
        "updated_step_count": updated,
    }


async def list_dataset_item_configs(
    session: AsyncSession, dataset_id: str
) -> Dict[str, Any]:
    ds = await find_dataset(session, dataset_id)
    if not ds:
        raise ValueError("dataset not found")
    await sync_dataset_item_scenario_keys(session, dataset_id)
    items = load_dataset_items(ds.data)
    out_items: List[Dict[str, Any]] = []
    for idx, raw in enumerate(items):
        item = dict(raw)
        ensure_dataset_item_id(item)
        ditem_id = str(item["id"])
        sk = item_scenario_key(ditem_id)
        tpl_row = await _get_template_row_by_item(session, ditem_id)
        detail = None
        if tpl_row:
            steps = (
                await session.execute(
                    select(ScenarioEvalStep)
                    .where(ScenarioEvalStep.template_id == tpl_row.id)
                    .order_by(ScenarioEvalStep.step_order)
                )
            ).scalars().all()
            detail = _serialize_template(tpl_row, steps)
        q = (
            item.get("question")
            or item.get("input")
            or item.get("query")
            or ""
        )
        weight_summary = None
        if detail:
            weight_summary = enabled_weight_summary(detail.get("steps") or [])
        out_items.append(
            {
                "dataset_item_id": ditem_id,
                "question_index": idx,
                "question": str(q),
                "scenario_key": sk,
                "item_scenario_key": item.get("scenario_key"),
                "scenario_key_in_sync": item.get("scenario_key") == sk,
                "template": detail,
                "has_template": detail is not None,
                "weight_summary": weight_summary,
                "llm_aggregate_warnings": (
                    collect_llm_aggregate_warnings_for_templates([detail])
                    if detail
                    else []
                ),
            }
        )
    all_templates = [i["template"] for i in out_items if i.get("template")]
    return {
        "dataset_id": ds.id,
        "dataset_name": ds.name,
        "item_count": len(out_items),
        "llm_configured": is_llm_configured(),
        "warnings": collect_llm_aggregate_warnings_for_templates(all_templates),
        "items": out_items,
    }


async def resolve_scenario_key_for_eval_item(
    session: AsyncSession,
    dataset_id: Optional[str],
    question_id: Optional[int],
    item: Dict[str, Any],
) -> Optional[str]:
    """
    评测 Pipeline 写入 eval_context 用：优先条目显式 key → 已建按题模板 → ditem_* → 数据集默认。
    """
    from services.dataset_scenario_service import (
        get_dataset_default_scenario_key,
        scenario_key_from_item,
    )

    ditem_id = str(item.get("id") or item.get("dataset_item_id") or "").strip()
    if ditem_id:
        resolved = await resolve_item_scenario_key_for_item(session, ditem_id)
        if resolved:
            return resolved
    explicit = scenario_key_from_item(item)
    if explicit:
        tpl = await get_template_by_key(session, explicit)
        if tpl:
            return explicit
    resolved = await resolve_item_scenario_key(session, dataset_id, question_id)
    if resolved:
        return resolved
    if ditem_id:
        return item_scenario_key(ditem_id)
    return await get_dataset_default_scenario_key(session, dataset_id)


async def dataset_has_per_item_templates(
    session: AsyncSession, dataset_id: Optional[str]
) -> bool:
    if not dataset_id:
        return False
    row = (
        await session.execute(
            select(ScenarioEvalTemplate.id)
            .where(
                ScenarioEvalTemplate.dataset_id == dataset_id,
                ScenarioEvalTemplate.enabled.is_(True),
                ScenarioEvalTemplate.scenario_key.like("ditem_%"),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def resolve_item_scenario_key_for_item(
    session: AsyncSession,
    dataset_item_id: str,
) -> Optional[str]:
    """按 dataset_item_id 查找已配置的按题模板 scenario_key。"""
    ditem_id = str(dataset_item_id or "").strip()
    if not ditem_id:
        return None
    tpl_row = await _get_template_row_by_item(session, ditem_id)
    if tpl_row and tpl_row.enabled:
        return item_scenario_key(ditem_id)
    return None


async def resolve_item_scenario_key(
    session: AsyncSession,
    dataset_id: Optional[str],
    question_id: Optional[int],
) -> Optional[str]:
    """条目已配置按题模板时返回 ditem_* scenario_key。"""
    if dataset_id is None or question_id is None:
        return None
    ds = await session.get(Dataset, dataset_id)
    if not ds:
        return None
    items = load_dataset_items(ds.data)
    if question_id < 0 or question_id >= len(items):
        return None
    item = items[question_id]
    ditem_id = str(item.get("id") or item.get("dataset_item_id") or "").strip()
    if not ditem_id:
        return None
    explicit = (item.get("scenario_key") or "").strip()
    if is_per_item_scenario_key(explicit):
        tpl = await get_template_by_key(session, explicit)
        if tpl:
            return explicit
    return await resolve_item_scenario_key_for_item(session, ditem_id)


async def count_existing_item_templates(
    session: AsyncSession, dataset_id: str
) -> int:
    row = (
        await session.execute(
            select(ScenarioEvalTemplate.id)
            .where(
                ScenarioEvalTemplate.dataset_id == dataset_id,
                ScenarioEvalTemplate.scenario_key.like("ditem_%"),
            )
        )
    ).scalars().all()
    return len(row)


async def auto_extract_steps_for_dataset(
    session: AsyncSession, dataset_id: str, *, force: bool = False
) -> Dict[str, Any]:
    """按题从 model_answer（优先）提炼场景子步骤并写入模板。"""
    ds = await find_dataset(session, dataset_id)
    if not ds:
        raise ValueError("dataset not found")
    items = normalize_dataset_items(load_dataset_items(ds.data))
    if not items:
        raise ValueError("dataset has no items")

    if not force:
        existing_count = await count_existing_item_templates(session, ds.id)
        if existing_count > 0:
            return {
                "dataset_id": ds.id,
                "dataset_name": ds.name,
                "conflict": True,
                "existing_template_count": existing_count,
                "message": (
                    f"当前评测集下已有 {existing_count} 道题配置了子步骤模板，"
                    "重新提炼将覆盖已有配置（包括基于Prompt评测、Prompt等手工设置），请慎重操作。"
                ),
            }

    seeded: List[str] = []
    step_counts: List[int] = []
    all_warnings: List[str] = []

    for idx, item in enumerate(items):
        ditem_id = ensure_dataset_item_id(item)
        question = (
            item.get("question")
            or item.get("input")
            or item.get("query")
            or ""
        )
        model_answer = (item.get("model_answer") or "").strip()
        ground_truth = (item.get("ground_truth") or item.get("expected_output") or "").strip()
        if not model_answer and not ground_truth:
            all_warnings.append(f"Q{idx + 1}: 无 model_answer/ground_truth，已跳过")
            continue

        draft = draft_steps_from_model_answer(
            model_answer,
            question=str(question),
            ground_truth=ground_truth or None,
            name=f"Q{idx + 1} " + str(question).strip()[:48],
        )
        steps = draft.get("steps") or []
        if not steps:
            all_warnings.append(f"Q{idx + 1}: 未能提炼子步骤")
            continue

        # LLM 跨步骤辨识关键词增强（失败静默回退到正则关键词）
        steps = await enhance_step_keywords_with_llm(steps)

        await upsert_item_template(
            session,
            dataset_id=ds.id,
            dataset_item_id=ditem_id,
            name=str(draft.get("name") or f"Q{idx + 1}"),
            description=str(draft.get("description") or ""),
            steps=steps,
            enabled=True,
        )
        seeded.append(ditem_id)
        step_counts.append(len(steps))
        for w in draft.get("warnings") or []:
            all_warnings.append(f"Q{idx + 1}: {w}")

    ds.data = json.dumps(items, ensure_ascii=False)
    await session.flush()
    return {
        "dataset_id": ds.id,
        "dataset_name": ds.name,
        "seeded_item_count": len(seeded),
        "seeded_item_ids": seeded,
        "step_counts": step_counts,
        "warnings": all_warnings[:20],
        "source": "model_answer",
    }


async def seed_zc_sample_for_dataset(
    session: AsyncSession, dataset_id: str
) -> Dict[str, Any]:
    """兼容旧接口：等同自动提炼。"""
    return await auto_extract_steps_for_dataset(session, dataset_id)


async def create_item_step(
    session: AsyncSession,
    dataset_item_id: str,
    *,
    step_key: str,
    name: str,
    step_order: Optional[int] = None,
    weight: float = 0.1,
    required: bool = True,
    enabled: bool = True,
    enforce_order: bool = False,
    gt_criteria: Optional[List[str]] = None,
    gt_expected: Optional[Dict[str, Any]] = None,
    match_rules: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    tpl = await _get_template_row_by_item(session, dataset_item_id)
    if not tpl:
        raise ValueError("item template not found; seed or create template first")
    existing = (
        await session.execute(
            select(ScenarioEvalStep).where(
                ScenarioEvalStep.template_id == tpl.id,
                ScenarioEvalStep.step_key == step_key,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise ValueError(f"step_key already exists: {step_key}")
    if step_order is None:
        max_order = (
            await session.execute(
                select(ScenarioEvalStep.step_order)
                .where(ScenarioEvalStep.template_id == tpl.id)
                .order_by(ScenarioEvalStep.step_order.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        step_order = (max_order or 0) + 1
    resolved_rules = ensure_step_match_rules(
        match_rules=match_rules,
        gt_expected=gt_expected,
        name=name,
    )
    validate_llm_aggregate_step(
        step_key=step_key,
        name=name,
        enabled=enabled,
        gt_expected=gt_expected,
        match_rules=resolved_rules,
    )
    row = ScenarioEvalStep(
        id=f"step_{tpl.id}_{step_key}",
        template_id=tpl.id,
        step_key=step_key,
        name=name,
        step_order=step_order,
        weight=weight,
        required=required,
        enabled=enabled,
        enforce_order=enforce_order,
        gt_criteria=gt_criteria,
        gt_expected=gt_expected,
        match_rules=resolved_rules,
    )
    session.add(row)
    await session.flush()
    return _step_to_dict(row)


async def update_item_step(
    session: AsyncSession,
    step_id: str,
    patch: Dict[str, Any],
) -> Dict[str, Any]:
    row = await session.get(ScenarioEvalStep, step_id)
    if not row:
        raise ValueError("step not found")
    for field in (
        "name",
        "step_order",
        "weight",
        "required",
        "enabled",
        "enforce_order",
        "gt_criteria",
        "gt_expected",
        "match_rules",
    ):
        if field in patch and patch[field] is not None:
            setattr(row, field, patch[field])
    row.match_rules = ensure_step_match_rules(
        match_rules=row.match_rules if isinstance(row.match_rules, dict) else None,
        gt_expected=row.gt_expected if isinstance(row.gt_expected, dict) else None,
        name=row.name,
    )
    validate_llm_aggregate_step(
        step_key=row.step_key,
        name=row.name,
        enabled=bool(row.enabled),
        gt_expected=row.gt_expected if isinstance(row.gt_expected, dict) else None,
        match_rules=row.match_rules if isinstance(row.match_rules, dict) else None,
    )
    await session.flush()
    return _step_to_dict(row)


async def delete_item_step(session: AsyncSession, step_id: str) -> None:
    row = await session.get(ScenarioEvalStep, step_id)
    if not row:
        raise ValueError("step not found")
    await session.delete(row)
    await session.flush()


async def reorder_item_steps(
    session: AsyncSession, dataset_item_id: str, step_ids: List[str]
) -> List[Dict[str, Any]]:
    tpl = await _get_template_row_by_item(session, dataset_item_id)
    if not tpl:
        raise ValueError("item template not found")
    steps = (
        await session.execute(
            select(ScenarioEvalStep).where(ScenarioEvalStep.template_id == tpl.id)
        )
    ).scalars().all()
    by_id = {s.id: s for s in steps}
    for order, sid in enumerate(step_ids, start=1):
        if sid in by_id:
            by_id[sid].step_order = order
    await session.flush()
    ordered = sorted(by_id.values(), key=lambda s: s.step_order)
    return [_step_to_dict(s) for s in ordered]


async def load_steps_for_dataset_items(
    session: AsyncSession, dataset_id: str
) -> Dict[str, List[Dict[str, Any]]]:
    """批量加载评测集各 item 的 enabled 子步骤。

    仅返回 enabled=True 的子步骤，按 step_order 升序排列。
    返回 ``{dataset_item_id: [step_dict, ...]}``；无模板/无启用步骤的 item 不出现。
    """
    tpl_rows = (
        await session.execute(
            select(ScenarioEvalTemplate).where(
                ScenarioEvalTemplate.dataset_id == dataset_id,
                ScenarioEvalTemplate.scenario_key.like("ditem_%"),
            )
        )
    ).scalars().all()
    if not tpl_rows:
        return {}

    tpl_by_id = {t.id: t for t in tpl_rows}
    step_rows = (
        await session.execute(
            select(ScenarioEvalStep)
            .where(ScenarioEvalStep.template_id.in_(list(tpl_by_id.keys())))
            .order_by(ScenarioEvalStep.template_id, ScenarioEvalStep.step_order)
        )
    ).scalars().all()

    out: Dict[str, List[Dict[str, Any]]] = {}
    for s in step_rows:
        if not s.enabled:
            continue
        tpl = tpl_by_id.get(s.template_id)
        if not tpl or not tpl.dataset_item_id:
            continue
        out.setdefault(str(tpl.dataset_item_id), []).append(_step_to_dict(s))
    return out


def _step_to_dict(s: ScenarioEvalStep) -> Dict[str, Any]:
    return {
        "id": s.id,
        "step_key": s.step_key,
        "name": s.name,
        "step_order": s.step_order,
        "weight": s.weight,
        "required": s.required,
        "enabled": s.enabled,
        "enforce_order": bool(getattr(s, "enforce_order", False)),
        "gt_criteria": s.gt_criteria,
        "gt_expected": s.gt_expected,
        "match_rules": s.match_rules,
    }
