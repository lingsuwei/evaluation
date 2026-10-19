"""
事实摄入服务（ADR-027）：提供 Langfuse trace 拉取能力。

v1.23 起 Webhook/Push 端点已移除，唯一物化路径为 Trace Sync Worker。
本模块仅保留 fetch_trace_from_langfuse（仍被 scenario_step_trace_service 等引用）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from config import LANGFUSE_ENABLED
from logger import log
from services.langfuse_client import get_langfuse_client


async def fetch_trace_from_langfuse(
    trace_id: str,
    langfuse_project: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not LANGFUSE_ENABLED:
        return None
    try:
        client = get_langfuse_client(langfuse_project)
        trace = await client.get_trace(trace_id)
        return trace if isinstance(trace, dict) else None
    except Exception as e:
        log("WARNING", "FACT_INGEST", "Langfuse 补拉 trace 失败", trace_id=trace_id, error=str(e))
        return None
