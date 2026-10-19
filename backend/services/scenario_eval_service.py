"""场景 QA 子步骤评测（场景 C）。"""
from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import (
    AgentRun,
    EvaluationResult,
    EvaluationTask,
    ScenarioEvalStep,
    ScenarioEvalTemplate,
    ScenarioStepResult,
)

CORRIDOR_SCENARIO_KEY = "corridor_defense"  # 仅用于清理历史 demo 数据

GENERIC_L0_SCENARIO_KEY = "generic_l0"
GENERIC_L0_TEMPLATE_ID = "scen_generic_l0_v1"

GENERIC_L0_STEPS: List[Dict[str, Any]] = [
    {
        "step_key": "L0-1",
        "name": "理解题意",
        "step_order": 1,
        "weight": 0.25,
        "required": True,
        "gt_criteria": ["正确理解问题与约束"],
        "gt_expected": {"keywords": ["理解", "分析", "问题", "任务"]},
    },
    {
        "step_key": "L0-2",
        "name": "推理与工具",
        "step_order": 2,
        "weight": 0.35,
        "required": True,
        "gt_criteria": ["调用工具或完成中间推理"],
        "gt_expected": {"keywords": ["推理", "工具", "检索", "计算"]},
    },
    {
        "step_key": "L0-3",
        "name": "生成答案",
        "step_order": 3,
        "weight": 0.40,
        "required": True,
        "gt_criteria": ["输出可交付的最终答案"],
        "gt_expected": {"keywords": ["答案", "结论", "综上", "因此"]},
    },
]


def _normalize_text(text: str) -> str:
    return (text or "").replace(" ", "").replace("\n", "")


# 符号归一化：去标点/装饰符号，用于 keyword 宽松匹配
_PUNCT_STRIP_RE = re.compile(r"[\s:：，,。.、！!？?@#~\-→✓✅\[\]（）()【】\"'`]+")


def _normalize_for_keyword_match(s: str) -> str:
    """归一化用于宽松匹配：去空格、标点、装饰符号。"""
    return _PUNCT_STRIP_RE.sub("", s or "")


_KEYWORD_FLEX_PATTERNS: Dict[str, re.Pattern[str]] = {
    "13.41": re.compile(r"13\.4\d*"),
    "144.99": re.compile(r"144\.99\d*"),
}


def _keyword_hit(norm: str, keyword: str, norm_stripped: str = "") -> bool:
    """检查 keyword 是否命中 norm（已 _normalize_text 处理）。

    三级匹配：
    1. 精确子串匹配（去空格后）
    2. 符号归一化后宽松匹配（处理 LM-01:1发 vs LM-01 1发 等格式差异）
    3. 坐标类 flex pattern
    """
    kw = keyword.replace(" ", "")
    if not kw:
        return False
    if kw in norm:
        return True
    # 符号归一化后宽松匹配
    if not norm_stripped:
        norm_stripped = _normalize_for_keyword_match(norm)
    kw_stripped = _normalize_for_keyword_match(kw)
    if kw_stripped and kw_stripped in norm_stripped:
        return True
    pat = _KEYWORD_FLEX_PATTERNS.get(kw)
    if pat is not None:
        return bool(pat.search(norm))
    return False


def _score_required_cmd_ids(
    text: str, cmd_ids: List[str]
) -> Tuple[float, bool, Dict[str, Any]]:
    """要求文本中逐个出现指定 cmd id。"""
    if not cmd_ids:
        return 0.0, False, {"reason": "no_required_cmd_ids"}
    norm = _normalize_text(text)
    norm_stripped = _normalize_for_keyword_match(norm)
    hits = [cid for cid in cmd_ids if _keyword_hit(norm, cid, norm_stripped)]
    missing = [cid for cid in cmd_ids if cid not in hits]
    ratio = len(hits) / len(cmd_ids)
    score = round(ratio, 2)
    passed = len(hits) == len(cmd_ids)
    return score, passed, {
        "matched_keywords": hits,
        "matched_cmd_ids": hits,
        "missing_cmd_ids": missing,
        "match_ratio": round(ratio, 3),
        "score_group": "required_cmd_ids",
        "required_cmd_count": len(cmd_ids),
    }


