"""
从 agent_runs 物化 Trace 列表（P2 G-04），供 Trace 探索 / sessions 读路径使用。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, RunSpanNode
from services.agent_run_facets import _load_runs_for_task
from services.eval_context import get_question_id, get_task_id
from services.eval_session import build_eval_session_id
from services.observation_latency import resolve_span_node_latency_ms


def _iso_dt(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat()


def _run_matches_filters(
    run: AgentRun,
    *,
    question_id: Optional[int] = None,
    agent_name: Optional[str] = None,
    agent_version: Optional[str] = None,
    question_keyword: Optional[str] = None,
    question_texts: Optional[Dict[int, str]] = None,
) -> bool:
    qnum = get_question_id(run)
    if question_id is not None and qnum != question_id:
        return False

    if agent_name and agent_name.strip():
        name = (run.agent_name or "").strip()
        if name != agent_name.strip():
            return False

    if agent_version:
        ver = (run.agent_version or "").strip()
        if ver != agent_version:
            return False

    if question_keyword and question_keyword.strip():
        kw = question_keyword.strip().lower()
        text = ""
        if qnum is not None and question_texts:
            text = question_texts.get(qnum, "")
        if kw not in text.lower():
            snap = run.input_snapshot if isinstance(run.input_snapshot, dict) else {}
            for key in ("question", "q", "input", "prompt"):
                val = snap.get(key)
                if val is not None and kw in str(val).lower():
                    break
            else:
                return False

    return True


def agent_run_to_trace_row(
    run: AgentRun,
    *,
    question_texts: Optional[Dict[int, str]] = None,
) -> Dict[str, Any]:
    """将 AgentRun 转为 Langfuse trace 列表兼容结构。"""
    qnum = get_question_id(run)
    task_id = get_task_id(run) or run.task_id or ""
    qtext = ""
    if qnum is not None and question_texts:
        qtext = question_texts.get(qnum, "")

    # 注意：不添加 "onto-eval" 标签。该标签仅用于 onto-eval 评估管线自身
    # 上报的"评估包装 trace"（name 以 "onto-eval/" 开头），前端
    # isEvalReportedTrace 会按此标签过滤。如果事实层行也带此标签，
    # 会导致所有 agent_runs trace 被前端误判为评估包装 trace 而过滤掉。
    tags: List[str] = []
    if task_id:
        tags.append(f"task:{task_id}")
    if run.logical_agent_id:
        tags.append(f"logical_agent:{run.logical_agent_id}")
    if run.agent_version and run.agent_version != "unknown":
        tags.append(f"version:{run.agent_version}")

    trace_id = (run.trace_id or "").strip() or run.id
    return {
        "id": trace_id,
        "name": run.agent_name or run.agent_id,
        "sessionId": (
            build_eval_session_id(task_id, qnum)
            if task_id and qnum is not None
            else None
        ),
        "timestamp": _iso_dt(run.started_at),
        "tags": tags,
        "metadata": {
            "onto_eval_task_id": task_id,
            "onto_eval_question_id": str(qnum) if qnum is not None else "",
            "onto_eval_question": qtext,
            "agent_name": run.agent_name,
            "logical_agent_id": run.logical_agent_id,
            "agent_version": run.agent_version,
            "agent_run_id": run.id,
            "trace_sync_status": run.trace_sync_status,
            "source": "agent_runs",
        },
        "input": run.input_snapshot,
        "latency": None,
    }


async def list_traces_from_agent_runs(
    session: AsyncSession,
    *,
    task_id: str,
    question_id: Optional[int] = None,
    agent_name: Optional[str] = None,
    agent_version: Optional[str] = None,
    question_keyword: Optional[str] = None,
    question_texts: Optional[Dict[int, str]] = None,
    limit: int = 100,
    page: int = 1,
) -> Dict[str, Any]:
    runs = await _load_runs_for_task(session, task_id)
    filtered = [
        run
        for run in runs
        if _run_matches_filters(
            run,
            question_id=question_id,
            agent_name=agent_name,
            agent_version=agent_version,
            question_keyword=question_keyword,
            question_texts=question_texts,
        )
    ]
    filtered.sort(key=lambda r: (get_question_id(r) or 999999, r.started_at or datetime.min))

    total = len(filtered)
    page = max(1, page)
    limit = max(1, limit)
    start = (page - 1) * limit
    page_rows = filtered[start : start + limit]
    data = [
        agent_run_to_trace_row(run, question_texts=question_texts) for run in page_rows
    ]
    total_pages = max(1, math.ceil(total / limit)) if total else 1

    return {
        "data": data,
        "meta": {
            "page": page,
            "limit": limit,
            "totalItems": total,
            "totalPages": total_pages,
            "source": "agent_runs",
        },
    }


async def load_observations_for_preview(
    session: AsyncSession,
    trace_id: str,
) -> Optional[List[Dict[str, Any]]]:
    """优先从物化 Span Tree 加载 observations；无 span 时返回 None 供 Langfuse 降级。"""
    detail = await build_trace_detail_from_fact_layer(session, trace_id)
    if not detail:
        return None
    observations = detail.get("observations") or []
    return observations if isinstance(observations, list) and observations else None


async def build_trace_detail_from_fact_layer(
    session: AsyncSession,
    trace_id: str,
) -> Optional[Dict[str, Any]]:
    """
    从 agent_runs + run_span_nodes 组装 trace 详情（Langfuse 不可用或无 trace_id 时降级）。
    """
    from sqlalchemy import or_, select

    tid = (trace_id or "").strip()
    if not tid:
        return None

    run_result = await session.execute(
        select(AgentRun).where(
            or_(
                AgentRun.trace_id == tid,
                AgentRun.id == tid,
            )
        )
    )
    run = run_result.scalars().first()
    if not run:
        return None

    span_result = await session.execute(
        select(RunSpanNode)
        .where(RunSpanNode.agent_run_id == run.id)
        .order_by(RunSpanNode.step_index, RunSpanNode.sibling_order)
    )
    spans = list(span_result.scalars().all())
    observations = []
    for node in spans:
        latency_ms = resolve_span_node_latency_ms(
            latency_ms=node.latency_ms,
            started_at=node.started_at,
            ended_at=node.ended_at,
        )
        observations.append(
            {
                "id": node.observation_id or node.id,
                "name": node.name,
                "type": (node.node_type or "span").upper(),
                "metadata": node.attributes if isinstance(node.attributes, dict) else {},
                "latency": latency_ms,
                "startTime": _iso_dt(node.started_at),
                "endTime": _iso_dt(node.ended_at),
                "output": node.summary_text,
            }
        )

    qnum = get_question_id(run)
    task_id = get_task_id(run) or run.task_id or ""
    return {
        "id": run.trace_id or run.id,
        "name": run.agent_name or run.agent_id,
        "sessionId": (
            build_eval_session_id(task_id, qnum)
            if task_id and qnum is not None
            else None
        ),
        "timestamp": _iso_dt(run.started_at),
        "metadata": {
            "onto_eval_task_id": task_id,
            "onto_eval_question_id": str(qnum) if qnum is not None else "",
            "agent_run_id": run.id,
            "source": "fact_layer",
        },
        "input": run.input_snapshot,
        "output": run.output_snapshot,
        "observations": observations,
    }
