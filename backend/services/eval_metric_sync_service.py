"""
evaluation_results → run_metrics 同步（P2 F-01～F-02）。

metric_key 命名：
- 评测分：`eval.<metric_name>`（小写、非字母数字替换为 `_`）
- 运行时：`runtime.*`（由 runtime_fact_builder 写入）
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, EvaluationResult, RunMetric
from evaluators.runtime_metrics import is_runtime_metric
from logger import log


def eval_metric_key(metric_name: str) -> str:
    """F-01：将评测因子名映射为 `eval.*` 键。"""
    raw = (metric_name or "unknown").strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "_", raw).strip("_") or "unknown"
    return f"eval.{slug}"


def _dims_hash(dims: Optional[Dict[str, Any]]) -> str:
    if not dims:
        return ""
    raw = json.dumps(dims, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def _upsert_eval_metric(
    session: AsyncSession,
    agent_run_id: str,
    metric_key: str,
    *,
    value_num: Optional[float] = None,
    value_json: Optional[Dict[str, Any]] = None,
    dims: Optional[Dict[str, Any]] = None,
) -> None:
    dims_hash = _dims_hash(dims)
    await session.execute(
        delete(RunMetric).where(
            RunMetric.agent_run_id == agent_run_id,
            RunMetric.metric_key == metric_key,
            RunMetric.source == "evaluation",
            RunMetric.dims_hash == dims_hash,
        )
    )
    session.add(
        RunMetric(
            id=f"metric_{uuid.uuid4().hex[:12]}",
            agent_run_id=agent_run_id,
            metric_key=metric_key,
            source="evaluation",
            value_num=value_num,
            value_json=value_json,
            dims=dims,
            dims_hash=dims_hash,
        )
    )


async def _resolve_runtime_raw_value(
    session: AsyncSession,
    result: EvaluationResult,
    metric_name: str,
) -> Optional[float]:
    """运行时指标：value_num 存原始测量值，而非效率归一化分。"""
    if metric_name == "inference_latency":
        run_id = (result.agent_run_id or "").strip()
        if run_id:
            from services.inference_duration_service import (
                batch_load_trace_latency_ms,
                resolve_agent_e2e_duration_ms,
            )

            agent_run = await session.get(AgentRun, run_id)
            trace_latency = (await batch_load_trace_latency_ms(session, [run_id])).get(
                run_id
            )
            if agent_run is not None:
                resolved = resolve_agent_e2e_duration_ms(
                    agent_run,
                    trace_latency_ms=trace_latency,
                    stored_inference_duration_ms=result.inference_duration_ms,
                )
                if resolved is not None:
                    return float(resolved)
        if result.inference_duration_ms is not None:
            return float(max(0, int(result.inference_duration_ms)))
        return None

    if metric_name == "tokens_total":
        run_id = (result.agent_run_id or "").strip()
        if run_id:
            agent_run = await session.get(AgentRun, run_id)
            if agent_run is not None:
                from services.inference_duration_service import resolve_tokens_total_for_run

                resolved = await resolve_tokens_total_for_run(session, agent_run)
                if resolved is not None:
                    return float(resolved)
        return None

    return None


async def _sync_token_submetrics_from_run(
    session: AsyncSession,
    agent_run_id: str,
) -> None:
    """tokens_total 同步时，写入 LLM/工具 Token 分项（优先 Trace 物化 run_metrics）。"""
    from services.inference_duration_service import load_token_breakdown_for_run

    tokens_llm, tokens_tool = await load_token_breakdown_for_run(session, agent_run_id)
    dims = {"metric_name": "tokens_breakdown"}
    if tokens_llm is not None:
        await _upsert_eval_metric(
            session,
            agent_run_id,
            "eval.tokens_llm",
            value_num=float(tokens_llm),
            dims=dims,
        )
    if tokens_tool is not None:
        await _upsert_eval_metric(
            session,
            agent_run_id,
            "eval.tokens_tool",
            value_num=float(tokens_tool),
            dims=dims,
        )


async def sync_evaluation_result_to_run_metrics(
    session: AsyncSession,
    result: EvaluationResult,
) -> bool:
    """
    将单条 evaluation_results 幂等同步到 run_metrics。
    无 agent_run_id 时跳过（离线 stub 等场景）。

    运行时指标（inference_latency / tokens_total）：
    - value_num = 原始测量值（ms / tokens）
    - value_json.efficiency_score = 效率归一化分（0～1）
    """
    run_id = (result.agent_run_id or "").strip()
    if not run_id:
        return False

    metric_name = (result.metric_name or "unknown").strip()
    key = eval_metric_key(metric_name)
    score = result.avg_score if result.avg_score is not None else result.score

    value_json: Dict[str, Any] = {
        "passed": bool(result.passed),
        "threshold": result.threshold,
        "eval_method": result.eval_method,
        "evaluation_result_id": result.id,
    }
    if result.script_name:
        value_json["script_name"] = result.script_name
    if result.inference_duration_ms is not None:
        value_json["inference_duration_ms"] = result.inference_duration_ms
    if result.eval_rounds is not None:
        value_json["eval_rounds"] = result.eval_rounds

    if result.eval_method == "runtime" and is_runtime_metric(metric_name):
        raw_value = await _resolve_runtime_raw_value(session, result, metric_name)
        value_num = raw_value
        if score is not None:
            value_json["efficiency_score"] = float(score)
    else:
        value_num = float(score) if score is not None else None

    dims = {"metric_name": metric_name}

    await _upsert_eval_metric(
        session,
        run_id,
        key,
        value_num=value_num,
        value_json=value_json,
        dims=dims,
    )
    if result.eval_method == "runtime" and metric_name == "tokens_total":
        await _sync_token_submetrics_from_run(session, run_id)
    await session.flush()
    log(
        "INFO",
        "EVAL_METRIC_SYNC",
        "评测分同步 run_metrics",
        agent_run_id=run_id,
        metric_key=key,
        value_num=value_num,
    )
    return True
