"""离线评测过程评估：复用 Langfuse 历史 trace observations 跑 Checkpoint + Scenario。

约定：仅当任务 evaluation_mode='offline' 且在 langfuse_source 模式下才调用。
实现方式：为每个历史 trace 创建 evaluation_mode='offline' 的 stub AgentRun，
将 trace.observations 物化成 RunSpanNode 后调用既有的 checkpoint / scenario 评估函数。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import LANGFUSE_ENABLED
from database import (
    AgentRun,
    CheckpointResult,
    EvaluationResult,
    EvaluationTask,
    RunSpanNode,
    ScenarioStepResult,
    ScenarioStepSpanRef,
)
from logger import log
from services.eval_context import build_eval_context
from services.langfuse_answer_source import fetch_langfuse_answer_map
from services.langfuse_client import LANGFUSE_WORKER_TIMEOUT, get_langfuse_client
from services.span_tree_builder import build_span_node_rows


def _extract_langfuse_source(task: EvaluationTask) -> Optional[Dict[str, Any]]:
    """优先 task.langfuse_source JSON 字段；缺失则尝试 summary 内嵌。"""
    raw = getattr(task, "langfuse_source", None)
    if raw:
        try:
            cfg = raw if isinstance(raw, dict) else json.loads(raw)
        except (TypeError, ValueError):
            cfg = None
        if isinstance(cfg, dict) and cfg:
            return cfg
    summary = getattr(task, "summary", None)
    if summary:
        try:
            meta = json.loads(summary) if isinstance(summary, str) else summary
        except (TypeError, ValueError):
            meta = None
        if isinstance(meta, dict):
            lf = meta.get("langfuse_source")
            if isinstance(lf, dict) and lf:
                return lf
    return None


def _build_eval_context_for_offline_run(
    *,
    task: EvaluationTask,
    question_id: int,
    run_index: int,
    dataset_item_id: Optional[str] = None,
    scenario_key: Optional[str] = None,
) -> Dict[str, Any]:
    return build_eval_context(
        task_id=task.id,
        dataset_id=task.dataset_id or "",
        question_id=question_id,
        run_index=run_index,
        dataset_item_id=dataset_item_id,
        scenario_key=scenario_key,
        extra={"evaluation_mode": "offline", "source": "langfuse_replay"},
    )


def _build_agent_snapshot(task: EvaluationTask) -> str:
    return json.dumps(
        {
            "source": "offline_langfuse_replay",
            "task_id": task.id,
            "agent_id": task.agent_id,
            "logical_agent_id": task.agent_id,
            "agent_version": "offline-stub",
            "agent_name": "offline-stub",
        },
        ensure_ascii=False,
    )


async def _clear_previous_offline_artifacts(
    session: AsyncSession, run_id: str
) -> None:
    await session.execute(
        delete(ScenarioStepSpanRef).where(ScenarioStepSpanRef.agent_run_id == run_id)
    )
    await session.execute(
        delete(ScenarioStepResult).where(ScenarioStepResult.agent_run_id == run_id)
    )
    await session.execute(
        delete(CheckpointResult).where(CheckpointResult.agent_run_id == run_id)
    )
    await session.execute(delete(RunSpanNode).where(RunSpanNode.agent_run_id == run_id))
    await session.execute(delete(AgentRun).where(AgentRun.id == run_id))


async def _clear_old_offline_stubs(
    session: AsyncSession,
    task_id: str,
    question_id: int,
) -> int:
    old_runs = (
        await session.execute(
            select(AgentRun.id).where(
                AgentRun.task_id == task_id,
                AgentRun.question_id == question_id,
                AgentRun.evaluation_mode == "offline",
            )
        )
    ).scalars().all()
    if not old_runs:
        return 0
    for old_id in old_runs:
        await _clear_previous_offline_artifacts(session, old_id)
    return len(old_runs)


async def _materialize_span_nodes(
    session: AsyncSession,
    run_id: str,
    rows: List[Dict[str, Any]],
) -> List[RunSpanNode]:
    if not rows:
        return []
    objects = [RunSpanNode(**row) for row in rows]
    session.add_all(objects)
    await session.flush()
    return objects


async def _fetch_trace_with_observations(
    trace_id: str, project: Optional[str]
) -> Optional[Dict[str, Any]]:
    try:
        client = get_langfuse_client(project)
    except Exception as e:  # pragma: no cover - 防御性
        log("WARN", "OFFLINE_PROCESS", "构造 Langfuse 客户端失败", error=str(e))
        return None
    try:
        return await client.get_trace(trace_id, timeout=LANGFUSE_WORKER_TIMEOUT)
    except Exception as e:
        log(
            "WARN",
            "OFFLINE_PROCESS",
            "拉取 Langfuse trace 详情失败",
            trace_id=trace_id,
            error=str(e),
        )
        return None


async def _load_dataset_items(
    session: AsyncSession,
    dataset_id: Optional[str],
) -> List[Dict[str, Any]]:
    if not dataset_id:
        return []
    from database import Dataset
    from services.dataset_scenario_service import load_dataset_items

    ds = await session.get(Dataset, dataset_id)
    if not ds:
        return []
    return load_dataset_items(ds.data)


async def _load_offline_agent_answer(
    session: AsyncSession,
    task: EvaluationTask,
    question_id: int,
) -> str:
    row = (
        await session.execute(
            select(EvaluationResult.agent_answer)
            .where(
                EvaluationResult.task_id == task.id,
                EvaluationResult.question_id == question_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return (row or "").strip()


async def _resolve_source_llm_model(
    session: AsyncSession, cfg: Dict[str, Any]
) -> Optional[str]:
    """从 langfuse_source.source_task_id 解析源在线任务的 llm_model。

    离线回放场景子步骤 LLM judge 需与源在线任务使用同一模型，
    否则 _resolve_effective_llm_model 会回退到离线任务自身的 llm_model
    （可能是 "auto" → 模型池第一个），导致打分不一致。
    """
    source_task_id = (cfg.get("source_task_id") or "").strip()
    if not source_task_id:
        return None
    source_task = await session.get(EvaluationTask, source_task_id)
    if not source_task:
        return None
    llm_model = (getattr(source_task, "llm_model", None) or "").strip()
    return llm_model or None


async def _evaluate_one_trace(
    session: AsyncSession,
    task: EvaluationTask,
    *,
    trace_id: str,
    question_id: int,
    project: Optional[str],
    run_index: int = 0,
    dataset_item_id: Optional[str] = None,
    scenario_key: Optional[str] = None,
    enable_checkpoint: bool = True,
    source_llm_model: Optional[str] = None,
) -> Dict[str, Any]:
    trace = await _fetch_trace_with_observations(trace_id, project)
    run_id = f"offline_run_{task.id}_{question_id}_{run_index}_{uuid.uuid4().hex[:8]}"
    base_record: Dict[str, Any] = {
        "run_id": run_id,
        "trace_id": trace_id,
        "project": project,
        "question_id": question_id,
        "checkpoint_count": 0,
        "scenario_count": 0,
        "ok": False,
    }

    if not trace:
        log(
            "WARN",
            "OFFLINE_PROCESS",
            "trace 详情为空，跳过",
            trace_id=trace_id,
            task_id=task.id,
        )
        return {**base_record, "skipped": True, "reason": "trace_empty"}

    await _clear_old_offline_stubs(session, task.id, question_id)

    agent_run = AgentRun(
        id=run_id,
        origin="evaluation",
        evaluation_mode="offline",
        eval_context=_build_eval_context_for_offline_run(
            task=task,
            question_id=question_id,
            run_index=run_index,
            dataset_item_id=dataset_item_id,
            scenario_key=scenario_key,
        ),
        task_id=task.id,
        dataset_id=task.dataset_id,
        question_id=question_id,
        run_index=run_index,
        agent_id=(task.agent_id or "offline-stub"),
        logical_agent_id=(task.agent_id or "offline-stub"),
        agent_version="offline-stub",
        agent_name="offline-stub",
        trace_id=trace_id,
        root_trace_id=trace_id,
        langfuse_project=project,
        # 继承源在线任务的 llm_model，使场景子步骤 LLM judge 使用同一模型。
        # _resolve_effective_llm_model 优先取 inference_model_id，避免回退到
        # 离线任务自身的 llm_model（可能是 "auto" → 模型池第一个）导致打分偏差。
        inference_model_id=source_llm_model,
        status="success",
        started_at=_trace_started_at(trace),
        ended_at=_trace_ended_at(trace),
        agent_snapshot=_build_agent_snapshot(task),
        input_snapshot={"question_id": question_id, "trace_id": trace_id},
        output_snapshot={"trace_id": trace_id, "source": "langfuse_replay"},
    )
    session.add(agent_run)
    await session.flush()

    rows = build_span_node_rows(trace, run_id)
    span_nodes = await _materialize_span_nodes(session, run_id, rows)

    checkpoint_rows: List[Dict[str, Any]] = []
    if enable_checkpoint:
        try:
            from services.checkpoint_batch_service import (
                batch_evaluate_checkpoints_for_run,
            )

            checkpoint_rows = await batch_evaluate_checkpoints_for_run(
                session, agent_run, span_nodes
            )
        except Exception as e:
            log(
                "ERROR",
                "OFFLINE_PROCESS",
                "Checkpoint 批量评估失败",
                run_id=run_id,
                error=str(e),
            )
    else:
        # enable_process_consistency=False：仅物化 AgentRun/span 供场景子步骤评估，
        # 不跑 checkpoint（过程一致性结论将由 verdict 层显示为 disabled）
        log(
            "INFO",
            "OFFLINE_PROCESS",
            "跳过 checkpoint 评估（enable_process_consistency=False）",
            run_id=run_id,
        )

    scenario_count = 0
    scenario_error: Optional[str] = None
    # 场景子步骤打分由 pipeline 中的 _run_auto_scenario_scoring 统一执行，
    # 此处不再重复调用，避免 existing_steps 导致第二次调用 skip_llm_judge

    base_record.update(
        {
            "ok": True,
            "checkpoint_count": len(checkpoint_rows),
            "scenario_count": scenario_count,
            "span_node_count": len(span_nodes),
        }
    )
    if scenario_error:
        base_record["scenario_error"] = scenario_error
    return base_record


def _trace_started_at(trace: Dict[str, Any]) -> Any:
    from services.observation_latency import parse_observation_datetime

    val = trace.get("timestamp") or trace.get("startTime") or trace.get("start_time")
    return parse_observation_datetime(val) if val else None


def _trace_ended_at(trace: Dict[str, Any]) -> Any:
    from services.observation_latency import parse_observation_datetime

    val = trace.get("endTime") or trace.get("end_time")
    return parse_observation_datetime(val) if val else None


async def run_offline_process_eval(
    session: AsyncSession,
    task_id: str,
    *,
    progress_cb: Optional[Any] = None,
    enable_checkpoint: bool = True,
) -> Optional[Dict[str, Any]]:
    """离线任务：拉 Langfuse 历史 trace → stub AgentRun → 跑 Checkpoint/Scenario。

    返回：{ run_count, runs, ok }；前置条件不满足时返回 skipped。
    progress_cb: 可选异步回调 async def cb(question_id: int, done: bool) -> None
    enable_checkpoint: False 时仅物化 AgentRun + span 节点供场景子步骤评估，跳过 checkpoint 评估
    """
    task = await session.get(EvaluationTask, task_id)
    if not task:
        return None
    from services.evaluation_mode import is_offline_task

    if not is_offline_task(task):
        return {
            "task_id": task_id,
            "skipped": True,
            "reason": "not_offline_task",
        }

    if not LANGFUSE_ENABLED:
        return {
            "task_id": task_id,
            "skipped": True,
            "reason": "langfuse_disabled",
        }

    cfg = _extract_langfuse_source(task)
    if not cfg:
        return {
            "task_id": task_id,
            "skipped": True,
            "reason": "no_langfuse_source",
        }

    if not cfg.get("enable_process_eval", True):
        return {
            "task_id": task_id,
            "skipped": True,
            "reason": "process_eval_disabled",
        }

    try:
        _, trace_meta = await fetch_langfuse_answer_map(
            project=cfg.get("project"),
            source_task_id=cfg.get("source_task_id") or task_id,
            trace_ids=cfg.get("trace_ids"),
            tags=cfg.get("tags"),
            session_id=cfg.get("session_id"),
            agent_name=cfg.get("agent_name"),
            session=session,
        )
    except Exception as e:
        log(
            "ERROR",
            "OFFLINE_PROCESS",
            "拉取 Langfuse 答案映射失败",
            task_id=task_id,
            error=str(e),
        )
        return {
            "task_id": task_id,
            "skipped": True,
            "reason": f"fetch_failed:{e}",
        }

    dataset_items = await _load_dataset_items(session, task.dataset_id)

    # 解析源在线任务的 llm_model，用于离线 stub AgentRun 的 inference_model_id，
    # 使场景子步骤 LLM judge 与源在线任务使用同一模型
    source_llm_model = await _resolve_source_llm_model(session, cfg)
    if source_llm_model:
        log(
            "INFO",
            "OFFLINE_PROCESS",
            "继承源任务 llm_model",
            task_id=task_id,
            source_llm_model=source_llm_model,
        )

    runs: List[Dict[str, Any]] = []
    for meta in trace_meta or []:
        trace_id = meta.get("trace_id")
        if not trace_id:
            continue
        project = cfg.get("project")
        q_idx = int(meta.get("question_id") or 0)
        ditem = dataset_items[q_idx] if 0 <= q_idx < len(dataset_items) else {}
        if progress_cb:
            try:
                await progress_cb(q_idx, False)
            except Exception:
                pass
        record = await _evaluate_one_trace(
            session,
            task,
            trace_id=trace_id,
            question_id=q_idx,
            project=project,
            dataset_item_id=str(ditem.get("id") or "").strip() or None,
            scenario_key=str(ditem.get("scenario_key") or "").strip() or None,
            enable_checkpoint=enable_checkpoint,
            source_llm_model=source_llm_model,
        )
        if progress_cb:
            try:
                await progress_cb(q_idx, True)
            except Exception:
                pass
        runs.append(record)

    succeeded = sum(1 for r in runs if r.get("ok"))
    errors = [
        {"question_id": r.get("question_id"), "error": r.get("scenario_error")}
        for r in runs
        if r.get("scenario_error")
    ]
    if succeeded > 0:
        task.process_eval_completed = True
    await session.flush()

    log(
        "INFO",
        "OFFLINE_PROCESS",
        "离线过程评估完成",
        task_id=task_id,
        run_count=len(runs),
        succeeded=succeeded,
        error_count=len(errors),
    )
    result: Dict[str, Any] = {
        "task_id": task_id,
        "skipped": False,
        "run_count": len(runs),
        "succeeded": succeeded,
        "runs": runs,
    }
    if errors:
        result["errors"] = errors
    return result
