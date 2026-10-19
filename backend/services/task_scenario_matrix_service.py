"""场景子步骤对比矩阵（单任务多版本 / 跨任务共用逻辑）。"""

from __future__ import annotations



import json

from collections import defaultdict

from typing import Any, Dict, List, Optional, Tuple



from sqlalchemy import select

from sqlalchemy.ext.asyncio import AsyncSession



from database import Agent, AgentRun, Dataset, EvaluationResult, RunSpanNode, ScenarioStepResult

from services.cross_task_cockpit_service import CrossTaskCockpitError, _load_task_bundle
from services.diff_summary_service import get_task_diff_highlights

from services.checkpoint_batch_service import evaluate_template_on_nodes, load_enabled_templates
from services.checkpoint_to_scenario_service import draft_steps_from_checkpoints
from services.eval_context import parse_eval_context

from services.scenario_eval_service import (
    GENERIC_L0_SCENARIO_KEY,
    ensure_generic_l0_template,
    get_template_by_key,
    score_run_against_scenario,
)

from services.scenario_template_utils import (

    merge_step_columns_union,

    step_columns_from_template,

)

from services.multi_run_aggregate_service import aggregate_scenario_cell
from services.chain_stability_service import build_chain_stability, build_question_chain_stability
from services.scenario_step_performance_service import build_scenario_step_performance_summary
from services.scenario_llm_aggregate_service import collect_llm_aggregate_warnings_for_task





def question_key_from_run(run: AgentRun) -> str:

    ctx = parse_eval_context(run.eval_context)

    qk = (ctx.get("question_key") or "").strip()

    if qk:

        return qk

    ditem = (ctx.get("dataset_item_id") or "").strip()

    if ditem:

        return f"ditem:{ditem}"

    if run.question_id is not None:

        return f"qidx:{run.question_id}"

    return "unknown"





def _pick_run(existing: Optional[AgentRun], candidate: AgentRun) -> AgentRun:

    if existing is None:

        return candidate

    if candidate.status == "success" and existing.status != "success":

        return candidate

    return existing





def _question_label_from_run(

    run: AgentRun, eval_rows: List[Any]

) -> str:

    label_src = run.input_snapshot

    if isinstance(label_src, str):

        try:

            label_src = json.loads(label_src)

        except json.JSONDecodeError:

            label_src = None

    if isinstance(label_src, dict):

        text = str(label_src.get("question") or "")[:120]

        if text:

            return text

    for er in eval_rows:

        if er.question_id == run.question_id and er.question:

            return (er.question or "")[:120]

    qk = question_key_from_run(run)

    return f"题目 {qk[:16]}…" if len(qk) > 16 else qk





def _normalize_matrix_scenario_key(scenario_key: Optional[str]) -> Optional[str]:

    if not scenario_key:

        return None

    sk = str(scenario_key).strip()

    if not sk or sk.lower() in ("auto", "per_item", "resolve"):

        return None

    return sk


async def _resolve_run_logical_agent_id(
    session: AsyncSession,
    run: AgentRun,
) -> str:
    """解析 Run 所属 logical_agent_id，供过程点配置查找。"""
    logical_agent_id = (run.logical_agent_id or "").strip()
    if logical_agent_id:
        return logical_agent_id

    agent = await session.get(Agent, run.agent_id) if run.agent_id else None
    if not agent:
        return ""

    logical_agent_id = (agent.logical_agent_id or "").strip()
    if logical_agent_id:
        return logical_agent_id

    from services.agent_identity import build_agent_identity_maps

    _, name_to_lid = await build_agent_identity_maps(session)
    name = (agent.name or "").strip()
    if name and name in name_to_lid:
        return name_to_lid[name]
    return ""


