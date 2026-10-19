"""
v_run_analytics 读模型（P2 F-04 / RF-01）。

PG：只读视图，报表/Analytics 禁止裸扫 run_metrics KV。
SQLite：ORM 组装同构宽表（开发/单测）。
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from config import is_postgresql_url
from database import AgentRun, CheckpointResult, RunMetric
from services.eval_context import get_question_id, get_task_id

# 与 Alembic 002 保持一致（文档/单测引用）
V_RUN_ANALYTICS_VIEW_NAME = "v_run_analytics"

V_RUN_ANALYTICS_SQL = """
CREATE OR REPLACE VIEW v_run_analytics AS
SELECT
    ar.id AS agent_run_id,
    COALESCE(ar.task_id, ar.eval_context->>'task_id') AS task_id,
    COALESCE(ar.dataset_id, ar.eval_context->>'dataset_id') AS dataset_id,
    COALESCE(
        ar.question_id,
        NULLIF(ar.eval_context->>'question_id', '')::integer
    ) AS question_id,
    COALESCE(
        ar.run_index,
        NULLIF(ar.eval_context->>'run_index', '')::integer,
        0
    ) AS run_index,
    ar.agent_id,
    ar.logical_agent_id,
    ar.agent_name,
    ar.agent_version,
    ar.status,
    ar.trace_id,
    ar.trace_sync_status,
    ar.tokens_total,
    ar.started_at,
    ar.ended_at,
    MAX(rm.value_num) FILTER (WHERE rm.metric_key = 'runtime.tokens_total')
        AS runtime_tokens_total,
    MAX(rm.value_num) FILTER (WHERE rm.metric_key = 'runtime.latency_ms')
        AS runtime_latency_ms,
    MAX(rm.value_num) FILTER (WHERE rm.metric_key = 'runtime.observation_count')
        AS runtime_observation_count,
    COALESCE(
        jsonb_object_agg(rm.metric_key, rm.value_num)
            FILTER (WHERE rm.metric_key LIKE 'eval.%' AND rm.value_num IS NOT NULL),
        '{}'::jsonb
    ) AS eval_scores,
    COUNT(DISTINCT cpr.id)::integer AS checkpoint_total,
    COUNT(DISTINCT cpr.id) FILTER (WHERE cpr.passed IS TRUE)::integer AS checkpoint_passed