def _score_keyword_group(
    text: str, keywords: List[str]
) -> Tuple[float, bool, Dict[str, Any]]:
    if not keywords:
        return 0.0, False, {"reason": "no_keywords"}
    norm = _normalize_text(text)
    norm_stripped = _normalize_for_keyword_match(norm)
    hits = [kw for kw in keywords if _keyword_hit(norm, kw, norm_stripped)]
    ratio = len(hits) / len(keywords)
    score = round(ratio, 2)
    passed = ratio >= 0.5 if len(keywords) <= 3 else ratio >= 0.6
    return score, passed, {"matched_keywords": hits, "match_ratio": round(ratio, 3)}


def _score_step(text: str, expected: Optional[Dict[str, Any]]) -> Tuple[float, bool, Dict[str, Any]]:
    if not expected:
        return 0.0, False, {"reason": "no_expected"}

    required_cmd_ids = expected.get("required_cmd_ids") or []
    if required_cmd_ids:
        cmd_score, cmd_passed, cmd_ev = _score_required_cmd_ids(text, list(required_cmd_ids))
        optional_kws = expected.get("keywords") or []
        if optional_kws:
            _, _, kw_ev = _score_keyword_group(text, optional_kws)
            optional_hits = kw_ev.get("matched_keywords") or []
            cmd_ev = {
                **cmd_ev,
                "optional_keywords": optional_kws,
                "optional_matched_keywords": optional_hits,
                "optional_keyword_hit": bool(optional_hits),
            }
        return cmd_score, cmd_passed, cmd_ev

    primary = expected.get("keywords") or []
    alt = expected.get("alt_keywords") or []
    if not primary and not alt:
        return 0.0, False, {"reason": "no_keywords"}

    best_score = 0.0
    best_passed = False
    best_ev: Dict[str, Any] = {"reason": "no_match", "matched_keywords": []}

    for group_name, kws in (("primary", primary), ("alt", alt)):
        if not kws:
            continue
        score, passed, ev = _score_keyword_group(text, kws)
        ev = {**ev, "score_group": group_name}
        if passed or score > best_score:
            best_score, best_passed, best_ev = score, passed, ev
        if passed:
            break

    return best_score, best_passed, best_ev


async def remove_corridor_demo_template(session: AsyncSession) -> bool:
    """删除临时 corridor_defense 演示模板（若存在）。"""
    row = (
        await session.execute(
            select(ScenarioEvalTemplate).where(
                ScenarioEvalTemplate.scenario_key == CORRIDOR_SCENARIO_KEY
            )
        )
    ).scalar_one_or_none()
    if not row:
        return False
    await session.execute(
        delete(ScenarioEvalStep).where(ScenarioEvalStep.template_id == row.id)
    )
    await session.delete(row)
    await session.flush()
    return True


