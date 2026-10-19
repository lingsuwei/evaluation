"""
前后端共享的 Langfuse trace 过滤规则。

集中放置"评估包装 trace"识别逻辑，避免在前端 LangfuseTraceExplore.tsx
和后端 langfuse_answer_source.fetch_langfuse_answer_map / 其他离线评估
链路两处分别维护。

判定规则（与前端 ``isEvalReportedTrace`` 保持完全一致）：
    1. trace.name 以 "onto-eval/" 开头 —— trace_agent_inference 拼出来的
       评估包装 trace 命名（见 backend/services/langfuse_tracing.py:91）。
    2. trace.tags 中包含 "onto-eval" —— onto-eval 端上报时统一打的标签。

任一命中即视为"评估包装 trace"，浏览/预览/答案映射场景下应当剔除。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

# 评估包装 trace 的两条特征 —— 与前端 isEvalReportedTrace 保持一致。
EVAL_REPORTED_NAME_PREFIX = "onto-eval/"
EVAL_REPORTED_TAG = "onto-eval"


def is_eval_reported_trace(trace: Dict[str, Any]) -> bool:
    """判定一条 Langfuse trace 是不是 onto-eval 自己上报的"评估包装 trace"。

    Args:
        trace: Langfuse trace dict（来自 list_traces / get_trace 的 data 字段）。

    Returns:
        True 表示是 onto-eval 内部上报的评估包装 trace，应当从业务视图剔除。
    """
    if not isinstance(trace, dict):
        return False
    name = trace.get("name")
    if isinstance(name, str) and name.strip().startswith(EVAL_REPORTED_NAME_PREFIX):
        return True
    tags = trace.get("tags")
    if isinstance(tags, Iterable) and EVAL_REPORTED_TAG in tags:
        return True
    return False


def filter_out_eval_reported_traces(
    traces: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """从 trace 列表里剔除评估包装 trace。"""
    return [t for t in traces if not is_eval_reported_trace(t)]
