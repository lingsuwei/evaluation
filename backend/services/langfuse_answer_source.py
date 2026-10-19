from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from config import LANGFUSE_ENABLED
from logger import log
from services.eval_session import parse_eval_session_id
from services.langfuse_client import get_langfuse_client
from services.langfuse_trace_filter import filter_out_eval_reported_traces
from services.langfuse_trace_resolver import find_traces_by_run_windows


def _extract_output_from_trace(trace: Dict[str, Any]) -> str:
    output = trace.get("output")
    if isinstance(output, str) and output.strip():
        return output.strip()
    if isinstance(output, dict):
        for key in ("final_answer", "answer", "output", "result", "content"):
            val = output.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return str(output)
    return ""


def _extract_question_id_from_trace(trace: Dict[str, Any]) -> Optional[int]:
    meta = trace.get("metadata") or {}
    qid_raw = meta.get("onto_eval_question_id")
    if qid_raw is not None:
        try:
            return int(qid_raw)
        except (TypeError, ValueError):
            pass
    sid = trace.get("sessionId") or ""
    _, parsed_q = parse_eval_session_id(sid)
    if parsed_q is not None:
        return parsed_q
    return None


async def _resolve_latest_task_id(
    *,
    project: Optional[str] = None,
    tags: Optional[str] = None,
    scan_pages: int = 3,
) -> Optional[str]:
    """
    在不指定 source_task_id 时，从该项目最新一批 trace 中提取最近一个
    sessionId 形如 "<task_id>:<question>" 的 task_id。

    实现：拉取最近 ``scan_pages`` 页（每页 50 条）→ 客户端按 createdAt 倒序
    → 找到第一个包含 ":" 的 sessionId，返回冒号前的部分。

    返回 None 表示未找到（用户可能需要手动指定）。
    """
    if not LANGFUSE_ENABLED:
        return None

    client = get_langfuse_client(project)
    collected: List[Dict[str, Any]] = []
    for page in range(1, max(1, scan_pages) + 1):
        try:
            data = await client.list_traces(limit=50, page=page, tags=tags)
        except Exception as e:
            log("WARN", "LANGFUSE", "解析最新 task_id 时拉取 traces 失败", error=str(e))
            break
        batch = data.get("data", []) or []
        if not batch:
            break
        collected.extend(batch)
        meta = data.get("meta", {}) or {}
        total_pages = meta.get("totalPages", 0)
        if total_pages and page >= total_pages:
            break

    if not collected:
        return None

    # 按 createdAt 倒序
    collected.sort(
        key=lambda t: t.get("createdAt") or t.get("timestamp") or "",
        reverse=True,
    )

    for t in collected:
        sid = (t.get("sessionId") or "").strip()
        if ":" in sid:
            return sid.split(":", 1)[0].strip()
        # 兼容：metadata.onto_eval_task_id
        meta_tid = ((t.get("metadata") or {}).get("onto_eval_task_id") or "").strip()
        if meta_tid:
            return meta_tid

    return None


