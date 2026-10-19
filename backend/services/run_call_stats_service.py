"""Run 级调用统计与 Step 序列（运行效能报告 P0）。"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple, Union

from database import AgentRun, RunSpanNode
from services.observation_latency import resolve_span_node_latency_ms

_LLM_ROLES = frozenset({"llm", "generation"})
_TOOL_ROLES = frozenset({"tool", "tool_call"})


def _attrs(node: RunSpanNode) -> Dict[str, Any]:
    raw = node.attributes
    return raw if isinstance(raw, dict) else {}


def is_llm_node(node: RunSpanNode) -> bool:
    role = (node.semantic_role or "").lower()
    if role in _LLM_ROLES:
        return True
    ntype = (node.node_type or "").lower()
    return ntype in ("generation", "llm")


def is_tool_node(node: RunSpanNode) -> bool:
    role = (node.semantic_role or "").lower()
    if role in _TOOL_ROLES:
        return True
    ntype = (node.node_type or "").lower()
    return ntype in ("tool", "tool_call", "tool_use")


def is_action_step(node: RunSpanNode) -> bool:
    return is_llm_node(node) or is_tool_node(node)


def derive_step_key(node: RunSpanNode) -> str:
    if node.aggregate_key and str(node.aggregate_key).strip():
        return str(node.aggregate_key).strip()
    attrs = _attrs(node)
    tool = attrs.get("agent.tool.name")
    if tool and str(tool).strip():
        return f"tool:{tool}"
    step_id = attrs.get("agent.step.id")
    if step_id and str(step_id).strip():
        return f"step:{step_id}"
    if is_tool_node(node):
        name = node.name or node.id
        return f"tool:{name}"
    if is_llm_node(node):
        name = node.name or "llm"
        return f"llm:{name}"
    return f"node:{node.observation_id or node.id}"


def step_label(node: RunSpanNode) -> str:
    attrs = _attrs(node)
    tool = attrs.get("agent.tool.name")
    if tool:
        return str(tool)
    if node.name:
        return str(node.name)
    return derive_step_key(node)


def _extract_tool_calls_from_llm_node(node: RunSpanNode) -> Tuple[int, List[str]]:
    """从 LLM 节点提取 tool_calls（onto-agent 工具调用嵌在 GENERATION.output）。

    优先读 attributes.agent.tool.call_count（span_tree_builder 写入）；
    兜底解析 summary_text（output JSON 字符串，旧数据兼容）。
    """
    attrs = _attrs(node)
    count = attrs.get("agent.tool.call_count")
    if isinstance(count, (int, float)) and count > 0:
        names = attrs.get("agent.tool.names")
        if isinstance(names, list):
            return int(count), [str(n) for n in names if n]
        return int(count), []
    # 旧数据兜底：summary_text 存的是 output 的 JSON 字符串
    text = node.summary_text
    if not text or not isinstance(text, str):
        return 0, []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # 截断的 JSON：尝试提取 tool_calls 片段
        return _parse_tool_calls_from_truncated_json(text)
    if not isinstance(parsed, dict):
        return 0, []
    names: List[str] = []
    top_tc = parsed.get("tool_calls")
    if isinstance(top_tc, list):
        for tc in top_tc:
            if isinstance(tc, dict):
                fn = tc.get("function")
                if isinstance(fn, dict) and fn.get("name"):
                    names.append(str(fn["name"]))
                elif tc.get("name"):
                    names.append(str(tc["name"]))
    choices = parsed.get("choices")
    if isinstance(choices, list):
        for ch in choices:
            if not isinstance(ch, dict):
                continue
            msg = ch.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("tool_calls"), list):
                continue
            for tc in msg["tool_calls"]:
                if isinstance(tc, dict):
                    fn = tc.get("function")
                    if isinstance(fn, dict) and fn.get("name"):
                        names.append(str(fn["name"]))
                    elif tc.get("name"):
                        names.append(str(tc["name"]))
    return len(names), names


def _parse_tool_calls_from_truncated_json(text: str) -> Tuple[int, List[str]]:
    """从被截断的 output JSON 字符串中尽力提取 tool_calls 工具名。

    summary_text 上限 8192 字符，超长会被截断导致 json.loads 失败。
    用括号匹配提取 "tool_calls" 数组片段后局部解析。
    """
    key = '"tool_calls"'
    idx = text.find(key)
    if idx < 0:
        return 0, []
    # 跳过 key 和冒号，定位数组起始 '['
    pos = idx + len(key)
    while pos < len(text) and text[pos] in ' \t\r\n':
        pos += 1
    if pos < len(text) and text[pos] == ':':
        pos += 1
    while pos < len(text) and text[pos] in ' \t\r\n':
        pos += 1
    if pos >= len(text) or text[pos] != '[':
        return 0, []
    # 括号匹配截取数组片段（'[', ']' 配对，考虑字符串内的转义）
    depth = 0
    start = pos
    in_str = False
    escape = False
    while pos < len(text):
        ch = text[pos]
        if in_str:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    break
        pos += 1
    fragment = text[start:pos + 1] if pos < len(text) else text[start:] + ']'
    try:
        arr = json.loads(fragment)
    except (json.JSONDecodeError, ValueError):
        return 0, []
    if not isinstance(arr, list):
        return 0, []
    names: List[str] = []
    for tc in arr:
        if isinstance(tc, dict):
            fn = tc.get("function")
            if isinstance(fn, dict) and fn.get("name"):
                names.append(str(fn["name"]))
            elif tc.get("name"):
                names.append(str(tc["name"]))
    return len(names), names


def summarize_call_counts_from_nodes(
    nodes: List[RunSpanNode],
) -> Tuple[int, int, Dict[str, int]]:
    """返回 (llm_count, tool_count, tool_name -> count)。

    onto-agent 的工具调用嵌在 GENERATION 的 output.tool_calls（非独立 observation），
    因此对 LLM 节点额外统计其 output 中的 tool_calls。
    """
    llm = 0
    tool = 0
    by_tool: Counter[str] = Counter()
    for node in nodes:
        if is_tool_node(node):
            tool += 1
            attrs = _attrs(node)
            tname = str(attrs.get("agent.tool.name") or node.name or "unknown")
            by_tool[tname] += 1
            continue
        if is_llm_node(node):
            llm += 1
            tc_count, tc_names = _extract_tool_calls_from_llm_node(node)
            if tc_count > 0:
                tool += tc_count
                for name in tc_names:
                    by_tool[name] += 1
    return llm, tool, dict(by_tool)


def build_step_sequence(nodes: List[RunSpanNode]) -> List[Dict[str, Any]]:
    """按 step_index 排序的可对比 Step 序列（仅 LLM/工具节点）。"""
    ordered = sorted(
        [n for n in nodes if is_action_step(n)],
        key=lambda n: (
            n.step_index if n.step_index is not None else 10_000,
            n.sibling_order or 0,
            n.id,
        ),
    )
    seq: List[Dict[str, Any]] = []
    for idx, node in enumerate(ordered):
        kind = "tool" if is_tool_node(node) else "llm"
        seq.append(
            {
                "index": idx + 1,
                "step_key": derive_step_key(node),
                "kind": kind,
                "label": step_label(node),
                "latency_ms": resolve_span_node_latency_ms(
                    latency_ms=node.latency_ms,
                    started_at=node.started_at,
                    ended_at=node.ended_at,
                ),
                "tokens": node.tokens,
                "observation_id": node.observation_id,
                "node_id": node.id,
            }
        )
    return seq


def backfill_step_sequence_latencies(
    seq: List[Dict[str, Any]],
    run: AgentRun,
    *,
    inference_duration_ms: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    当 Span 节点未物化 latency 时，用 Run 级耗时兜底（常见于单步 answer 场景）。

    优先级：inference_duration_ms（evaluation_results）> run.started_at/ended_at
    """
    if not seq:
        return seq
    if not all(s.get("latency_ms") is None for s in seq):
        return seq
    fallback = inference_duration_ms
    if fallback is None:
        fallback = resolve_span_node_latency_ms(
            latency_ms=None,
            started_at=run.started_at,
            ended_at=run.ended_at,
        )
    if fallback is None or len(seq) != 1:
        return seq
    out = [dict(s) for s in seq]
    out[0]["latency_ms"] = fallback
    return out