def _checkpoint_template_to_matrix_template(templates: List[Any]) -> Dict[str, Any]:
    """将过程点配置临时转换成矩阵可消费的模板结构（不落库）。"""
    steps = draft_steps_from_checkpoints(list(templates))
    for idx, step in enumerate(steps):
        tpl = templates[idx]
        step["id"] = tpl.id
        step["source_checkpoint_id"] = tpl.id
    logical_agent_id = (
        str(getattr(templates[0], "logical_agent_id", "") or "") if templates else ""
    )
    return {
        "id": f"checkpoint_runtime_{logical_agent_id or 'unknown'}",
        "scenario_key": f"checkpoint:{logical_agent_id or 'unknown'}",
        "name": "过程点配置子步骤",
        "description": "由当前 Agent 启用的过程点配置动态生成",
        "ground_truth_ref": None,
        "steps": steps,
    }


async def _score_checkpoint_cell(
    session: AsyncSession,
    run: AgentRun,
) -> Optional[Dict[str, Any]]:
    logical_agent_id = await _resolve_run_logical_agent_id(session, run)
    templates = await load_enabled_templates(session, logical_agent_id)
    if not templates:
        return None

    span_nodes = (
        await session.execute(
            select(RunSpanNode)
            .where(RunSpanNode.agent_run_id == run.id)
            .order_by(RunSpanNode.step_index, RunSpanNode.sibling_order, RunSpanNode.id)
        )
    ).scalars().all()

    template_meta = _checkpoint_template_to_matrix_template(templates)
    step_specs = template_meta.get("steps") or []
    evaluations = [evaluate_template_on_nodes(tpl, list(span_nodes)) for tpl in templates]

    total_weight = 0.0
    composite = 0.0
    steps: List[Dict[str, Any]] = []
    for spec, ev in zip(step_specs, evaluations):
        weight = float(spec.get("weight") or 0)
        score = 1.0 if ev.get("passed") else 0.0
        total_weight += weight
        composite += score * weight
        evidence = {
            **(ev.get("evidence") or {}),
            "hit": ev.get("hit"),
            "order_ok": ev.get("order_ok"),
            "matched_count": ev.get("matched_count"),
            "checkpoint_id": ev.get("checkpoint_id"),
            "checkpoint_name": ev.get("checkpoint_name"),
            "source": "process_checkpoint",
        }
        steps.append(
            {
                "step_id": spec.get("id"),
                "step_key": spec.get("step_key"),
                "name": spec.get("name"),
                "weight": weight,
                "required": spec.get("required", True),
                "enabled": spec.get("enabled", True),
                "gt_criteria": gt_spec if isinstance(gt_spec := spec.get("gt_criteria"), list) else ([gt_spec] if gt_spec else []),
                "score": score,
                "passed": bool(ev.get("passed")),
                "weighted_score": round(score * weight, 4),
                "evidence": evidence,
                "trace_binding": evidence,
            }
        )

    if total_weight > 0:
        composite = composite / total_weight

    # 构建 langfuse 链接：用 resolve_langfuse_project_id 取真实 cxxx ID
    from services.langfuse_link_service import (
        resolve_langfuse_host,
        resolve_run_langfuse_project_key,
        resolve_langfuse_project_id,
        build_trace_urls_sync,
    )
    _ck_project_key = await resolve_run_langfuse_project_key(session, run)
    _ck_host = resolve_langfuse_host(_ck_project_key)
    _ck_pid = await resolve_langfuse_project_id(_ck_project_key)
    _ck_langfuse = build_trace_urls_sync(
        trace_id=(run.trace_id or run.root_trace_id or "").strip(),
        langfuse_project_id=_ck_pid,
        host=_ck_host,
    )
    _ck_langfuse["langfuse_project_key"] = _ck_project_key
    _ck_langfuse["langfuse_host"] = _ck_host

    return {
        "agent_run_id": run.id,
        "scenario_key": template_meta["scenario_key"],
        "template": template_meta,
        "composite_score": round(composite, 4),
        "steps": steps,
        "langfuse": _ck_langfuse,
    }


