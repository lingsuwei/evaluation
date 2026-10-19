"""任务级版本实验（P3-B1）。"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, Experiment, ExperimentSummary
from services.diff.diff_registry import resolve_experiment_diff_types
from services.diff.diff_types import OUTCOME_DIFF, SEMANTIC_DIFF, STRUCTURAL_DIFF
from services.diff.semantic_diff_calculator import compact_semantic_payload
from services.diff.structural_diff_calculator import compact_structural_payload
from services.run_comparison_service import compare_runs


class ExperimentError(ValueError):
    pass


def _group_runs_by_question_agent(
    runs: List[AgentRun],
) -> Dict[int, Dict[str, AgentRun]]:
    matrix: Dict[int, Dict[str, AgentRun]] = defaultdict(dict)
    for run in runs:
        if run.question_id is None:
            continue
        matrix[run.question_id][run.agent_id] = run
    return dict(matrix)


def _pick_baseline_agent_id(
    matrix: Dict[int, Dict[str, AgentRun]],
    preferred: Optional[str],
) -> Optional[str]:
    if preferred:
        return preferred
    agent_ids: set[str] = set()
    for per_q in matrix.values():
        agent_ids.update(per_q.keys())
    if not agent_ids:
        return None
    return sorted(agent_ids)[0]


def _outcome_winner_from_diff(diffs: List[Dict[str, Any]]) -> str:
    outcome = next((d for d in diffs if d.get("diff_type") == OUTCOME_DIFF), None)
    if not outcome:
        return "unknown"
    metrics = (outcome.get("payload") or {}).get("metrics") or {}
    if not metrics:
        return "unknown"
    deltas = [
        m.get("delta")
        for m in metrics.values()
        if isinstance(m, dict) and m.get("delta") is not None
    ]
    if not deltas:
        return "unknown"
    avg_delta = sum(deltas) / len(deltas)
    if avg_delta > 0.001:
        return "right"
    if avg_delta < -0.001:
        return "left"
    return "tie"


def _build_highlights(comparison: Dict[str, Any]) -> Dict[str, Any]:
    highlights: Dict[str, Any] = {"comparison_id": comparison["id"]}
    for d in comparison.get("diffs") or []:
        dtype = d.get("diff_type")
        payload = d.get("payload") or {}
        if dtype == OUTCOME_DIFF:
            highlights["outcome"] = payload.get("metrics")
        elif dtype == "cost_diff":
            highlights["cost"] = payload.get("run_level")
        elif dtype == "event_agg_diff":
            highlights["structural_count"] = len(payload.get("events") or [])
        elif dtype == SEMANTIC_DIFF:
            highlights["semantic"] = compact_semantic_payload(payload)
        elif dtype == STRUCTURAL_DIFF:
            highlights["structural"] = compact_structural_payload(payload)
    return highlights


async def get_or_create_experiment_for_task(
    session: AsyncSession,
    task_id: str,
    *,
    name: Optional[str] = None,
    baseline_agent_id: Optional[str] = None,
) -> Experiment:
    existing = (
        await session.execute(
            select(Experiment)
            .where(Experiment.task_id == task_id)
            .order_by(Experiment.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        if baseline_agent_id and not existing.baseline_agent_id:
            existing.baseline_agent_id = baseline_agent_id
        return existing
    exp = Experiment(
        id=f"exp_{uuid.uuid4().hex[:12]}",
        task_id=task_id,
        name=name or f"版本实验 {task_id[:8]}",
        baseline_agent_id=baseline_agent_id,
        status="draft",
        comparison_scope="version_experiment",
        created_at=datetime.utcnow(),
    )
    session.add(exp)
    await session.flush()
    return exp


async def get_experiment(
    session: AsyncSession, experiment_id: str
) -> Optional[Experiment]:
    return await session.get(Experiment, experiment_id)


async def get_experiment_detail(
    session: AsyncSession, experiment_id: str
) -> Optional[Dict[str, Any]]:
    exp = await get_experiment(session, experiment_id)
    if not exp:
        return None
    summaries = (
        await session.execute(
            select(ExperimentSummary)
            .where(ExperimentSummary.experiment_id == experiment_id)
            .order_by(ExperimentSummary.question_id)
        )
    ).scalars().all()
    return serialize_experiment(exp, summaries)


async def get_experiment_for_task(
    session: AsyncSession, task_id: str
) -> Optional[Dict[str, Any]]:
    exp = (
        await session.execute(
            select(Experiment)
            .where(Experiment.task_id == task_id)
            .order_by(Experiment.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not exp:
        return None
    return await get_experiment_detail(session, exp.id)


async def run_version_experiment(
    session: AsyncSession,
    experiment_id: str,
    *,
    diff_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    exp = await get_experiment(session, experiment_id)
    if not exp:
        raise ExperimentError("experiment not found")

    runs = (
        await session.execute(
            select(AgentRun).where(AgentRun.task_id == exp.task_id)
        )
    ).scalars().all()
    matrix = _group_runs_by_question_agent(runs)
    baseline_id = _pick_baseline_agent_id(matrix, exp.baseline_agent_id)
    if not baseline_id:
        raise ExperimentError("no agent runs for experiment")

    exp.baseline_agent_id = baseline_id
    exp.status = "running"
    await session.flush()

    await session.execute(
        delete(ExperimentSummary).where(ExperimentSummary.experiment_id == experiment_id)
    )

    pairs: List[Tuple[int, AgentRun, AgentRun]] = []
    for qid, per_agent in matrix.items():
        baseline_run = per_agent.get(baseline_id)
        if not baseline_run:
            continue
        for agent_id, run in per_agent.items():
            if agent_id == baseline_id:
                continue
            pairs.append((qid, baseline_run, run))

    if not pairs:
        exp.status = "completed"
        exp.completed_at = datetime.utcnow()
        exp.meta = {**(exp.meta or {}), "message": "single_agent_only", "pair_count": 0}
        await session.commit()
        return await get_experiment_detail(session, experiment_id) or {}

    created_summaries: List[ExperimentSummary] = []
    effective_diff_types = resolve_experiment_diff_types(diff_types)
    for qid, left_run, right_run in pairs:
        comparison = await compare_runs(
            session,
            left_run_id=left_run.id,
            right_run_id=right_run.id,
            comparison_scope="version_experiment",
            diff_types=effective_diff_types,
            meta={"experiment_id": experiment_id, "question_id": qid},
        )
        winner = _outcome_winner_from_diff(comparison.get("diffs") or [])
        summary = ExperimentSummary(
            id=f"esum_{uuid.uuid4().hex[:12]}",
            experiment_id=experiment_id,
            question_id=qid,
            comparison_id=comparison["id"],
            left_agent_id=left_run.agent_id,
            right_agent_id=right_run.agent_id,
            left_run_id=left_run.id,
            right_run_id=right_run.id,
            outcome_winner=winner,
            highlights=_build_highlights(comparison),
            created_at=datetime.utcnow(),
        )
        session.add(summary)
        created_summaries.append(summary)

    exp.status = "completed"
    exp.completed_at = datetime.utcnow()
    exp.meta = {
        **(exp.meta or {}),
        "pair_count": len(created_summaries),
        "question_count": len(matrix),
    }
    await session.commit()
    return await get_experiment_detail(session, experiment_id) or {}


def serialize_experiment(
    exp: Experiment, summaries: List[ExperimentSummary]
) -> Dict[str, Any]:
    wins = {"left": 0, "right": 0, "tie": 0, "unknown": 0}
    for s in summaries:
        key = s.outcome_winner or "unknown"
        wins[key] = wins.get(key, 0) + 1
    return {
        "id": exp.id,
        "task_id": exp.task_id,
        "name": exp.name,
        "baseline_agent_id": exp.baseline_agent_id,
        "status": exp.status,
        "comparison_scope": exp.comparison_scope,
        "meta": exp.meta,
        "created_at": exp.created_at.isoformat() if exp.created_at else None,
        "completed_at": exp.completed_at.isoformat() if exp.completed_at else None,
        "outcome_wins": wins,
        "summaries": [
            {
                "id": s.id,
                "question_id": s.question_id,
                "comparison_id": s.comparison_id,
                "left_agent_id": s.left_agent_id,
                "right_agent_id": s.right_agent_id,
                "left_run_id": s.left_run_id,
                "right_run_id": s.right_run_id,
                "outcome_winner": s.outcome_winner,
                "highlights": s.highlights,
            }
            for s in summaries
        ],
    }
