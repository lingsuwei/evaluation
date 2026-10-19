# -*- coding: utf-8 -*-
"""从 Langfuse trace 提取 Agent 推理过程数据(Phase 2)。

把 trace.observations 遍历一遍,产出 tool_calls/thoughts/retrieval_context/turns 四元组,
供 Phase 3 的在线评估通路透传给 DeepEvalMetricAdapter.evaluate(...) 的 keyword-only 参。

提取规则基于 onto-agent 上报的 OTel 风格 metadata(agent.step.kind / agent.tool.name),
并参考 agent_eval.langfuse_reader 的字段映射(input→arguments、level→error)。
纯函数:不调 Langfuse API、不依赖 deepeval;输入 trace dict,输出 TraceProcessData。

数据来源优先级(按可靠性):
1. observation metadata(OTel attributes,onto-agent 上报最规范)
2. observation type + input/output 顶层字段
3. trace 顶层 metadata(trace 级聚合,兜底)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from services.observation_latency import (
    parse_observation_datetime,
    resolve_observation_latency_ms,
)
from services.otel_attributes import normalize_observation_attributes

from evaluators.deepeval_metrics.types import ToolCallRecord


@dataclass
class Turn:
    """多轮对话的单个回合(轻量结构,不依赖 deepeval)。

    DeepEvalMetricAdapter._build_conversational_test_case 用 getattr(t, "role"/"content") 读取,
    与 deepeval.test_case.Turn 同构(但 deepeval 未装时也能用)。
    """

    role: str  # user | assistant | system
    content: str


@dataclass
class TraceProcessData:
    """从一条 Langfuse trace 提取出的 Agent 推理过程数据。

    字段与 DeepEvalMetricAdapter.evaluate 的 keyword-only 参一一对应:
    tool_calls / thoughts / retrieval_context / turns。
    任一字段为空列表 → 适配器门控判定该指标「已跳过」(score=None),不产 0 分误判。
    """

    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    thoughts: list[str] = field(default_factory=list)
    retrieval_context: list[str] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)


# observation type 别名(与 span_tree_builder._infer_semantic_role 对齐)
_TOOL_TYPES = ("tool", "tool_call", "tool_use")
_LLM_TYPES = ("generation", "llm")
_RETRIEVAL_TYPES = ("retriever", "retrieval", "rag")
_TURN_TYPES = ("turn", "chat", "message")


def extract_process_data(trace: dict[str, Any]) -> TraceProcessData:
    """从 Langfuse trace 提取 tool_calls/thoughts/retrieval_context/turns。

    纯函数:不调外部 API,不抛异常(解析失败的字段返回空列表)。
    缺失字段返回空 TraceProcessData(适配器门控会让对应指标自动「已跳过」)。

    Args:
        trace: Langfuse trace dict(含 observations 列表)

    Returns:
        TraceProcessData: 四类过程数据,空列表表示该类数据不可用
    """
    observations = _observations(trace)
    # 按 startTime 排序,保证工具调用/思考的时间序
    sorted_obs = sorted(observations, key=_sort_key)

    tool_calls = _extract_tool_calls(sorted_obs)
    thoughts = _extract_thoughts(trace, sorted_obs)
    retrieval_context = _extract_retrieval_context(trace, sorted_obs)
    turns = _extract_turns(trace, sorted_obs)

    return TraceProcessData(
        tool_calls=tool_calls,
        thoughts=thoughts,
        retrieval_context=retrieval_context,
        turns=turns,
    )


# ---- 各类过程数据提取 ----


def _extract_tool_calls(observations: list[dict[str, Any]]) -> list[ToolCallRecord]:
    """提取工具调用记录:type∈tool_types 或 name 前缀 'tool:' 或 step.kind=='tool'。"""
    records: list[ToolCallRecord] = []
    for obs in observations:
        attrs = normalize_observation_attributes(obs)
        kind = str(attrs.get("agent.step.kind") or "").lower()
        obs_type = str(obs.get("type") or "").lower()
        name = str(obs.get("name") or "")

        is_tool = (
            obs_type in _TOOL_TYPES
            or name.startswith("tool:")
            or kind == "tool"
        )
        if not is_tool:
            continue

        # name:优先 agent.tool.name,兜底 obs.name(去掉 'tool:' 前缀)
        tool_name = str(attrs.get("agent.tool.name") or "").strip()
        if not tool_name:
            tool_name = name[5:] if name.startswith("tool:") else name
        if not tool_name:
            tool_name = "tool"

        arguments = _parse_args(obs.get("input"))
        output = obs.get("output")
        # error:level==error 时取 statusMessage
        error = None
        if str(obs.get("level") or "").lower() == "error":
            error = str(
                obs.get("statusMessage")
                or obs.get("status_message")
                or obs.get("status")
                or ""
            ) or None
        latency_ms = float(resolve_observation_latency_ms(obs) or 0)

        records.append(
            ToolCallRecord(
                name=tool_name,
                arguments=arguments,
                output=output,
                error=error,
                latency_ms=latency_ms,
            )
        )
    return records


def _extract_thoughts(
    trace: dict[str, Any], observations: list[dict[str, Any]]
) -> list[str]:
    """提取思考轨迹:优先 trace.metadata.thoughts,兜底 step.kind=='reasoning' 的 output。"""
    # 候选 1:trace 顶层 metadata.thoughts
    meta = trace.get("metadata")
    if isinstance(meta, dict):
        raw = meta.get("thoughts")
        thoughts = _to_str_list(raw)
        if thoughts:
            return thoughts

    # 候选 2:step.kind=='reasoning' 的 observation output
    thoughts: list[str] = []
    for obs in observations:
        attrs = normalize_observation_attributes(obs)
        kind = str(attrs.get("agent.step.kind") or "").lower()
        if kind != "reasoning":
            continue
        text = _summary_text(obs)
        if text:
            thoughts.append(text)
    if thoughts:
        return thoughts

    # 候选 3:LLM 类 observation metadata 里携带 thought/reasoning 字段
    for obs in observations:
        obs_type = str(obs.get("type") or "").lower()
        if obs_type not in _LLM_TYPES:
            continue
        attrs = normalize_observation_attributes(obs)
        for key in ("thought", "reasoning", "reasoning_content"):
            val = attrs.get(key)
            if isinstance(val, str) and val.strip():
                thoughts.append(val.strip())
    return thoughts


def _extract_retrieval_context(
    trace: dict[str, Any], observations: list[dict[str, Any]]
) -> list[str]:
    """提取检索上下文:优先 trace.metadata.retrieval_context,兜底 retriever observation。"""
    # 候选 1:trace 顶层 metadata.retrieval_context
    meta = trace.get("metadata")
    if isinstance(meta, dict):
        raw = meta.get("retrieval_context")
        ctx = _to_str_list(raw)
        if ctx:
            return ctx

    # 候选 2:type∈retrieval_types 或 step.kind=='retrieval' 的 output/metadata
    ctx: list[str] = []
    for obs in observations:
        attrs = normalize_observation_attributes(obs)
        kind = str(attrs.get("agent.step.kind") or "").lower()
        obs_type = str(obs.get("type") or "").lower()
        is_retrieval = obs_type in _RETRIEVAL_TYPES or kind == "retrieval"
        if not is_retrieval:
            continue
        # 优先 observation 自带 metadata.retrieval_context
        meta_ctx = _to_str_list(attrs.get("retrieval_context"))
        if meta_ctx:
            ctx.extend(meta_ctx)
            continue
        # output 可能是 list(检索片段)/str/dict,用 _to_str_list 统一 flatten
        out_ctx = _to_str_list(obs.get("output"))
        if out_ctx:
            ctx.extend(out_ctx)
            continue
        # 兜底:output→completion→answer→text 文本
        text = _summary_text(obs)
        if text:
            ctx.append(text)
    return ctx


def _extract_turns(
    trace: dict[str, Any], observations: list[dict[str, Any]]
) -> list[Turn]:
    """提取多轮对话回合:优先 trace.metadata.turns,兜底 turn 类 observation。

    Phase 2 尽力提取;Phase 4 会话模拟器才是多轮指标的主路径。
    无 turns 时返回空列表(适配器门控让 conversational 指标自动跳过)。
    """
    # 候选 1:trace 顶层 metadata.turns
    meta = trace.get("metadata")
    if isinstance(meta, dict):
        raw_turns = meta.get("turns")
        turns = _to_turn_list(raw_turns)
        if turns:
            return turns

    # 候选 2:type∈turn_types 的 observation,input→user/output→assistant
    turns: list[Turn] = []
    for obs in observations:
        obs_type = str(obs.get("type") or "").lower()
        if obs_type not in _TURN_TYPES:
            continue
        role = str(obs.get("role") or "").lower()
        content = _summary_text(obs)
        if not content:
            continue
        if not role:
            # input 有值视为 user,output 有值视为 assistant
            role = "user" if obs.get("input") else "assistant"
        turns.append(Turn(role=role, content=content))
    return turns


# ---- 辅助函数(参考 agent_eval.langfuse_reader + onto-eval span_tree_builder) ----


def _observations(trace: dict[str, Any]) -> list[dict[str, Any]]:
    """安全取 trace.observations 列表(多别名兼容)。"""
    for key in ("observations", "spans", "children"):
        val = trace.get(key)
        if isinstance(val, list):
            return [o for o in val if isinstance(o, dict)]
    return []


def _sort_key(obs: dict[str, Any]):
    """按 startTime 排序;无时间戳的排最后(保持原序)。"""
    dt = parse_observation_datetime(obs.get("startTime") or obs.get("start_time"))
    return (dt is None, dt or parse_observation_datetime("2000-01-01T00:00:00Z"))


def _parse_args(value: Any) -> dict[str, Any]:
    """observation.input(可能是 dict/str/None)→ arguments dict。

    参考agent_eval.langfuse_reader._parse_args:str 尝试 json.loads,解析不出包一层。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
        except (json.JSONDecodeError, ValueError):
            return {"value": value}
    return {}


