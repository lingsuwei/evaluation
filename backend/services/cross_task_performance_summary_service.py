"""跨任务运行效能结构对比（P1：扩展 outcome_summaries）。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import RunMetric
from services.cross_task_cockpit_service import _TaskBundle


async def load_run_metrics_for_bundles(
    session: AsyncSession,
    bundles: Dict[str, _TaskBundle],
) -> Dict[str, Dict[str, Dict[str, float]]]:
    """task_id → run_id → metric_key → value（仅无 dims 的标量指标）。"""
    run_ids = [r.id for b in bundles.values() for r in b.runs]
    if not run_ids:
        return {tid: {} for tid in bundles}

    rows = (
        await session.execute(
            select(RunMetric).where(RunMetric.agent_run_id.in_(run_ids))
        )
    ).scalars().all()
    by_run: Dict[str, Dict[str, float]] = defaultdict(dict)
    for m in rows:
        if m.value_num is None:
            continue
        if isinstance(m.dims, dict) and m.dims:
            continue
        by_run[m.agent_run_id][m.metric_key] = float(m.value_num)

    out: Dict[str, Dict[str, Dict[str, float]]] = {}
    for tid, bundle in bundles.items():
        out[tid] = {r.id: dict(by_run.get(r.id) or {}) for r in bundle.runs}
    return out


def _sum_llm_tool(
    bundle: _TaskBundle,
    metrics_by_run: Dict[str, Dict[str, float]],
) -> tuple[int, int]:
    llm = 0
    tool = 0
    for run in bundle.runs:
        mm = metrics_by_run.get(run.id) or {}
        llm += int(mm.get("runtime.llm_call_count") or 0)
        tool += int(mm.get("runtime.tool_call_count") or 0)
    return llm, tool


def _avg_tokens(bundle: _TaskBundle) -> Optional[float]:
    vals = [float(r.tokens_total) for r in bundle.runs if r.tokens_total is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _avg_latency_ms(bundle: _TaskBundle) -> Optional[float]:
    vals = [
        float(r.inference_duration_ms)
        for r in bundle.eval_rows
        if r.inference_duration_ms is not None
    ]
    if not vals:
        return None
    return sum(vals) / len(vals)


def build_cross_task_performance_summaries(
    bundles: Dict[str, _TaskBundle],
    member_ids: List[str],
    *,
    baseline_task_id: str,
    members: List[Dict[str, Any]],
    run_metrics: Dict[str, Dict[str, Dict[str, float]]],
    compliance_by_task: Optional[Dict[str, Optional[float]]] = None,
) -> Dict[str, Any]:
    """任务级 LLM/工具调用结构 + Tokens/时延 + 相对 baseline Δ。"""
    compliance_by_task = compliance_by_task or {}
    label_by_id = {m["member_id"]: m.get("label") or m["member_id"] for m in members}
    stats: Dict[str, Dict[str, Optional[float]]] = {}

    for tid in member_ids:
        b = bundles[tid]
        task_metrics = run_metrics.get(tid) or {}
        llm, tool = _sum_llm_tool(b, task_metrics)
        q_count = len({r.question_id for r in b.runs if r.question_id is not None}) or max(
            len(b.runs), 1
        )
        stats[tid] = {
            "llm_call_count": float(llm),
            "tool_call_count": float(tool),
            "avg_llm_per_question": round(llm / q_count, 2) if q_count else None,
            "avg_tool_per_question": round(tool / q_count, 2) if q_count else None,
            "avg_tokens_total": _avg_tokens(b),
            "avg_inference_latency_ms": _avg_latency_ms(b),
            "checkpoint_compliance_rate": compliance_by_task.get(tid),
        }

    baseline = stats.get(baseline_task_id) or {}
    rows: List[Dict[str, Any]] = []
    for tid in member_ids:
        s = stats[tid]
        is_baseline = tid == baseline_task_id
        vs_baseline = None
        if not is_baseline:

            def _delta(key: str, digits: int = 2) -> Optional[float]:
                a = s.get(key)
                bv = baseline.get(key)
                if a is None or bv is None:
                    return None
                return round(a - bv, digits)

            vs_baseline = {
                "llm_delta": _delta("llm_call_count"),
                "tool_delta": _delta("tool_call_count"),
                "avg_llm_delta": _delta("avg_llm_per_question"),
                "avg_tool_delta": _delta("avg_tool_per_question"),
                "tokens_delta": _delta("avg_tokens_total", 1),
                "latency_delta_ms": _delta("avg_inference_latency_ms", 1),
                "compliance_delta": _delta("checkpoint_compliance_rate", 4),
            }

        rows.append(
            {
                "member_id": tid,
                "task_id": tid,
                "label": label_by_id.get(tid, tid),
                "is_baseline": is_baseline,
                "llm_call_count": int(s["llm_call_count"]),
                "tool_call_count": int(s["tool_call_count"]),
                "avg_llm_per_question": s["avg_llm_per_question"],
                "avg_tool_per_question": s["avg_tool_per_question"],
                "avg_tokens_total": s["avg_tokens_total"],
                "avg_inference_latency_ms": s["avg_inference_latency_ms"],
                "checkpoint_compliance_rate": s["checkpoint_compliance_rate"],
                "vs_baseline": vs_baseline,
            }
        )

    return {
        "baseline_task_id": baseline_task_id,
        "rows": rows,
    }