def _build_offline_stub_runs(
    task: Any, eval_rows: List[Any]
) -> List[AgentRun]:
    """离线任务无 AgentRun，从 evaluation_results 构建虚拟 stub run。

    stub run 不落库，仅用于矩阵构建时的内存数据结构，
    让 score_run_against_scenario 能通过 run.id 关联到 evaluation_results.agent_answer。
    """
    import uuid
    stub_runs: List[AgentRun] = []
    dummy_agent_id = task.agent_id or "offline-stub"
    for er in eval_rows:
        if not er.agent_answer:
            continue
        run_id = f"stub_offline_{task.id}_{er.question_id}_{er.id}"
        stub = AgentRun(
            id=run_id,
            agent_id=dummy_agent_id,
            task_id=task.id,
            question_id=er.question_id,
            run_index=0,
            status="success",
            evaluation_mode="offline",
            eval_context={"task_id": task.id, "question_id": er.question_id},
            started_at=task.created_at,
        )
        # 关联 evaluation_result 的 agent_answer 到 stub run
        stub._offline_agent_answer = er.agent_answer  # type: ignore[attr-defined]
        stub._offline_eval_row_id = er.id  # type: ignore[attr-defined]
        stub_runs.append(stub)
    return stub_runs


async def _build_question_rows(

    session: AsyncSession,

    *,
    forced_scenario_key: Optional[str],
    dataset_id: Optional[str] = None,
    member_ids: List[str],

    matrix_runs: Dict[str, Dict[str, List[AgentRun]]],

    question_labels: Dict[str, str],

    members: List[Dict[str, Any]],

    baseline_member_id: str,

    use_checkpoint_config: bool = False,

) -> Tuple[List[Dict[str, Any]], List[str], List[Dict[str, Any]]]:

    scored_cache: Dict[str, Dict[str, Any]] = {}

    warnings: List[str] = []

    templates_for_union: List[Dict[str, Any]] = []

    # 批量预取所有 Run 的 ScenarioStepResult 和 EvaluationResult，消除 N+1 查询
    all_run_ids: List[str] = [
        r.id for qk in matrix_runs for mid in matrix_runs[qk] for r in matrix_runs[qk][mid]
    ]
    preloaded_steps_by_run: Dict[str, List[Any]] = {}
    preloaded_eval_by_run: Dict[str, Any] = {}
    if all_run_ids:
        _steps_rows = (
            await session.execute(
                select(ScenarioStepResult).where(
                    ScenarioStepResult.agent_run_id.in_(all_run_ids)
                )
            )
        ).scalars().all()
        for _sr in _steps_rows:
            preloaded_steps_by_run.setdefault(_sr.agent_run_id, []).append(_sr)
        _eval_rows = (
            await session.execute(
                select(EvaluationResult).where(
                    EvaluationResult.agent_run_id.in_(all_run_ids)
                )
            )
        ).scalars().all()
        for _er in _eval_rows:
            preloaded_eval_by_run.setdefault(_er.agent_run_id, _er)
    # 模板缓存：同 scenario_key 只查一次 DB
    template_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    async def _get_cached_template(sk: str) -> Optional[Dict[str, Any]]:
        if sk not in template_cache:
            template_cache[sk] = await get_template_by_key(session, sk)
        return template_cache[sk]



    async def _score_item_scenario_cell(run: AgentRun) -> Optional[Dict[str, Any]]:
        # 离线 stub run：直接从内存属性获取 agent_answer
        offline_answer = getattr(run, "_offline_agent_answer", None)
        ctx = parse_eval_context(run.eval_context)
        explicit_sk = _normalize_matrix_scenario_key(ctx.get("scenario_key"))
        ditem_id = (ctx.get("dataset_item_id") or "").strip()
        # 离线 stub run 没有 eval_context 里的 scenario_key，从 dataset item 推断
        if not explicit_sk and offline_answer is not None and dataset_id:
            if run.question_id is not None:
                ds_q = await session.execute(
                    select(Dataset).where(Dataset.id == dataset_id)
                )
                ds = ds_q.scalar_one_or_none()
                if ds and ds.data:
                    import json as _json
                    try:
                        items = _json.loads(ds.data)
                        for idx, it in enumerate(items):
                            if idx == run.question_id:
                                ditem_id = str(it.get("id") or f"ditem_{idx}")
                                break
                    except Exception:
                        pass
        if explicit_sk and ditem_id:
            from services.dataset_item_scenario_service import item_scenario_key

            expected_sk = item_scenario_key(ditem_id)
            if explicit_sk != expected_sk:
                explicit_sk = expected_sk
        if explicit_sk:
            _tpl = await _get_cached_template(explicit_sk)
            return await score_run_against_scenario(
                session, run.id, explicit_sk, persist=True, agent_answer=offline_answer, run_obj=run,
                _preloaded_steps=preloaded_steps_by_run.get(run.id),
                _preloaded_eval_row=preloaded_eval_by_run.get(run.id),
                _preloaded_template=_tpl,
            )
        if ditem_id:
            from services.dataset_item_scenario_service import (
                item_scenario_key,
                resolve_item_scenario_key_for_item,
            )

            item_sk = await resolve_item_scenario_key_for_item(session, ditem_id)
            if not item_sk:
                item_sk = item_scenario_key(ditem_id)
            _tpl = await _get_cached_template(item_sk) if item_sk else None
            return await score_run_against_scenario(
                session, run.id, item_sk, persist=True, agent_answer=offline_answer, run_obj=run,
                _preloaded_steps=preloaded_steps_by_run.get(run.id),
                _preloaded_eval_row=preloaded_eval_by_run.get(run.id),
                _preloaded_template=_tpl,
            )
        return await score_run_against_scenario(
            session, run.id, persist=True, agent_answer=offline_answer, run_obj=run,
            _preloaded_steps=preloaded_steps_by_run.get(run.id),
            _preloaded_eval_row=preloaded_eval_by_run.get(run.id),
        )

    async def _score_cell(run: AgentRun) -> Optional[Dict[str, Any]]:

        if run.id in scored_cache:

            return scored_cache[run.id]

        if forced_scenario_key:
            _tpl = await _get_cached_template(forced_scenario_key)
            rep = await score_run_against_scenario(
                session, run.id, forced_scenario_key, persist=True,
                agent_answer=getattr(run, "_offline_agent_answer", None),
                run_obj=run,
                _preloaded_steps=preloaded_steps_by_run.get(run.id),
                _preloaded_eval_row=preloaded_eval_by_run.get(run.id),
                _preloaded_template=_tpl,
            )
        else:
            rep = None
            if use_checkpoint_config:
                rep = await _score_checkpoint_cell(session, run)
            if rep is None:
                rep = await _score_item_scenario_cell(run)

        if rep:

            scored_cache[run.id] = rep

            tpl = rep.get("template")
            sk = (tpl or {}).get("scenario_key")
            if tpl and sk and not any(
                t.get("scenario_key") == sk for t in templates_for_union
            ):
                templates_for_union.append(tpl)
        return rep



    question_rows: List[Dict[str, Any]] = []

    for qk in sorted(matrix_runs.keys(), key=lambda k: question_labels.get(k, k)):

        cells: Dict[str, Any] = {}

        composites: Dict[str, Optional[float]] = {}

        row_scenario_key: Optional[str] = forced_scenario_key

        row_template: Optional[Dict[str, Any]] = None



        for mid in member_ids:

            runs_list = matrix_runs[qk].get(mid) or []

            if not runs_list:

                cells[mid] = None

                composites[mid] = None

                continue

            scored_reps: List[Dict[str, Any]] = []

            for run in runs_list:

                rep = await _score_cell(run)

                if rep:

                    scored_reps.append({**rep, "agent_run_id": run.id})

            if not scored_reps:

                cells[mid] = None

                composites[mid] = None

                continue

            if not row_scenario_key:

                row_scenario_key = scored_reps[0].get("scenario_key")

            if row_template is None:

                row_template = scored_reps[0].get("template")

            if len(runs_list) == 1:

                rep = scored_reps[0]

                cells[mid] = {

                    "agent_run_id": runs_list[0].id,

                    "composite_score": rep["composite_score"],

                    "steps": rep["steps"],

                    "langfuse": rep.get("langfuse"),

                    "scenario_key": rep.get("scenario_key"),

                    "round_count": 1,

                    "runs": [

                        {

                            "agent_run_id": runs_list[0].id,

                            "run_index": runs_list[0].run_index if runs_list[0].run_index is not None else 0,

                            "composite_score": rep["composite_score"],

                            "steps": rep["steps"],

                            "langfuse": rep.get("langfuse"),

                        }

                    ],

                }

                composites[mid] = rep["composite_score"]

            else:

                chain_info = await build_question_chain_stability(session, runs_list)

                cell = aggregate_scenario_cell(

                    runs_list, scored_reps, chain_info=chain_info

                )

                cells[mid] = cell

                composites[mid] = cell["composite_score"]



        valid = {k: v for k, v in composites.items() if v is not None}

        winner = max(valid, key=valid.get) if valid else None  # type: ignore[arg-type]

        winner_member = next(

            (m for m in members if m["member_id"] == winner), None

        )

        baseline_runs = matrix_runs[qk].get(baseline_member_id) or []

        baseline_run = baseline_runs[0] if baseline_runs else None

        row_step_columns = (

            step_columns_from_template(row_template) if row_template else []

        )

        question_rows.append(

            {

                "question_key": qk,

                "question_id": baseline_run.question_id if baseline_run else 0,

                "label": question_labels.get(qk, qk),

                "scenario_key": row_scenario_key,

                "step_columns": row_step_columns,

                "cells": cells,

                "composite_by_member": composites,

                "winner_member_id": winner,

                "winner_label": winner_member["label"] if winner_member else None,

            }

        )



    matched_count = sum(1 for r in question_rows if any(r["cells"].values()))

    if matched_count == 0:

        warnings.append(

            "当前任务未匹配到评测题场景子步骤，请在评测集管理中为题目标记场景子步骤"

        )

    return question_rows, warnings, templates_for_union





