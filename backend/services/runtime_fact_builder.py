"""
Runtime Fact Builder（P2 C-01）：trace → agent_runs + run_span_nodes + run_metrics。

P1.5 最小 token 逻辑已并入 build_facts_for_trace 统一入口。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, RunMetric, RunSpanNode
from logger import log
from services.checkpoint_batch_service import batch_evaluate_checkpoints_for_run
from services.inference_metrics import normalize_reported_submetric
from services.inference_model_display import (
    apply_inference_model_to_run,
    inference_model_label,
    pick_primary_inference_model_from_trace,
)
from services.observation_latency import (
    latency_ms_from_interval,
    resolve_observation_latency_ms,
)
from services.run_call_stats_service import materialize_call_stats_for_run
from services.member_trace_slicer import materialize_member_stats
from services.span_tree_builder import build_span_node_rows, span_attributes_complete
from services.trace_adapter import inject_eval_operation_ids


def _int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dims_hash(dims: Optional[Dict[str, Any]]) -> str:
    if not dims:
        return ""
    raw = json.dumps(dims, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _observation_tokens(obs: Dict[str, Any]) -> Optional[int]:
    usage = obs.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    for key in ("total", "totalTokens", "total_tokens", "totalTokenCount", "total_token_count"):
        n = _int_or_none(usage.get(key))
        if n is not None:
            return n
    inp = _int_or_none(
        usage.get("input")
        or usage.get("prompt")
        or usage.get("promptTokens")
        or usage.get("prompt_tokens")
        or usage.get("inputTokens")
        or usage.get("input_tokens")
    )
    out = _int_or_none(
        usage.get("output")
        or usage.get("completion")
        or usage.get("completionTokens")
        or usage.get("completion_tokens")
        or usage.get("outputTokens")
        or usage.get("output_tokens")
    )
    if inp is not None or out is not None:
        return (inp or 0) + (out or 0)
    return None


def _is_tool_observation(obs: Dict[str, Any]) -> bool:
    obs_type = (obs.get("type") or "").lower()
    return obs_type in ("tool", "tool_call", "tool_use")


def _is_llm_observation(obs: Dict[str, Any]) -> bool:
    obs_type = (obs.get("type") or "").lower()
    return obs_type in ("generation", "llm")


def summarize_token_breakdown_from_trace(
    trace: Dict[str, Any],
) -> Tuple[Optional[int], Optional[int]]:
    """
    从 trace observations 按类型拆分 LLM / 工具 Token（H-02）。

    - GENERATION/LLM → tokens_llm
    - TOOL/tool_call → tokens_tool
    - 其余带 usage 的 observation 归入 tokens_llm
    """
    observations = trace.get("observations") or []
    if not isinstance(observations, list):
        return None, None

    llm_total = 0
    tool_total = 0
    llm_found = False
    tool_found = False
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        obs_tokens = _observation_tokens(obs)
        if obs_tokens is None:
            continue
        if _is_tool_observation(obs):
            tool_total += obs_tokens
            tool_found = True
        else:
            llm_total += obs_tokens
            llm_found = True

    return (llm_total if llm_found else None), (tool_total if tool_found else None)


def _token_breakdown_from_run_snapshot(
    run: AgentRun,
) -> Tuple[Optional[int], Optional[int]]:
    """从 Pipeline 写入的 output_snapshot 读取 sync 路径已解析的细分。"""
    snap = run.output_snapshot
    if isinstance(snap, str):
        try:
            snap = json.loads(snap)
        except json.JSONDecodeError:
            return None, None
    if not isinstance(snap, dict):
        return None, None

    llm = _int_or_none(snap.get("tokens_llm"))
    tool = normalize_reported_submetric(snap.get("tokens_tool"))
    usage = snap.get("usage")
    if isinstance(usage, dict):
        if llm is None:
            from services.inference_metrics import extract_inference_token_breakdown

            llm, tool_from_usage, _ = extract_inference_token_breakdown({"usage": usage})
            if tool is None:
                tool = tool_from_usage
    return llm, tool


def _merge_token_breakdown(
    primary: Tuple[Optional[int], Optional[int]],
    secondary: Tuple[Optional[int], Optional[int]],
) -> Tuple[Optional[int], Optional[int]]:
    """合并两路细分，同项取较大值（兼容 sync 与 trace 双来源）。"""
    llm_vals = [v for v in (primary[0], secondary[0]) if v is not None]
    tool_vals = [v for v in (primary[1], secondary[1]) if v is not None]
    return (
        max(llm_vals) if llm_vals else None,
        max(tool_vals) if tool_vals else None,
    )


def summarize_tokens_from_trace(trace: Dict[str, Any]) -> Optional[int]:
    """从 observations 的 usage 字段汇总 token（尽力而为）。"""
    observations = trace.get("observations") or []
    if not isinstance(observations, list):
        return None

    total = 0
    found = False
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        obs_tokens = _observation_tokens(obs)
        if obs_tokens is not None:
            total += obs_tokens
            found = True

    return total if found else None


def count_observations(trace: Dict[str, Any]) -> int:
    observations = trace.get("observations") or []
    return len(observations) if isinstance(observations, list) else 0


async def _replace_span_nodes(
    session: AsyncSession,
    agent_run_id: str,
    node_rows: List[Dict[str, Any]],
) -> int:
    await session.execute(
        delete(RunSpanNode).where(RunSpanNode.agent_run_id == agent_run_id)
    )
    for row in node_rows:
        session.add(RunSpanNode(**row))
    await session.flush()
    return len(node_rows)


async def _upsert_runtime_metric(
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
            RunMetric.source == "runtime",
            RunMetric.dims_hash == dims_hash,
        )
    )
    session.add(
        RunMetric(
            id=f"metric_{uuid.uuid4().hex[:12]}",
            agent_run_id=agent_run_id,
            metric_key=metric_key,
            source="runtime",
            value_num=value_num,
            value_json=value_json,
            dims=dims,
            dims_hash=dims_hash,
        )
    )


def _backfill_role_roster(run: AgentRun, member_stats: Dict[str, Any]) -> None:
    """M-W3-1: 从 materialize_member_stats 结果回填 role_roster 到 agent_snapshot.workflow。

    member_stats 格式: {member_name: {role: ..., tool_count: ...}}
    role_roster 格式: [{member_name, role}]
    """
    try:
        snap = json.loads(run.agent_snapshot) if run.agent_snapshot else {}
    except (json.JSONDecodeError, TypeError):
        snap = {}
    if not isinstance(snap, dict):
        snap = {}
    workflow = snap.setdefault("workflow", {})
    if not isinstance(workflow, dict):
        workflow = {}
        snap["workflow"] = workflow
    roster = [
        {"member_name": name, "role": info.get("role", "member")}
        for name, info in member_stats.items()
        if isinstance(info, dict)
    ]
    roster.sort(key=lambda r: r["member_name"])
    workflow["role_roster"] = roster
    run.agent_snapshot = json.dumps(snap, ensure_ascii=False)


async def build_facts_for_trace(
    session: AsyncSession,
    run: AgentRun,
    trace: Dict[str, Any],
) -> Dict[str, Any]:
    """
    幂等物化：重建 Span Tree + runtime run_metrics + 补全 agent_runs 摘要字段。

    COUP-C04 / ADR-033：run.tokens_total 仅由本函数（Builder）写入，
    Pipeline 不再写头表。此为唯一写入源。
    """
    # 归属校验兜底：trace 的 metadata.run_id 必须与当前 run 匹配，
    # 防止 Level 2/3 fallback 误匹配导致题 A 物化了题 B 的 trace 数据
    _trace_meta = trace.get("metadata") or {}
    _trace_run_id = str(_trace_meta.get("run_id") or "").strip()
    _onto_run_id = (getattr(run, "onto_agent_run_id", None) or "").strip()
    if _trace_run_id and _onto_run_id and _trace_run_id != _onto_run_id:
        log(
            "ERROR",
            "TRACE_SYNC",
            "trace 归属校验失败，拒绝物化",
            trace_id=trace.get("id"),
            trace_run_id=_trace_run_id,
            run_id=_onto_run_id,
            agent_run_id=run.id,
        )
        raise ValueError(
            f"trace 归属不匹配: trace.run_id={_trace_run_id}"
            f" != run.onto_agent_run_id={_onto_run_id}"
        )

    trace_tokens = summarize_tokens_from_trace(trace)
    if trace_tokens is not None:
        run.tokens_total = trace_tokens

    obs_count = count_observations(trace)
    node_rows = build_span_node_rows(trace, run.id, provider=run.logical_agent_id)
    attributes_complete = span_attributes_complete(node_rows)
    span_count = await _replace_span_nodes(session, run.id, node_rows)

    span_nodes = (
        await session.execute(
            select(RunSpanNode).where(RunSpanNode.agent_run_id == run.id)
        )
    ).scalars().all()

    # A4：Trace Adapter 注入 eval.operation.id（查 Registry → 匹配 → 注入属性）
    injected_count = await inject_eval_operation_ids(session, run, list(span_nodes))

    # 过程一致性维度关闭时，跳过 CheckpointResult 物化（不从源头写数据）
    from database import EvaluationTask

    _task = await session.get(EvaluationTask, run.task_id) if run.task_id else None
    _enable_pc = bool(getattr(_task, "enable_process_consistency", True)) if _task else True
    if _enable_pc:
        checkpoint_evals = await batch_evaluate_checkpoints_for_run(session, run, list(span_nodes))
    else:
        checkpoint_evals = []

    call_stats = await materialize_call_stats_for_run(
        session, run.id, list(span_nodes), _upsert_runtime_metric
    )

    # M-W2-3: per-member 统计物化（按 agent.member.name 切片）
    member_stats = await materialize_member_stats(
        session, run.id, list(span_nodes), _upsert_runtime_metric
    )

    # M-W3-1: 从 member_stats 回填 role_roster 到 agent_snapshot.workflow
    if member_stats:
        _backfill_role_roster(run, member_stats)

    if trace_tokens is not None:
        await _upsert_runtime_metric(
            session,
            run.id,
            "runtime.tokens_total",
            value_num=float(trace_tokens),
        )

    trace_llm, trace_tool = summarize_token_breakdown_from_trace(trace)
    snap_llm, snap_tool = _token_breakdown_from_run_snapshot(run)
    tokens_llm, tokens_tool = _merge_token_breakdown(
        (trace_llm, trace_tool),
        (snap_llm, snap_tool),
    )
    if tokens_llm is not None:
        await _upsert_runtime_metric(
            session,
            run.id,
            "runtime.tokens_llm",
            value_num=float(tokens_llm),
        )
    if tokens_tool is not None:
        await _upsert_runtime_metric(
            session,
            run.id,
            "runtime.tokens_tool",
            value_num=float(tokens_tool),
        )

    # 合并后的 tokens_llm + tokens_tool 可能大于 trace 各 observation 之和
    # （当快照上报的 LLM tokens 大于 trace observation 汇总时），
    # 需确保 tokens_total >= tokens_llm + tokens_tool，避免 LLM Tokens > 总Tokens
    merged_total: Optional[int] = None
    if tokens_llm is not None or tokens_tool is not None:
        merged_total = (tokens_llm or 0) + (tokens_tool or 0)
    if merged_total is not None and (trace_tokens is None or merged_total > trace_tokens):
        trace_tokens = merged_total
        run.tokens_total = merged_total
        await _upsert_runtime_metric(
            session,
            run.id,
            "runtime.tokens_total",
            value_num=float(merged_total),
        )

    if trace_tokens is not None:
        from services.inference_duration_service import reconcile_tokens_total_for_run

        await reconcile_tokens_total_for_run(session, run, int(trace_tokens))

    await _upsert_runtime_metric(
        session,
        run.id,
        "runtime.observation_count",
        value_num=float(obs_count),
    )

    trace_latency = _int_or_none(trace.get("latencyMs") or trace.get("latency_ms"))
    if trace_latency is None:
        trace_latency = latency_ms_from_interval(
            trace.get("startTime") or trace.get("start_time") or trace.get("timestamp"),
            trace.get("endTime") or trace.get("end_time"),
        )
    if trace_latency is None:
        raw = trace.get("latency")
        if raw is not None:
            trace_latency = resolve_observation_latency_ms({"latency": raw})
    if trace_latency is not None:
        await _upsert_runtime_metric(
            session,
            run.id,
            "runtime.latency_ms",
            value_num=float(trace_latency),
        )
        from services.inference_duration_service import reconcile_inference_latency_for_run

        await reconcile_inference_latency_for_run(session, run, int(trace_latency))

    trace_model = pick_primary_inference_model_from_trace(trace)
    if inference_model_label(trace_model):
        apply_inference_model_to_run(run, trace_model, overwrite=True)

    await session.flush()

    # 物化不完整检测：span_nodes=0 但 trace.observations 非空
    # 典型场景：onto-agent 异步上传未完成时被 300s 超时强制处理
    incomplete_materialization = None
    if span_count == 0 and obs_count > 0:
        incomplete_materialization = {
            "reason": "zero_spans_with_observations",
            "obs_count_at_materialization": obs_count,
        }

    log(
        "INFO",
        "RUNTIME_FACT_BUILDER",
        "物化 Execution Fact",
        agent_run_id=run.id,
        trace_id=run.trace_id,
        tokens_total=run.tokens_total,
        observation_count=obs_count,
        span_nodes=span_count,
        checkpoints=len(checkpoint_evals),
        attributes_incomplete=not attributes_complete,
        incomplete=bool(incomplete_materialization),
    )
    return {
        "agent_run_id": run.id,
        "tokens_total": run.tokens_total,
        "tokens_llm": tokens_llm,
        "tokens_tool": tokens_tool,
        "observation_count": obs_count,
        "span_nodes": span_count,
        "checkpoints": len(checkpoint_evals),
        "llm_call_count": call_stats.get("llm_call_count"),
        "tool_call_count": call_stats.get("tool_call_count"),
        "attributes_incomplete": not attributes_complete,
        "incomplete_materialization": incomplete_materialization,
        "injected_operation_ids": injected_count,
    }


async def apply_runtime_facts_from_trace(
    session: AsyncSession,
    run: AgentRun,
    trace: Dict[str, Any],
) -> Dict[str, Any]:
    """兼容 P1.5 入口，委托 build_facts_for_trace。"""
    return await build_facts_for_trace(session, run, trace)
