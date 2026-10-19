"""
Analytics 读层（P2 F-03 / F-04）。

- 聚合摘要：`aggregate_task_run_metrics`（按 metric_key 均值/计数）
- 宽表读模型：见 `run_analytics_view.fetch_run_analytics_rows_for_task`（PG 视图 / SQLite ORM 回退）
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, RunMetric
from services.eval_context import get_question_id, get_task_id


async def _load_task_runs(session: AsyncSession, task_id: str) -> List[AgentRun]:
    tid = (task_id or "").strip()
    if not tid:
        return []
    result = await session.execute(
        select(AgentRun).where(
            or_(
                AgentRun.task_id == tid,
                AgentRun.eval_context["task_id"].as_string() == tid,
            )
        )
    )
    runs = list(result.scalars().all())
    if not runs:
        fallback = await session.execute(select(AgentRun))
        runs = [r for r in fallback.scalars().all() if get_task_id(r) == tid]
    return runs


async def aggregate_task_run_metrics(
    session: AsyncSession,
    task_id: str,
) -> Dict[str, Any]:
    """
    按 task 聚合 run_metrics。
    返回 eval/runtime 分组、按 metric_key 的均值、按 question 的明细。
    """
    runs = await _load_task_runs(session, task_id)
    if not runs:
        return {
            "task_id": task_id,
            "run_count": 0,
            "metric_keys": [],
            "by_metric_key": {},
            "by_question": [],
        }

    run_ids = [r.id for r in runs]
    run_by_id = {r.id: r for r in runs}
    metrics_result = await session.execute(
        select(RunMetric).where(RunMetric.agent_run_id.in_(run_ids))
    )
    metrics = list(metrics_result.scalars().all())

    by_key: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"source": "", "values": [], "count": 0}
    )
    by_question: Dict[int, Dict[str, Any]] = {}

    for metric in metrics:
        run = run_by_id.get(metric.agent_run_id)
        if not run:
            continue
        key = metric.metric_key
        bucket = by_key[key]
        bucket["source"] = metric.source or bucket["source"]
        if metric.value_num is not None:
            bucket["values"].append(float(metric.value_num))
        bucket["count"] += 1

        qnum = get_question_id(run)
        if qnum is not None:
            qrow = by_question.setdefault(
                qnum,
                {
                    "question_id": qnum,
                    "agent_run_id": run.id,
                    "metrics": {},
                },
            )
            qrow["metrics"][key] = {
                "value_num": metric.value_num,
                "source": metric.source,
                "value_json": metric.value_json,
            }

    by_metric_key: Dict[str, Any] = {}
    for key, bucket in sorted(by_key.items()):
        values = bucket["values"]
        by_metric_key[key] = {
            "source": bucket["source"],
            "count": bucket["count"],
            "avg": round(sum(values) / len(values), 4) if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }

    return {
        "task_id": task_id,
        "run_count": len(runs),
        "metric_keys": sorted(by_metric_key.keys()),
        "by_metric_key": by_metric_key,
        "by_question": sorted(by_question.values(), key=lambda x: x["question_id"]),
    }
