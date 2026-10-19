"""Run Span Tree 读模型（P4 span_tree_ui）。"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, RunSpanNode
from services.observation_latency import resolve_span_node_latency_ms


def _json_default(obj: Any) -> Any:
    """json.dumps 的 default 回调：将 set/tuple 等非 JSON 原生类型转为 list。"""
    if isinstance(obj, (set, tuple, frozenset)):
        return list(obj)
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def _try_format_json(val: Any, *, max_len: int = 8192) -> Optional[str]:
    """请求路径上轻量格式化：仅尝试 json.loads 一次（C 实现，最快）。

    已移除 ast.literal_eval 和 _repair_truncated_literal 字符级扫描——
    它们在每节点 input/output 上消耗数毫秒~数十毫秒，N 个节点累计阻塞事件循环，
    是 span-tree 接口慢的根本原因。Python dict 风格（单引号）的文本会原样返回，
    由前端 formatIoText 做轻量正则转换（远比 ast.literal_eval 快）。
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    # 非结构化文本（不以 { 或 [ 开头）直接截断返回
    if s[0] not in '{[':
        return s if len(s) <= max_len else s[: max_len - 1] + "…"
    # 唯一一次解析尝试：标准 JSON
    try:
        formatted = json.dumps(json.loads(s), ensure_ascii=False, indent=2, default=_json_default)
        return formatted if len(formatted) <= max_len else formatted[: max_len - 1] + "…"
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    # 解析失败，原样返回（截断）
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _parse_iteration(raw: Any) -> Optional[int]:
    """将 agent.loop.iteration 属性解析为 int；非法/缺失返回 None。"""
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _node_to_dict_lite(node: RunSpanNode) -> Dict[str, Any]:
    """轻量序列化：仅保留 trace 链条对比所需的结构字段，排除 input/output 等大文本。"""
    latency_ms = resolve_span_node_latency_ms(
        latency_ms=node.latency_ms,
        started_at=node.started_at,
        ended_at=node.ended_at,
    )
    return {
        "id": node.id,
        "observation_id": node.observation_id,
        "parent_id": node.parent_id,
        "name": node.name or node.observation_id or node.id,
        "node_type": node.node_type,
        "aggregate_key": node.aggregate_key,
        "semantic_role": node.semantic_role,
        "latency_ms": latency_ms,
        "depth": node.depth,
        "step_index": node.step_index,
        "sibling_order": node.sibling_order,
    }


def _node_to_dict(node: RunSpanNode) -> Dict[str, Any]:
    attrs = node.attributes if isinstance(node.attributes, dict) else {}
    latency_ms = resolve_span_node_latency_ms(
        latency_ms=node.latency_ms,
        started_at=node.started_at,
        ended_at=node.ended_at,
    )
    # 提取 input/output 文本（span_tree_builder 已存入 attributes）
    input_text = attrs.get("agent.io.input")
    # fallback: onto-agent 将 input 存在 attributes.message（工具查询描述）或 messages_preview（LLM 对上文）
    if not input_text:
        input_text = attrs.get("message")
    if not input_text:
        input_text = attrs.get("messages_preview")
    # 尝试格式化为可读 JSON（支持 Python dict 风格的单引号字符串）
    input_text = _try_format_json(input_text)
    output_text = _try_format_json(node.summary_text)
    return {
        "id": node.id,
        "observation_id": node.observation_id,
        "parent_id": node.parent_id,
        "name": node.name or node.observation_id or node.id,
        "node_type": node.node_type,
        "aggregate_key": node.aggregate_key,
        "semantic_role": node.semantic_role,
        "latency_ms": latency_ms,
        "tokens": node.tokens,
        "depth": node.depth,
        "step_index": node.step_index,
        "sibling_order": node.sibling_order,
        "status": node.status,
        "agent_step_id": attrs.get("agent.step.id"),
        "iteration": _parse_iteration(attrs.get("agent.loop.iteration")),
        "is_deterministic_io": bool(node.is_deterministic_io),
        "mock_response_payload": node.mock_response_payload,
        "input": input_text,
        "output": output_text,
        "children": [],
    }


