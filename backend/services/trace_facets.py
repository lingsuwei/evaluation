"""
从 Langfuse trace 分页扫描并聚合 facets（问题 / Agent / 版本），供 Trace 探索下拉使用。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from config import TRACE_FACET_SCAN_MAX_ITEMS, TRACE_FACET_SCAN_PAGE_SIZE
from services.langfuse_client import LangfuseClient


def _meta_str(trace: Dict[str, Any], key: str) -> str:
    v = (trace.get("metadata") or {}).get(key)
    if v is None:
        return ""
    return str(v)


def _parse_session_id(session_id: Optional[str]) -> Tuple[str, str]:
    from services.eval_session import parse_eval_session_id

    return parse_eval_session_id(session_id or "")


def _trace_agent_key(
    trace: Dict[str, Any],
    lid_to_name: Optional[Dict[str, str]] = None,
) -> str:
    """界面筛选用 key：业务 Agent 名称（不暴露 logical_agent_id）。"""
    from services.agent_identity import agent_display_name_from_trace

    name = agent_display_name_from_trace(trace, lid_to_name=lid_to_name)
    if name:
        return name
    return _meta_str(trace, "agent_name")


def _trace_version(trace: Dict[str, Any]) -> str:
    ver = _meta_str(trace, "agent_version")
    if ver and ver != "unknown":
        return ver
    for tag in trace.get("tags") or []:
        if isinstance(tag, str) and tag.startswith("version:"):
            return tag[len("version:") :]
    return ""


def _extract_question_text(trace: Dict[str, Any], question_texts: Optional[Dict[int, str]]) -> str:
    task_id, qid_str = _parse_session_id(trace.get("sessionId"))
    qid_raw = _meta_str(trace, "onto_eval_question_id") or qid_str
    try:
        qid = int(qid_raw) if qid_raw else None
    except (TypeError, ValueError):
        qid = None
    if question_texts and qid is not None and question_texts.get(qid):
        return question_texts[qid]
    q_meta = _meta_str(trace, "onto_eval_question")
    if q_meta.strip():
        return q_meta.strip()
    inp = trace.get("input")
    if isinstance(inp, str) and inp.strip():
        return inp.strip()
    if isinstance(inp, dict):
        for key in ("user_query", "question", "q", "query", "input", "prompt"):
            v = inp.get(key)
            if v is not None and str(v).strip():
                return str(v).strip()
    return ""


def _question_id_from_trace(trace: Dict[str, Any]) -> str:
    parsed_task, parsed_q = _parse_session_id(trace.get("sessionId"))
    qid = _meta_str(trace, "onto_eval_question_id") or parsed_q
    return qid if qid else ""


async def scan_traces(
    client: LangfuseClient,
    *,
    tags: Optional[str],
    session_id: Optional[str] = None,
    page_size: int = TRACE_FACET_SCAN_PAGE_SIZE,
    max_items: int = TRACE_FACET_SCAN_MAX_ITEMS,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    分页拉取 trace 直至达到 max_items 或无更多页。
    返回 (traces, scan_meta)。
    """
    all_traces: List[Dict[str, Any]] = []
    page = 1
    total_items: Optional[int] = None
    total_pages: Optional[int] = None
    truncated = False

    while len(all_traces) < max_items:
        batch_limit = min(page_size, max_items - len(all_traces))
        data = await client.list_traces(
            limit=batch_limit,
            page=page,
            session_id=session_id,
            tags=tags,
        )
        batch = data.get("data") or []
        meta = data.get("meta") or {}
        if total_items is None:
            total_items = meta.get("totalItems")
            total_pages = meta.get("totalPages")
        if not batch:
            break
        all_traces.extend(batch)
        if total_pages is not None and page >= total_pages:
            break
        if len(batch) < batch_limit:
            break
        page += 1
        if len(all_traces) >= max_items:
            truncated = True
            all_traces = all_traces[:max_items]
            break

    if total_items is not None and total_items > max_items:
        truncated = True

    scan_meta = {
        "scanned": len(all_traces),
        "total_items": total_items,
        "total_pages": total_pages,
        "truncated": truncated,
        "max_scan": max_items,
    }
    return all_traces, scan_meta


def build_facets_from_traces(
    traces: List[Dict[str, Any]],
    question_texts: Optional[Dict[int, str]] = None,
    lid_to_name: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """从 trace 列表聚合去重 facets。"""
    questions: Dict[str, Dict[str, str]] = {}
    agents: Dict[str, str] = {}
    versions_by_agent: Dict[str, set] = {}

    for trace in traces:
        qid = _question_id_from_trace(trace)
        if qid:
            text = _extract_question_text(trace, question_texts)
            prev = questions.get(qid, {}).get("text", "")
            merged = text if len(text) > len(prev) else prev
            questions[qid] = {"id": qid, "text": merged}

        agent_key = _trace_agent_key(trace, lid_to_name=lid_to_name)
        if agent_key:
            agents[agent_key] = agent_key
            ver = _trace_version(trace)
            if ver:
                versions_by_agent.setdefault(agent_key, set()).add(ver)

    question_list = sorted(
        questions.values(),
        key=lambda x: (int(x["id"]) if str(x["id"]).isdigit() else 999999, x["id"]),
    )
    agent_list = sorted(
        [{"value": k, "label": v} for k, v in agents.items()],
        key=lambda x: x["label"],
    )
    versions_map = {
        k: sorted(v) for k, v in sorted(versions_by_agent.items())
    }

    return {
        "questions": question_list,
        "agents": agent_list,
        "versions_by_agent": versions_map,
        "trace_count": len(traces),
    }
