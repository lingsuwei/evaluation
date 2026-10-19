"""跨评估任务版本对比驾驶舱（历史多选 → 复用已有 EvaluationResult / AgentRun）。"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Agent, AgentRun, CheckpointResult, Dataset, EvaluationResult, EvaluationTask
from services.checkpoint_result_service import build_task_checkpoint_report
from services.eval_context import parse_eval_context
from services.version_cockpit_service import _build_l2, _build_l3


class CrossTaskCockpitError(ValueError):
    pass


@dataclass
class _TaskBundle:
    task: EvaluationTask
    dataset_name: str
    agent: Optional[Agent]
    runs: List[AgentRun]
    eval_rows: List[EvaluationResult]


def _question_key_from_run(run: AgentRun) -> str:
    ctx = parse_eval_context(run.eval_context)
    qk = (ctx.get("question_key") or "").strip()
    if qk:
        return qk
    ditem = (ctx.get("dataset_item_id") or "").strip()
    if ditem:
        return f"ditem:{ditem}"
    if run.question_id is not None:
        return f"qidx:{run.question_id}"
    return "unknown"


def _member_label(
    task: EvaluationTask,
    agent: Optional[Agent],
    *,
    dataset_name: str,
) -> str:
    if agent:
        ver = (agent.version or "").strip()
        name = (agent.name or "").strip()
        base = f"{name} {ver}".strip() if ver else name
    elif task.agent_id:
        base = "Agent"
    else:
        base = "离线评估"
    completed = ""
    if task.completed_at:
        completed = task.completed_at.strftime("%Y-%m-%d %H:%M")
    parts = [p for p in [base, dataset_name, completed] if p]
    return " · ".join(parts[:2]) if len(parts) <= 2 else f"{base} · {completed}"


async def _load_task_bundle(session: AsyncSession, task_id: str) -> _TaskBundle:
    task = (
        await session.execute(select(EvaluationTask).where(EvaluationTask.id == task_id))
    ).scalar_one_or_none()
    if not task:
        raise CrossTaskCockpitError(f"任务不存在: {task_id}")

    dataset_name = ""
    if task.dataset_id:
        ds = (
            await session.execute(select(Dataset).where(Dataset.id == task.dataset_id))
        ).scalar_one_or_none()
        if ds:
            dataset_name = ds.name or task.dataset_id

    agent = None
    if task.agent_id:
        agent = (
            await session.execute(select(Agent).where(Agent.id == task.agent_id))
        ).scalar_one_or_none()

    runs = (
        await session.execute(select(AgentRun).where(AgentRun.task_id == task_id))
    ).scalars().all()
    eval_rows = (
        await session.execute(
            select(EvaluationResult).where(EvaluationResult.task_id == task_id)
        )
    ).scalars().all()
    return _TaskBundle(
        task=task,
        dataset_name=dataset_name,
        agent=agent,
        runs=list(runs),
        eval_rows=list(eval_rows),
    )


def _primary_score_for_run(
    run: Optional[AgentRun],
    eval_rows: List[EvaluationResult],
    question_key: str,
) -> Optional[float]:
    if not run:
        return None
    rows = [er for er in eval_rows if er.agent_run_id == run.id]
    if not rows:
        rows = [
            er
            for er in eval_rows
            if er.question_id == run.question_id
        ]
    if not rows:
        return None
    preferred = next(
        (r for r in rows if (r.metric_name or "").lower() == "correctness"),
        None,
    )
    row = preferred or rows[0]
    if row.avg_score is not None:
        return float(row.avg_score)
    if row.score is not None:
        return float(row.score)
    return None


async def build_cross_task_cockpit(
    session: AsyncSession,
    task_ids: List[str],
    *,
    baseline_task_id: Optional[str] = None,
    question_key: Optional[str] = None,
    right_task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    将多个已完成评测任务按 question_key 对齐，生成与单任务驾驶舱同结构的读模型。
    列键为 task_id（member_id）。
    """
    ids = [t.strip() for t in task_ids if t and str(t).strip()]
    if len(ids) < 2:
        raise CrossTaskCockpitError("请至少选择 2 个任务进行对比")
    if len(ids) > 6:
        raise CrossTaskCockpitError("单次最多对比 6 个任务")

    bundles: Dict[str, _TaskBundle] = {}
    for tid in ids:
        bundles[tid] = await _load_task_bundle(session, tid)

    warnings: List[str] = []
    dataset_ids = {b.task.dataset_id for b in bundles.values() if b.task.dataset_id}
    if len(dataset_ids) > 1:
        warnings.append("所选任务的评测集不一致，题目按 question_key 尽力对齐，结果仅供参考")

    incomplete = [
        tid
        for tid, b in bundles.items()
        if (b.task.status or "").lower() not in ("completed", "cancelled")
    ]
    if incomplete:
        raise CrossTaskCockpitError(
            f"仅支持已完成或已取消的任务参与对比，未完成: {', '.join(incomplete[:3])}"
        )

    baseline_id = baseline_task_id if baseline_task_id in ids else ids[0]
    members: List[Dict[str, Any]] = []
    for tid in ids:
        b = bundles[tid]
        agent = b.agent
        members.append(
            {
                "member_id": tid,
                "agent_id": agent.id if agent else tid,
                "task_id": tid,
                "agent_name": agent.name if agent else ("离线评估" if not b.task.agent_id else None),
                "agent_version": agent.version if agent else None,
                "label": _member_label(b.task, agent, dataset_name=b.dataset_name),
                "dataset_name": b.dataset_name,
                "completed_at": b.task.completed_at.isoformat() if b.task.completed_at else None,
                "is_baseline": tid == baseline_id,
            }
        )

    # question_key -> { task_id: AgentRun }
    matrix: Dict[str, Dict[str, AgentRun]] = defaultdict(dict)
    question_labels: Dict[str, str] = {}

    for tid, b in bundles.items():
        for run in b.runs:
            if run.question_id is None:
                continue
            qk = _question_key_from_run(run)
            existing = matrix[qk].get(tid)
            if existing is None or (run.status == "success" and existing.status != "success"):
                matrix[qk][tid] = run
            label_src = run.input_snapshot
            if isinstance(label_src, str):
                try:
                    label_src = json.loads(label_src)
                except json.JSONDecodeError:
                    label_src = None
            question_text = ""
            if isinstance(label_src, dict):
                question_text = str(label_src.get("question") or "")[:120]
            if not question_text:
                for er in b.eval_rows:
                    if er.question_id == run.question_id and er.question:
                        question_text = (er.question or "")[:120]
                        break
            if question_text:
                question_labels[qk] = question_text
            elif qk not in question_labels:
                question_labels[qk] = f"题目 {qk[:16]}…" if len(qk) > 16 else qk

    if not matrix:
        raise CrossTaskCockpitError("所选任务均无可用 AgentRun，无法生成对比")

    member_ids = ids
    l1_rows: List[Dict[str, Any]] = []
    baseline_bundle = bundles[baseline_id]

    for qk in sorted(matrix.keys(), key=lambda k: question_labels.get(k, k)):
        outcomes: Dict[str, Optional[float]] = {}
        qid_by_member: Dict[str, int] = {}
        for mid in member_ids:
            run = matrix[qk].get(mid)
            qid_by_member[mid] = run.question_id if run and run.question_id is not None else -1
            outcomes[mid] = _primary_score_for_run(
                run,
                bundles[mid].eval_rows,
                qk,
            )
        valid = {k: v for k, v in outcomes.items() if v is not None}
        winner = max(valid, key=valid.get) if valid else None  # type: ignore[arg-type]
        winner_member = next((m for m in members if m["member_id"] == winner), None) if winner else None
        baseline_qid = qid_by_member.get(baseline_id, 0)
        l1_rows.append(
            {
                "question_key": qk,
                "question_id": baseline_qid if baseline_qid >= 0 else 0,
                "label": question_labels.get(qk, qk),
                "outcomes": outcomes,
                "winner_agent_id": winner,
                "winner_member_id": winner,
                "winner_label": winner_member["label"] if winner_member else None,
            }
        )

    payload: Dict[str, Any] = {
        "comparison_mode": "cross_task",
        "task_id": baseline_id,
        "task_ids": ids,
        "baseline_member_id": baseline_id,
        "dataset_id": baseline_bundle.task.dataset_id,
        "dataset_name": baseline_bundle.dataset_name,
        "warnings": warnings,
        "agents": members,
        "l1": {
            "rows": l1_rows,
            "agent_columns": member_ids,
        },
        "experiment": None,
        "baseline_agent_id": baseline_id,
    }

    q_for_detail = question_key
    if q_for_detail and q_for_detail not in matrix:
        q_for_detail = None
    if q_for_detail is None and l1_rows:
        q_for_detail = l1_rows[0]["question_key"]

    if q_for_detail:
        # 适配单任务 L2：构造伪 matrix[question_id][agent_id] → 用 baseline question_id + member 作 agent_id
        baseline_run = matrix[q_for_detail].get(baseline_id)
        pseudo_qid = baseline_run.question_id if baseline_run else 0
        pseudo_matrix: Dict[int, Dict[str, AgentRun]] = {pseudo_qid: {}}
        agents_for_l2 = []
        eval_by_run: Dict[str, List[EvaluationResult]] = defaultdict(list)
        eval_by_q_metric: Dict[Tuple[int, str], List[EvaluationResult]] = defaultdict(list)
        all_eval_rows: List[EvaluationResult] = []

        for mid in member_ids:
            run = matrix[q_for_detail].get(mid)
            if not run:
                continue
            pseudo_matrix[pseudo_qid][mid] = run
            agents_for_l2.append(
                next(m for m in members if m["member_id"] == mid)
            )
            for er in bundles[mid].eval_rows:
                if er.agent_run_id == run.id or (
                    er.question_id == run.question_id and er.task_id == mid
                ):
                    eval_by_run[run.id].append(er)
                    all_eval_rows.append(er)
                    eval_by_q_metric[(pseudo_qid, er.metric_name or "score")].append(er)

        if len(agents_for_l2) >= 2:
            payload["l2"] = await _build_l2(
                session,
                baseline_id,
                pseudo_qid,
                agents_for_l2,
                pseudo_matrix,
                eval_by_run,
                eval_by_q_metric,
                all_eval_rows,
                None,
            )
            payload["l2"]["question_key"] = q_for_detail
            payload["selected_question_id"] = pseudo_qid
            payload["selected_question_key"] = q_for_detail

            from services.cross_task_diff_service import enrich_l2_with_cross_task_diff

            payload["l2"] = await enrich_l2_with_cross_task_diff(
                session,
                payload["l2"],
                matrix_row=matrix[q_for_detail],
                member_ids=member_ids,
                members=members,
                baseline_task_id=baseline_id,
                question_key=q_for_detail,
                right_task_id=right_task_id,
            )

            # L3：按各任务 checkpoint 报告合并
            steps_merged: Dict[str, Dict[str, Any]] = {}
            for mid in member_ids:
                run = matrix[q_for_detail].get(mid)
                if not run:
                    continue
                cp_report = await build_task_checkpoint_report(session, mid)
                l3_part = _build_l3(
                    cp_report,
                    run.question_id if run.question_id is not None else 0,
                    {mid: {"label": next(m["label"] for m in members if m["member_id"] == mid)}},
                )
                for step in l3_part.get("steps") or []:
                    cid = step.get("checkpoint_id") or step.get("name")
                    if cid not in steps_merged:
                        steps_merged[cid] = {
                            "checkpoint_id": cid,
                            "name": step.get("name"),
                            "order": step.get("order"),
                            "required": step.get("required"),
                            "agents": {},
                        }
                    steps_merged[cid]["agents"].update(step.get("agents") or {})

            payload["l3"] = {
                "question_id": pseudo_qid,
                "question_key": q_for_detail,
                "steps": sorted(
                    steps_merged.values(),
                    key=lambda s: (s.get("order") is None, s.get("order") or 0),
                ),
            }

    return payload