def build_nested_tree(flat_nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将扁平节点列表组装为森林（多根）。"""
    by_id = {n["id"]: n for n in flat_nodes}
    roots: List[Dict[str, Any]] = []
    for node in flat_nodes:
        pid = node.get("parent_id")
        parent = by_id.get(pid) if pid else None
        if parent and parent is not node:
            parent["children"].append(node)
        else:
            roots.append(node)
    for node in flat_nodes:
        node["children"].sort(key=lambda c: (c.get("step_index") or 0, c.get("sibling_order") or 0))
    roots.sort(key=lambda r: (r.get("step_index") or 0, r.get("sibling_order") or 0))
    return roots


def collect_aggregate_keys(flat_nodes: List[Dict[str, Any]]) -> Set[str]:
    keys: Set[str] = set()
    for n in flat_nodes:
        k = (n.get("aggregate_key") or "").strip()
        if k and k != "unknown":
            keys.add(k)
    return keys


async def _load_run_and_spans(
    session: AsyncSession, run_id: str
) -> tuple[Optional[AgentRun], List[RunSpanNode]]:
    rid = (run_id or "").strip()
    if not rid:
        return None, []
    run = await session.get(AgentRun, rid)
    if not run:
        result = await session.execute(
            select(AgentRun).where(
                or_(AgentRun.trace_id == rid, AgentRun.id == rid)
            )
        )
        run = result.scalars().first()
    if not run:
        return None, []
    spans = (
        await session.execute(
            select(RunSpanNode)
            .where(RunSpanNode.agent_run_id == run.id)
            .order_by(RunSpanNode.step_index, RunSpanNode.sibling_order)
        )
    ).scalars().all()
    return run, list(spans)


async def build_span_tree_for_run(
    session: AsyncSession, run_id: str
) -> Optional[Dict[str, Any]]:
    """GET /runs/{id}/span-tree 响应体。"""
    run, spans = await _load_run_and_spans(session, run_id)
    if not run:
        return None
    flat = [_node_to_dict(s) for s in spans]
    # 计算 LLM/工具调用次数
    from services.run_call_stats_service import summarize_call_counts_from_nodes
    llm_count, tool_count, _ = summarize_call_counts_from_nodes(spans)
    # 解析 run 级别 Langfuse host + project_id
    # resolve_langfuse_project_id 首次调 Langfuse API 取真实 cxxx ID，之后永久缓存
    from services.langfuse_link_service import (
        resolve_langfuse_host,
        resolve_run_langfuse_project_key,
        resolve_langfuse_project_id,
    )
    project_key = await resolve_run_langfuse_project_key(session, run)
    langfuse_host = resolve_langfuse_host(project_key)
    langfuse_project_id = await resolve_langfuse_project_id(project_key)
    return {
        "agent_run_id": run.id,
        "trace_id": run.trace_id,
        "agent_id": run.agent_id,
        "agent_name": run.agent_name,
        "task_id": run.task_id,
        "question_id": run.question_id,
        "langfuse_host": langfuse_host,
        "langfuse_project_id": langfuse_project_id,
        "nodes": flat,
        "tree": build_nested_tree(flat),
        "node_count": len(flat),
        "llm_call_count": llm_count,
        "tool_call_count": tool_count,
    }


async def build_dual_span_trees_for_comparison(
    session: AsyncSession, comparison_id: str
) -> Optional[Dict[str, Any]]:
    """对比页双树 + aggregate_key 对齐集。"""
    from services.run_comparison_service import get_comparison

    cmp = await get_comparison(session, comparison_id)
    if not cmp:
        return None
    left = await build_span_tree_for_run(session, cmp["left_run_id"])
    right = await build_span_tree_for_run(session, cmp["right_run_id"])
    if not left or not right:
        return None
    from services.checkpoint_result_service import build_run_checkpoint_report

    left_cp = await build_run_checkpoint_report(session, left["agent_run_id"])
    right_cp = await build_run_checkpoint_report(session, right["agent_run_id"])

    def _timeline(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        ordered = sorted(
            nodes,
            key=lambda n: (n.get("step_index") or 0, n.get("sibling_order") or 0),
        )
        return [
            {
                "id": n.get("id"),
                "name": n.get("name"),
                "aggregate_key": n.get("aggregate_key"),
                "latency_ms": n.get("latency_ms"),
                "mock_response_payload": n.get("mock_response_payload"),
                "is_deterministic_io": n.get("is_deterministic_io"),
            }
            for n in ordered
        ]

    left_keys = collect_aggregate_keys(left["nodes"])
    right_keys = collect_aggregate_keys(right["nodes"])
    align = sorted(left_keys & right_keys)
    only_left = sorted(left_keys - right_keys)
    only_right = sorted(right_keys - left_keys)
    return {
        "comparison_id": comparison_id,
        "left_run_id": cmp["left_run_id"],
        "right_run_id": cmp["right_run_id"],
        "left": left,
        "right": right,
        "align_aggregate_keys": align,
        "only_left_aggregate_keys": only_left,
        "only_right_aggregate_keys": only_right,
        "diffs": cmp.get("diffs") or [],
        "left_checkpoints": left_cp,
        "right_checkpoints": right_cp,
        "left_timeline": _timeline(left["nodes"]),
        "right_timeline": _timeline(right["nodes"]),
        "left_trace_id": left.get("trace_id"),
        "right_trace_id": right.get("trace_id"),
    }


async def build_span_tree_batch_for_task(
    session: AsyncSession, task_id: str
) -> Dict[str, Dict[str, Any]]:
    """GET /tasks/{task_id}/span-tree-batch 响应体。

    单次 DB 查询获取任务下所有 run 的 span-tree 轻量数据，
    替代前端逐 run 请求。返回扁平节点列表（不含 input/output 大文本）。
    """
    from services.process_consistency_service import _load_runs_for_task

    runs = await _load_runs_for_task(session, task_id)
    if not runs:
        return {}

    run_ids = [r.id for r in runs]
    # 单次批量查询所有 run 的 span 节点
    rows = (
        await session.execute(
            select(RunSpanNode)
            .where(RunSpanNode.agent_run_id.in_(run_ids))
            .order_by(RunSpanNode.agent_run_id, RunSpanNode.step_index, RunSpanNode.sibling_order)
        )
    ).scalars().all()

    # 按 agent_run_id 分组，轻量序列化
    by_run: Dict[str, List[RunSpanNode]] = defaultdict(list)
    for s in rows:
        by_run[s.agent_run_id].append(s)

    result: Dict[str, Dict[str, Any]] = {}
    for run_id in run_ids:
        nodes = by_run.get(run_id, [])
        result[run_id] = {
            "nodes": [_node_to_dict_lite(s) for s in nodes],
            "node_count": len(nodes),
        }
    return result