def _summary_text(obs: dict[str, Any], *, max_len: int = 4000) -> Optional[str]:
    """取 observation 的输出文本(output→completion→answer→text),截断。"""
    for key in ("output", "completion", "answer", "text"):
        val = obs.get(key)
        if val is not None:
            s = str(val).strip()
            if s:
                return s if len(s) <= max_len else s[: max_len - 1] + "…"
    return None


def _to_str_list(value: Any) -> list[str]:
    """把 str/list/dict 归一为非空字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                s = item.strip()
                if s:
                    out.append(s)
            elif item is not None:
                s = str(item).strip()
                if s:
                    out.append(s)
        return out
    # dict 等其它类型:整体转字符串
    s = str(value).strip()
    return [s] if s else []


def _to_turn_list(value: Any) -> list[Turn]:
    """把 [{role, content}, ...] 归一为 Turn 列表。"""
    if not isinstance(value, list):
        return []
    turns: list[Turn] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if not role or not content:
            continue
        turns.append(Turn(role=role, content=content))
    return turns


# ---- 异步 fetcher(pipeline 调用,带容错) ----


async def fetch_process_data_for_trace(
    trace_id: Optional[str], project: Optional[str] = None
) -> TraceProcessData:
    """离线/通用:按 trace_id + project 拉 Langfuse trace,提取过程数据。

    任何失败(Langfuse 未配置/网络异常/trace 不存在)都返回空 TraceProcessData,
    不抛异常——pipeline 不应因 trace 拉取失败而中断,对应指标会自动「已跳过」。
    """
    if not trace_id:
        return TraceProcessData()
    try:
        from config import LANGFUSE_ENABLED

        if not LANGFUSE_ENABLED:
            return TraceProcessData()
        from services.langfuse_client import get_langfuse_client

        client = get_langfuse_client(project)
        trace = await client.get_trace(trace_id)
        return extract_process_data(trace or {})
    except Exception:  # noqa: BLE001 - 容错:trace 拉取失败不阻断评测
        return TraceProcessData()


async def fetch_process_data_for_run(
    session: Any, agent_run_id: Optional[str]
) -> TraceProcessData:
    """在线:从 AgentRun 查 trace_id + langfuse_project,拉 trace 提取过程数据。

    Phase 4:若 AgentRun.eval_context.conversation_turns 存在(会话模拟器生成),
    则 turns 优先取 eval_context(模拟器输出是 ground truth),其余过程数据仍从 trace 提取。

    Args:
        session: AsyncSession(用于查 AgentRun)
        agent_run_id: AgentRun.id;推理刚完成时 trace_id 已落库,可直接拉

    失败返回空 TraceProcessData(指标自动跳过)。
    """
    if not agent_run_id:
        return TraceProcessData()
    try:
        from database import AgentRun

        run = await session.get(AgentRun, agent_run_id)
        if run is None:
            return TraceProcessData()

        # Phase 4: 优先读 eval_context.conversation_turns(模拟器输出)
        sim_turns: list[Turn] = []
        eval_ctx = getattr(run, "eval_context", None) or {}
        if isinstance(eval_ctx, dict):
            raw_turns = eval_ctx.get("conversation_turns") or []
            if isinstance(raw_turns, list):
                sim_turns = _to_turn_list(raw_turns)

        if not run.trace_id:
            # 无 trace 但有模拟器 turns → 只返回 turns(指标门控按需跳过)
            return TraceProcessData(turns=sim_turns)

        # 有 trace → 拉 trace 提取 tool_calls/thoughts/retrieval_context
        trace_data = await fetch_process_data_for_trace(run.trace_id, run.langfuse_project)
        # turns 优先用模拟器输出;trace 提取的 turns 作为兜底
        if sim_turns:
            return TraceProcessData(
                tool_calls=trace_data.tool_calls,
                thoughts=trace_data.thoughts,
                retrieval_context=trace_data.retrieval_context,
                turns=sim_turns,
            )
        return trace_data
    except Exception:  # noqa: BLE001
        return TraceProcessData()
