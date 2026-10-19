"""场景子步骤配置页：Trace 绑定试跑（对齐过程点 checkpoint_verify 流程）。"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import LANGFUSE_ENABLED
from database import Agent, ScenarioEvalStep
from services.agent_identity import resolve_logical_agent_id_by_name
from services.agent_run_trace_list import load_observations_for_preview
from services.checkpoint_verify_service import (
    _find_agent_run_by_trace_ref,
    _format_trace_not_ready_error,
    _run_has_span_tree,
    list_sample_runs_for_agent,
)
from services.dataset_item_scenario_service import (
    _get_template_row_by_item,
    item_scenario_key,
)
from services.langfuse_errors import LangfuseApiError
from services.scenario_eval_service import _normalize_text, _score_step
from services.scenario_llm_aggregate_service import (
    llm_aggregate_step_output,
    parse_eval_config,
)
from services.scenario_step_trace_service import (
    _final_answer_steps_for_scenario,
    bind_step_observation,
    extract_observation_io,
    get_step_trace_binding,
)
from services.scenario_template_utils import template_with_active_steps_only
from trace_routes import _client_for_project as _default_client_for_project

__all__ = [
    "list_sample_runs_for_agent",
    "preview_scenario_step_on_trace",
    "batch_preview_scenario_steps_for_item",
]


async def _resolve_project_by_agent_name(
    session: AsyncSession,
    agent_name: str,
    body_project: Optional[str],
) -> Optional[str]:
    explicit = (body_project or "").strip()
    if explicit:
        return explicit
    lid = await resolve_logical_agent_id_by_name(session, agent_name.strip())
    q = select(Agent.langfuse_project).where(Agent.logical_agent_id == lid)
    for proj in (await session.execute(q)).scalars().all():
        pid = (proj or "").strip()
        if pid:
            return pid
    return None


async def _load_trace_observations(
    session: AsyncSession,
    *,
    trace_id: str,
    project: Optional[str],
    source: Optional[str],
    agent_name: str,
    client_for_project=None,
) -> Tuple[List[Dict[str, Any]], str, Optional[str], List[str]]:
    """加载 trace observations；附带 observation 名称列表供配置页提示。"""
    tid = trace_id.strip()
    use_langfuse = (source or "").strip().lower() == "langfuse"
    observations: List[Dict[str, Any]] = []
    obs_source = "langfuse"

    if not use_langfuse:
        fact_obs = await load_observations_for_preview(session, tid)
        if fact_obs:
            observations = fact_obs
            obs_source = "fact_layer"

    resolved_project = await _resolve_project_by_agent_name(session, agent_name, project)
    matched_run = await _find_agent_run_by_trace_ref(session, tid)

    if not observations:
        if not LANGFUSE_ENABLED:
            if matched_run and not await _run_has_span_tree(session, matched_run.id):
                raise ValueError(_format_trace_not_ready_error(matched_run))
            raise ValueError(
                "Langfuse 未配置且无物化 Span；请先完成一次在线评测并等待 Trace 同步，"
                "或传 source=langfuse 并填写项目 id"
            )
        try:
            lf_client = (client_for_project or _default_client_for_project)(resolved_project)
            trace = await lf_client.get_trace(tid)
        except LangfuseApiError as e:
            if matched_run and not await _run_has_span_tree(session, matched_run.id):
                raise ValueError(
                    f"{_format_trace_not_ready_error(matched_run)}\n\n"
                    f"（已尝试从 Langfuse 拉取 trace，失败原因：{e.message}）"
                ) from e
            raise ValueError(f"拉取 trace 失败: {e.message}") from e
        raw = trace.get("observations") or []
        observations = raw if isinstance(raw, list) else []
        obs_source = "langfuse"

    if not observations:
        if matched_run and not await _run_has_span_tree(session, matched_run.id):
            raise ValueError(
                f"{_format_trace_not_ready_error(matched_run)}\n\n"
                "（Trace 已拉取但未包含 observation，子步骤无法匹配。）"
            )
        raise ValueError(
            "Trace 中无 observation，无法匹配子步骤。"
            "请确认 Agent 已上报 Langfuse Span，或等待 Trace 同步完成后再试。"
        )

    names = sorted(
        {
            str(o.get("name") or "").strip()
            for o in observations
            if isinstance(o, dict) and str(o.get("name") or "").strip()
        }
    )
    return observations, obs_source, resolved_project, names


def _step_def_dict(
    *,
    step_key: str,
    name: str,
    step_order: int = 0,
    gt_expected: Optional[Dict[str, Any]] = None,
    match_rules: Optional[Dict[str, Any]] = None,
    enabled: bool = True,
    required: bool = True,
) -> Dict[str, Any]:
    return {
        "step_key": step_key,
        "name": name,
        "step_order": step_order,
        "gt_expected": gt_expected if isinstance(gt_expected, dict) else {},
        "match_rules": match_rules if isinstance(match_rules, dict) else {},
        "enabled": enabled,
        "required": required,
    }


def _evaluate_step_binding(
    *,
    scenario_key: str,
    step_def: Dict[str, Any],
    observations: List[Dict[str, Any]],
    final_answer_text: str = "",
    final_answer_steps: Optional[frozenset[str]] = None,
) -> Dict[str, Any]:
    step_key = str(step_def.get("step_key") or "")
    binding = get_step_trace_binding(scenario_key, step_key, step_def)
    obs, meta = bind_step_observation(
        step_key,
        observations,
        binding=binding,
        final_answer_text=final_answer_text,
        final_answer_steps=final_answer_steps,
    )
    io = extract_observation_io(obs)
    bind_source = str(meta.get("bind_source") or "unbound")
    span_bound = obs is not None
    keyword_ok = bind_source in ("heuristic", "agent_step_id") or bool(meta.get("matched_keywords"))
    perf_ready = span_bound and io.get("observation_id")

    suggestion = None
    if not span_bound:
        if bind_source == "final_answer_fallback":
            suggestion = "仅终局答案命中关键词，无 Span 绑定；运行效能表将为 —"
        else:
            suggestion = (
                "未绑定 Span：请调整 match_rules.trace_binding.span_name_patterns，"
                "或让 Agent 上报 agent.step.id / 多段 llm:main_iteration"
            )
    elif bind_source == "heuristic_no_keyword":
        suggestion = "已匹配 Span 名称，但 output 未命中本子步骤关键词；效能可统计，判分可能偏低"

    return {
        "step_key": step_key,
        "step_name": step_def.get("name") or step_key,
        "enabled": bool(step_def.get("enabled", True)),
        "required": bool(step_def.get("required", True)),
        "step_order": step_def.get("step_order"),
        "span_bound": span_bound,
        "perf_ready": bool(perf_ready),
        "bind_source": bind_source,
        "keyword_ok": keyword_ok,
        "matched_observation_name": io.get("observation_name") or meta.get("observation_name"),
        "observation_id": io.get("observation_id"),
        "matched_keywords": meta.get("matched_keywords") or [],
        "span_name_patterns": binding.get("span_name_patterns") or [],
        "suggestion": suggestion,
        "output_preview": io.get("output_preview") or "",
        "input_preview": io.get("input_preview") or "",
    }


async def preview_scenario_step_on_trace(
    session: AsyncSession,
    *,
    dataset_item_id: str,
    trace_id: str,
    agent_name: str,
    step_key: str,
    name: str,
    gt_expected: Optional[Dict[str, Any]] = None,
    match_rules: Optional[Dict[str, Any]] = None,
    enabled: bool = True,
    required: bool = True,
    step_order: Optional[int] = None,
    project: Optional[str] = None,
    source: Optional[str] = None,
    client_for_project=None,
) -> Dict[str, Any]:
    """单条子步骤（含未保存草稿）在 trace 上试跑 Span 绑定。"""
    tid = trace_id.strip()
    if not tid:
        raise ValueError("trace_id 不能为空")
    if not (step_key or "").strip():
        raise ValueError("step_key 不能为空")

    scenario_key = item_scenario_key(dataset_item_id.strip())
    step_def = _step_def_dict(
        step_key=step_key.strip(),
        name=(name or step_key).strip(),
        step_order=int(step_order or 0),
        gt_expected=gt_expected,
        match_rules=match_rules,
        enabled=enabled,
        required=required,
    )

    observations, obs_source, resolved_project, obs_names = await _load_trace_observations(
        session,
        trace_id=tid,
        project=project,
        source=source,
        agent_name=agent_name,
        client_for_project=client_for_project,
    )

    ev = _evaluate_step_binding(
        scenario_key=scenario_key,
        step_def=step_def,
        observations=observations,
        final_answer_steps=_final_answer_steps_for_scenario(scenario_key, {"steps": [step_def]}),
    )

    # ★ LLM 汇聚试跑
    eval_config = parse_eval_config(match_rules)
    if eval_config["mode"] == "llm_aggregate" and eval_config["aggregate_prompt"]:
        output_text = (ev.get("output_preview") or "").strip()
        input_text = (ev.get("input_preview") or "").strip()
        if input_text and input_text not in output_text:
            score_text = f"{input_text}\n{output_text}".strip() if output_text else input_text
        else:
            score_text = output_text
        if score_text:
            aggregated = await llm_aggregate_step_output(
                step_output=score_text,
                aggregate_prompt=eval_config["aggregate_prompt"],
                llm_model=eval_config.get("llm_model"),
            )
            # Ground_Truth评测预判
            gt = gt_expected if isinstance(gt_expected, dict) else {}
            kw_list = [str(k).strip() for k in (gt.get("keywords") or []) if str(k).strip()]
            norm_agg = _normalize_text(aggregated)
            kw_hits = [k for k in kw_list if k.replace(" ", "") in norm_agg]
            ev["eval_mode"] = "llm_aggregate"
            ev["aggregate_prompt_preview"] = eval_config["aggregate_prompt"][:200]
            ev["aggregated_answer_preview"] = (aggregated or "")[:500]
            ev["aggregate_keyword_hits"] = kw_hits
        else:
            ev["eval_mode"] = "llm_aggregate"
            ev["aggregate_prompt_preview"] = eval_config["aggregate_prompt"][:200]
            ev["aggregated_answer_preview"] = ""
            ev["aggregate_keyword_hits"] = []

    return {
        "trace_id": tid,
        "project": resolved_project,
        "observation_count": len(observations),
        "observation_source": obs_source,
        "observation_names": obs_names,
        "dataset_item_id": dataset_item_id,
        "scenario_key": scenario_key,
        **ev,
    }


async def batch_preview_scenario_steps_for_item(
    session: AsyncSession,
    *,
    dataset_item_id: str,
    trace_id: str,
    agent_name: str,
    project: Optional[str] = None,
    source: Optional[str] = None,
    client_for_project=None,
) -> Dict[str, Any]:
    """本题全部子步骤在一条 trace 上批量试跑 Span 绑定。"""
    ditem_id = dataset_item_id.strip()
    tid = trace_id.strip()
    if not tid:
        raise ValueError("trace_id 不能为空")

    tpl_row = await _get_template_row_by_item(session, ditem_id)
    if not tpl_row:
        raise ValueError("本题尚无场景子步骤模板，请先自动提炼或新增子步骤")

    steps = (
        await session.execute(
            select(ScenarioEvalStep)
            .where(ScenarioEvalStep.template_id == tpl_row.id)
            .order_by(ScenarioEvalStep.step_order, ScenarioEvalStep.step_key)
        )
    ).scalars().all()
    if not steps:
        raise ValueError("本题尚无子步骤，请先配置子步骤")

    scenario_key = item_scenario_key(ditem_id)
    tpl_data = template_with_active_steps_only(
        {
            "scenario_key": scenario_key,
            "steps": [
                {
                    "step_key": s.step_key,
                    "name": s.name,
                    "step_order": s.step_order,
                    "enabled": s.enabled,
                    "gt_expected": s.gt_expected,
                }
                for s in steps
            ],
        }
    )
    fa_steps = _final_answer_steps_for_scenario(scenario_key, tpl_data)

    observations, obs_source, resolved_project, obs_names = await _load_trace_observations(
        session,
        trace_id=tid,
        project=project,
        source=source,
        agent_name=agent_name,
        client_for_project=client_for_project,
    )

    results: List[Dict[str, Any]] = []
    enabled_total = 0
    bound_count = 0
    perf_ready_count = 0

    for s in steps:
        gt = s.gt_expected if isinstance(s.gt_expected, dict) else {}
        if isinstance(s.gt_expected, str):
            try:
                gt = json.loads(s.gt_expected)
            except json.JSONDecodeError:
                gt = {}
        mr = s.match_rules if isinstance(s.match_rules, dict) else {}
        if isinstance(s.match_rules, str):
            try:
                mr = json.loads(s.match_rules)
            except json.JSONDecodeError:
                mr = {}

        step_def = _step_def_dict(
            step_key=s.step_key,
            name=s.name,
            step_order=s.step_order or 0,
            gt_expected=gt,
            match_rules=mr,
            enabled=bool(s.enabled),
            required=bool(s.required),
        )
        ev = _evaluate_step_binding(
            scenario_key=scenario_key,
            step_def=step_def,
            observations=observations,
            final_answer_steps=fa_steps,
        )

        # ★ LLM 汇聚试跑
        eval_config = parse_eval_config(mr)
        if eval_config["mode"] == "llm_aggregate" and eval_config["aggregate_prompt"]:
            output_text = (ev.get("output_preview") or "").strip()
            input_text = (ev.get("input_preview") or "").strip()
            if input_text and input_text not in output_text:
                step_score_text = f"{input_text}\n{output_text}".strip() if output_text else input_text
            else:
                step_score_text = output_text
            if step_score_text:
                aggregated = await llm_aggregate_step_output(
                    step_output=step_score_text,
                    aggregate_prompt=eval_config["aggregate_prompt"],
                    llm_model=eval_config.get("llm_model"),
                )
                kw_list = [str(k).strip() for k in (gt.get("keywords") or []) if str(k).strip()]
                norm_agg = _normalize_text(aggregated)
                kw_hits = [k for k in kw_list if k.replace(" ", "") in norm_agg]
                ev["eval_mode"] = "llm_aggregate"
                ev["aggregate_prompt_preview"] = eval_config["aggregate_prompt"][:200]
                ev["aggregated_answer_preview"] = (aggregated or "")[:500]
                ev["aggregate_keyword_hits"] = kw_hits
            else:
                ev["eval_mode"] = "llm_aggregate"
                ev["aggregate_prompt_preview"] = eval_config["aggregate_prompt"][:200]
                ev["aggregated_answer_preview"] = ""
                ev["aggregate_keyword_hits"] = []

        ev["step_id"] = s.id
        results.append(ev)
        if s.enabled:
            enabled_total += 1
            if ev["span_bound"]:
                bound_count += 1
            if ev["perf_ready"]:
                perf_ready_count += 1

    all_enabled_bound = enabled_total > 0 and bound_count == enabled_total
    any_perf = perf_ready_count > 0

    return {
        "dataset_item_id": ditem_id,
        "scenario_key": scenario_key,
        "agent_name": agent_name.strip(),
        "trace_id": tid,
        "project": resolved_project,
        "observation_count": len(observations),
        "observation_source": obs_source,
        "observation_names": obs_names,
        "step_total": len(steps),
        "enabled_total": enabled_total,
        "bound_count": bound_count,
        "perf_ready_count": perf_ready_count,
        "all_enabled_bound": all_enabled_bound,
        "any_perf_ready": any_perf,
        "results": results,
    }