async def fetch_langfuse_answer_map(
    *,
    project: Optional[str] = None,
    source_task_id: Optional[str] = None,
    trace_ids: Optional[List[str]] = None,
    tags: Optional[str] = None,
    session_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    session: Optional[AsyncSession] = None,
) -> Tuple[Dict[int, str], List[Dict[str, Any]]]:
    if not LANGFUSE_ENABLED:
        raise ValueError("Langfuse 未配置，无法从 Langfuse 获取历史答案")

    client = get_langfuse_client(project)


    traces: List[Dict[str, Any]] = []

    if trace_ids:
        for tid in trace_ids:
            try:
                detail = await client.get_trace(tid.strip())
                traces.append(detail)
            except Exception as e:
                log("WARN", "LANGFUSE", "获取 trace 详情失败", trace_id=tid, error=str(e))
    else:
        combined_tags = tags or "onto-eval"
        if source_task_id:
            combined_tags = f"{combined_tags},task:{source_task_id}"

        page = 1
        while True:
            try:
                data = await client.list_traces(
                    limit=50,
                    page=page,
                    tags=combined_tags,
                    session_id=session_id,
                )
            except Exception as e:
                log("ERROR", "LANGFUSE", "获取 trace 列表失败", error=str(e))
                raise

            batch = data.get("data", [])
            if not batch:
                break

            if source_task_id:
                batch = [
                    t for t in batch
                    if (t.get("sessionId") or "").startswith(f"{source_task_id}:")
                    or str((t.get("metadata") or {}).get("onto_eval_task_id")) == source_task_id
                ]

            traces.extend(batch)

            meta = data.get("meta", {})
            total_pages = meta.get("totalPages", 0)
            if page >= total_pages:
                break
            page += 1

    # 剔除 onto-eval 端 trace_agent_inference 上报的"评估包装 trace"，
    # 让答案映射只来自业务 agent 自己的原始 trace；规则与前端
    # LangfuseTraceExplore.isEvalReportedTrace 完全一致，集中维护在
    # services.langfuse_trace_filter。
    if traces:
        before = len(traces)
        traces = filter_out_eval_reported_traces(traces)
        dropped = before - len(traces)
        if dropped:
            log(
                "INFO",
                "LANGFUSE",
                "已剔除 onto-eval 评估包装 trace",
                source_task_id=source_task_id,
                dropped=dropped,
                kept=len(traces),
            )

    if agent_name:
        from services.agent_identity import build_agent_identity_maps, trace_matches_agent_name
        from database import async_session

        async with async_session() as session:
            _, name_to_lid = await build_agent_identity_maps(session)
        traces = [
            t for t in traces
            if trace_matches_agent_name(t, agent_name, name_to_lid=name_to_lid)
        ]

    # 时间窗 fallback：tags=task:xxx 路径在 onto-agent 端不可用时拿不到任何
    # 业务 trace。落到这一步如果还没有 trace，就按 agent_runs 的 started_at
    # 时间窗列 traces（已剔除评估包装 trace），按 run 维度把命中的业务 trace
    # 合并进 answer_map。
    fallback_runs: List[Dict[str, Any]] = []
    if not traces and source_task_id and session is not None:
        try:
            from database import AgentRun
            from sqlalchemy import select

            runs_result = await session.execute(
                select(AgentRun)
                .where(
                    AgentRun.task_id == source_task_id,
                    AgentRun.started_at.isnot(None),
                )
                .order_by(AgentRun.started_at.asc())
            )
            runs = runs_result.scalars().all()
            run_dicts = [
                {
                    "id": r.id,
                    "task_id": r.task_id,
                    "question_id": getattr(r, "question_id", None),
                    "run_index": r.run_index,
                    "started_at": r.started_at,
                    "trace_id": r.trace_id,
                    "langfuse_project": r.langfuse_project,
                }
                for r in runs
            ]
            matches = await find_traces_by_run_windows(
                client=client,
                runs=run_dicts,
                project_label=project or "",
            )
            for run_dict, trace in matches:
                traces.append(trace)
                fallback_runs.append(run_dict)
            if matches:
                log(
                    "INFO",
                    "LANGFUSE",
                    "时间窗 fallback 命中业务 trace",
                    source_task_id=source_task_id,
                    matched_runs=len(matches),
                )
        except Exception as e:
            log(
                "WARN",
                "LANGFUSE",
                "时间窗 fallback 失败",
                source_task_id=source_task_id,
                error=str(e),
            )

    answer_map: Dict[int, str] = {}
    trace_meta_list: List[Dict[str, Any]] = []

    for idx, trace in enumerate(traces):
        qid = _extract_question_id_from_trace(trace)
        # 既要兼容 None（解析失败），也要兼容空字符串（业务 trace 的
        # sessionId 不一定带 question_id 后缀）。
        if qid is None or qid == "":
            # 时间窗 fallback 拿到的 trace 没有 onto-eval 上报的 metadata
            # （不含 question_id），按对应 run 的 question_id 取。
            if idx < len(fallback_runs):
                run_dict = fallback_runs[idx]
                run_qid = run_dict.get("question_id")
                if run_qid is not None:
                    try:
                        qid = int(run_qid)
                    except (TypeError, ValueError):
                        qid = run_qid
        if qid is None or qid == "":
            qid = idx

        answer = _extract_output_from_trace(trace)
        if not answer:
            input_val = trace.get("input")
            if isinstance(input_val, str):
                answer = input_val
            elif isinstance(input_val, dict):
                answer = str(input_val)

        answer_map[qid] = answer
        trace_meta_list.append({
            "question_id": qid,
            "trace_id": trace.get("id"),
            "name": trace.get("name"),
            "answer_preview": answer[:200] if answer else "",
            "timestamp": trace.get("timestamp"),
        })

    log(
        "INFO",
        "LANGFUSE",
        "从 Langfuse 获取答案映射完成",
        trace_count=len(traces),
        answer_count=len(answer_map),
    )

    return answer_map, trace_meta_list