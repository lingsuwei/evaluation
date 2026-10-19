"""跨任务 Outcome / Cost 任务级摘要（方案 B：替代任务内版本实验 Tab）。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from services.cross_task_cockpit_service import _TaskBundle

_RUNTIME_METRICS = frozenset({"inference_latency", "tokens_total"})


def _is_quality_row(row) -> bool:
    name = (getattr(row, "metric_name", None) or "").strip().lower()
    if name in _RUNTIME_METRICS:
        return False
    if (getattr(row, "eval_method", None) or "").strip().lower() == "runtime":
        return False
    return True


def _avg_correctness(bundle: _TaskBundle) -> Optional[float]:
    scores: List[float] = []
    for row in bundle.eval_rows:
        if not _is_quality_row(row):
            continue
        name = (row.metric_name or "").lower()
        if name != "correctness":
            continue
        val = row.avg_score if row.avg_score is not None else row.score
        if val is not None:
            scores.append(float(val))
    if not scores:
        for row in bundle.eval_rows:
            if not _is_quality_row(row):
                continue
            val = row.avg_score if row.avg_score is not None else row.score
            if val is not None:
                scores.append(float(val))
    if not scores:
        return None
    return sum(scores) / len(scores)


def _pass_rate(bundle: _TaskBundle) -> Optional[float]:
    passed = 0
    total = 0
    for row in bundle.eval_rows:
        if not _is_quality_row(row):
            continue
        if row.passed is None:
            continue
        total += 1
        if row.passed:
            passed += 1
    if total == 0:
        return None
    return passed / total


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


def build_cross_task_outcome_summaries(
    bundles: Dict[str, _TaskBundle],
    member_ids: List[str],
    *,
    baseline_task_id: str,
    members: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """按任务列汇总质量/效率指标，并相对 baseline 计算 Δ。"""
    label_by_id = {m["member_id"]: m.get("label") or m["member_id"] for m in members}
    stats: Dict[str, Dict[str, Optional[float]]] = {}
    for tid in member_ids:
        b = bundles[tid]
        stats[tid] = {
            "avg_correctness": _avg_correctness(b),
            "pass_rate": _pass_rate(b),
            "avg_tokens_total": _avg_tokens(b),
            "avg_inference_latency_ms": _avg_latency_ms(b),
        }

    baseline = stats.get(baseline_task_id) or {}
    rows: List[Dict[str, Any]] = []
    for tid in member_ids:
        s = stats[tid]
        is_baseline = tid == baseline_task_id
        vs_baseline = None
        if not is_baseline:

            def _delta(key: str) -> Optional[float]:
                a = s.get(key)
                b = baseline.get(key)
                if a is None or b is None:
                    return None
                return round(a - b, 4 if key == "avg_correctness" else 1)

            vs_baseline = {
                "correctness_delta": _delta("avg_correctness"),
                "pass_rate_delta": _delta("pass_rate"),
                "tokens_delta": _delta("avg_tokens_total"),
                "latency_delta_ms": _delta("avg_inference_latency_ms"),
            }
        rows.append(
            {
                "member_id": tid,
                "task_id": tid,
                "label": label_by_id.get(tid, tid),
                "is_baseline": is_baseline,
                "avg_correctness": s["avg_correctness"],
                "pass_rate": s["pass_rate"],
                "avg_tokens_total": s["avg_tokens_total"],
                "avg_inference_latency_ms": s["avg_inference_latency_ms"],
                "vs_baseline": vs_baseline,
            }
        )

    return {
        "baseline_task_id": baseline_task_id,
        "rows": rows,
    }
