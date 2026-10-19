"""跨任务（跨版本）单题稳定性对比（P2）。

仅当基线版本与对比版本对该题均有 ≥2 轮 Run 时返回稳定性读模型；
否则 eligible=False 并给出空态原因。全部读已物化 RunSpanNode 事实。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, EvaluationResult, RunSpanNode
from services.run_step_diff_service import (
    build_round_stability_matrix,
    build_structure_summary,
)
from services.task_performance_report_service import (
    _latency_by_run,
    _load_task_runs,
    step_sequences_for_runs,
)
from services.task_scenario_matrix_service import question_key_from_run


async def _runs_for_question(
    session: AsyncSession, task_id: str, question_key: str
) -> List[AgentRun]:
    runs = await _load_task_runs(session, task_id)
    matched = [r for r in runs if question_key_from_run(r) == question_key]
    matched.sort(
        key=lambda r: (
            r.run_index if r.run_index is not None else 0,
            r.started_at or "",
        )
    )
    return matched


async def _load_run_metrics_light(
    session: AsyncSession, runs: List[AgentRun]
) -> Dict[str, Dict[str, float]]:
    """run_id -> {llm, tool} from RunSpanNode fallback."""
    from services.run_call_stats_service import summarize_call_counts_from_nodes

    if not runs:
        return {}
    # 批量查询所有 runs 的 RunSpanNode（消除 N+1）
    run_ids = [r.id for r in runs]
    all_nodes = (
        await session.execute(
            select(RunSpanNode).where(RunSpanNode.agent_run_id.in_(run_ids))
        )
    ).scalars().all()
    nodes_by_run: Dict[str, List[RunSpanNode]] = defaultdict(list)
    for n in all_nodes:
        nodes_by_run[n.agent_run_id].append(n)

    out: Dict[str, Dict[str, float]] = {}
    for run in runs:
        nodes = nodes_by_run.get(run.id, [])
        llm, tool, _ = summarize_call_counts_from_nodes(list(nodes))
        out[run.id] = {
            "llm_call_count": float(llm),
            "tool_call_count": float(tool),
        }
    return out


async def _stability_for_runs(
    session: AsyncSession,
    runs: List[AgentRun],
    *,
    eval_rows: List[EvaluationResult],
) -> Optional[Dict[str, Any]]:
    if len(runs) < 2:
        return None

    latency_map = await _latency_by_run(session, runs)
    metrics_map = await _load_run_metrics_light(session, runs)

    # 批量查询所有 runs 的 RunSpanNode（消除 N+1）
    run_ids = [r.id for r in runs]
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
    seq_tuples: List[Tuple[str, int, List[Dict[str, Any]]]] = [
        (
            run.id,
            run.run_index if run.run_index is not None else 0,
            seqs_by_run.get(run.id, []),
        )
        for run in runs
    ]

    structure = build_structure_summary(seq_tuples)
    round_stability = build_round_stability_matrix(seq_tuples)

    latencies = [float(latency_map[r.id]) for r in runs if r.id in latency_map]
    tokens = [int(r.tokens_total) for r in runs if r.tokens_total is not None]
    llms = [int(metrics_map.get(r.id, {}).get("llm_call_count") or 0) for r in runs]
    tools = [int(metrics_map.get(r.id, {}).get("tool_call_count") or 0) for r in runs]
    step_counts = [len(s[2]) for s in seq_tuples]

    unstable_steps = sum(
        1 for row in round_stability.get("rows") or [] if row.get("stability") != "stable"
    )

    return {
        "round_count": len(runs),
        "multi_round": True,
        "structure_summary": structure,
        "round_stability": round_stability,
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
        "sequence_jaccard_min_pair": structure.get("sequence_jaccard_min_pair"),
        "headline": structure.get("headline"),
        "unstable_step_count": unstable_steps,
    }


async def build_cross_task_stability_compare(
    session: AsyncSession,
    *,
    question_key: str,
    baseline_task_id: str,
    compare_task_id: str,
) -> Dict[str, Any]:
    """基线版本 vs 对比版本：同题多轮稳定性并排对比。"""
    qk = (question_key or "").strip()
    base_runs = await _runs_for_question(session, baseline_task_id, qk)
    cmp_runs = await _runs_for_question(session, compare_task_id, qk)

    base_count = len(base_runs)
    cmp_count = len(cmp_runs)
    eligible = base_count >= 2 and cmp_count >= 2

    empty_reason: Optional[str] = None
    if not eligible:
        parts: List[str] = []
        if base_count < 2:
            parts.append(f"基线版本仅 {base_count} 轮")
        if cmp_count < 2:
            parts.append(f"对比版本仅 {cmp_count} 轮")
        empty_reason = (
            "跨版本稳定性对比要求两版本对该题均执行过多轮评测（≥2 轮）。"
            f"当前：{'；'.join(parts)}。"
            "单轮任务请使用上方「Step 序列对比」查看结构差异。"
        )

    async def _eval_rows(task_id: str) -> List[EvaluationResult]:
        rows = (
            await session.execute(
                select(EvaluationResult).where(EvaluationResult.task_id == task_id)
            )
        ).scalars().all()
        return list(rows)

    baseline = None
    compare = None
    if eligible:
        baseline = await _stability_for_runs(
            session, base_runs, eval_rows=await _eval_rows(baseline_task_id)
        )
        compare = await _stability_for_runs(
            session, cmp_runs, eval_rows=await _eval_rows(compare_task_id)
        )

    vs_summary = None
    if baseline and compare:
        b_j = baseline.get("sequence_jaccard_min_pair")
        c_j = compare.get("sequence_jaccard_min_pair")
        b_spread = (baseline.get("latency_max_ms") or 0) - (
            baseline.get("latency_min_ms") or 0
        )
        c_spread = (compare.get("latency_max_ms") or 0) - (
            compare.get("latency_min_ms") or 0
        )
        vs_summary = {
            "jaccard_delta": round(c_j - b_j, 4)
            if b_j is not None and c_j is not None
            else None,
            "latency_spread_delta_ms": round(c_spread - b_spread, 1),
            "unstable_step_delta": (compare.get("unstable_step_count") or 0)
            - (baseline.get("unstable_step_count") or 0),
            "more_stable_side": (
                "baseline"
                if (b_j or 1) >= (c_j or 1)
                and (baseline.get("unstable_step_count") or 0)
                <= (compare.get("unstable_step_count") or 0)
                else "compare"
                if (c_j or 1) > (b_j or 1)
                or (compare.get("unstable_step_count") or 0)
                < (baseline.get("unstable_step_count") or 0)
                else "tie"
            ),
        }

    return {
        "question_key": qk,
        "baseline_task_id": baseline_task_id,
        "compare_task_id": compare_task_id,
        "eligible": eligible,
        "empty_reason": empty_reason,
        "baseline_round_count": base_count,
        "compare_round_count": cmp_count,
        "baseline": baseline,
        "compare": compare,
        "vs_summary": vs_summary,
    }
