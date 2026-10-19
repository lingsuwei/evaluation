"""单题运行效能跨版本对比（P0）。

按 question_key 对齐，行=题目，列=任务（Agent 版本），单元格为该版本该题的
运行时指标（时延/Tokens/LLM/工具调用），并相对基线任务给出 Δ。

全部读已物化事实（AgentRun / EvaluationResult / RunMetric），不在线重跑。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from database import AgentRun, EvaluationResult
from services.inference_duration_service import (
    RUNTIME_LATENCY_METRIC_KEY,
    resolve_agent_e2e_duration_ms,
)


def _latency_by_run(
    runs: List[AgentRun],
    eval_rows: List[EvaluationResult],
    metrics_by_run: Dict[str, Dict[str, float]],
) -> Dict[str, int]:
    """run_id -> Agent 端到端推理时延(ms)。"""
    stored: Dict[str, int] = {}
    for er in eval_rows:
        if er.agent_run_id is None or er.inference_duration_ms is None:
            continue
        if er.agent_run_id not in stored:
            stored[er.agent_run_id] = int(er.inference_duration_ms)

    out: Dict[str, int] = {}
    for run in runs:
        mm = metrics_by_run.get(run.id) or {}
        trace_lat = None
        raw = mm.get(RUNTIME_LATENCY_METRIC_KEY)
        if raw is not None:
            try:
                parsed = int(raw)
                if parsed > 0:
                    trace_lat = parsed
            except (TypeError, ValueError):
                pass
        resolved = resolve_agent_e2e_duration_ms(
            run,
            trace_latency_ms=trace_lat,
            stored_inference_duration_ms=stored.get(run.id),
        )
        if resolved is not None:
            out[run.id] = resolved
    return out


def _avg(values: List[float]) -> Optional[float]:
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)


def _representative_run(runs: List[AgentRun]) -> Optional[AgentRun]:
    """首个 success 的 Run；否则首个 Run。"""
    for r in runs:
        if (r.status or "").lower() == "success":
            return r
    return runs[0] if runs else None


def _cell_for_member(
    runs: List[AgentRun],
    latency_by_run: Dict[str, int],
    metrics_by_run: Dict[str, Dict[str, float]],
) -> Dict[str, Any]:
    if not runs:
        return {
            "missing": True,
            "run_count": 0,
            "latency_ms": None,
            "tokens_total": None,
            "llm_call_count": None,
            "tool_call_count": None,
            "trace_id": None,
            "agent_run_id": None,
        }

    latencies = [float(latency_by_run[r.id]) for r in runs if r.id in latency_by_run]
    tokens = [float(r.tokens_total) for r in runs if r.tokens_total is not None]
    llm_vals = [
        float((metrics_by_run.get(r.id) or {}).get("runtime.llm_call_count"))
        for r in runs
        if (metrics_by_run.get(r.id) or {}).get("runtime.llm_call_count") is not None
    ]
    tool_vals = [
        float((metrics_by_run.get(r.id) or {}).get("runtime.tool_call_count"))
        for r in runs
        if (metrics_by_run.get(r.id) or {}).get("runtime.tool_call_count") is not None
    ]
    rep = _representative_run(runs)
    return {
        "missing": False,
        "run_count": len(runs),
        "latency_ms": _avg(latencies),
        "tokens_total": _avg(tokens),
        "llm_call_count": _avg(llm_vals),
        "tool_call_count": _avg(tool_vals),
        "trace_id": rep.trace_id if rep else None,
        "agent_run_id": rep.id if rep else None,
    }


def _round(value: Optional[float], digits: int = 1) -> Optional[float]:
    if value is None:
        return None
    return round(value, digits)


def build_cross_task_question_runtime(
    *,
    member_ids: List[str],
    matrix_runs: Dict[str, Dict[str, List[AgentRun]]],
    question_labels: Dict[str, str],
    eval_rows_by_task: Dict[str, List[EvaluationResult]],
    run_metrics: Dict[str, Dict[str, Dict[str, float]]],
    baseline_task_id: str,
) -> Dict[str, Any]:
    """生成单题运行效能对比读模型。"""
    latency_by_task = {}
    for tid, rows in eval_rows_by_task.items():
        task_runs: List[AgentRun] = []
        for qruns in matrix_runs.values():
            task_runs.extend(qruns.get(tid) or [])
        latency_by_task[tid] = _latency_by_run(
            task_runs,
            rows,
            run_metrics.get(tid, {}),
        )

    rows: List[Dict[str, Any]] = []
    has_any_runtime = False

    for qk in sorted(matrix_runs.keys(), key=lambda k: question_labels.get(k, k)):
        per_task = matrix_runs[qk]
        cells: Dict[str, Dict[str, Any]] = {}
        question_id: Optional[int] = None

        for tid in member_ids:
            runs = per_task.get(tid) or []
            if runs and question_id is None:
                question_id = runs[0].question_id
            cells[tid] = _cell_for_member(
                runs,
                latency_by_task.get(tid, {}),
                run_metrics.get(tid, {}),
            )

        baseline_cell = cells.get(baseline_task_id) or {}
        for tid in member_ids:
            cell = cells[tid]
            if cell["missing"]:
                cell["vs_baseline"] = None
                continue
            if any(
                cell.get(k) is not None
                for k in ("latency_ms", "tokens_total", "llm_call_count", "tool_call_count")
            ):
                has_any_runtime = True
            if tid == baseline_task_id:
                cell["vs_baseline"] = None
                continue

            def _delta(key: str, digits: int = 1) -> Optional[float]:
                a = cell.get(key)
                b = baseline_cell.get(key)
                if a is None or b is None:
                    return None
                return round(a - b, digits)

            cell["vs_baseline"] = {
                "latency_delta_ms": _delta("latency_ms"),
                "tokens_delta": _delta("tokens_total"),
                "llm_delta": _delta("llm_call_count", 2),
                "tool_delta": _delta("tool_call_count", 2),
            }

        for tid in member_ids:
            cell = cells[tid]
            cell["latency_ms"] = _round(cell["latency_ms"])
            cell["tokens_total"] = _round(cell["tokens_total"])
            cell["llm_call_count"] = _round(cell["llm_call_count"], 2)
            cell["tool_call_count"] = _round(cell["tool_call_count"], 2)

        rows.append(
            {
                "question_key": qk,
                "question_id": question_id if question_id is not None else 0,
                "label": question_labels.get(qk, qk),
                "cells": cells,
            }
        )

    return {
        "baseline_task_id": baseline_task_id,
        "member_ids": member_ids,
        "rows": rows,
        "has_runtime": has_any_runtime,
    }
