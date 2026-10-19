"""
Agent 端到端推理时延统一解析（Langfuse trace 物化优先）。

语义：Agent 侧一次 run 从受理到完成的耗时（含 LLM/工具/编排），不含 eval↔Agent HTTP。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, EvaluationResult, RunMetric

RUNTIME_LATENCY_METRIC_KEY = "runtime.latency_ms"
RUNTIME_TOKENS_TOTAL_METRIC_KEY = "runtime.tokens_total"
RUNTIME_TOKENS_LLM_METRIC_KEY = "runtime.tokens_llm"
RUNTIME_TOKENS_TOOL_METRIC_KEY = "runtime.tokens_tool"

_DURATION_SNAPSHOT_KEYS = (
    "duration_ms",
    "durationMs",
    "elapsed_ms",
    "elapsedMs",
    "latency_ms",
    "latencyMs",
)


def _positive_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _snapshot_dict(run: AgentRun) -> Optional[dict]:
    snap = run.output_snapshot
    return snap if isinstance(snap, dict) else None


def duration_ms_from_agent_snapshot(run: AgentRun) -> Optional[int]:
    """从 sync 响应快照读取 Agent 上报 duration_ms。"""
    snap = _snapshot_dict(run)
    if not snap:
        return None
    for key in _DURATION_SNAPSHOT_KEYS:
        val = _positive_int(snap.get(key))
        if val is not None:
            return val
    for nested_key in ("usage", "metrics", "metadata", "output", "result"):
        nested = snap.get(nested_key)
        if not isinstance(nested, dict):
            continue
        for key in _DURATION_SNAPSHOT_KEYS:
            val = _positive_int(nested.get(key))
            if val is not None:
                return val
    return None


def duration_ms_from_run_interval(run: AgentRun) -> Optional[int]:
    """AgentRun started_at → ended_at（Agent 进程侧区间）。"""
    if not run.started_at or not run.ended_at:
        return None
    if run.ended_at < run.started_at:
        return None
    return max(0, int((run.ended_at - run.started_at).total_seconds() * 1000))


def resolve_agent_e2e_duration_ms(
    run: AgentRun,
    *,
    trace_latency_ms: Optional[int] = None,
    stored_inference_duration_ms: Optional[int] = None,
) -> Optional[int]:
    """
    解析 Agent 端到端推理时延（毫秒）。

    优先级：
    1. Langfuse 物化 runtime.latency_ms
    2. AgentRun started_at → ended_at
    3. sync 快照 duration_ms
    4. 已落库 evaluation_results.inference_duration_ms
    """
    for val in (
        trace_latency_ms,
        duration_ms_from_run_interval(run),
        duration_ms_from_agent_snapshot(run),
        stored_inference_duration_ms,
    ):
        parsed = _positive_int(val)
        if parsed is not None:
            return parsed
    return None


async def batch_load_trace_latency_ms(
    session: AsyncSession,
    run_ids: Iterable[str],
) -> Dict[str, int]:
    """批量读取 Trace 物化后的 runtime.latency_ms。"""
    ids = [rid for rid in run_ids if rid]
    if not ids:
        return {}

    rows = (
        await session.execute(
            select(RunMetric).where(
                RunMetric.agent_run_id.in_(ids),
                RunMetric.metric_key == RUNTIME_LATENCY_METRIC_KEY,
                RunMetric.source == "runtime",
            )
        )
    ).scalars().all()

    out: Dict[str, int] = {}
    for row in rows:
        parsed = _positive_int(row.value_num)
        if parsed is not None:
            out[row.agent_run_id] = parsed
    return out


async def batch_load_stored_inference_duration_ms(
    session: AsyncSession,
    run_ids: Iterable[str],
) -> Dict[str, int]:
    """批量读取 evaluation_results 已存的 inference_duration_ms。"""
    ids = [rid for rid in run_ids if rid]
    if not ids:
        return {}

    rows = (
        await session.execute(
            select(EvaluationResult).where(
                EvaluationResult.agent_run_id.in_(ids),
                EvaluationResult.metric_name == "inference_latency",
                EvaluationResult.inference_duration_ms.isnot(None),
            )
        )
    ).scalars().all()

    out: Dict[str, int] = {}
    for row in rows:
        if not row.agent_run_id or row.agent_run_id in out:
            continue
        parsed = _positive_int(row.inference_duration_ms)
        if parsed is not None:
            out[row.agent_run_id] = parsed
    return out


async def batch_resolve_agent_e2e_duration_ms(
    session: AsyncSession,
    runs: List[AgentRun],
    *,
    stored_by_run: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    """批量解析 Agent 端到端时延。"""
    run_ids = [r.id for r in runs if r and r.id]
    trace_by_run = await batch_load_trace_latency_ms(session, run_ids)
    stored = stored_by_run if stored_by_run is not None else await batch_load_stored_inference_duration_ms(
        session, run_ids
    )

    out: Dict[str, int] = {}
    for run in runs:
        if not run or not run.id:
            continue
        resolved = resolve_agent_e2e_duration_ms(
            run,
            trace_latency_ms=trace_by_run.get(run.id),
            stored_inference_duration_ms=stored.get(run.id),
        )
        if resolved is not None:
            out[run.id] = resolved
    return out


async def reconcile_inference_latency_for_run(
    session: AsyncSession,
    run: AgentRun,
    trace_latency_ms: int,
) -> int:
    """
    Trace 物化后：用 Langfuse 权威时延回写 evaluation_results 并重算效率分。
    """
    from evaluators.runtime_metrics import build_runtime_metric_values, score_runtime_metric
    from services.eval_metric_sync_service import sync_evaluation_result_to_run_metrics

    latency = max(0, int(trace_latency_ms))
    rows = (
        await session.execute(
            select(EvaluationResult).where(
                EvaluationResult.agent_run_id == run.id,
                EvaluationResult.metric_name == "inference_latency",
                EvaluationResult.eval_method == "runtime",
            )
        )
    ).scalars().all()

    for row in rows:
        row.inference_duration_ms = latency
        threshold = float(row.threshold or 0)
        raw_value, detail = build_runtime_metric_values(
            "inference_latency",
            inference_duration_ms=latency,
            tokens_total=None,
        )
        normalized, passed, _label = score_runtime_metric(
            "inference_latency", raw_value, threshold
        )
        row.score = normalized
        row.avg_score = normalized
        row.passed = passed
        row.cot = f"[运行时指标] {detail}"
        await sync_evaluation_result_to_run_metrics(session, row)

    await session.flush()
    return latency


async def load_token_breakdown_for_run(
    session: AsyncSession,
    agent_run_id: str,
) -> tuple[Optional[int], Optional[int]]:
    """优先读 Trace 物化后的 runtime token 分项，快照兜底。"""
    rows = (
        await session.execute(
            select(RunMetric).where(
                RunMetric.agent_run_id == agent_run_id,
                RunMetric.source == "runtime",
                RunMetric.metric_key.in_(
                    [
                        RUNTIME_TOKENS_LLM_METRIC_KEY,
                        RUNTIME_TOKENS_TOOL_METRIC_KEY,
                    ]
                ),
            )
        )
    ).scalars().all()

    llm_tokens: Optional[int] = None
    tool_tokens: Optional[int] = None
    for row in rows:
        parsed = _positive_int(row.value_num)
        if parsed is None:
            continue
        if row.metric_key == RUNTIME_TOKENS_LLM_METRIC_KEY:
            llm_tokens = parsed
        elif row.metric_key == RUNTIME_TOKENS_TOOL_METRIC_KEY:
            tool_tokens = parsed

    if llm_tokens is not None or tool_tokens is not None:
        return llm_tokens, tool_tokens

    run = await session.get(AgentRun, agent_run_id)
    if run is None:
        return None, None
    from services.runtime_fact_builder import _token_breakdown_from_run_snapshot

    return _token_breakdown_from_run_snapshot(run)


async def resolve_tokens_total_for_run(
    session: AsyncSession,
    run: AgentRun,
) -> Optional[int]:
    """解析 Langfuse 物化后的 tokens_total（仅 agent_runs 头表）。"""
    if run.tokens_total is not None:
        parsed = _positive_int(run.tokens_total)
        if parsed is not None:
            return parsed

    row = (
        await session.execute(
            select(RunMetric).where(
                RunMetric.agent_run_id == run.id,
                RunMetric.metric_key == RUNTIME_TOKENS_TOTAL_METRIC_KEY,
                RunMetric.source == "runtime",
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return _positive_int(row.value_num)
    return None


async def reconcile_tokens_total_for_run(
    session: AsyncSession,
    run: AgentRun,
    tokens_total: int,
) -> int:
    """
    Trace 物化后：用 Langfuse 权威 tokens_total 回写 evaluation_results 并重算效率分。
    """
    from evaluators.runtime_metrics import build_runtime_metric_values, score_runtime_metric
    from services.eval_metric_sync_service import sync_evaluation_result_to_run_metrics

    tok = max(0, int(tokens_total))
    rows = (
        await session.execute(
            select(EvaluationResult).where(
                EvaluationResult.agent_run_id == run.id,
                EvaluationResult.metric_name == "tokens_total",
                EvaluationResult.eval_method == "runtime",
            )
        )
    ).scalars().all()

    for row in rows:
        threshold = float(row.threshold or 0)
        raw_value, detail = build_runtime_metric_values(
            "tokens_total",
            inference_duration_ms=None,
            tokens_total=tok,
        )
        normalized, passed, _label = score_runtime_metric(
            "tokens_total", raw_value, threshold
        )
        row.score = normalized
        row.avg_score = normalized
        row.passed = passed
        row.cot = f"[运行时指标] {detail}"
        await sync_evaluation_result_to_run_metrics(session, row)

    await session.flush()
    return tok
