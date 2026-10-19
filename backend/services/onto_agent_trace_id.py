"""onto-agent run_id ↔ Langfuse trace_id（与 LangfuseExporter._stable_trace_id 对齐）。"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional


def stable_langfuse_trace_id(session_id: str, run_id: str) -> str:
    """
    与 onto-agent `LangfuseExporter._stable_trace_id` 相同：
    uuid5(NAMESPACE_DNS, "{session_id}:{run_id}").hex
    """
    return uuid.uuid5(
        uuid.NAMESPACE_DNS,
        "{}:{}".format(session_id or "", run_id),
    ).hex


def resolve_trace_id_from_agent_response(result: Dict[str, Any]) -> Optional[str]:
    """
    从 agentic/runs/sync 等 JSON 响应解析 Langfuse trace_id。

    优先显式字段；否则用 session_id + run_id 推导（onto-agent 写入 Langfuse 的稳定 id）。
    """
    if not isinstance(result, dict):
        return None

    for key in ("trace_id", "langfuse_trace_id", "langfuseTraceId"):
        tid = str(result.get(key) or "").strip()
        if tid:
            return tid

    run_id = str(result.get("run_id") or "").strip()
    if not run_id:
        return None

    session_id = str(result.get("session_id") or "").strip()
    return stable_langfuse_trace_id(session_id, run_id)


def extract_onto_agent_run_fields(result: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """提取 onto-agent 侧 run 标识，供落库与日志。"""
    if not isinstance(result, dict):
        return {"onto_agent_run_id": None, "session_id": None, "trace_id": None}
    return {
        "onto_agent_run_id": str(result.get("run_id") or "").strip() or None,
        "session_id": str(result.get("session_id") or "").strip() or None,
        "trace_id": resolve_trace_id_from_agent_response(result),
    }