async def ensure_generic_l0_template(session: AsyncSession) -> ScenarioEvalTemplate:
    existing = (
        await session.execute(
            select(ScenarioEvalTemplate).where(
                ScenarioEvalTemplate.scenario_key == GENERIC_L0_SCENARIO_KEY
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    tpl = ScenarioEvalTemplate(
        id=GENERIC_L0_TEMPLATE_ID,
        scenario_key=GENERIC_L0_SCENARIO_KEY,
        name="通用 QA 三步（L0）",
        description="无 GT / 无专用场景时的默认子步骤模板",
        question_match={"keywords": []},
        enabled=True,
    )
    session.add(tpl)
    await session.flush()
    for spec in GENERIC_L0_STEPS:
        session.add(
            ScenarioEvalStep(
                id=f"step_{tpl.id}_{spec['step_key']}",
                template_id=tpl.id,
                step_key=spec["step_key"],
                name=spec["name"],
                step_order=spec["step_order"],
                weight=spec["weight"],
                required=spec["required"],
                gt_criteria=spec.get("gt_criteria"),
                gt_expected=spec.get("gt_expected"),
            )
        )
    await session.flush()
    return tpl


async def get_template_by_key(
    session: AsyncSession, scenario_key: str
) -> Optional[Dict[str, Any]]:
    if scenario_key == GENERIC_L0_SCENARIO_KEY:
        await ensure_generic_l0_template(session)
    row = (
        await session.execute(
            select(ScenarioEvalTemplate).where(
                ScenarioEvalTemplate.scenario_key == scenario_key,
                ScenarioEvalTemplate.enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if not row:
        return None
    steps = (
        await session.execute(
            select(ScenarioEvalStep)
            .where(ScenarioEvalStep.template_id == row.id)
            .order_by(ScenarioEvalStep.step_order)
        )
    ).scalars().all()
    return _serialize_template(row, steps)


async def resolve_scenario_for_run(
    session: AsyncSession, run_id: str
) -> Optional[str]:
    from services.dataset_item_scenario_service import (
        dataset_has_per_item_templates,
        is_per_item_scenario_key,
        item_scenario_key,
        resolve_item_scenario_key,
        resolve_item_scenario_key_for_item,
    )
    from services.dataset_scenario_service import get_item_scenario_key, load_dataset_items
    from services.eval_context import (
        get_dataset_id,
        get_dataset_item_id,
        get_question_id,
        parse_eval_context,
    )

    run = await session.get(AgentRun, run_id)
    if not run:
        return None

    ctx = parse_eval_context(run.eval_context)
    ctx_key = (ctx.get("scenario_key") or "").strip()
    if ctx_key:
        tpl = await get_template_by_key(session, ctx_key)
        if tpl:
            return ctx_key
        if is_per_item_scenario_key(ctx_key):
            await ensure_generic_l0_template(session)
            return GENERIC_L0_SCENARIO_KEY

    dataset_id = get_dataset_id(run) or run.dataset_id
    question_id = get_question_id(run)
    if question_id is None:
        question_id = run.question_id

    ditem_id = get_dataset_item_id(run) or (ctx.get("dataset_item_id") or "").strip()
    if ditem_id:
        item_key = await resolve_item_scenario_key_for_item(session, ditem_id)
        if item_key:
            return item_key

    item_ditem_key = await resolve_item_scenario_key(session, dataset_id, question_id)
    if item_ditem_key:
        return item_ditem_key

    if not ditem_id and dataset_id is not None and question_id is not None:
        from database import Dataset

        ds = await session.get(Dataset, dataset_id)
        if ds:
            items = load_dataset_items(ds.data)
            if 0 <= question_id < len(items):
                ditem_id = str(
                    items[question_id].get("id")
                    or items[question_id].get("dataset_item_id")
                    or ""
                ).strip()
    if ditem_id:
        sk = item_scenario_key(ditem_id)
        tpl = await get_template_by_key(session, sk)
        if tpl:
            return sk
        if await dataset_has_per_item_templates(session, dataset_id):
            await ensure_generic_l0_template(session)
            return GENERIC_L0_SCENARIO_KEY

    item_key, default_key = await get_item_scenario_key(
        session, dataset_id, question_id
    )
    for candidate in (item_key, default_key):
        if candidate:
            tpl = await get_template_by_key(session, candidate)
            if tpl:
                return candidate

    if await dataset_has_per_item_templates(session, dataset_id):
        await ensure_generic_l0_template(session)
        return GENERIC_L0_SCENARIO_KEY

    await ensure_generic_l0_template(session)
    return GENERIC_L0_SCENARIO_KEY


async def score_run_against_scenario(
    session: AsyncSession,
    run_id: str,
    scenario_key: Optional[str] = None,
    *,
    persist: bool = True,
    agent_answer: Optional[str] = None,
    run_obj: Optional[Any] = None,
    _emit_step_event=None,
    _task_id: Optional[str] = None,
    _current_step_ref=None,
    _total_steps: int = 0,
    _preloaded_steps: Optional[List[Any]] = None,
    _preloaded_eval_row: Optional[Any] = None,
    _preloaded_template: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    # 离线 stub run 不在数据库中，从调用方传入
    run = run_obj or await session.get(AgentRun, run_id)
    if not run:
        return None
    # 场景子步骤维度关闭时，不触发实时补算（避免 Pipeline 跳过后又被接口回填）
    if run.task_id:
        _task = await session.get(EvaluationTask, run.task_id)
        if _task and not getattr(_task, "auto_scenario_score", True):
            return None
    resolved_key = (scenario_key or "").strip() or await resolve_scenario_for_run(
        session, run_id
    )
    if not resolved_key:
        return None
    if _preloaded_template is not None:
        tpl_raw = _preloaded_template
    else:
        tpl_raw = await get_template_by_key(session, resolved_key)
    if not tpl_raw:
        return None
    from services.scenario_template_utils import template_with_active_steps_only

    tpl_data = template_with_active_steps_only(tpl_raw)
    if not tpl_data.get("steps"):
        return None
    if agent_answer is None:
        if _preloaded_eval_row is not None:
            eval_row = _preloaded_eval_row
        else:
            eval_row = (
                await session.execute(
                    select(EvaluationResult)
                    .where(EvaluationResult.agent_run_id == run.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
        agent_answer = (eval_row.agent_answer if eval_row else "") or ""
    from services.scenario_step_trace_service import compute_run_scenario_steps
    from database import ScenarioStepResult as _SSR

    _is_stub = str(run.id).startswith("stub_offline_")

    # 优先读取已持久化的打分结果，避免 LLM 重复调用
    if _preloaded_steps is not None:
        existing_steps = _preloaded_steps
    else:
        existing_steps = (
            await session.execute(
                select(_SSR).where(_SSR.agent_run_id == run.id)
            )
        ).scalars().all()

    # 计算当前 run 的步骤偏移量
    _step_offset = _current_step_ref[0] if _current_step_ref else 0
    active_steps_count = len(tpl_data.get("steps") or [])

    if existing_steps and persist:
        if _emit_step_event is None:
            # 快路径：已有持久化结果且非实时评测，直接从 DB 构建
            # 跳过 compute_run_scenario_steps（避免 Langfuse HTTP 调用和 observation 加载）
            # 也跳过 DELETE+INSERT+flush（结果未变化）
            persisted_map = {r.step_id: r for r in existing_steps}
            step_results: List[Dict[str, Any]] = []
            composite = 0.0
            weight_sum = 0.0
            for step_spec in tpl_data.get("steps") or []:
                step_id = step_spec.get("id")
                pr = persisted_map.get(step_id)
                if not pr:
                    continue
                w = float(step_spec.get("weight") or 1.0)
                score = float(pr.score) if pr.score is not None else 0.0
                composite += score * w
                weight_sum += w
                step_results.append({
                    "step_id": step_id,
                    "step_key": step_spec.get("step_key"),
                    "name": step_spec.get("name"),
                    "step_order": step_spec.get("step_order"),
                    "weight": step_spec.get("weight"),
                    "required": step_spec.get("required"),
                    "enabled": step_spec.get("enabled", True),
                    "enforce_order": bool(step_spec.get("enforce_order", False)),
                    "gt_criteria": step_spec.get("gt_criteria"),
                    "score": score,
                    "passed": pr.passed,
                    "weighted_score": round(score * w, 4),
                    "evidence": pr.evidence if pr.evidence else {},
                    # 持久化的 trace_binding（含 input/output_preview、observation_id/name、
                    # langfuse 链接）；旧数据可能为 None，前端兜底处理
                    "trace_binding": pr.trace_binding or {},
                })
            composite = round(composite / weight_sum, 4) if weight_sum > 0 else 0.0
            # 快路径返回 langfuse 链接：用 resolve_langfuse_project_id 取真实 cxxx ID
            # （首次调用 Langfuse API，之后永久缓存；配置中的 langfuse_project_id 可能
            # 是人工名称如 "197_project"，非 Langfuse 内部 ID，直接用会打不开 trace）
            from services.langfuse_link_service import (
                resolve_langfuse_host,
                resolve_run_langfuse_project_key,
                resolve_langfuse_project_id,
                build_trace_urls_sync,
            )
            _fast_project_key = await resolve_run_langfuse_project_key(session, run)
            _fast_host = resolve_langfuse_host(_fast_project_key)
            _fast_pid = await resolve_langfuse_project_id(_fast_project_key)
            _fast_langfuse = build_trace_urls_sync(
                trace_id=(run.trace_id or run.root_trace_id or "").strip(),
                langfuse_project_id=_fast_pid,
                host=_fast_host,
            )
            _fast_langfuse["langfuse_project_key"] = _fast_project_key
            _fast_langfuse["langfuse_host"] = _fast_host
            return {
                "agent_run_id": run.id,
                "scenario_key": resolved_key,
                "template": tpl_data,
                "composite_score": composite,
                "langfuse": _fast_langfuse,
                "steps": step_results,
            }
        # 实时评测且有增量事件回调：仍需完整路径以推送进度
        result = await compute_run_scenario_steps(
            session,
            run,
            tpl_data,
            agent_answer=agent_answer,
            persist_span_refs=not _is_stub,
            skip_llm_judge=True,
        )
        persisted_map = {r.step_id: r for r in existing_steps}
        for item in result["steps"]:
            pr = persisted_map.get(item.get("step_id"))
            if pr:
                item["score"] = pr.score
                item["passed"] = pr.passed
                if pr.evidence:
                    item["evidence"] = pr.evidence
        composite = 0.0
        weight_sum = 0.0
        for item in result["steps"]:
            w = item.get("weight", 1.0)
            composite += item["score"] * w
            weight_sum += w
        result["composite_score"] = round(composite / weight_sum, 4) if weight_sum > 0 else 0.0
    else:
        # 实时评估：传入增量事件回调，每个步骤评分后立即推送
        result = await compute_run_scenario_steps(
            session,
            run,
            tpl_data,
            agent_answer=agent_answer,
            persist_span_refs=not _is_stub,
            _emit_step_event=_emit_step_event,
            _task_id=_task_id,
            _current_step_ref=_current_step_ref,
            _total_steps=_total_steps or (active_steps_count + _step_offset),
        )
    step_results = result["steps"]
    composite = result["composite_score"]

    # 仅在持久化缓存命中（未通过增量回调发送事件）时补发步骤事件
    if _emit_step_event and _task_id and _current_step_ref is not None and (existing_steps and persist):
        for item in step_results:
            _current_step_ref[0] += 1
            await _emit_step_event(_task_id, "scenario_step_done", {
                "step_index": _current_step_ref[0],
                "total_steps": _total_steps or (active_steps_count + _step_offset),
                "step_key": item.get("step_key"),
                "step_name": item.get("name"),
                "question_id": run.question_id,
                "score": item.get("score"),
                "passed": item.get("passed"),
                "source": "persisted",
            })

    if persist and not _is_stub and not (existing_steps and persist):
        # 已有持久化结果时跳过 DELETE+INSERT+flush（快路径已提前返回，
        # 此分支仅覆盖实时评测回退路径）
        await session.execute(
            delete(ScenarioStepResult).where(
                ScenarioStepResult.agent_run_id == run.id,
            )
        )
        for item in step_results:
            session.add(
                ScenarioStepResult(
                    id=f"ssr_{uuid.uuid4().hex[:12]}",
                    agent_run_id=run.id,
                    step_id=item["step_id"],
                    score=item["score"],
                    passed=item["passed"],
                    evidence=item.get("evidence"),
                    trace_binding=item.get("trace_binding"),
                )
            )
        await session.flush()
    return {
        "agent_run_id": run.id,
        "scenario_key": resolved_key,
        "template": tpl_data,
        "composite_score": composite,
        "langfuse": result.get("langfuse"),
        "steps": step_results,
    }


async def score_task_scenario(
    session: AsyncSession,
    task_id: str,
    scenario_key: Optional[str] = None,
) -> Dict[str, Any]:
    runs = (
        await session.execute(select(AgentRun).where(AgentRun.task_id == task_id))
    ).scalars().all()
    reports = []
    keys_used: List[str] = []
    for run in runs:
        rep = await score_run_against_scenario(
            session, run.id, scenario_key, persist=True
        )
        if rep:
            reports.append(rep)
            keys_used.append(rep["scenario_key"])
    primary_key = scenario_key or (keys_used[0] if keys_used else None)
    return {
        "task_id": task_id,
        "scenario_key": primary_key,
        "scenario_keys": list(dict.fromkeys(keys_used)),
        "runs": reports,
    }


def _serialize_template(
    tpl: ScenarioEvalTemplate, steps: List[ScenarioEvalStep]
) -> Dict[str, Any]:
    return {
        "id": tpl.id,
        "scenario_key": tpl.scenario_key,
        "name": tpl.name,
        "description": tpl.description,
        "ground_truth_ref": tpl.ground_truth_ref,
        "dataset_id": tpl.dataset_id,
        "question_match": tpl.question_match,
        "dataset_item_id": getattr(tpl, "dataset_item_id", None),
        "enabled": tpl.enabled,
        "steps": [
            {
                "id": s.id,
                "step_key": s.step_key,
                "name": s.name,
                "step_order": s.step_order,
                "weight": s.weight,
                "required": s.required,
                "enabled": getattr(s, "enabled", True),
                "enforce_order": bool(getattr(s, "enforce_order", False)),
                "gt_criteria": s.gt_criteria if isinstance(s.gt_criteria, list) else ([s.gt_criteria] if s.gt_criteria else []),
                "gt_expected": s.gt_expected,
                "match_rules": s.match_rules,
            }
            for s in steps
        ],
    }
