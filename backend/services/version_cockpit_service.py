"""版本对比驾驶舱读模型（场景 A：Outcome → 因子 → 过程点）。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, CheckpointResult, EvaluationResult, RunDiff
from services.checkpoint_result_service import build_task_checkpoint_report
from services.diff.diff_types import SEMANTIC_DIFF, STRUCTURAL_DIFF
from services.diff.semantic_diff_calculator import compact_semantic_payload
from services.diff.structural_diff_calculator import compact_structural_payload
from services.experiment_service import get_experiment_for_task


def _agent_label(run: AgentRun) -> str:
    ver = (run.agent_version or "").strip()
    name = (run.agent_name or run.agent_id or "").strip()
    return f"{name} {ver}".strip() if ver else name


async def build_version_cockpit(
    session: AsyncSession,
    task_id: str,
    *,
    question_id: Optional[int] = None,
) -> Dict[str, Any]:
    runs = (
        await session.execute(select(AgentRun).where(AgentRun.task_id == task_id))
    ).scalars().all()
    eval_rows = (
        await session.execute(
            select(EvaluationResult).where(EvaluationResult.task_id == task_id)
        )
    ).scalars().all()

    agents_map: Dict[str, Dict[str, Any]] = {}
    matrix: Dict[int, Dict[str, AgentRun]] = defaultdict(dict)
    for run in runs:
        if run.question_id is None:
            continue
        matrix[run.question_id][run.agent_id] = run
        agents_map[run.agent_id] = {
            "agent_id": run.agent_id,
            "agent_name": run.agent_name,
            "agent_version": run.agent_version,
            "label": _agent_label(run),
        }
    agents = sorted(agents_map.values(), key=lambda a: a["label"])

    run_by_id = {r.id: r for r in runs}
    eval_by_run: Dict[str, List[EvaluationResult]] = defaultdict(list)
    eval_by_q_metric: Dict[Tuple[int, str], List[EvaluationResult]] = defaultdict(list)
    question_labels: Dict[int, str] = {}
    for er in eval_rows:
        if er.question_id is not None:
            question_labels[er.question_id] = (er.question or f"Q{er.question_id + 1}")[:120]
            eval_by_q_metric[(er.question_id, er.metric_name or "score")].append(er)
        if er.agent_run_id:
            eval_by_run[er.agent_run_id].append(er)

    def _primary_score(qid: int, agent_id: str) -> Optional[float]:
        run = matrix.get(qid, {}).get(agent_id)
        if not run:
            return None
        rows = eval_by_run.get(run.id) or []
        if not rows:
            rows = [er for er in eval_rows if er.question_id == qid]
        if not rows:
            return None
        preferred = next((r for r in rows if (r.metric_name or "").lower() == "correctness"), None)
        row = preferred or rows[0]
        return float(row.avg_score) if row.avg_score is not None else None

    l1_rows: List[Dict[str, Any]] = []
    qids = sorted(matrix.keys())
    for qid in qids:
        outcomes: Dict[str, Optional[float]] = {
            a["agent_id"]: _primary_score(qid, a["agent_id"]) for a in agents
        }
        valid = {k: v for k, v in outcomes.items() if v is not None}
        winner = max(valid, key=valid.get) if valid else None  # type: ignore[arg-type]
        winner_label = agents_map[winner]["label"] if winner and winner in agents_map else None
        l1_rows.append(
            {
                "question_id": qid,
                "label": question_labels.get(qid, f"Q{qid + 1}"),
                "outcomes": outcomes,
                "winner_agent_id": winner,
                "winner_label": winner_label,
            }
        )

    experiment = await get_experiment_for_task(session, task_id)
    checkpoint_report = await build_task_checkpoint_report(session, task_id)

    for a in agents:
        a["member_id"] = a["agent_id"]
        a["task_id"] = task_id
        a["is_baseline"] = a["agent_id"] == (experiment or {}).get("baseline_agent_id")

    payload: Dict[str, Any] = {
        "comparison_mode": "intra_task",
        "task_id": task_id,
        "task_ids": [task_id],
        "baseline_member_id": (experiment or {}).get("baseline_agent_id"),
        "agents": agents,
        "l1": {"rows": l1_rows, "agent_columns": [a["agent_id"] for a in agents]},
        "experiment": experiment,
        "baseline_agent_id": (experiment or {}).get("baseline_agent_id"),
        "warnings": (
            ["本任务仅包含一个 Agent 版本，请在同任务内配置多版本评测，或从评估历史选择多个任务做跨任务对比"]
            if len(agents) < 2
            else []
        ),
    }

    q_for_detail = question_id if question_id is not None else (qids[0] if qids else None)
    if q_for_detail is not None:
        payload["l2"] = await _build_l2(
            session,
            task_id,
            q_for_detail,
            agents,
            matrix,
            eval_by_run,
            eval_by_q_metric,
            eval_rows,
            experiment,
        )
        payload["l3"] = _build_l3(checkpoint_report, q_for_detail, agents_map)
        payload["selected_question_id"] = q_for_detail

    return payload


async def _build_l2(
    session: AsyncSession,
    task_id: str,
    question_id: int,
    agents: List[Dict[str, Any]],
    matrix: Dict[int, Dict[str, AgentRun]],
    eval_by_run: Dict[str, List[EvaluationResult]],
    eval_by_q_metric: Dict[Tuple[int, str], List[EvaluationResult]],
    eval_rows: List[EvaluationResult],
    experiment: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    factor_rows: List[Dict[str, Any]] = []
    metric_names = sorted(
        {m for (qid, m) in eval_by_q_metric if qid == question_id}
    )
    for metric_name in metric_names:
        scores: Dict[str, Optional[float]] = {}
        for agent in agents:
            aid = agent["agent_id"]
            run = matrix.get(question_id, {}).get(aid)
            val = None
            if run:
                for er in eval_by_run.get(run.id, []):
                    if er.metric_name == metric_name and er.avg_score is not None:
                        val = float(er.avg_score)
                        break
            if val is None:
                for er in eval_by_q_metric.get((question_id, metric_name), []):
                    if run and er.agent_run_id == run.id and er.avg_score is not None:
                        val = float(er.avg_score)
                        break
            scores[aid] = val
        sample = next(
            (er for er in eval_rows if er.question_id == question_id and er.metric_name == metric_name),
            None,
        )
        eval_method = getattr(sample, "eval_method", None) or "llm"
        ftype = "script" if eval_method == "script" else "judge"
        factor_rows.append(
            {
                "factor_key": metric_name,
                "name": metric_name,
                "type": ftype,
                "scores": scores,
                "drill": False,
            }
        )

    behavioral: Dict[str, Optional[float]] = {}
    latency: Dict[str, Optional[float]] = {}
    tokens: Dict[str, Optional[float]] = {}
    for agent in agents:
        aid = agent["agent_id"]
        run = matrix.get(question_id, {}).get(aid)
        if not run:
            behavioral[aid] = latency[aid] = tokens[aid] = None
            continue
        stored_latency = None
        for er in eval_by_run.get(run.id, []):
            if er.inference_duration_ms is not None:
                stored_latency = int(er.inference_duration_ms)
                break
        trace_lat = (await batch_load_trace_latency_ms(session, [run.id])).get(run.id)
        resolved = resolve_agent_e2e_duration_ms(
            run,
            trace_latency_ms=trace_lat,
            stored_inference_duration_ms=stored_latency,
        )
        latency[aid] = float(resolved) if resolved is not None else None
        tokens[aid] = float(run.tokens_total) if run.tokens_total is not None else None
        cp_rows = (
            await session.execute(
                select(CheckpointResult).where(CheckpointResult.agent_run_id == run.id)
            )
        ).scalars().all()
        if cp_rows:
            passed = sum(1 for c in cp_rows if c.passed)
            behavioral[aid] = round(passed / len(cp_rows), 4)
        else:
            behavioral[aid] = None

    factor_rows.append(
        {
            "factor_key": "behavioral",
            "name": "过程点合规率",
            "type": "diff",
            "scores": behavioral,
            "drill": True,
        }
    )
    factor_rows.append(
        {
            "factor_key": "latency_ms",
            "name": "端到端延迟",
            "type": "diff",
            "scores": latency,
            "lower_better": True,
            "drill": False,
        }
    )
    factor_rows.append(
        {
            "factor_key": "token_cost",
            "name": "Token 总量",
            "type": "diff",
            "scores": tokens,
            "lower_better": True,
            "drill": False,
        }
    )

    structural_summary = None
    semantic_summary = None
    comparison_id = None
    if experiment:
        for s in experiment.get("summaries") or []:
            if s.get("question_id") == question_id:
                comparison_id = s.get("comparison_id")
                hl = s.get("highlights") or {}
                if hl.get("structural"):
                    structural_summary = compact_structural_payload(hl["structural"]).get("summary")
                if hl.get("semantic"):
                    semantic_summary = compact_semantic_payload(hl["semantic"]).get("judge_summary")
                break

    if comparison_id:
        diffs = (
            await session.execute(
                select(RunDiff).where(RunDiff.comparison_id == comparison_id)
            )
        ).scalars().all()
        sem_scores: Dict[str, Optional[float]] = {a["agent_id"]: None for a in agents}
        str_scores: Dict[str, Optional[float]] = {a["agent_id"]: None for a in agents}
        right_id = None
        for s in experiment.get("summaries") or []:
            if s.get("question_id") == question_id:
                right_id = s.get("right_agent_id")
                break
        for d in diffs:
            if d.diff_type == SEMANTIC_DIFF and right_id:
                payload = d.payload or {}
                sem_scores[right_id] = 1.0 - min(
                    (payload.get("summary_mismatch_count") or 0) / 10.0, 1.0
                )
            if d.diff_type == STRUCTURAL_DIFF and right_id:
                payload = d.payload or {}
                str_scores[right_id] = 1.0 if payload.get("same_topology") else 0.5
        factor_rows.append(
            {
                "factor_key": "semantic",
                "name": "语义一致性",
                "type": "diff",
                "scores": sem_scores,
                "drill": False,
            }
        )
        factor_rows.append(
            {
                "factor_key": "structural",
                "name": "结构对齐度",
                "type": "diff",
                "scores": str_scores,
                "drill": False,
            }
        )

    return {
        "question_id": question_id,
        "factors": factor_rows,
        "structural_summary": structural_summary,
        "semantic_summary": semantic_summary,
        "comparison_id": comparison_id,
    }


def _build_l3(
    checkpoint_report: Optional[Dict[str, Any]],
    question_id: int,
    agents_map: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    if not checkpoint_report:
        return {"question_id": question_id, "steps": [], "by_agent": {}}
    runs = [
        r
        for r in checkpoint_report.get("runs") or []
        if r.get("question_id") == question_id
    ]
    step_index: Dict[str, Dict[str, Any]] = {}
    by_agent: Dict[str, Any] = {}
    for run in runs:
        aid = run.get("agent_id") or "unknown"
        by_agent[aid] = {
            "agent_run_id": run.get("agent_run_id"),
            "compliance_rate": run.get("compliance_rate"),
            "passed": run.get("checkpoint_passed"),
            "total": run.get("checkpoint_total"),
        }
        for item in run.get("items") or []:
            cid = item.get("checkpoint_id") or item.get("name")
            if cid not in step_index:
                step_index[cid] = {
                    "checkpoint_id": cid,
                    "name": item.get("name"),
                    "order": item.get("order"),
                    "required": item.get("required"),
                    "agents": {},
                }
            step_index[cid]["agents"][aid] = {
                "hit": item.get("hit"),
                "order_ok": item.get("order_ok"),
                "passed": item.get("passed"),
            }
    steps = sorted(step_index.values(), key=lambda x: (x.get("order") or 999, x.get("name") or ""))
    return {"question_id": question_id, "steps": steps, "by_agent": by_agent, "agent_labels": agents_map}