def summarize_loop_iterations(nodes: List[RunSpanNode]) -> Optional[int]:
    """主循环轮次：取 agent.loop.iteration 最大值 + 1；无属性时返回 None。"""
    max_iter = -1
    found = False
    for node in nodes:
        raw = _attrs(node).get("agent.loop.iteration")
        if raw is None:
            continue
        try:
            max_iter = max(max_iter, int(raw))
            found = True
        except (TypeError, ValueError):
            continue
    if found:
        return max_iter + 1
    return None


async def materialize_call_stats_for_run(
    session,
    agent_run_id: str,
    nodes: List[RunSpanNode],
    upsert_fn,
) -> Dict[str, Any]:
    """
    将调用统计写入 run_metrics（幂等）。
    upsert_fn: runtime_fact_builder._upsert_runtime_metric 注入，避免循环 import。
    """
    llm_count, tool_count, by_tool = summarize_call_counts_from_nodes(nodes)
    await upsert_fn(
        session,
        agent_run_id,
        "runtime.llm_call_count",
        value_num=float(llm_count),
    )
    await upsert_fn(
        session,
        agent_run_id,
        "runtime.tool_call_count",
        value_num=float(tool_count),
    )
    for tool_name, count in by_tool.items():
        await upsert_fn(
            session,
            agent_run_id,
            "runtime.tool_call_count",
            value_num=float(count),
            dims={"tool": tool_name},
        )
    loop_iters = summarize_loop_iterations(nodes)
    if loop_iters is not None:
        await upsert_fn(
            session,
            agent_run_id,
            "runtime.loop_iteration_count",
            value_num=float(loop_iters),
        )
    return {
        "llm_call_count": llm_count,
        "tool_call_count": tool_count,
        "tool_breakdown": by_tool,
        "loop_iteration_count": loop_iters,
    }