FROM agent_runs ar
LEFT JOIN run_metrics rm ON rm.agent_run_id = ar.id
LEFT JOIN checkpoint_results cpr ON cpr.agent_run_id = ar.id
GROUP BY ar.id;
"""


def _serialize_eval_scores(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _row_to_dict(row: Any) -> Dict[str, Any]:
    data = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    if "eval_scores" in data:
        data["eval_scores"] = _serialize_eval_scores(data["eval_scores"])
    return data


async def _load_runs_for_task(session: AsyncSession, task_id: str) -> List[AgentRun]:
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


def _build_wide_row_from_parts(
    run: AgentRun,
    metrics: List[RunMetric],
    checkpoints: List[CheckpointResult],
) -> Dict[str, Any]:
    runtime: Dict[str, Optional[float]] = {}
    eval_scores: Dict[str, float] = {}
    for m in metrics:
        if m.value_num is None:
            continue
        if m.metric_key.startswith("eval."):
            eval_scores[m.metric_key] = float(m.value_num)
        elif m.metric_key.startswith("runtime."):
            runtime[m.metric_key] = float(m.value_num)

    cp_total = len(checkpoints)
    cp_passed = sum(1 for c in checkpoints if c.passed)

    return {
        "agent_run_id": run.id,
        "task_id": get_task_id(run) or run.task_id,
        "dataset_id": run.dataset_id,
        "question_id": get_question_id(run) if get_question_id(run) is not None else run.question_id,
        "run_index": run.run_index,
        "agent_id": run.agent_id,
        "logical_agent_id": run.logical_agent_id,
        "agent_name": run.agent_name,
        "agent_version": run.agent_version,
        "status": run.status,
        "trace_id": run.trace_id,
        "trace_sync_status": run.trace_sync_status,
        "tokens_total": run.tokens_total,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "runtime_tokens_total": runtime.get("runtime.tokens_total"),
        "runtime_tokens_llm": runtime.get("runtime.tokens_llm"),
        "runtime_tokens_tool": runtime.get("runtime.tokens_tool"),
        "runtime_latency_ms": runtime.get("runtime.latency_ms"),
        "runtime_observation_count": runtime.get("runtime.observation_count"),
        "eval_scores": eval_scores,
        "checkpoint_total": cp_total,
        "checkpoint_passed": cp_passed,
    }


async def _fetch_run_analytics_orm_fallback(
    session: AsyncSession,
    task_id: str,
) -> List[Dict[str, Any]]:
    runs = await _load_runs_for_task(session, task_id)
    if not runs:
        return []

    run_ids = [r.id for r in runs]
    metrics_result = await session.execute(
        select(RunMetric).where(RunMetric.agent_run_id.in_(run_ids))
    )
    metrics_by_run: Dict[str, List[RunMetric]] = defaultdict(list)
    for m in metrics_result.scalars().all():
        metrics_by_run[m.agent_run_id].append(m)

    cp_result = await session.execute(
        select(CheckpointResult).where(CheckpointResult.agent_run_id.in_(run_ids))
    )
    cp_by_run: Dict[str, List[CheckpointResult]] = defaultdict(list)
    for c in cp_result.scalars().all():
        cp_by_run[c.agent_run_id].append(c)

    rows = [
        _build_wide_row_from_parts(
            run,
            metrics_by_run.get(run.id, []),
            cp_by_run.get(run.id, []),
        )
        for run in runs
    ]
    rows.sort(
        key=lambda r: (
            r.get("question_id") if r.get("question_id") is not None else 999999,
            r.get("started_at") or "",
        )
    )
    return rows


async def fetch_run_analytics_rows_for_task(
    session: AsyncSession,
    task_id: str,
) -> List[Dict[str, Any]]:
    """按 task_id 读取 v_run_analytics 宽表行（PG 视图 / SQLite ORM 回退）。"""
    tid = (task_id or "").strip()
    if not tid:
        return []

    if is_postgresql_url():
        try:
            result = await session.execute(
                text(
                    f"""
                    SELECT * FROM {V_RUN_ANALYTICS_VIEW_NAME}
                    WHERE task_id = :task_id
                    ORDER BY question_id NULLS LAST, started_at
                    """
                ),
                {"task_id": tid},
            )
            return [_row_to_dict(row) for row in result.fetchall()]
        except Exception:
            # 视图未迁移时回退 ORM
            pass

    return await _fetch_run_analytics_orm_fallback(session, tid)


async def fetch_run_analytics_row_for_run(
    session: AsyncSession,
    agent_run_id: str,
) -> Optional[Dict[str, Any]]:
    """单 Run 宽表行。"""
    rid = (agent_run_id or "").strip()
    if not rid:
        return None

    if is_postgresql_url():
        try:
            result = await session.execute(
                text(
                    f"""
                    SELECT * FROM {V_RUN_ANALYTICS_VIEW_NAME}
                    WHERE agent_run_id = :agent_run_id
                    LIMIT 1
                    """
                ),
                {"agent_run_id": rid},
            )
            row = result.first()
            return _row_to_dict(row) if row else None
        except Exception:
            pass

    run = await session.get(AgentRun, rid)
    if not run:
        return None
    metrics = (
        await session.execute(select(RunMetric).where(RunMetric.agent_run_id == rid))
    ).scalars().all()
    checkpoints = (
        await session.execute(
            select(CheckpointResult).where(CheckpointResult.agent_run_id == rid)
        )
    ).scalars().all()
    return _build_wide_row_from_parts(run, list(metrics), list(checkpoints))
