"""同评测集多任务 → 场景子步骤对比矩阵（Phase A：question_key 对齐）。"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, Dataset
from services.cross_task_outcome_summary_service import build_cross_task_outcome_summaries
from services.cross_task_performance_summary_service import (
    build_cross_task_performance_summaries,
    load_run_metrics_for_bundles,
)
from services.cross_task_question_runtime_service import (
    build_cross_task_question_runtime,
)
from services.checkpoint_result_service import build_task_checkpoint_report
from services.chain_stability_service import build_chain_stability
from services.version_verdict_service import build_overall_verdict
from services.scenario_step_performance_service import (
    build_scenario_step_performance_summary,
)
from services.cross_task_cockpit_service import CrossTaskCockpitError, _load_task_bundle, _member_label
from services.scenario_eval_service import (
    GENERIC_L0_SCENARIO_KEY,
    ensure_generic_l0_template,
    get_template_by_key,
)
from services.task_scenario_matrix_service import (
    _build_question_rows,
    _normalize_matrix_scenario_key,
    _question_label_from_run,
    question_key_from_run,
)
from services.scenario_template_utils import merge_step_columns_union, step_columns_from_template
from services.scenario_llm_aggregate_service import collect_llm_aggregate_warnings_for_task

# 短期内存缓存：避免矩阵展示 + 报告导出重复计算
_matrix_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_MATRIX_CACHE_TTL = 300  # 5 分钟


async def build_cross_task_scenario_matrix(
    session: AsyncSession,
    task_ids: List[str],
    *,
    scenario_key: Optional[str] = None,
    baseline_task_id: Optional[str] = None,
    use_cache: bool = False,
) -> Dict[str, Any]:
    """
    将多个已完成任务按 question_key 对齐，列=任务（Agent 版本），行=场景题；
    单元格为场景子步骤综合分与各步得分。
    """
    # 短期缓存：报告导出（use_cache=True）可直接复用矩阵展示的计算结果；
    # 矩阵展示（use_cache=False）始终重算但写入缓存供后续报告导出使用
    sorted_ids_raw = sorted(t.strip() for t in task_ids if t and str(t).strip())
    cache_key = f"{'|'.join(sorted_ids_raw)}@{baseline_task_id or ''}@{scenario_key or ''}"
    if use_cache:
        now = time.time()
        cached = _matrix_cache.get(cache_key)
        if cached and now - cached[0] < _MATRIX_CACHE_TTL:
            return cached[1]

    ids = [t.strip() for t in task_ids if t and str(t).strip()]
    if len(ids) < 2:
        raise CrossTaskCockpitError("请至少选择 2 个任务进行场景对比")
    if len(ids) > 6:
        raise CrossTaskCockpitError("单次最多对比 6 个任务")

    forced_key = _normalize_matrix_scenario_key(scenario_key)
    await ensure_generic_l0_template(session)

    reference_tpl = None
    if forced_key:
        if forced_key == GENERIC_L0_SCENARIO_KEY:
            await ensure_generic_l0_template(session)
        reference_tpl = await get_template_by_key(session, forced_key)
        if not reference_tpl:
            raise CrossTaskCockpitError(f"场景模板不存在: {forced_key}")

    bundles = {tid: await _load_task_bundle(session, tid) for tid in ids}

    incomplete = [
        tid
        for tid, b in bundles.items()
        if (b.task.status or "").lower() not in ("completed", "cancelled")
    ]
    if incomplete:
        raise CrossTaskCockpitError(
            f"仅支持已完成或已取消的任务，未完成: {', '.join(incomplete[:3])}"
        )

    dataset_ids = {b.task.dataset_id for b in bundles.values() if b.task.dataset_id}
    if len(dataset_ids) != 1:
        raise CrossTaskCockpitError(
            "场景对比要求所选任务属于同一评测集，请仅勾选相同评测集下的任务"
        )
    dataset_id = next(iter(dataset_ids))
    ds = (
        await session.execute(select(Dataset).where(Dataset.id == dataset_id))
    ).scalar_one_or_none()
    dataset_name = (ds.name if ds else None) or dataset_id

    baseline_id = baseline_task_id if baseline_task_id in ids else ids[0]

    members: List[Dict[str, Any]] = []
    for tid in ids:
        b = bundles[tid]
        agent = b.agent
        members.append(
            {
                "member_id": tid,
                "task_id": tid,
                "agent_id": agent.id if agent else tid,
                "agent_name": agent.name if agent else None,
                "agent_version": agent.version if agent else None,
                "label": _member_label(b.task, agent, dataset_name=b.dataset_name),
                "is_baseline": tid == baseline_id,
            }
        )

    matrix_runs: Dict[str, Dict[str, List[AgentRun]]] = defaultdict(
        lambda: defaultdict(list)
    )
    question_labels: Dict[str, str] = {}

    for tid, b in bundles.items():
        for run in b.runs:
            if run.question_id is None:
                continue
            qk = question_key_from_run(run)
            matrix_runs[qk][tid].append(run)
            if qk not in question_labels:
                question_labels[qk] = _question_label_from_run(run, b.eval_rows)

    for qk in matrix_runs:
        for tid in matrix_runs[qk]:
            matrix_runs[qk][tid].sort(
                key=lambda r: (
                    r.run_index if r.run_index is not None else 0,
                    r.started_at or "",
                )
            )

    if not matrix_runs:
        raise CrossTaskCockpitError("所选任务均无可用 Run，无法生成场景对比")

    all_runs = [run for b in bundles.values() for run in b.runs]
    warnings: List[str] = list(
        await collect_llm_aggregate_warnings_for_task(
            session,
            all_runs,
            forced_scenario_key=forced_key,
        )
    )

    question_rows, row_warnings, templates_union = await _build_question_rows(
        session,
        forced_scenario_key=forced_key,
        member_ids=ids,
        matrix_runs=matrix_runs,
        question_labels=question_labels,
        members=members,
        baseline_member_id=baseline_id,
        use_checkpoint_config=False,
    )

    warnings.extend(row_warnings)

    if reference_tpl:
        step_columns = step_columns_from_template(reference_tpl)
        display_key = forced_key
        template_meta = {
            "name": reference_tpl.get("name"),
            "ground_truth_ref": reference_tpl.get("ground_truth_ref"),
        }
    else:
        step_columns = merge_step_columns_union(templates_union)
        keys_seen = [
            r["scenario_key"] for r in question_rows if r.get("scenario_key")
        ]
        display_key = keys_seen[0] if len(set(keys_seen)) == 1 else "auto"
        template_meta = {
            "name": "按题场景子步骤",
            "ground_truth_ref": None,
        }

    run_metrics = await load_run_metrics_for_bundles(session, bundles)
    compliance_by_task: Dict[str, Optional[float]] = {}
    for tid in ids:
        cp = await build_task_checkpoint_report(session, tid)
        compliance_by_task[tid] = (
            (cp or {}).get("summary", {}).get("avg_required_compliance_rate")
            or (cp or {}).get("summary", {}).get("avg_compliance_rate")
        )

    step_performance = await build_scenario_step_performance_summary(
        session, question_rows, ids
    )

    # 任务级稳定性摘要（多轮多题链条稳定性）
    stability_rows: List[Dict[str, Any]] = []
    for tid in ids:
        cs = await build_chain_stability(session, tid, runs=bundles[tid].runs)
        member = next((m for m in members if m["member_id"] == tid), None)
        stability_rows.append(
            {
                "member_id": tid,
                "task_id": tid,
                "label": member.get("label") if member else tid,
                "is_baseline": tid == baseline_id,
                "chain_pass_rate": cs.get("chain_pass_rate"),
                "questions_all_rounds_ok": cs.get("questions_all_rounds_ok"),
                "question_count": cs.get("question_count"),
                "max_rounds_per_question": cs.get("max_rounds_per_question"),
                "weakest_chain": cs.get("weakest_chain"),
                "has_multi_round": (cs.get("max_rounds_per_question") or 1) >= 2,
            }
        )
    stability_summaries = {"baseline_task_id": baseline_id, "rows": stability_rows}

    payload = {
        "comparison_mode": "cross_task_scenario",
        "scenario_key": display_key,
        "scenario_mode": "auto" if not forced_key else "fixed",
        "task_ids": ids,
        "baseline_member_id": baseline_id,
        "dataset_id": dataset_id,
        "dataset_name": dataset_name,
        "template": template_meta,
        "warnings": warnings,
        "members": members,
        "member_ids": ids,
        "step_columns": step_columns,
        "questions": question_rows,
        "outcome_summaries": build_cross_task_outcome_summaries(
            bundles,
            ids,
            baseline_task_id=baseline_id,
            members=members,
        ),
        "performance_summaries": build_cross_task_performance_summaries(
            bundles,
            ids,
            baseline_task_id=baseline_id,
            members=members,
            run_metrics=run_metrics,
            compliance_by_task=compliance_by_task,
        ),
        "question_runtime_summary": build_cross_task_question_runtime(
            member_ids=ids,
            matrix_runs=matrix_runs,
            question_labels=question_labels,
            eval_rows_by_task={tid: bundles[tid].eval_rows for tid in ids},
            run_metrics=run_metrics,
            baseline_task_id=baseline_id,
        ),
        "step_performance_summary": step_performance,
        "stability_summaries": stability_summaries,
    }
    payload["overall_verdict"] = await build_overall_verdict(payload)
    _matrix_cache[cache_key] = (time.time(), payload)
    return payload
