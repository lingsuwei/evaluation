"""同题多轮 Run 场景子步骤聚合（P1）。"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any, Dict, List, Optional

from database import AgentRun


def _primary_run(runs: List[AgentRun]) -> AgentRun:
    """代表 Run：优先 run_index=0，其次 status=success，否则最早。"""
    if not runs:
        raise ValueError("runs empty")
    by_index = sorted(runs, key=lambda r: r.run_index if r.run_index is not None else 0)
    zero = next((r for r in by_index if (r.run_index or 0) == 0), None)
    if zero:
        return zero
    success = next((r for r in runs if r.status == "success"), None)
    return success or by_index[0]


def aggregate_steps_across_runs(
    scored_runs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    scored_runs: [{agent_run_id, run_index, composite_score, steps: [...]}, ...]
    按 step_key 聚合 score / pass_rate。
    """
    if not scored_runs:
        return []

    total_r = len(scored_runs)
    by_key: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    meta: Dict[str, Dict[str, Any]] = {}

    for rep in scored_runs:
        for step in rep.get("steps") or []:
            key = step.get("step_key") or ""
            if not key:
                continue
            by_key[key].append(step)
            if key not in meta:
                meta[key] = {
                    "step_key": key,
                    "name": step.get("name"),
                    "required": bool(step.get("required")),
                    "weight": step.get("weight"),
                    "step_order": step.get("step_order"),
                    "enforce_order": bool(step.get("enforce_order", False)),
                }

    aggregated: List[Dict[str, Any]] = []
    for key, instances in by_key.items():
        scores = [float(s.get("score") or 0) for s in instances]
        pass_count = sum(1 for s in instances if s.get("passed"))
        present = len(instances)
        mean_score = round(sum(scores) / present, 4) if present else 0.0
        std_score = (
            round(statistics.pstdev(scores), 4) if len(scores) > 1 else 0.0
        )
        min_score = round(min(scores), 4) if scores else 0.0
        row = {
            **meta[key],
            "score": mean_score,
            "passed": pass_count == total_r and present == total_r,
            "round_stats": {
                "round_count": total_r,
                "present_count": present,
                "pass_count": pass_count,
                "pass_rate": f"{pass_count}/{total_r}",
                "mean_score": mean_score,
                "min_score": min_score,
                "std_score": std_score,
            },
        }
        if instances[0].get("evidence") is not None:
            row["evidence"] = instances[0].get("evidence")
        if instances[0].get("trace_binding") is not None:
            row["trace_binding"] = instances[0].get("trace_binding")
        aggregated.append(row)

    aggregated.sort(
        key=lambda s: (
            s.get("step_order") if s.get("step_order") is not None else 10_000,
            s.get("step_key") or "",
        )
    )
    return aggregated


def aggregate_scenario_cell(
    runs: List[AgentRun],
    scored_reps: List[Dict[str, Any]],
    *,
    chain_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """将多 Run 打分结果合并为矩阵 cell。"""
    if not scored_reps:
        raise ValueError("scored_reps empty")

    primary = _primary_run(runs)
    primary_rep = scored_reps[0]
    for rep in scored_reps:
        if rep.get("agent_run_id") == primary.id:
            primary_rep = rep
            break

    composites = [float(r.get("composite_score") or 0) for r in scored_reps]
    mean_c = round(sum(composites) / len(composites), 4)
    std_c = round(statistics.pstdev(composites), 4) if len(composites) > 1 else 0.0
    min_c = round(min(composites), 4)

    required_steps = aggregate_steps_across_runs(scored_reps)
    req_pass_rates = [
        s["round_stats"]["pass_count"] / s["round_stats"]["round_count"]
        for s in required_steps
        if s.get("required")
    ]
    chain_pass_rate = (
        round(min(req_pass_rates), 4) if req_pass_rates else None
    )
    if chain_info and chain_info.get("chain_pass_rate") is not None:
        chain_pass_rate = chain_info["chain_pass_rate"]

    return {
        "agent_run_id": primary_rep.get("agent_run_id") or primary.id,
        "primary_run_index": primary.run_index if primary.run_index is not None else 0,
        "composite_score": mean_c,
        "steps": aggregate_steps_across_runs(scored_reps),
        "langfuse": primary_rep.get("langfuse"),
        "scenario_key": primary_rep.get("scenario_key"),
        "round_count": len(scored_reps),
        "runs": [
            {
                "agent_run_id": rep.get("agent_run_id"),
                "run_index": next(
                    (
                        r.run_index if r.run_index is not None else 0
                        for r in runs
                        if r.id == rep.get("agent_run_id")
                    ),
                    0,
                ),
                "composite_score": rep.get("composite_score"),
                "steps": rep.get("steps"),
                "langfuse": rep.get("langfuse"),
            }
            for rep in scored_reps
        ],
        "stability": {
            "mean": mean_c,
            "std": std_c,
            "min": min_c,
            "round_count": len(scored_reps),
        },
        "chain_pass_rate": chain_pass_rate,
        "chain_all_rounds_ok": bool(chain_info.get("all_rounds_ok")) if chain_info else None,
    }
