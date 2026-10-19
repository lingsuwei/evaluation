"""
从 Execution Fact Layer 聚合 Facets（P2 G-01）。

主路径：agent_runs + run_metrics + checkpoint_results（同一 PG / DATABASE_URL）。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, CheckpointResult, EvaluationResult, RunMetric
from services.eval_context import get_question_id, get_task_id


async def _load_runs_for_task(session: AsyncSession, task_id: str) -> List[AgentRun]:
    """按 task_id 加载 Run（镜像列 + eval_context，兼容 PG 事实层）。"""
    tid = (task_id or "").strip()
    if not tid:
        return []
    result = await session.execute(
        select(AgentRun)
        .where(
            or_(
                AgentRun.task_id == tid,
                AgentRun.eval_context["task_id"].as_string() == tid,
            )
        )
        .order_by(AgentRun.question_id, AgentRun.started_at)
    )
    runs = list(result.scalars().all())
    # 兜底：部分旧行 eval_context JSON 路径不可用时的 Python 过滤
    if not runs:
        fallback = await session.execute(select(AgentRun).order_by(AgentRun.question_id))
        runs = [r for r in fallback.scalars().all() if get_task_id(r) == tid]
    return runs


async def _load_metrics_by_run(
    session: AsyncSession,
    run_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    if not run_ids:
        return {}
    result = await session.execute(
        select(RunMetric).where(RunMetric.agent_run_id.in_(run_ids))
    )
    by_run: Dict[str, Dict[str, Any]] = defaultdict(dict)
    for metric in result.scalars().all():
        by_run[metric.agent_run_id][metric.metric_key] = {
            "value_num": metric.value_num,
            "source": metric.source,
            "value_json": metric.value_json,
        }
    return dict(by_run)


async def _load_checkpoint_summary_by_run(
    session: AsyncSession,
    run_ids: List[str],
) -> Dict[str, Dict[str, int]]:
    if not run_ids:
        return {}
    result = await session.execute(
        select(CheckpointResult.agent_run_id, CheckpointResult.passed).where(
            CheckpointResult.agent_run_id.in_(run_ids)
        )
    )
    summary: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0})
    for run_id, passed in result.all():
        summary[run_id]["total"] += 1
        if passed:
            summary[run_id]["passed"] += 1
    return dict(summary)


def _build_run_fact_row(
    run: AgentRun,
    metrics: Dict[str, Any],
    checkpoints: Optional[Dict[str, int]],
) -> Dict[str, Any]:
    qnum = get_question_id(run)
    row: Dict[str, Any] = {
        "agent_run_id": run.id,
        "question_id": str(qnum) if qnum is not None else None,
        "agent_name": run.agent_name,
        "agent_version": run.agent_version,
        "status": run.status,
        "trace_sync_status": run.trace_sync_status,
        "tokens_total": run.tokens_total,
        "metrics": metrics,
    }
    if checkpoints and checkpoints.get("total", 0) > 0:
        row["checkpoints"] = checkpoints
    return row


async def build_facets_from_agent_runs(
    session: AsyncSession,
    *,
    task_id: str,
    question_texts: Optional[Dict[int, str]] = None,
) -> Dict[str, Any]:
    runs = await _load_runs_for_task(session, task_id)
    run_ids = [r.id for r in runs]
    metrics_by_run = await _load_metrics_by_run(session, run_ids)
    checkpoints_by_run = await _load_checkpoint_summary_by_run(session, run_ids)

    questions: Dict[str, Dict[str, str]] = {}
    agents: Dict[str, str] = {}
    versions_by_agent: Dict[str, set] = {}
    metric_keys: set[str] = set()
    run_facts: List[Dict[str, Any]] = []

    for run in runs:
        qnum = get_question_id(run)
        if qnum is None:
            continue
        qid = str(qnum)
        text = ""
        if question_texts and qnum in question_texts:
            text = question_texts[qnum] or ""
        prev = questions.get(qid, {}).get("text", "")
        if len(text) > len(prev):
            questions[qid] = {"id": qid, "text": text}
        elif qid not in questions:
            questions[qid] = {"id": qid, "text": text}

        agent_key = (run.agent_name or run.agent_id or "").strip()
        if agent_key:
            agents[agent_key] = agent_key
            ver = (run.agent_version or "").strip()
            if ver and ver != "unknown":
                versions_by_agent.setdefault(agent_key, set()).add(ver)

        run_metrics = metrics_by_run.get(run.id, {})
        metric_keys.update(run_metrics.keys())
        run_facts.append(
            _build_run_fact_row(
                run,
                run_metrics,
                checkpoints_by_run.get(run.id),
            )
        )

    question_list = sorted(
        questions.values(),
        key=lambda x: (int(x["id"]) if str(x["id"]).isdigit() else 999999, x["id"]),
    )
    agent_list = sorted(
        [{"value": k, "label": v} for k, v in agents.items()],
        key=lambda x: x["label"],
    )
    versions_map = {k: sorted(v) for k, v in sorted(versions_by_agent.items())}

    return {
        "questions": question_list,
        "agents": agent_list,
        "versions_by_agent": versions_map,
        "trace_count": len(runs),
        "source": "agent_runs",
        "fact_layer": {
            "run_count": len(runs),
            "metric_keys": sorted(metric_keys),
            "runs": run_facts,
        },
    }


async def load_question_texts_for_task(
    session: AsyncSession,
    task_id: str,
) -> Dict[int, str]:
    """从 evaluation_results 或数据集补全题干（供 Facets 展示）。"""
    result = await session.execute(
        select(EvaluationResult.question_id, EvaluationResult.question).where(
            EvaluationResult.task_id == task_id,
            EvaluationResult.question.isnot(None),
        )
    )
    texts: Dict[int, str] = {}
    for qid, qtext in result.all():
        if qid is not None and qtext and qid not in texts:
            texts[int(qid)] = str(qtext)
    return texts
