"""运行时目标历史建议（M-02）：同 Agent + 数据集历史 Run 的 P75/P90。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Agent, AgentRun, EvaluationTask
from services.inference_duration_service import batch_resolve_agent_e2e_duration_ms


def _percentile(values: List[float], p: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (p / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return float(ordered[low] + (ordered[high] - ordered[low]) * weight)


async def suggest_runtime_targets(
    session: AsyncSession,
    task_id: str,
    *,
    exclude_task_id: bool = True,
    sample_limit: int = 500,
) -> Optional[Dict[str, Any]]:
    """
    按 logical_agent_id + dataset_id 聚合历史成功 Run，计算 inference_latency / tokens_total 的 P75/P90。
    不自动写入任务目标，仅返回建议供 UI 确认。
    """
    task = await session.get(EvaluationTask, task_id)
    if not task:
        return None

    logical_agent_id: Optional[str] = None
    if task.agent_id:
        agent = await session.get(Agent, task.agent_id)
        if agent:
            logical_agent_id = agent.logical_agent_id or agent.id

    dataset_id = task.dataset_id
    if not dataset_id:
        return _empty_suggestion(logical_agent_id, dataset_id, reason="missing_dataset_id")

    query = (
        select(AgentRun)
        .where(
            AgentRun.dataset_id == dataset_id,
            AgentRun.status == "success",
            AgentRun.origin == "evaluation",
        )
        .order_by(AgentRun.started_at.desc())
        .limit(sample_limit)
    )
    if logical_agent_id:
        query = query.where(AgentRun.logical_agent_id == logical_agent_id)
    if exclude_task_id:
        query = query.where(AgentRun.task_id != task_id)

    runs = (await session.execute(query)).scalars().all()
    if not runs:
        return _empty_suggestion(logical_agent_id, dataset_id, reason="insufficient_history")

    run_by_id = {run.id: run for run in runs}
    run_ids = list(run_by_id.keys())
    latency_ms: List[float] = []
    token_vals: List[float] = []

    if run_ids:
        latency_by_run = await batch_resolve_agent_e2e_duration_ms(session, runs)
        for val in latency_by_run.values():
            latency_ms.append(float(val))

        for run in runs:
            if run.tokens_total is not None and run.tokens_total > 0:
                token_vals.append(float(run.tokens_total))

    if not latency_ms and not token_vals:
        return _empty_suggestion(logical_agent_id, dataset_id, reason="insufficient_history")

    return {
        "source": "historical",
        "logical_agent_id": logical_agent_id,
        "dataset_id": dataset_id,
        "sample_count": max(len(latency_ms), len(token_vals)),
        "latency_sample_count": len(latency_ms),
        "tokens_sample_count": len(token_vals),
        "inference_latency_ms": _metric_suggestion(latency_ms),
        "tokens_total": _metric_suggestion(token_vals),
    }


def _metric_suggestion(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {"p75": None, "p90": None, "sample_count": 0}
    return {
        "p75": round(_percentile(values, 75) or 0),
        "p90": round(_percentile(values, 90) or 0),
        "sample_count": len(values),
    }


def _empty_suggestion(
    logical_agent_id: Optional[str],
    dataset_id: Optional[str],
    *,
    reason: str,
) -> Dict[str, Any]:
    return {
        "source": "historical",
        "logical_agent_id": logical_agent_id,
        "dataset_id": dataset_id,
        "sample_count": 0,
        "reason": reason,
        "inference_latency_ms": {"p75": None, "p90": None, "sample_count": 0},
        "tokens_total": {"p75": None, "p90": None, "sample_count": 0},
    }


def build_task_run_distribution(
    latency_ms: List[float],
    token_vals: List[float],
) -> Dict[str, Any]:
    """
    单次评测任务内：按本任务各题 Run 的时延 / Tokens 计算 P75/P90。
    1 条样本 = 1 个 AgentRun（同题多轮则每轮各算 1 条）。
    """
    if not latency_ms and not token_vals:
        return {
            "source": "task",
            "sample_count": 0,
            "reason": "insufficient_data",
            "inference_latency_ms": {"p75": None, "p90": None, "sample_count": 0},
            "tokens_total": {"p75": None, "p90": None, "sample_count": 0},
        }
    return {
        "source": "task",
        "sample_count": max(len(latency_ms), len(token_vals)),
        "latency_sample_count": len(latency_ms),
        "tokens_sample_count": len(token_vals),
        "inference_latency_ms": _metric_suggestion(latency_ms),
        "tokens_total": _metric_suggestion(token_vals),
    }
