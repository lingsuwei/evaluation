"""
Langfuse SDK 埋点封装（v2.1 写入规范，SDK v4+）
"""
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import os

from config import LANGFUSE_ENABLED, LANGFUSE_HOST, LANGFUSE_WRITE_PROJECT
from logger import log

_langfuse_by_project: dict = {}


def _get_langfuse():
    """评测写入：使用 LANGFUSE_WRITE_PROJECT 或默认项目的 API Key"""
    from langfuse import Langfuse

    from services.langfuse_project_registry import get_registry

    write_project = (LANGFUSE_WRITE_PROJECT or os.getenv("LANGFUSE_WRITE_PROJECT", "")).strip()
    cfg = get_registry().get_config(write_project or None)
    cache_key = cfg.id
    if cache_key not in _langfuse_by_project:
        _langfuse_by_project[cache_key] = Langfuse(
            host=LANGFUSE_HOST,
            public_key=cfg.public_key,
            secret_key=cfg.secret_key,
        )
    return _langfuse_by_project[cache_key]


def get_langfuse_client():
    """公开导出：供 chat LLM trace 等跨模块复用。

    返回 Langfuse 客户端实例；若 Langfuse 未启用或项目未注册则返回 None。
    """
    if not LANGFUSE_ENABLED:
        return None
    try:
        return _get_langfuse()
    except Exception as e:
        log("WARNING", "LANGFUSE", "chat trace 获取客户端失败，回退无 trace",
            error=str(e))
        return None


@dataclass
class TraceContext:
    task_id: str
    question_id: int
    run_index: int
    agent_id: str
    logical_agent_id: Optional[str]
    agent_version: Optional[str]
    dataset_id: str
    agent_name: str


def _build_metadata(ctx: TraceContext, question: str = "") -> Dict[str, str]:
    """propagate_attributes 要求 metadata 值为字符串"""
    raw = {
        "onto_eval_task_id": ctx.task_id,
        "onto_eval_question_id": str(ctx.question_id),
        "onto_eval_question": (question or "")[:500],
        "onto_eval_run_index": str(ctx.run_index),
        "agent_id": ctx.agent_id,
        "logical_agent_id": ctx.logical_agent_id or ctx.agent_id,
        "agent_version": ctx.agent_version or "unknown",
        "dataset_id": ctx.dataset_id,
        "agent_name": ctx.agent_name,
    }
    return {k: str(v) for k, v in raw.items()}


def _build_tags(ctx: TraceContext) -> list:
    logical_id = ctx.logical_agent_id or ctx.agent_id
    version = ctx.agent_version or "unknown"
    return [
        "onto-eval",
        f"logical_agent:{logical_id}",
        f"version:{version}",
        f"task:{ctx.task_id}",
    ]


async def trace_agent_inference(
    *,
    question: str,
    ctx: TraceContext,
    call_fn: Callable,
) -> Dict[str, Any]:
    """
    在 Langfuse 中记录一次 Agent 推理 trace，并执行实际推理回调。
    """
    if not LANGFUSE_ENABLED:
        return await call_fn()

    from langfuse import propagate_attributes

    from services.eval_session import build_eval_session_id

    session_id = build_eval_session_id(ctx.task_id, ctx.question_id)
    metadata = _build_metadata(ctx, question=question)
    tags = _build_tags(ctx)
    trace_name = f"onto-eval/{ctx.agent_name}/q{ctx.question_id}/run{ctx.run_index}"

    langfuse = None
    try:
        langfuse = _get_langfuse()
        with langfuse.start_as_current_observation(
            as_type="span",
            name=trace_name,
            input={"question": question[:2000]},
        ):
            with propagate_attributes(
                session_id=session_id,
                metadata=metadata,
                tags=tags,
                trace_name=trace_name,
            ):
                with langfuse.start_as_current_observation(
                    as_type="generation",
                    name="agent-inference",
                    model=ctx.logical_agent_id or ctx.agent_name,
                    input=question,
                ) as generation:
                    result = await call_fn()
                    output = result.get("model_answer", "")
                    if result.get("error"):
                        generation.update(
                            output=output[:2000] if output else None,
                            level="ERROR",
                            status_message=str(result.get("error"))[:500],
                        )
                    else:
                        generation.update(output=output[:4000] if output else None)
                    return result
    except Exception as e:
        from services.langfuse_project_registry import ProjectNotFoundError

        if isinstance(e, ProjectNotFoundError):
            log(
                "WARNING",
                "LANGFUSE",
                "评测埋点项目未在 langfuse.projects.json 注册，回退为无 trace 推理",
                project_id=e.project_id,
            )
        else:
            log("ERROR", "LANGFUSE", "埋点失败，回退为无 trace 推理", error=str(e))
        return await call_fn()
    finally:
        if langfuse is not None:
            try:
                langfuse.flush()
            except Exception as flush_err:
                log("WARNING", "LANGFUSE", "flush 失败", error=str(flush_err))
