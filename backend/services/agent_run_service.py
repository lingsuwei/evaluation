"""
AgentRun 落库服务（P1 运行事实层）。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Agent, AgentRun
from services.eval_context import build_eval_context, sync_legacy_columns_from_context
from services.inference_model_display import parse_inference_model_from_config
from services.snapshot_utils import bounded_snapshot_json


def _hash_inference_config(config: Dict[str, Any]) -> str:
    raw = json.dumps(config, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_agent_snapshot(
    agent: Agent,
    inference_config: Optional[Dict[str, Any]] = None,
) -> str:
    cfg = inference_config or {}
    if agent.inference_config and not cfg:
        try:
            cfg = json.loads(agent.inference_config)
        except (json.JSONDecodeError, TypeError):
            cfg = {}
    snapshot = {
        "agent_id": agent.id,
        "logical_agent_id": agent.logical_agent_id or agent.id,
        "agent_version": agent.version or "unknown",
        "agent_name": agent.name,
        "inference_config_hash": _hash_inference_config(cfg) if cfg else "",
        "workspace_id": cfg.get("workspace_id") or cfg.get("workspaceId") or "",
        "model": cfg.get("model") or cfg.get("model_name") or "",
    }
    model_info = parse_inference_model_from_config(cfg)
    snapshot.update(
        {
            "inference_model_provider": model_info.get("provider") or "",
            "inference_model_name": model_info.get("name") or "",
            "inference_model_version": model_info.get("version") or "",
            "inference_model_id": model_info.get("model_id") or "",
        }
    )
    return json.dumps(snapshot, ensure_ascii=False)


async def create_agent_run_started(
    session: AsyncSession,
    *,
    task_id: str,
    dataset_id: str,
    question_id: int,
    run_index: int,
    agent: Agent,
    inference_config: Optional[Dict[str, Any]] = None,
    langfuse_project: Optional[str] = None,
    dataset_item_id: Optional[str] = None,
    question_key: Optional[str] = None,
    scenario_key: Optional[str] = None,
    question: Optional[str] = None,
    ground_truth: Optional[str] = None,
) -> AgentRun:
    now = datetime.utcnow()
    eval_ctx = build_eval_context(
        task_id=task_id,
        dataset_id=dataset_id,
        question_id=question_id,
        run_index=run_index,
        dataset_item_id=dataset_item_id,
        question_key=question_key,
        scenario_key=scenario_key,
    )
    model_info = parse_inference_model_from_config(inference_config or {})
    run = AgentRun(
        id=f"run_{uuid.uuid4().hex[:12]}",
        origin="evaluation",
        eval_context=eval_ctx,
        task_id=task_id,
        dataset_id=dataset_id,
        question_id=question_id,
        run_index=run_index,
        agent_id=agent.id,
        logical_agent_id=agent.logical_agent_id or agent.id,
        agent_version=agent.version,
        agent_name=agent.name,
        langfuse_project=langfuse_project,
        inference_model_provider=model_info.get("provider"),
        inference_model_name=model_info.get("name"),
        inference_model_version=model_info.get("version"),
        inference_model_id=model_info.get("model_id"),
        status="running",
        started_at=now,
        agent_snapshot=build_agent_snapshot(agent, inference_config),
        input_snapshot=bounded_snapshot_json(
            {
                "question": question,
                "ground_truth": ground_truth,
            }
            if (question or ground_truth)
            else None
        ),
        created_at=now,
    )
    sync_legacy_columns_from_context(run)
    session.add(run)
    await session.flush()
    return run


async def complete_agent_run(
    session: AsyncSession,
    run: AgentRun,
    *,
    status: str,
    ended_at: Optional[datetime] = None,
    trace_id: Optional[str] = None,
    error_message: Optional[str] = None,
    model_answer: Optional[str] = None,
    tokens_total: Optional[int] = None,  # 已废弃（COUP-C04）：Builder 为唯一写入源，Pipeline 不再写头表
    tokens_llm: Optional[int] = None,
    tokens_tool: Optional[int] = None,
    usage: Optional[Dict[str, Any]] = None,
    execution_mode: Optional[Dict[str, Any]] = None,
    onto_agent_run_id: Optional[str] = None,
    onto_agent_session_id: Optional[str] = None,
) -> AgentRun:
    run.status = status
    run.ended_at = ended_at or datetime.utcnow()
    # COUP-C04 / ADR-033：run.tokens_total 仅由 Runtime Fact Builder 写入，
    # Pipeline 不再写头表。tokens_total 参数保留但不再写入 run.tokens_total。
    if trace_id:
        run.trace_id = trace_id
        if not getattr(run, "root_trace_id", None):
            run.root_trace_id = trace_id
        from services.trace_ref_service import ensure_root_trace_ref

        await ensure_root_trace_ref(session, run, trace_id=trace_id)
    if onto_agent_run_id:
        run.onto_agent_run_id = onto_agent_run_id
    if onto_agent_session_id:
        run.onto_agent_session_id = onto_agent_session_id
    if error_message:
        run.error_message = (error_message or "")[:2000]
    if model_answer is not None or status in ("success", "failed"):
        snapshot: Dict[str, Any] = {
            "model_answer": model_answer,
            "status": status,
        }
        if isinstance(usage, dict):
            snapshot["usage"] = usage
        if tokens_llm is not None:
            snapshot["tokens_llm"] = max(0, int(tokens_llm))
        from services.inference_metrics import normalize_reported_submetric

        reported_tool = normalize_reported_submetric(tokens_tool)
        if reported_tool is not None:
            snapshot["tokens_tool"] = reported_tool
        if isinstance(execution_mode, dict) and execution_mode:
            snapshot["execution_mode"] = execution_mode
        run.output_snapshot = bounded_snapshot_json(snapshot)
    await session.flush()
    return run


async def get_agent_run(
    session: AsyncSession,
    run_id: str,
) -> Optional[AgentRun]:
    result = await session.execute(select(AgentRun).where(AgentRun.id == run_id))
    return result.scalar_one_or_none()


def build_offline_agent_snapshot(*, llm_model: str, llm_model_name: str) -> str:
    return json.dumps(
        {
            "mode": "offline",
            "llm_model": llm_model,
            "llm_model_name": llm_model_name,
        },
        ensure_ascii=False,
    )


async def get_agent_run_for_question(
    session: AsyncSession,
    *,
    task_id: str,
    question_id: int,
    run_index: int = 0,
    agent_id: Optional[str] = None,
) -> Optional[AgentRun]:
    stmt = select(AgentRun).where(
        AgentRun.task_id == task_id,
        AgentRun.question_id == question_id,
        AgentRun.run_index == run_index,
    )
    if agent_id:
        stmt = stmt.where(AgentRun.agent_id == agent_id)
    result = await session.execute(stmt)
    rows = result.scalars().all()
    if not rows:
        return None
    if agent_id or len(rows) == 1:
        return rows[0]
    return rows[0]


async def get_or_create_offline_eval_agent_run(
    session: AsyncSession,
    *,
    task_id: str,
    dataset_id: str,
    question_id: int,
    run_index: int,
    question: str,
    ground_truth: str,
    model_answer: str,
    llm_model: str,
    llm_model_name: str,
    dataset_item_id: Optional[str] = None,
    question_key: Optional[str] = None,
    scenario_key: Optional[str] = None,
) -> AgentRun:
    """离线评测 AgentRun stub：无 trace，trace_sync_status=skipped（TD-P1-03 / H-01）。"""
    existing = await get_agent_run_for_question(
        session, task_id=task_id, question_id=question_id, run_index=run_index
    )
    if existing:
        return existing

    now = datetime.utcnow()
    eval_ctx = build_eval_context(
        task_id=task_id,
        dataset_id=dataset_id,
        question_id=question_id,
        run_index=run_index,
        dataset_item_id=dataset_item_id,
        question_key=question_key,
        scenario_key=scenario_key,
    )
    pseudo_agent_id = f"offline_{task_id}"
    run = AgentRun(
        id=f"run_{uuid.uuid4().hex[:12]}",
        origin="evaluation",
        eval_context=eval_ctx,
        task_id=task_id,
        dataset_id=dataset_id,
        question_id=question_id,
        run_index=run_index,
        agent_id=pseudo_agent_id,
        logical_agent_id="offline_eval",
        agent_version=llm_model,
        agent_name=llm_model_name,
        inference_model_provider="offline",
        inference_model_name=llm_model_name or llm_model,
        inference_model_version=llm_model,
        inference_model_id=llm_model,
        status="success",
        started_at=now,
        ended_at=now,
        trace_sync_status="skipped",
        agent_snapshot=build_offline_agent_snapshot(
            llm_model=llm_model, llm_model_name=llm_model_name
        ),
        input_snapshot=bounded_snapshot_json(
            {"question": question, "ground_truth": ground_truth}
        ),
        output_snapshot=bounded_snapshot_json({"model_answer": model_answer}),
        error_message=None,
        created_at=now,
    )
    sync_legacy_columns_from_context(run)
    session.add(run)
    await session.flush()
    return run
