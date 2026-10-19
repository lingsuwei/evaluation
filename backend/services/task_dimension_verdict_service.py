"""任务级评测四维结论读模型（P0-a）：纯聚合，不重算分。"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, EvaluationResult, EvaluationTask, RunSpanNode, ScenarioEvalStep, ScenarioStepResult
from services.dimension_verdict_constants import (
    CORRECTNESS_PASS_RATE_GATE,
    METRIC_DISPLAY_NAMES,
    QUALITY_GATE_TYPES,
    QUALITY_STD_GATE,
)
from services.verdict_threshold_service import (
    VerdictThresholds,
    resolve_verdict_thresholds,
    threshold_meta,
)
from services.eval_context import get_question_id
from services.evaluation_mode import get_evaluation_mode, is_offline_task
from services.process_consistency_service import (
    build_process_consistency,
    score_metrics_from_consistency_payload,
)
from services.quality_score_service import _is_quality_metric, compute_task_quality_score
from services.run_step_diff_service import build_structure_summary
from services.task_performance_report_service import (
    _effective_target,
    _load_task_runs,
    step_sequences_for_runs,
)


def _fmt_score(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{float(value):.2f}"


def _round_score(value: Optional[float]) -> Optional[float]:
    return round(float(value), 4) if value is not None else None


def _fmt_pct_fraction(value: Optional[float]) -> str:
    """0–1 小数 → 百分比字符串。"""
    if value is None:
        return "—"
    return f"{float(value) * 100:.0f}"


def _fmt_pct_percent(value: Optional[float]) -> str:
    """0–100 百分比 → 字符串。"""
    if value is None:
        return "—"
    return f"{float(value):.0f}"


async def _load_quality_metric_stats(
    session: AsyncSession, task_id: str,
    *,
    results: Optional[List[EvaluationResult]] = None,
) -> Dict[str, Dict[str, float]]:
    if results is None:
        results = (
            await session.execute(
                select(EvaluationResult).where(EvaluationResult.task_id == task_id)
            )
        ).scalars().all()
    rows = results
    buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"passed": 0, "scored": 0, "skipped": 0, "scores": []}
    )
    for row in rows:
        if not _is_quality_metric(row):
            continue
        name = (row.metric_name or "").strip()
        score = row.avg_score if row.avg_score is not None else row.score
        if score is None:
            # 跳过项(score=None)：不参与 pass_rate 分母，与 detail 接口对齐
            buckets[name]["skipped"] += 1
            continue
        buckets[name]["scored"] += 1
        if row.passed:
            buckets[name]["passed"] += 1
        buckets[name]["scores"].append(float(score))

    out: Dict[str, Dict[str, float]] = {}
    for name, data in buckets.items():
        scored = data["scored"]
        if scored <= 0:
            # 全部跳过，无有效评分，不纳入 pass_rate 统计
            continue
        scores = data["scores"]
        out[name] = {
            "pass_rate": round(data["passed"] / scored * 100, 1),
            "avg_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
            "skipped_count": data["skipped"],
            "scored_count": scored,
        }
    return out


def _verdict_quality(
    quality_score: Dict[str, Any],
    metric_stats: Dict[str, Dict[str, float]],
    thr: VerdictThresholds,
) -> Dict[str, Any]:
    total = quality_score.get("total_score")
    result = quality_score.get("result_score")
    gates = quality_score.get("gates") or []
    # 维度独立性：只认 quality 自身 gate（low_result_score / quality_metric_pass_rate_low /
    # safety_pass_rate_low / quality_variance_high）。跨维度 gate（process_consistency_fail /
    # required_checkpoint_pass_rate_low 属于过程一致性维度）只封 total_score，不翻转 quality
    # 维度的 status，也不出现在 quality.metrics.gates 里。total 封顶信号仍通过
    # quality_score.gates（raw）透传给前端分数 tooltip。
    applied_gates = [
        g for g in gates
        if g.get("applied") and g.get("type") in QUALITY_GATE_TYPES
    ]

    if not metric_stats and result is None:
        return {
            "status": "na",
            "score": None,
            "headline": "未配置或未产出质量指标",
            "tab_id": "quality",
            "metrics": {
                "total_score": total,
                "result_score": result,
                "process_score": quality_score.get("process_score"),
                "metric_pass_rates": {},
                "gates": applied_gates,
                "correctness_stability": quality_score.get("correctness_stability"),
            },
        }

    pass_rates_frac = [v["pass_rate"] / 100.0 for v in metric_stats.values()]
    worst_metric = None
    worst_rate = 101.0
    for name, stats in metric_stats.items():
        rate = stats["pass_rate"]
        if rate < worst_rate:
            worst_rate = rate
            worst_metric = name

    failed_names = [
        METRIC_DISPLAY_NAMES.get(n, n)
        for n, s in metric_stats.items()
        if s["pass_rate"] / 100.0 < thr.quality_pass_rate_warn
    ]

    status = "warn"
    if applied_gates or (
        result is not None and result < thr.quality_total_warn
    ) or any(r < thr.quality_pass_rate_warn for r in pass_rates_frac):
        status = "fail"
    elif (
        result is not None
        and result >= thr.quality_total_pass
        and pass_rates_frac
        and all(r >= thr.quality_pass_rate_pass for r in pass_rates_frac)
    ):
        status = "pass"
    elif result is None and pass_rates_frac and all(
        r >= thr.quality_pass_rate_pass for r in pass_rates_frac
    ):
        status = "pass"

    if status == "fail":
        if failed_names:
            headline = f"质量未达标：{'、'.join(failed_names[:2])} 低于阈值"
        elif applied_gates:
            headline = applied_gates[0].get("reason") or "结果分受门禁限制"
        else:
            headline = f"结果分 {_fmt_score(result)}，低于及格线"
    elif status == "pass":
        label = METRIC_DISPLAY_NAMES.get(worst_metric or "", worst_metric or "质量")
        headline = f"结果分 {_fmt_score(result)}；{label} 通过率 {_fmt_pct_percent(worst_rate)}%"
    else:
        label = METRIC_DISPLAY_NAMES.get(worst_metric or "", worst_metric or "质量")
        headline = f"结果分 {_fmt_score(result)}；{label} 通过率 {_fmt_pct_percent(worst_rate)}%，需关注"

    return {
        "status": status,
        "score": result,
        "headline": headline,
        "tab_id": "quality",
        "metrics": {
            "total_score": total,
            "result_score": result,
            "process_score": quality_score.get("process_score"),
            "metric_pass_rates": metric_stats,
            "gates": applied_gates,
            "correctness_stability": quality_score.get("correctness_stability"),
        },
    }


def _verdict_process_consistency(
    payload: Dict[str, Any], thr: VerdictThresholds
) -> Dict[str, Any]:
    summary = payload.get("summary") or {}
    questions = payload.get("questions") or []
    total_runs = summary.get("total_runs") or 0
    process_score = score_metrics_from_consistency_payload(payload).get("process_score")

    metrics = {
        "green": summary.get("green") or 0,
        "yellow": summary.get("yellow") or 0,
        "red": summary.get("red") or 0,
        "question_count": summary.get("question_count") or len(questions),
        "multi_run_questions": summary.get("multi_run_questions") or 0,
        "chain_pass_rate": summary.get("chain_pass_rate"),
        "questions_all_rounds_ok": summary.get("questions_all_rounds_ok"),
        "weakest_chain": summary.get("weakest_chain"),
        "process_score": process_score,
    }

    if total_runs == 0 and not questions:
        return {
            "status": "na",
            "score": None,
            "headline": "无 Agent Run，无法评估过程一致性",
            "tab_id": "consistency",
            "metrics": metrics,
        }

    green = metrics["green"]
    yellow = metrics["yellow"]
    red = metrics["red"]
    q_total = metrics["question_count"] or len(questions) or 0
    chain_rate = metrics["chain_pass_rate"]
    weakest = metrics["weakest_chain"]

    if red > 0:
        status = "fail"
        headline = f"{red} 题必达未过"
        if weakest:
            headline += f"；最弱 {weakest.get('name')} {weakest.get('pass_rate')}"
    elif yellow > 0 or (
        chain_rate is not None
        and chain_rate < 1.0
        and chain_rate >= thr.process_chain_pass_warn
    ):
        status = "warn"
        headline = f"{green}/{q_total} 题过程全绿" if red == 0 else f"{green}/{q_total} 题合规"
        if yellow > 0:
            headline += f"；{yellow} 题有差异"
        if chain_rate is not None and chain_rate < 1.0:
            headline += f"；链条全轮通过率 {_fmt_pct_fraction(chain_rate)}%"
    elif chain_rate is not None and chain_rate < thr.process_chain_pass_warn:
        status = "warn"
        headline = f"{green}/{q_total} 题过程全绿；链条全轮通过率 {_fmt_pct_fraction(chain_rate)}%"
    else:
        status = "pass"
        headline = f"{green}/{q_total} 题过程全绿"
        if metrics["multi_run_questions"] > 0 and chain_rate is not None:
            headline += f"；链条全轮通过率 {_fmt_pct_fraction(chain_rate)}%"

    return {
        "status": status,
        "score": _round_score(process_score) if process_score is not None else None,
        "headline": headline,
        "tab_id": "consistency",
        "metrics": metrics,
    }


async def _aggregate_scenario_stats(
    session: AsyncSession, task_id: str, thr: VerdictThresholds
) -> Dict[str, Any]:
    rows = (
        await session.execute(
            select(ScenarioStepResult, ScenarioEvalStep, AgentRun)
            .join(ScenarioEvalStep, ScenarioStepResult.step_id == ScenarioEvalStep.id)
            .join(AgentRun, ScenarioStepResult.agent_run_id == AgentRun.id)
            .where(AgentRun.task_id == task_id)
        )
    ).all()
    if not rows:
        return {
            "question_count": 0,
            "avg_composite_score": None,
            "process_score": None,
            "required_pass_rate": None,
            "required_passed": 0,
            "required_total": 0,
            "trace_evidence_coverage": None,
            "volatile_question_count": 0,
            "weakest_step": None,
            "has_data": False,
        }

    by_run: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    by_step_scores: Dict[str, List[float]] = defaultdict(list)
    step_meta: Dict[str, Dict[str, Any]] = {}
    run_question: Dict[str, int] = {}
    required_total = 0
    required_passed = 0
    evidence_total = 0
    evidence_hit = 0

    for result, step, run in rows:
        weight = float(step.weight or 0.0) or 1.0
        score = float(result.score or 0.0)
        by_run[run.id].append((score, weight))
        by_step_scores[step.step_key].append(score)
        step_meta[step.step_key] = {
            "step_key": step.step_key,
            "name": step.name,
            "required": bool(step.required),
        }
        if bool(step.required):
            required_total += 1
            if bool(result.passed):
                required_passed += 1
        evidence_total += 1
        if result.evidence or {}:
            evidence_hit += 1
        qid = get_question_id(run)
        if qid is None and run.question_id is not None:
            qid = int(run.question_id)
        if qid is not None:
            run_question[run.id] = int(qid)

    run_composites: List[float] = []
    by_question_runs: Dict[int, List[float]] = defaultdict(list)
    for run_id, items in by_run.items():
        weight_sum = sum(w for _, w in items)
        if weight_sum <= 0:
            continue
        composite = sum(s * w for s, w in items) / weight_sum
        run_composites.append(composite)
        qid = run_question.get(run_id)
        if qid is not None:
            by_question_runs[qid].append(composite)

    volatile_count = 0
    for scores in by_question_runs.values():
        if len(scores) >= 2:
            try:
                if statistics.stdev(scores) > thr.scenario_score_std_volatile:
                    volatile_count += 1
            except statistics.StatisticsError:
                pass

    weakest_key = None
    weakest_avg = 2.0
    for key, scores in by_step_scores.items():
        if not scores:
            continue
        avg = sum(scores) / len(scores)
        meta = step_meta.get(key, {})
        required = meta.get("required", False)
        if required and avg < weakest_avg:
            weakest_avg = avg
            weakest_key = key
    if weakest_key is None:
        for key, scores in by_step_scores.items():
            avg = sum(scores) / len(scores)
            if avg < weakest_avg:
                weakest_avg = avg
                weakest_key = key

    weakest_step = None
    if weakest_key is not None:
        meta = step_meta.get(weakest_key, {})
        weakest_step = {
            "step_key": weakest_key,
            "name": meta.get("name") or weakest_key,
            "avg_score": round(weakest_avg, 4),
        }

    return {
        "question_count": len(by_question_runs) or len(set(run_question.values())),
        "avg_composite_score": round(sum(run_composites) / len(run_composites), 4)
        if run_composites
        else None,
        "process_score": round(sum(run_composites) / len(run_composites), 4)
        if run_composites
        else None,
        "required_pass_rate": round(required_passed / required_total, 4)
        if required_total > 0
        else None,
        "required_passed": required_passed,
        "required_total": required_total,
        "trace_evidence_coverage": round(evidence_hit / evidence_total, 4)
        if evidence_total > 0
        else None,
        "volatile_question_count": volatile_count,
        "weakest_step": weakest_step,
        "has_data": True,
    }


def _verdict_scenario_delivery(
    scenario_stats: Dict[str, Any],
    thr: VerdictThresholds,
) -> Dict[str, Any]:
    process_score = scenario_stats.get("process_score")
    required_pass_rate = scenario_stats.get("required_pass_rate")
    required_passed = scenario_stats.get("required_passed") or 0
    required_total = scenario_stats.get("required_total") or 0
    trace_cov = scenario_stats.get("trace_evidence_coverage")

    metrics = {
        "process_score": process_score,
        "required_pass_rate": required_pass_rate,
        "required_passed": required_passed,
        "required_total": required_total,
        "trace_evidence_coverage": trace_cov,
        "question_count": scenario_stats.get("question_count"),
        "avg_composite_score": scenario_stats.get("avg_composite_score"),
        "volatile_question_count": scenario_stats.get("volatile_question_count"),
        "weakest_step": scenario_stats.get("weakest_step"),
    }

    if not scenario_stats.get("has_data") and required_total == 0 and process_score is None:
        return {
            "status": "na",
            "score": None,
            "headline": "未配置场景子步骤或未产出打分",
            "tab_id": "scenario",
            "metrics": metrics,
        }

    ps = process_score if process_score is not None else -1.0
    rp = required_pass_rate if required_pass_rate is not None else -1.0

    if (
        (process_score is not None and process_score < thr.scenario_process_warn)
        or (required_pass_rate is not None and required_pass_rate < thr.scenario_required_warn)
    ):
        status = "fail"
    elif (
        process_score is not None
        and process_score >= thr.scenario_process_pass
        and (required_pass_rate is None or required_pass_rate >= thr.scenario_required_pass)
    ):
        status = "pass"
    elif process_score is None and required_pass_rate is not None:
        status = (
            "pass"
            if required_pass_rate >= thr.scenario_required_pass
            else "warn"
            if required_pass_rate >= thr.scenario_required_warn
            else "fail"
        )
    else:
        status = "warn"

    headline = f"场景综合 {_fmt_score(process_score)}"
    if required_total > 0:
        headline += f"；必做步骤 {required_passed}/{required_total} 通过"
    weakest = scenario_stats.get("weakest_step")
    if status != "pass" and weakest:
        headline += f"；最弱 {weakest['name']} 均分 {_fmt_score(weakest['avg_score'])}"
    volatile = scenario_stats.get("volatile_question_count") or 0
    if volatile > 0:
        headline += f"；{volatile} 题多轮交付波动"

    return {
        "status": status,
        "score": process_score,
        "headline": headline,
        "tab_id": "scenario",
        "metrics": metrics,
    }


async def _structural_stability_stats(
    session: AsyncSession,
    task_id: str,
) -> Dict[str, Any]:
    runs = await _load_task_runs(session, task_id)
    if not runs:
        return {
            "multi_round_question_count": 0,
            "drift_question_count": 0,
            "drift_question_ratio": None,
            "focus_question_id": None,
            "sequence_jaccard_min": None,
        }

    # 批量预取所有 run 的 step 序列，避免逐 run 查询 RunSpanNode / trace latency 造成 N+1。
    seqs_by_run = await step_sequences_for_runs(session, runs)

    by_q: Dict[int, List[Tuple[str, int, List[Dict[str, Any]]]]] = defaultdict(list)
    for run in runs:
        qid = get_question_id(run)
        if qid is None and run.question_id is not None:
            qid = int(run.question_id)
        if qid is None:
            continue
        by_q[int(qid)].append(
            (
                run.id,
                run.run_index if run.run_index is not None else 0,
                seqs_by_run.get(run.id, []),
            )
        )

    multi_round = {q: seqs for q, seqs in by_q.items() if len(seqs) >= 2}
    if not multi_round:
        return {
            "multi_round_question_count": 0,
            "drift_question_count": 0,
            "drift_question_ratio": None,
            "focus_question_id": None,
            "sequence_jaccard_min": None,
        }

    drift_count = 0
    focus_q: Optional[int] = None
    min_jaccard: Optional[float] = None

    for qid, seqs in multi_round.items():
        summary = build_structure_summary(seqs)
        if summary.get("sometimes_missing"):
            drift_count += 1
        jac = summary.get("sequence_jaccard_min_pair")
        if jac is not None and (min_jaccard is None or jac < min_jaccard):
            min_jaccard = jac
            focus_q = qid

    multi_count = len(multi_round)
    ratio = round(drift_count / multi_count, 4) if multi_count else None

    return {
        "multi_round_question_count": multi_count,
        "drift_question_count": drift_count,
        "drift_question_ratio": ratio,
        "focus_question_id": focus_q,
        "sequence_jaccard_min": min_jaccard,
    }


async def _runtime_pass_rates(
    session: AsyncSession, task_id: str,
    *,
    results: Optional[List[EvaluationResult]] = None,
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
    if results is None:
        results = (
            await session.execute(
                select(EvaluationResult).where(EvaluationResult.task_id == task_id)
            )
        ).scalars().all()
    rows = results

    lat_rows = [r for r in rows if (r.metric_name or "") == "inference_latency"]
    tok_rows = [r for r in rows if (r.metric_name or "") == "tokens_total"]

    lat_rate = None
    if lat_rows:
        passed = sum(1 for r in lat_rows if r.passed)
        lat_rate = round(passed / len(lat_rows) * 100, 1)

    tok_rate = None
    if tok_rows:
        passed = sum(1 for r in tok_rows if r.passed)
        tok_rate = round(passed / len(tok_rows) * 100, 1)

    lat_ms = None
    runs = (
        await session.execute(
            select(AgentRun).where(AgentRun.task_id == task_id)
        )
    ).scalars().all()
    if runs:
        from services.inference_duration_service import batch_resolve_agent_e2e_duration_ms

        latency_by_run = await batch_resolve_agent_e2e_duration_ms(session, list(runs))
        if latency_by_run:
            lat_ms = round(sum(latency_by_run.values()) / len(latency_by_run), 1)

    tok_avg = None
    task = await session.get(EvaluationTask, task_id)
    if task and tok_rows:
        run_tokens = list(runs) if runs else (
            await session.execute(
                select(AgentRun).where(AgentRun.task_id == task_id)
            )
        ).scalars().all()
        tok_by_q = {
            r.question_id: r.tokens_total
            for r in run_tokens
            if r.question_id is not None and r.tokens_total is not None
        }
        tok_vals = [tok_by_q.get(r.question_id) for r in tok_rows]
        tok_vals = [t for t in tok_vals if t is not None]
        if tok_vals:
            tok_avg = round(sum(tok_vals) / len(tok_vals), 1)

    target_lat = target_tok = None
    if task:
        target_lat, _ = _effective_target(task, "inference_latency", "inference_latency_ms")
        target_tok, _ = _effective_target(task, "tokens_total", "tokens_total")

    return lat_rate, tok_rate, lat_ms, tok_avg, target_lat, target_tok


def _structural_status_and_headline(
    stats: Dict[str, Any], thr: VerdictThresholds
) -> Tuple[str, str]:
    multi = stats.get("multi_round_question_count") or 0
    if multi <= 0:
        return "na", "单轮无可对比"

    drift = stats.get("drift_question_count") or 0
    ratio = stats.get("drift_question_ratio") or 0.0

    if drift == 0:
        return "consistent", f"各轮 step 拓扑一致（{multi} 题多轮）"
    if ratio >= thr.struct_drift_ratio_significant:
        return "significant_drift", f"{drift}/{multi} 题存在非全轮 Step"
    return "occasional_drift", f"{drift}/{multi} 题存在非全轮 Step"


def _performance_status_and_headline(
    lat_rate: Optional[float],
    tok_rate: Optional[float],
    thr: VerdictThresholds,
) -> Tuple[str, str]:
    rates = [r for r in (lat_rate, tok_rate) if r is not None]
    if not rates:
        return "na", "无运行时指标"

    effective = min(rates)
    parts: List[str] = []
    if lat_rate is not None:
        parts.append(f"时延 {_fmt_pct_percent(lat_rate)}% 达标")
    if tok_rate is not None:
        parts.append(f"Token {_fmt_pct_percent(tok_rate)}% 达标")

    if effective >= thr.perf_pass_rate_within:
        status = "within_target"
    elif effective >= thr.perf_pass_rate_elevated:
        status = "elevated"
    else:
        status = "severe"

    return status, "、".join(parts)


def _quality_stability_subdimension(
    cs: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """correctness 多轮稳定性子维度（结果正确性稳定性）。"""
    cs = cs or {}
    round_count = int(cs.get("round_count", 0) or 0)
    pr = cs.get("pass_rate")
    avg_std = cs.get("avg_std")
    metrics: Dict[str, Any] = {
        "pass_rate": pr,
        "avg_std": avg_std,
        "max_std": cs.get("max_std"),
        "min_score": cs.get("min_score"),
        "round_count": round_count,
        "judge_total": cs.get("judge_total"),
        "pass_total": cs.get("pass_total"),
    }
    if round_count <= 1:
        return {
            "status": "na",
            "headline": "单轮推理，无多轮质量稳定性",
            "metrics": metrics,
        }
    if pr is not None and pr >= 0.8:
        status = "pass"
    elif pr is not None and pr >= CORRECTNESS_PASS_RATE_GATE:
        status = "warn"
    else:
        status = "fail"
    pt = cs.get("pass_total")
    jt = cs.get("judge_total")
    frac = f"{pt}/{jt}" if pt is not None and jt is not None else "—"
    headline = (
        f"多轮正确率 {frac}"
        f"（{pr * 100:.0f}%）"
        f"，质量波动 std {avg_std if avg_std is not None else '—'}"
    )
    return {"status": status, "headline": headline, "metrics": metrics}


def _verdict_runtime_stability(
    structural_stats: Dict[str, Any],
    lat_rate: Optional[float],
    tok_rate: Optional[float],
    lat_ms: Optional[float],
    tok_avg: Optional[float],
    target_lat: Optional[float],
    target_tok: Optional[float],
    offline: bool,
    thr: VerdictThresholds,
    correctness_stability: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if offline:
        return {
            "status": "descriptive",
            "headline": "离线任务不含运行稳定性评测",
            "tab_id": "perf",
            "structural": {
                "status": "na",
                "headline": "离线任务不适用",
                "metrics": structural_stats,
            },
            "performance": {
                "status": "na",
                "headline": "离线任务不适用",
                "metrics": {
                    "latency_pass_rate": None,
                    "tokens_pass_rate": None,
                    "avg_latency_ms": None,
                    "target_latency_ms": None,
                    "avg_tokens": None,
                    "target_tokens": None,
                },
            },
            "quality_stability": _quality_stability_subdimension(None),
        }

    struct_status, struct_headline = "na", "步骤结构评测已下线"
    perf_status, perf_headline = _performance_status_and_headline(lat_rate, tok_rate, thr)

    qs = _quality_stability_subdimension(correctness_stability)
    merged_parts = [f"性能：{perf_headline}"]
    if qs["status"] != "na":
        merged_parts.append(f"正确性：{qs['headline']}")
    merged = "；".join(merged_parts)

    return {
        "status": "descriptive",
        "headline": merged,
        "tab_id": "perf",
        "structural": {
            "status": struct_status,
            "headline": struct_headline,
            "metrics": structural_stats,
        },
        "performance": {
            "status": perf_status,
            "headline": perf_headline,
            "metrics": {
                "latency_pass_rate": lat_rate,
                "tokens_pass_rate": tok_rate,
                "avg_latency_ms": lat_ms,
                "target_latency_ms": target_lat,
                "target_tokens": target_tok,
                "avg_tokens": tok_avg,
            },
        },
        "quality_stability": qs,
    }


def _na_process_consistency(*, disabled: bool = False) -> Dict[str, Any]:
    return {
        "status": "disabled" if disabled else "na",
        "score": None,
        "headline": "本任务未启用过程一致性评测" if disabled else "离线任务不含过程一致性评测",
        "tab_id": "consistency",
        "metrics": {
            "green": 0,
            "yellow": 0,
            "red": 0,
            "question_count": 0,
            "multi_run_questions": 0,
            "chain_pass_rate": None,
            "questions_all_rounds_ok": None,
            "weakest_chain": None,
        },
    }


def _disabled_scenario_delivery() -> Dict[str, Any]:
    """用户在任务发起时关闭场景子步骤维度 → 结论显示 disabled。"""
    return {
        "status": "disabled",
        "score": None,
        "headline": "本任务未启用场景子步骤评测",
        "tab_id": "scenario",
        "metrics": {
            "process_score": None,
            "required_pass_rate": None,
            "required_passed": 0,
            "required_total": 0,
            "trace_evidence_coverage": None,
            "question_count": 0,
            "avg_composite_score": None,
            "volatile_question_count": 0,
            "weakest_step": None,
        },
    }


def _na_scenario_delivery() -> Dict[str, Any]:
    return {
        "status": "na",
        "score": None,
        "headline": "离线任务不含场景子步骤评测",
        "tab_id": "scenario",
        "metrics": {
            "process_score": None,
            "required_pass_rate": None,
            "required_passed": 0,
            "required_total": 0,
            "trace_evidence_coverage": None,
            "question_count": 0,
            "avg_composite_score": None,
            "volatile_question_count": 0,
            "weakest_step": None,
        },
    }


def _na_runtime_stability(*, disabled: bool = False, single_round: bool = False) -> Dict[str, Any]:
    """稳定性维度结论：disabled=用户关闭，single_round=单轮推理，否则为离线任务不适用。"""
    if disabled:
        status = "disabled"
        headline = "本任务未启用稳定性评测"
        sub_headline = "未启用稳定性评测"
    elif single_round:
        status = "na"
        headline = "单轮推理，无多轮运行稳定性评测"
        sub_headline = "单轮推理不适用"
    else:
        status = "na"
        headline = "离线任务不含运行稳定性评测"
        sub_headline = "离线任务不适用"
    return {
        "status": status,
        "headline": headline,
        "tab_id": "perf",
        "structural": {
            "status": status,
            "headline": sub_headline,
            "metrics": {
                "multi_round_question_count": 0,
                "drift_question_count": 0,
                "drift_question_ratio": None,
                "focus_question_id": None,
                "sequence_jaccard_min": None,
            },
        },
        "performance": {
            "status": status,
            "headline": sub_headline,
            "metrics": {
                "latency_pass_rate": None,
                "tokens_pass_rate": None,
                "avg_latency_ms": None,
                "target_latency_ms": None,
                "avg_tokens": None,
                "target_tokens": None,
            },
        },
        "quality_stability": _quality_stability_subdimension(None),
    }


def compact_dimension_verdict_badges(payload: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """历史列表用紧凑徽章（仅 status）。"""
    if not payload:
        return {
            "quality": "na",
            "process_consistency": "na",
            "scenario_delivery": "na",
            "runtime_performance": "na",
        }
    rs = payload.get("runtime_stability") or {}
    return {
        "quality": (payload.get("quality") or {}).get("status", "na"),
        "process_consistency": (payload.get("process_consistency") or {}).get("status", "na"),
        "scenario_delivery": (payload.get("scenario_delivery") or {}).get("status", "na"),
        "runtime_performance": (rs.get("performance") or {}).get("status", "na"),
    }


async def build_dimension_verdict_badges_for_tasks(
    session: AsyncSession,
    tasks: List[EvaluationTask],
    *,
    quality_scores: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, str]]:
    """批量构建历史列表四维徽章（仅已完成/已取消/失败任务）。

    quality_scores: 调用方已通过 compute_task_quality_scores 批量算好的结果，
    传入可避免每个 task 在 build_task_dimension_verdicts 内部重复计算。
    """
    out: Dict[str, Dict[str, str]] = {}
    for task in tasks:
        if task.status not in ("completed", "cancelled", "failed"):
            continue
        full = await build_task_dimension_verdicts(
            session,
            task.id,
            task=task,
            include_threshold_meta=False,
            quality_score=(quality_scores or {}).get(task.id),
        )
        if full:
            out[task.id] = compact_dimension_verdict_badges(full)
    return out


async def build_task_dimension_verdicts(
    session: AsyncSession,
    task_id: str,
    *,
    task: Optional[EvaluationTask] = None,
    thresholds: Optional[VerdictThresholds] = None,
    include_threshold_meta: bool = True,
    quality_score: Optional[Dict[str, Any]] = None,
    results: Optional[List[EvaluationResult]] = None,
) -> Optional[Dict[str, Any]]:
    """构建任务级四维结论；任务不存在时返回 None。

    quality_score: 调用方已计算的 compute_task_quality_score 结果，传入可避免重复计算。
    """
    tid = (task_id or "").strip()
    if task is None:
        task = await session.get(EvaluationTask, tid)
    if not task:
        return None

    thr = thresholds or resolve_verdict_thresholds(task)
    offline = is_offline_task(task)
    inference_rounds = max(1, int(getattr(task, "inference_rounds", None) or 1))
    enable_pc = bool(getattr(task, "enable_process_consistency", True))
    enable_stab = bool(getattr(task, "enable_stability", True))
    enable_scenario = bool(getattr(task, "auto_scenario_score", True))

    if quality_score is None:
        quality_score = await compute_task_quality_score(session, tid, results=results)
    metric_stats = await _load_quality_metric_stats(session, tid, results=results)
    quality = _verdict_quality(quality_score, metric_stats, thr)

    if offline:
        # 场景子步骤维度关闭时，直接返回 disabled，不查数据
        if not enable_scenario:
            scenario_delivery = _disabled_scenario_delivery()
        else:
            # 离线任务也支持场景交付打分（通过 Langfuse 历史回放物化 ScenarioStepResult）。
            # 检查是否配置了 langfuse_source：
            # - 未配置 → 返回"未启用"，提示用户配置 Langfuse 数据源
            # - 已配置但无数据 → 返回 NA，说明场景打分未产出结果
            has_langfuse = bool(getattr(task, "langfuse_source", None))
            process_eval_done = bool(getattr(task, "process_eval_completed", False))
            if not has_langfuse and not process_eval_done:
                scenario_delivery = {
                    "status": "na",
                    "score": None,
                    "headline": "离线任务未配置 Langfuse 数据源，场景交付未启用",
                    "tab_id": "scenario",
                    "metrics": {
                        "process_score": None,
                        "required_pass_rate": None,
                        "required_passed": 0,
                        "required_total": 0,
                        "trace_evidence_coverage": None,
                        "question_count": 0,
                        "avg_composite_score": None,
                        "volatile_question_count": 0,
                        "weakest_step": None,
                        "reason": "no_langfuse_source",
                    },
                }
            else:
                scenario_stats = await _aggregate_scenario_stats(session, tid, thr)
                scenario_delivery = _verdict_scenario_delivery(scenario_stats, thr)
        result = {
            "task_id": tid,
            "evaluation_mode": get_evaluation_mode(task),
            "inference_rounds": inference_rounds,
            "quality": quality,
            "process_consistency": _na_process_consistency(disabled=not enable_pc),
            "scenario_delivery": scenario_delivery,
            "runtime_stability": _na_runtime_stability(disabled=not enable_stab)
            if not enable_stab
            else _verdict_runtime_stability(
                {
                    "multi_round_question_count": 0,
                    "drift_question_count": 0,
                    "drift_question_ratio": None,
                    "focus_question_id": None,
                    "sequence_jaccard_min": None,
                },
                None,
                None,
                None,
                None,
                None,
                None,
                offline=True,
                thr=thr,
                correctness_stability=None,
            ),
        }
        if include_threshold_meta:
            result["thresholds"] = threshold_meta(task)
        return result

    process_consistency = (
        _na_process_consistency(disabled=True)
        if not enable_pc
        else _verdict_process_consistency(await build_process_consistency(session, tid), thr)
    )

    scenario_delivery = (
        _disabled_scenario_delivery()
        if not enable_scenario
        else _verdict_scenario_delivery(await _aggregate_scenario_stats(session, tid, thr), thr)
    )

    if not enable_stab:
        runtime_stability = _na_runtime_stability(disabled=True)
    elif inference_rounds <= 1:
        runtime_stability = _na_runtime_stability(single_round=True)
    else:
        lat_rate, tok_rate, lat_ms, tok_avg, target_lat, target_tok = await _runtime_pass_rates(
            session, tid, results=results
        )
        runtime_stability = _verdict_runtime_stability(
            {},
            lat_rate,
            tok_rate,
            lat_ms,
            tok_avg,
            target_lat,
            target_tok,
            offline=False,
            thr=thr,
            correctness_stability=quality_score.get("correctness_stability"),
        )

    result = {
        "task_id": tid,
        "evaluation_mode": get_evaluation_mode(task),
        "inference_rounds": inference_rounds,
        "quality": quality,
        "process_consistency": process_consistency,
        "scenario_delivery": scenario_delivery,
        "runtime_stability": runtime_stability,
    }
    if include_threshold_meta:
        result["thresholds"] = threshold_meta(task)
    return result


_VERDICT_STATUS_CN = {
    "pass": "达标",
    "warn": "需关注",
    "fail": "未达标",
    "na": "不适用",
}

_STRUCTURAL_STATUS_CN = {
    "consistent": "结构一致",
    "occasional_drift": "偶发波动",
    "significant_drift": "明显波动",
    "na": "不适用",
}

_PERFORMANCE_STATUS_CN = {
    "within_target": "性能达标",
    "elevated": "性能偏高",
    "severe": "严重偏高",
    "na": "不适用",
}


def format_dimension_verdicts_for_summary(payload: Optional[Dict[str, Any]]) -> str:
    """将四维结论格式化为 LLM 总结 prompt 用文本。"""
    if not payload:
        return "（四维结论暂无）"

    lines: List[str] = []

    quality = payload.get("quality") or {}
    q_status = _VERDICT_STATUS_CN.get(quality.get("status"), quality.get("status"))
    q_score = quality.get("score")
    q_line = f"质量指标 [{q_status}]"
    if q_score is not None:
        q_line += f" 总分 {q_score}"
    q_line += f"：{quality.get('headline') or '—'}"
    lines.append(q_line)

    process = payload.get("process_consistency") or {}
    if process.get("status") != "na":
        p_status = _VERDICT_STATUS_CN.get(process.get("status"), process.get("status"))
        lines.append(f"过程一致性 [{p_status}]：{process.get('headline') or '—'}")
    else:
        lines.append("过程一致性：不适用（离线或无 Agent Run）")

    scenario = payload.get("scenario_delivery") or {}
    if scenario.get("status") != "na":
        s_status = _VERDICT_STATUS_CN.get(scenario.get("status"), scenario.get("status"))
        s_line = f"场景子步骤交付 [{s_status}]"
        if scenario.get("score") is not None:
            s_line += f" 综合分 {scenario.get('score')}"
        s_line += f"：{scenario.get('headline') or '—'}"
        lines.append(s_line)
    else:
        lines.append("场景子步骤交付：不适用")

    runtime = payload.get("runtime_stability") or {}
    if runtime.get("performance", {}).get("status") == "na":
        lines.append("运行稳定性：不适用")
    else:
        performance = runtime.get("performance") or {}
        perf_label = _PERFORMANCE_STATUS_CN.get(
            performance.get("status"), performance.get("status")
        )
        lines.append(f"运行稳定性：{runtime.get('headline') or '—'}")
        lines.append(f"  · 性能 [{perf_label}]：{performance.get('headline') or '—'}")
        qs = runtime.get("quality_stability") or {}
        if qs.get("status") not in ("na", None):
            lines.append(f"  · 结果正确性稳定性 [{_VERDICT_STATUS_CN.get(qs.get('status'), qs.get('status'))}]：{qs.get('headline') or '—'}")

    return "\n".join(lines)
