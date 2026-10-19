"""任务级运行效能报告（P0）。"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import METRICS_CONFIG
from database import AgentRun, EvaluationResult, EvaluationTask, RunMetric, RunSpanNode
from services.checkpoint_cost_attribution_service import build_checkpoint_cost_attribution
from services.checkpoint_result_service import build_task_checkpoint_report
from services.eval_context import get_question_id, get_task_id
from services.run_analytics_view import fetch_run_analytics_rows_for_task
from services.run_call_stats_service import (
    backfill_step_sequence_latencies,
    build_step_sequence,
    summarize_call_counts_from_nodes,
)
from services.runtime_target_suggestion_service import build_task_run_distribution
from services.task_agent_summary import build_task_agent_summary
from services.fact_materialization_status import (
    compute_fact_materialization_status,
    summarize_trace_sync_counts,
)
from services.inference_duration_service import (
    batch_load_stored_inference_duration_ms,
    batch_load_trace_latency_ms,
    batch_resolve_agent_e2e_duration_ms,
    resolve_agent_e2e_duration_ms,
)
from services.chain_stability_service import build_chain_stability


async def _inference_duration_ms_for_run(
    session: AsyncSession, run: AgentRun
) -> Optional[int]:
    """解析该 Run 的 Agent 端到端推理时延（毫秒）。"""
    trace_lat = (await batch_load_trace_latency_ms(session, [run.id])).get(run.id)
    stored = (await batch_load_stored_inference_duration_ms(session, [run.id])).get(run.id)
    return resolve_agent_e2e_duration_ms(
        run, trace_latency_ms=trace_lat, stored_inference_duration_ms=stored
    )


async def _latency_by_run(
    session: AsyncSession, runs: List[AgentRun]
) -> Dict[str, int]:
    return await batch_resolve_agent_e2e_duration_ms(session, runs)


async def _step_sequence_for_run(
    session: AsyncSession,
    run: AgentRun,
    nodes: List[RunSpanNode],
) -> List[Dict[str, Any]]:
    infer_ms = await _inference_duration_ms_for_run(session, run)
    seq = build_step_sequence(list(nodes))
    return backfill_step_sequence_latencies(
        seq, run, inference_duration_ms=infer_ms
    )


async def step_sequences_for_runs(
    session: AsyncSession,
    runs: List[AgentRun],
    nodes_by_run: Optional[Dict[str, List[RunSpanNode]]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """批量构建多个 run 的 step 序列，避免逐 run 查询导致的 N+1。

    一次性加载所有 run 的 RunSpanNode 与 trace latency，再在内存里组装。
    若调用方已批量预取过 RunSpanNode，可通过 nodes_by_run 传入复用，跳过本函数内的查询。
    返回 {run_id: step_sequence}。
    """
    if not runs:
        return {}
    run_ids = [r.id for r in runs]
    if nodes_by_run is None:
        nodes_rows = (
            await session.execute(
                select(RunSpanNode)
                .where(RunSpanNode.agent_run_id.in_(run_ids))
                .order_by(
                    RunSpanNode.agent_run_id,
                    RunSpanNode.step_index,
                    RunSpanNode.sibling_order,
                    RunSpanNode.id,
                )
            )
        ).scalars().all()
        nodes_by_run = defaultdict(list)
        for n in nodes_rows:
            nodes_by_run[n.agent_run_id].append(n)
    trace_lat_by_run = await batch_load_trace_latency_ms(session, run_ids)
    result: Dict[str, List[Dict[str, Any]]] = {}
    for run in runs:
        infer_ms = resolve_agent_e2e_duration_ms(
            run, trace_latency_ms=trace_lat_by_run.get(run.id)
        )
        seq = build_step_sequence(list(nodes_by_run.get(run.id, [])))
        result[run.id] = backfill_step_sequence_latencies(
            seq, run, inference_duration_ms=infer_ms
        )
    return result


def _runtime_targets_from_task(task: EvaluationTask) -> Dict[str, float]:
    raw = getattr(task, "runtime_metric_targets", None)
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        return {}


def _effective_target(task: EvaluationTask, metric_key: str, field: str) -> Tuple[float, str]:
    """返回 (阈值, task_custom|system_default)。"""
    custom = _runtime_targets_from_task(task)
    if field in custom and custom[field] is not None:
        return float(custom[field]), "task_custom"
    if metric_key in METRICS_CONFIG:
        return float(METRICS_CONFIG[metric_key]), "system_default"
    return 0.0, "system_default"


def _target_status(current: Optional[float], threshold: float) -> Optional[str]:
    if current is None or threshold <= 0:
        return None
    return "ok" if current <= threshold else "warn"


def _build_target_comparison(
    summary: Dict[str, Any],
    task: EvaluationTask,
    distribution: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    lat_ms, lat_src = _effective_target(task, "inference_latency", "inference_latency_ms")
    tok, tok_src = _effective_target(task, "tokens_total", "tokens_total")
    avg_lat = summary.get("avg_latency_ms")
    avg_tok = summary.get("avg_tokens_per_question")
    task_lat = (distribution or {}).get("inference_latency_ms") or {}
    task_tok = (distribution or {}).get("tokens_total") or {}

    return {
        "inference_latency_ms": {
            "current_ms": avg_lat,
            "target_ms": lat_ms,
            "target_source": lat_src,
            "status": _target_status(avg_lat, lat_ms),
            "task_p75": task_lat.get("p75"),
            "task_p90": task_lat.get("p90"),
        },
        "tokens_total": {
            "current_avg": avg_tok,
            "target": tok,
            "target_source": tok_src,
            "status": _target_status(avg_tok, tok),
            "task_p75": task_tok.get("p75"),
            "task_p90": task_tok.get("p90"),
        },
    }


async def _load_task_runs(session: AsyncSession, task_id: str) -> List[AgentRun]:
    tid = (task_id or "").strip()
    result = await session.execute(
        select(AgentRun)
        .where(
            or_(
                AgentRun.task_id == tid,
                AgentRun.eval_context["task_id"].as_string() == tid,
            )
        )
        .order_by(AgentRun.question_id, AgentRun.run_index, AgentRun.started_at)
    )
    runs = list(result.scalars().all())
    if not runs:
        fallback = await session.execute(select(AgentRun))
        runs = [r for r in fallback.scalars().all() if get_task_id(r) == tid]
    if not runs:
        er_run_ids = (
            await session.execute(
                select(EvaluationResult.agent_run_id).where(
                    EvaluationResult.task_id == tid,
                    EvaluationResult.agent_run_id.isnot(None),
                )
            )
        ).scalars().all()
        unique_ids = sorted({rid for rid in er_run_ids if rid})
        if unique_ids:
            runs = list(
                (
                    await session.execute(
                        select(AgentRun)
                        .where(AgentRun.id.in_(unique_ids))
                        .order_by(AgentRun.question_id, AgentRun.run_index, AgentRun.started_at)
                    )
                ).scalars().all()
            )
    return runs


async def _runs_linked_to_task(
    session: AsyncSession, task_id: str, run_ids: List[str]
) -> bool:
    tid = (task_id or "").strip()
    if not tid or not run_ids:
        return False
    rows = (
        await session.execute(
            select(EvaluationResult.agent_run_id).where(
                EvaluationResult.task_id == tid,
                EvaluationResult.agent_run_id.in_(run_ids),
            )
        )
    ).scalars().all()
    linked = {rid for rid in rows if rid}
    return all(rid in linked for rid in run_ids)


async def _load_correctness_by_run(
    session: AsyncSession, task_id: str
) -> Dict[str, Tuple[float, bool]]:
    """加载 correctness 指标每轮评分：agent_run_id → (score, passed)。"""
    rows = (
        await session.execute(
            select(EvaluationResult)
            .where(
                EvaluationResult.task_id == task_id,
                EvaluationResult.metric_name == "correctness",
                EvaluationResult.agent_run_id.isnot(None),
            )
        )
    ).scalars().all()
    out: Dict[str, Tuple[float, bool]] = {}
    for r in rows:
        score = r.avg_score if r.avg_score is not None else r.score
        if score is None:
            continue
        out[r.agent_run_id] = (float(score), bool(r.passed))
    return out


async def _load_run_metrics_map(
    session: AsyncSession, run_ids: List[str]
) -> Dict[str, Dict[str, Any]]:
    if not run_ids:
        return {}
    rows = (
        await session.execute(
            select(RunMetric).where(RunMetric.agent_run_id.in_(run_ids))
        )
    ).scalars().all()
    out: Dict[str, Dict[str, Any]] = defaultdict(dict)
    tool_breakdown: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for m in rows:
        rid = m.agent_run_id
        key = m.metric_key
        if key == "runtime.tool_call_count" and isinstance(m.dims, dict) and m.dims.get("tool"):
            tool_breakdown[rid][str(m.dims["tool"])] = float(m.value_num or 0)
        elif m.value_num is not None and not (isinstance(m.dims, dict) and m.dims):
            out[rid][key] = float(m.value_num)
    for rid, tools in tool_breakdown.items():
        out[rid]["_tool_dims"] = dict(tools)
    return dict(out)


def _metric_val(metrics: Dict[str, Any], key: str) -> Optional[float]:
    v = metrics.get(key)
    return float(v) if v is not None else None


def _aggregate_tool_breakdown(
    runs: List[AgentRun], metrics_map: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    totals: Dict[str, float] = defaultdict(float)
    for run in runs:
        dims = (metrics_map.get(run.id) or {}).get("_tool_dims") or {}
        for name, cnt in dims.items():
            totals[name] += float(cnt)
    if not totals:
        return []
    grand = sum(totals.values()) or 1.0
    return sorted(
        [
            {
                "tool": name,
                "count": int(cnt),
                "share": round(cnt / grand, 4),
            }
            for name, cnt in totals.items()
        ],
        key=lambda x: -x["count"],
    )


async def build_task_performance_report(
    session: AsyncSession,
    task_id: str,
) -> Optional[Dict[str, Any]]:
    task = await session.get(EvaluationTask, task_id)
    if not task:
        return None

    runs = await _load_task_runs(session, task_id)
    run_ids = [r.id for r in runs]
    metrics_map = await _load_run_metrics_map(session, run_ids)
    latency_map = await _latency_by_run(session, runs)
    correctness_map = await _load_correctness_by_run(session, task_id)
    sync_summary = summarize_trace_sync_counts(runs)
    agent_summary = await build_task_agent_summary(session, task)
    checkpoint_report = await build_task_checkpoint_report(session, task_id)

    llm_total = 0
    tool_total = 0
    tokens_total = 0
    latency_vals: List[int] = []
    token_run_vals: List[float] = []
    tokens_llm_vals: List[float] = []
    tokens_tool_vals: List[float] = []
    loop_vals: List[float] = []

    run_rows: List[Dict[str, Any]] = []
    # 批量预取 RunSpanNode 与 step 序列，消除循环内逐 run 查询的 N+1。
    all_nodes = (
        await session.execute(
            select(RunSpanNode)
            .where(RunSpanNode.agent_run_id.in_(run_ids))
            .order_by(
                RunSpanNode.agent_run_id,
                RunSpanNode.step_index,
                RunSpanNode.sibling_order,
                RunSpanNode.id,
            )
        )
    ).scalars().all()
    nodes_by_run: Dict[str, List[RunSpanNode]] = defaultdict(list)
    for n in all_nodes:
        nodes_by_run[n.agent_run_id].append(n)
    seqs_by_run = await step_sequences_for_runs(session, runs, nodes_by_run=nodes_by_run)
    for run in runs:
        mm = metrics_map.get(run.id) or {}
        llm_c = int(_metric_val(mm, "runtime.llm_call_count") or 0)
        tool_c = int(_metric_val(mm, "runtime.tool_call_count") or 0)
        if tool_c == 0:
            nodes = nodes_by_run.get(run.id, [])
            if nodes:
                llm_c, tool_c, _ = summarize_call_counts_from_nodes(list(nodes))
        llm_total += llm_c
        tool_total += tool_c
        tok = run.tokens_total
        if tok:
            tokens_total += int(tok)
        lat = latency_map.get(run.id)
        if lat is not None:
            latency_vals.append(lat)
        if tok is not None and tok > 0:
            token_run_vals.append(float(tok))
        t_llm = _metric_val(mm, "runtime.tokens_llm")
        t_tool = _metric_val(mm, "runtime.tokens_tool")
        if t_llm is not None:
            tokens_llm_vals.append(t_llm)
        if t_tool is not None:
            tokens_tool_vals.append(t_tool)
        loop_c = _metric_val(mm, "runtime.loop_iteration_count")
        if loop_c is not None:
            loop_vals.append(loop_c)

        qid = get_question_id(run)
        if qid is None:
            qid = run.question_id
        seq = seqs_by_run.get(run.id, [])
        summary_parts = []
        for s in seq[:8]:
            summary_parts.append("LLM" if s["kind"] == "llm" else s["label"][:12])
        run_rows.append(
            {
                "agent_run_id": run.id,
                "question_id": qid,
                "run_index": run.run_index if run.run_index is not None else 0,
                "llm_call_count": llm_c,
                "tool_call_count": tool_c,
                "latency_ms": lat,
                "tokens_total": run.tokens_total,
                "trace_sync_status": run.trace_sync_status,
                "step_summary": "→".join(summary_parts) if summary_parts else None,
                "step_count": len(seq),
                "steps": seq,
                "correctness_score": correctness_map.get(run.id, (None, None))[0],
                "correctness_passed": correctness_map.get(run.id, (None, None))[1],
            }
        )

    q_count = len({r["question_id"] for r in run_rows if r["question_id"] is not None})
    q_count = q_count or max(len(run_rows), 1)

    slowest = sorted(
        [r for r in run_rows if r.get("latency_ms") is not None],
        key=lambda x: x["latency_ms"],
        reverse=True,
    )[:5]
    costliest = sorted(
        [r for r in run_rows if r.get("tokens_total") is not None],
        key=lambda x: x["tokens_total"],
        reverse=True,
    )[:5]

    cp_summary = checkpoint_report.get("summary") if checkpoint_report else {}
    run_distribution = build_task_run_distribution(
        [float(v) for v in latency_vals],
        token_run_vals,
    )

    runs_by_question: Dict[int, int] = defaultdict(int)
    for r in run_rows:
        q = r.get("question_id")
        if q is not None:
            runs_by_question[int(q)] += 1
    max_rounds = max(
        int(getattr(task, "inference_rounds", None) or 0),
        max(runs_by_question.values()) if runs_by_question else 1,
    )
    max_rounds = max(1, max_rounds)

    structure_summary = None
    chain_stability = None
    stability_questions: List[Dict[str, Any]] = []
    stability_focus: Optional[Dict[str, Any]] = None
    if max_rounds > 1:
        from services.run_step_diff_service import build_structure_summary

        chain_stability = await build_chain_stability(session, task_id, runs)
        by_q: Dict[int, List[Tuple[str, int, List[Dict[str, Any]]]]] = defaultdict(list)
        for run in runs:
            qid = get_question_id(run)
            if qid is None:
                qid = run.question_id
            if qid is None:
                continue
            by_q[int(qid)].append(
                (
                    run.id,
                    run.run_index if run.run_index is not None else 0,
                    seqs_by_run.get(run.id, []),
                )
            )
        if by_q:
            worst_q = min(
                by_q.keys(),
                key=lambda q: build_structure_summary(by_q[q]).get(
                    "sequence_jaccard_min_pair"
                )
                or 1.0,
            )
            structure_summary = build_structure_summary(by_q[worst_q])
            structure_summary["question_id"] = worst_q

            rows_by_q: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
            for row in run_rows:
                q = row.get("question_id")
                if q is not None:
                    rows_by_q[int(q)].append(row)

            for qid, seqs in by_q.items():
                if len(seqs) < 2:
                    continue
                ss = build_structure_summary(seqs)
                q_rows = rows_by_q.get(qid, [])
                latencies = [
                    float(r["latency_ms"])
                    for r in q_rows
                    if r.get("latency_ms") is not None
                ]
                tokens = [
                    int(r["tokens_total"])
                    for r in q_rows
                    if r.get("tokens_total") is not None
                ]
                llms = [int(r.get("llm_call_count") or 0) for r in q_rows]
                tools = [int(r.get("tool_call_count") or 0) for r in q_rows]
                step_counts = [len(s[2]) for s in seqs]
                corr_scores = [
                    float(r["correctness_score"])
                    for r in q_rows
                    if r.get("correctness_score") is not None
                ]
                corr_passes = sum(1 for r in q_rows if r.get("correctness_passed"))
                corr_total = len(corr_scores)

                stability_questions.append(
                    {
                        "question_id": qid,
                        "round_count": len(seqs),
                        "step_count_min": min(step_counts) if step_counts else None,
                        "step_count_max": max(step_counts) if step_counts else None,
                        "latency_min_ms": round(min(latencies), 1) if latencies else None,
                        "latency_max_ms": round(max(latencies), 1) if latencies else None,
                        "tokens_min": min(tokens) if tokens else None,
                        "tokens_max": max(tokens) if tokens else None,
                        "llm_call_min": min(llms) if llms else None,
                        "llm_call_max": max(llms) if llms else None,
                        "tool_call_min": min(tools) if tools else None,
                        "tool_call_max": max(tools) if tools else None,
                        "sequence_jaccard_min_pair": ss.get("sequence_jaccard_min_pair"),
                        "headline": ss.get("headline"),
                        "is_focus": qid == worst_q,
                        "correctness_mean": round(sum(corr_scores) / len(corr_scores), 4)
                        if corr_scores
                        else None,
                        "correctness_min": round(min(corr_scores), 4)
                        if corr_scores
                        else None,
                        "correctness_max": round(max(corr_scores), 4)
                        if corr_scores
                        else None,
                        "correctness_std": round(statistics.stdev(corr_scores), 4)
                        if len(corr_scores) >= 2
                        else 0.0,
                        "correctness_pass_count": corr_passes,
                        "correctness_pass_rate": round(corr_passes / corr_total, 4)
                        if corr_total > 0
                        else None,
                    }
                )
            stability_questions.sort(
                key=lambda x: (
                    x.get("sequence_jaccard_min_pair")
                    if x.get("sequence_jaccard_min_pair") is not None
                    else 1.0,
                    -((x.get("latency_max_ms") or 0) - (x.get("latency_min_ms") or 0)),
                )
            )
            if stability_questions:
                focus_row = next(
                    (sq for sq in stability_questions if sq["is_focus"]),
                    stability_questions[0],
                )
                stability_focus = {
                    "question_id": focus_row["question_id"],
                    "reason": "structure_volatility",
                }

    logical_agent_id = None
    if agent_summary and isinstance(agent_summary, dict):
        logical_agent_id = agent_summary.get("logical_agent_id")
    checkpoint_attribution = await build_checkpoint_cost_attribution(
        session, task_id, logical_agent_id=logical_agent_id
    )

    return {
        "task_id": task_id,
        "agent_summary": agent_summary,
        "materialization": compute_fact_materialization_status(
            sync_summary, agent_run_count=len(runs)
        ),
        "summary": {
            "run_count": len(runs),
            "question_count": q_count,
            "inference_rounds": max_rounds,
            "multi_round_mode": max_rounds > 1,
            "llm_call_count": llm_total,
            "tool_call_count": tool_total,
            "avg_llm_per_question": round(llm_total / q_count, 2) if q_count else None,
            "avg_tool_per_question": round(tool_total / q_count, 2) if q_count else None,
            "tokens_total": tokens_total if tokens_total else None,
            "avg_tokens_per_question": round(tokens_total / q_count, 1)
            if tokens_total and q_count
            else None,
            "avg_latency_ms": round(sum(latency_vals) / len(latency_vals), 1)
            if latency_vals
            else None,
            "tokens_llm_total": int(sum(tokens_llm_vals)) if tokens_llm_vals else None,
            "tokens_tool_total": int(sum(tokens_tool_vals)) if tokens_tool_vals else None,
            "checkpoint_compliance_rate": cp_summary.get("avg_required_compliance_rate")
            or cp_summary.get("avg_compliance_rate"),
            "checkpoint_optional_hit_rate": cp_summary.get("avg_optional_hit_rate"),
            "avg_loop_iterations": round(sum(loop_vals) / len(loop_vals), 2)
            if loop_vals
            else None,
        },
        "runtime_targets": _runtime_targets_from_task(task),
        "target_comparison": _build_target_comparison(
            {
                "avg_latency_ms": round(sum(latency_vals) / len(latency_vals), 1)
                if latency_vals
                else None,
                "avg_tokens_per_question": round(tokens_total / q_count, 1)
                if tokens_total and q_count
                else None,
            },
            task,
            run_distribution,
        ),
        "tool_breakdown": _aggregate_tool_breakdown(runs, metrics_map),
        "run_rows": run_rows,
        "focus_questions": {
            "slowest": slowest,
            "costliest": costliest,
        },
        "run_distribution": run_distribution,
        "checkpoint_summary": cp_summary,
        "checkpoint_cost_attribution": checkpoint_attribution,
        "structure_summary": structure_summary,
        "chain_stability": chain_stability,
        "stability_questions": stability_questions,
        "stability_focus": stability_focus,
    }


async def build_run_step_compare(
    session: AsyncSession,
    *,
    left_run_id: str,
    right_run_id: str,
    task_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    from services.run_step_diff_service import (
        align_steps,
        build_diff_summary,
        build_round_stability_matrix,
        build_structure_summary,
    )

    left = await session.get(AgentRun, left_run_id)
    right = await session.get(AgentRun, right_run_id)
    if not left or not right:
        return None

    if task_id:
        tid = (task_id or "").strip()
        left_tid = get_task_id(left) or left.task_id
        right_tid = get_task_id(right) or right.task_id
        if left_tid != tid or right_tid != tid:
            if not await _runs_linked_to_task(session, tid, [left_run_id, right_run_id]):
                return None

    async def _load_seq(run: AgentRun) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        nodes = (
            await session.execute(
                select(RunSpanNode)
                .where(RunSpanNode.agent_run_id == run.id)
                .order_by(RunSpanNode.step_index, RunSpanNode.sibling_order, RunSpanNode.id)
            )
        ).scalars().all()
        seq = await _step_sequence_for_run(session, run, list(nodes))
        llm, tool, _ = summarize_call_counts_from_nodes(list(nodes))
        return seq, {
            "llm_call_count": llm,
            "tool_call_count": tool,
            "step_count": len(seq),
        }

    left_seq, left_meta = await _load_seq(left)
    right_seq, right_meta = await _load_seq(right)
    aligned = align_steps(left_seq, right_seq)
    bullets = build_diff_summary(
        aligned, left_meta=left_meta, right_meta=right_meta
    )

    qid = get_question_id(left) if get_question_id(left) is not None else left.question_id
    task_id = get_task_id(left) or left.task_id
    stability = {"columns": [], "rows": []}
    structure_summary = None
    same_q: List[AgentRun] = []
    if task_id and qid is not None:
        all_runs = await _load_task_runs(session, task_id)
        same_q = [
            r
            for r in all_runs
            if (get_question_id(r) if get_question_id(r) is not None else r.question_id)
            == qid
        ]
        if len(same_q) >= 2:
            same_q_seqs = await step_sequences_for_runs(session, same_q)
            seq_tuples = [
                (
                    r.id,
                    r.run_index if r.run_index is not None else 0,
                    same_q_seqs.get(r.id, []),
                )
                for r in same_q
            ]
            stability = build_round_stability_matrix(seq_tuples)
            structure_summary = build_structure_summary(seq_tuples)

    return {
        "left_run_id": left_run_id,
        "right_run_id": right_run_id,
        "question_id": qid,
        "left": {
            "run_index": left.run_index,
            "trace_id": left.trace_id,
            **left_meta,
            "steps": left_seq,
        },
        "right": {
            "run_index": right.run_index,
            "trace_id": right.trace_id,
            **right_meta,
            "steps": right_seq,
        },
        "diff_summary": bullets,
        "aligned_steps": aligned,
        "round_stability": stability,
        "structure_summary": structure_summary,
    }