async def build_task_scenario_matrix(

    session: AsyncSession,

    task_id: str,

    *,

    scenario_key: Optional[str] = None,

    baseline_agent_id: Optional[str] = None,

) -> Dict[str, Any]:

    """单任务内按 question_key × agent_id 构建场景子步骤矩阵（列=Agent 版本）。"""

    forced_key = _normalize_matrix_scenario_key(scenario_key)

    bundle = await _load_task_bundle(session, task_id)

    if not bundle.runs:
        # 离线任务没有 AgentRun，从 evaluation_results 构建虚拟 stub run
        from services.evaluation_mode import is_offline_task
        if is_offline_task(bundle.task) and bundle.eval_rows:
            bundle.runs = _build_offline_stub_runs(bundle.task, bundle.eval_rows)
        if not bundle.runs:
            raise CrossTaskCockpitError("任务无可用 AgentRun")



    if forced_key == GENERIC_L0_SCENARIO_KEY:
        await ensure_generic_l0_template(session)



    reference_tpl: Optional[Dict[str, Any]] = None

    if forced_key:

        reference_tpl = await get_template_by_key(session, forced_key)

        if not reference_tpl:

            raise CrossTaskCockpitError(f"场景模板不存在: {forced_key}")



    dataset_name = bundle.dataset_name

    if bundle.task.dataset_id:

        ds = (

            await session.execute(

                select(Dataset).where(Dataset.id == bundle.task.dataset_id)

            )

        ).scalar_one_or_none()

        if ds:

            dataset_name = ds.name or bundle.task.dataset_id



    matrix_runs: Dict[str, Dict[str, List[AgentRun]]] = defaultdict(lambda: defaultdict(list))

    question_labels: Dict[str, str] = {}

    agent_ids_seen: set[str] = set()



    for run in bundle.runs:

        if run.question_id is None or not run.agent_id:

            continue

        qk = question_key_from_run(run)

        aid = str(run.agent_id).strip()

        agent_ids_seen.add(aid)

        matrix_runs[qk][aid].append(run)

        if qk not in question_labels:

            question_labels[qk] = _question_label_from_run(run, bundle.eval_rows)



    for qk in matrix_runs:

        for aid in matrix_runs[qk]:

            matrix_runs[qk][aid].sort(

                key=lambda r: (

                    r.run_index if r.run_index is not None else 0,

                    r.started_at or "",

                )

            )



    if not matrix_runs:

        raise CrossTaskCockpitError("任务无可用 Run，无法生成场景矩阵")



    member_ids = sorted(agent_ids_seen)

    agent_rows = (

        await session.execute(select(Agent).where(Agent.id.in_(member_ids)))

    ).scalars().all()

    agents_by_id = {a.id: a for a in agent_rows}



    baseline_id = (

        baseline_agent_id

        if baseline_agent_id in member_ids

        else (bundle.task.agent_id if bundle.task.agent_id in member_ids else member_ids[0])

    )



    warnings: List[str] = []
    llm_warnings = await collect_llm_aggregate_warnings_for_task(
        session,
        bundle.runs,
        forced_scenario_key=forced_key,
    )
    warnings.extend(llm_warnings)

    members: List[Dict[str, Any]] = []

    for aid in member_ids:

        ag = agents_by_id.get(aid)

        ver = (ag.version or "").strip() if ag else ""

        name = (ag.name or "").strip() if ag else aid[:8]

        label = f"{name} {ver}".strip() if ver else name

        members.append(

            {

                "member_id": aid,

                "agent_id": aid,

                "agent_name": ag.name if ag else None,

                "agent_version": ag.version if ag else None,

                "label": label,

                "is_baseline": aid == baseline_id,

            }

        )



    question_rows, row_warnings, templates_union = await _build_question_rows(

        session,

        forced_scenario_key=forced_key,
        dataset_id=bundle.task.dataset_id,

        member_ids=member_ids,

        matrix_runs=matrix_runs,

        question_labels=question_labels,

        members=members,

        baseline_member_id=baseline_id,

        use_checkpoint_config=False,

    )



    warnings.extend(row_warnings)

    if reference_tpl:

        global_step_columns = step_columns_from_template(reference_tpl)

        display_scenario_key = forced_key

        template_meta = {

            "name": reference_tpl.get("name"),

            "ground_truth_ref": reference_tpl.get("ground_truth_ref"),

        }

    else:

        global_step_columns = merge_step_columns_union(templates_union)

        keys_seen = [

            r["scenario_key"] for r in question_rows if r.get("scenario_key")

        ]

        display_scenario_key = keys_seen[0] if len(set(keys_seen)) == 1 else "auto"

        template_meta = {

            "name": "按题场景子步骤",

            "ground_truth_ref": None,

        }



    diff_highlights = await get_task_diff_highlights(session, task_id)
    diff_by_question: Dict[int, Dict[str, Any]] = {}
    for item in diff_highlights:
        qid = item.get("question_id")
        if qid is not None and qid not in diff_by_question:
            diff_by_question[qid] = item
    for row in question_rows:
        qid = row.get("question_id")
        if qid is not None:
            row["diff_highlight"] = diff_by_question.get(qid)

    empty_reason = None
    if not global_step_columns or not any(
        any(cell for cell in row.get("cells", {}).values()) for row in question_rows
    ):
        empty_reason = (
            "当前任务未配置评测题场景子步骤，请在评测集管理中为题目标记场景子步骤。"
        )

    step_performance = await build_scenario_step_performance_summary(
        session, question_rows, member_ids
    )

    max_rounds = max(
        (len(runs) for q in matrix_runs.values() for runs in q.values()),
        default=1,
    )
    chain_stability = await build_chain_stability(session, task_id, bundle.runs)

    return {

        "comparison_mode": "intra_task_scenario",

        "scenario_key": display_scenario_key,

        "scenario_mode": "auto" if not forced_key else "fixed",

        "task_id": task_id,

        "inference_rounds": max_rounds,

        "multi_round_mode": max_rounds > 1,

        "chain_stability": chain_stability,

        "baseline_member_id": baseline_id,

        "dataset_id": bundle.task.dataset_id,

        "dataset_name": dataset_name,

        "template": template_meta,

        "warnings": warnings,
        "empty_reason": empty_reason,

        "members": members,

        "member_ids": member_ids,

        "step_columns": global_step_columns,

        "questions": question_rows,

        "comparison_count": len(diff_highlights),

        "step_performance_summary": step_performance,

    }


