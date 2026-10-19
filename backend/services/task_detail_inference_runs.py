"""任务详情：按题聚合 Agent 推理轮次（inference_rounds）。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

from database import AgentRun, EvaluationResult
from services.inference_duration_service import resolve_agent_e2e_duration_ms


def model_answer_from_run(run: AgentRun) -> str:
    snap = run.output_snapshot
    if isinstance(snap, dict):
        for key in ("model_answer", "answer", "final_answer", "output"):
            val = snap.get(key)
            if val is not None and str(val).strip():
                return str(val)
    if run.error_message:
        return f"[推理失败: {run.error_message}]"
    return ""


def build_inference_runs_by_question(
    agent_runs: List[AgentRun],
    results: List[EvaluationResult],
    *,
    token_breakdown_fn,
    trace_latency_by_run: Optional[Dict[str, int]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """按 question_id 返回排序后的推理轮次列表（run_index 升序）。"""
    answers_by_run: Dict[str, str] = {}
    stored_latency_by_run: Dict[str, int] = {}
    for row in results:
        rid = row.agent_run_id
        if not rid:
            continue
        if row.agent_answer and rid not in answers_by_run:
            answers_by_run[rid] = row.agent_answer
        if (
            row.metric_name == "inference_latency"
            and row.inference_duration_ms is not None
            and row.inference_duration_ms > 0
        ):
            stored_latency_by_run[rid] = int(row.inference_duration_ms)

    trace_latency_by_run = trace_latency_by_run or {}

    grouped: Dict[int, List[AgentRun]] = defaultdict(list)
    for run in agent_runs:
        if run.question_id is None:
            continue
        grouped[int(run.question_id)].append(run)

    out: Dict[str, List[Dict[str, Any]]] = {}
    for qid in sorted(grouped.keys()):
        runs = sorted(
            grouped[qid],
            key=lambda r: (
                r.run_index if r.run_index is not None else 0,
                r.started_at or r.created_at,
            ),
        )
        items: List[Dict[str, Any]] = []
        for run in runs:
            run_index = run.run_index if run.run_index is not None else 0
            llm_tokens, tool_tokens = token_breakdown_fn(run)
            answer = model_answer_from_run(run) or answers_by_run.get(run.id, "")
            duration_ms = resolve_agent_e2e_duration_ms(
                run,
                trace_latency_ms=trace_latency_by_run.get(run.id),
                stored_inference_duration_ms=stored_latency_by_run.get(run.id),
            )
            items.append(
                {
                    "run_index": run_index,
                    "agent_run_id": run.id,
                    "agent_answer": answer,
                    "inference_duration_ms": duration_ms,
                    "tokens_total": run.tokens_total,
                    "tokens_llm": llm_tokens,
                    "tokens_tool": tool_tokens,
                    "trace_sync_status": run.trace_sync_status,
                    "trace_id": run.trace_id,
                    "status": run.status,
                    "is_primary": run_index == 0,
                }
            )
        out[str(qid)] = items
    return out
