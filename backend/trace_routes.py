"""
Trace BFF 路由 — 代理 Langfuse Public API
"""
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    LANGFUSE_ENABLED,
    LANGFUSE_HOST,
    LANGFUSE_SYNC_PROJECT_NAMES,
    TRACE_FACET_SCAN_MAX_ITEMS,
    TRACE_LIST_PAGE_SIZE_DEFAULT,
    TRACE_LIST_PAGE_SIZE_MAX,
)
from database import AgentRun, Dataset, EvaluationResult, EvaluationTask, get_session


def _format_dt_utc(dt):
    """将 naive UTC datetime 格式化为带时区的 ISO 字符串，供前端正确转本地时区。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()

from services.langfuse_client import LangfuseClient
from services.langfuse_project_meta import build_public_project_views
from services.langfuse_project_registry import ProjectNotFoundError, get_registry
from services.agent_identity import (
    build_agent_identity_maps,
    trace_matches_agent_name,
)
from services.langfuse_errors import LangfuseApiError
from services.agent_run_facets import build_facets_from_agent_runs, load_question_texts_for_task
from services.agent_run_trace_list import (
    build_trace_detail_from_fact_layer,
    list_traces_from_agent_runs,
    load_observations_for_preview,
)
from services.eval_session import build_eval_session_id, parse_eval_session_id
from services.trace_facets import build_facets_from_traces, scan_traces
from services.trace_sync_service import list_trace_sync_jobs
from services.trace_sync_worker import drain_pending_trace_sync_jobs
from services.trace_sync_observability import get_trace_sync_queue_stats
from services.legacy_aggregate_report import report_legacy_aggregate_ratio

trace_router = APIRouter(prefix="/trace", tags=["Trace"])


def _raise_from_langfuse(exc: Exception) -> None:
    """将 Langfuse 客户端异常转为 FastAPI HTTPException。"""
    if isinstance(exc, LangfuseApiError):
        raise HTTPException(exc.http_status, exc.message) from exc
    raise HTTPException(502, f"Langfuse API 错误: {exc}") from exc


def _client_for_project(project: Optional[str] = None) -> LangfuseClient:
    try:
        return get_registry().get_client(project)
    except ProjectNotFoundError as e:
        raise HTTPException(
            404,
            f"未知 Langfuse 项目: {e.project_id}，请从 GET /trace/projects 查看可用项目",
        ) from e


def _meta_str(trace: Dict[str, Any], key: str) -> str:
    v = (trace.get("metadata") or {}).get(key)
    if v is None:
        return ""
    return str(v)


def _trace_matches_filters(
    trace: Dict[str, Any],
    *,
    question_id: Optional[int] = None,
    agent_name: Optional[str] = None,
    logical_agent_id: Optional[str] = None,
    agent_version: Optional[str] = None,
    question_keyword: Optional[str] = None,
    question_texts: Optional[Dict[int, str]] = None,
    name_to_lid: Optional[Dict[str, str]] = None,
) -> bool:
    meta = trace.get("metadata") or {}
    tags = trace.get("tags") or []

    if question_id is not None:
        qid_meta = meta.get("onto_eval_question_id")
        sid = trace.get("sessionId") or ""
        _, parsed_q = parse_eval_session_id(sid)
        if str(qid_meta) != str(question_id) and str(parsed_q) != str(question_id):
            return False

    if agent_name and agent_name.strip():
        if not trace_matches_agent_name(trace, agent_name, name_to_lid=name_to_lid):
            return False
    elif logical_agent_id:
        lid = _meta_str(trace, "logical_agent_id") or _meta_str(trace, "agent_id")
        tag_hit = f"logical_agent:{logical_agent_id}" in tags
        if lid != logical_agent_id and not tag_hit:
            return False

    if agent_version:
        ver = _meta_str(trace, "agent_version")
        tag_hit = f"version:{agent_version}" in tags
        if ver != agent_version and not tag_hit:
            return False

    if question_keyword and question_keyword.strip():
        kw = question_keyword.strip().lower()
        qid_raw = meta.get("onto_eval_question_id")
        try:
            qid = int(qid_raw) if qid_raw is not None else None
        except (TypeError, ValueError):
            qid = None
        text = ""
        if question_texts and qid is not None:
            text = question_texts.get(qid, "")
        if kw not in text.lower():
            inp = trace.get("input")
            if isinstance(inp, str):
                text = inp
            elif isinstance(inp, dict):
                text = str(
                    inp.get("user_query")
                    or inp.get("question")
                    or inp.get("q")
                    or inp.get("query")
                    or inp.get("input")
                    or ""
                )
            if kw not in text.lower():
                return False

    return True


def _build_tags(
    *,
    tags: Optional[str],
    task_id: Optional[str],
    logical_agent_id: Optional[str] = None,
    agent_version: Optional[str] = None,
) -> Optional[str]:
    tag_parts = []
    if tags:
        tag_parts.append(tags)
    # 不再在无 task_id 时默认追加 "onto-eval" 标签：onto-agent 上报的业务 trace
    # 不带此标签，强制过滤会导致默认页面查不到任何数据。
    # 评估包装 trace 的过滤由前端 isEvalReportedTrace / 后端
    # langfuse_trace_filter.filter_out_eval_reported_traces 统一处理。
    if task_id:
        tag_parts.append(f"task:{task_id}")
    if logical_agent_id:
        tag_parts.append(f"logical_agent:{logical_agent_id}")
    if agent_version:
        tag_parts.append(f"version:{agent_version}")
    return ",".join(tag_parts) if tag_parts else None


def _apply_trace_filters(
    data: Dict[str, Any],
    *,
    question_id: Optional[int] = None,
    agent_name: Optional[str] = None,
    logical_agent_id: Optional[str] = None,
    agent_version: Optional[str] = None,
    question_keyword: Optional[str] = None,
    question_texts: Optional[Dict[int, str]] = None,
    name_to_lid: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    traces = data.get("data", [])
    filtered = [
        t
        for t in traces
        if _trace_matches_filters(
            t,
            question_id=question_id,
            agent_name=agent_name,
            logical_agent_id=logical_agent_id,
            agent_version=agent_version,
            question_keyword=question_keyword,
            question_texts=question_texts,
            name_to_lid=name_to_lid,
        )
    ]
    return {**data, "data": filtered}


@trace_router.get("/projects")
async def list_langfuse_projects():
    """返回可切换的 Langfuse 项目列表（不含 API Key；展示名可从 Langfuse 同步）"""
    try:
        registry = get_registry()
    except Exception as e:
        raise HTTPException(
            503,
            f"Langfuse 项目配置加载失败: {e}。请检查 LANGFUSE_PROJECTS_FILE 或单 Key 配置。",
        ) from e
    if not registry.enabled:
        raise HTTPException(503, "Langfuse 未配置：需要 HOST 与至少一个项目的 API Key")
    views = await build_public_project_views(registry)
    projects = [
        {
            "id": v.id,
            "name": v.name,
            "langfuse_project_id": v.langfuse_project_id,
            "name_source": v.name_source,
            **({"sync_error": v.sync_error} if v.sync_error else {}),
        }
        for v in views
    ]
    return {
        "host": LANGFUSE_HOST,
        "default_project": registry.default_project_id,
        "sync_names": LANGFUSE_SYNC_PROJECT_NAMES,
        "projects": projects,
        "count": len(projects),
    }


@trace_router.get("/health")
async def trace_health(
    project: Optional[str] = Query(None, description="onto-eval 项目 id，见 /trace/projects"),
):
    if not LANGFUSE_ENABLED:
        return {"enabled": False, "status": "disabled"}
    client = _client_for_project(project)
    result = await client.health()
    cfg = get_registry().get_config(project)
    return {
        **result,
        "project": cfg.id,
        "project_name": cfg.name,
        "langfuse_project_id": cfg.langfuse_project_id,
    }


def _facets_project_meta(project: Optional[str]) -> Dict[str, Any]:
    """Facets 响应中的项目信息；Langfuse 未启用时仍返回可用字段。"""
    try:
        cfg = get_registry().get_config(project)
        return {
            "project": cfg.id,
            "project_name": cfg.name,
        }
    except ProjectNotFoundError:
        pid = (project or "").strip() or "default"
        return {"project": pid, "project_name": pid}


@trace_router.get("/facets")
async def get_trace_facets(
    project: Optional[str] = Query(None, description="onto-eval 项目 id"),
    task_id: Optional[str] = Query(None, description="评测任务 ID，强烈建议填写以缩小扫描范围"),
    tags: Optional[str] = Query(None),
    source: Optional[str] = Query(
        None,
        description="facets 数据源：默认 agent_runs（事实层）；langfuse 强制扫 Langfuse",
    ),
    session: AsyncSession = Depends(get_session),
):
    """
    聚合问题 / Agent / 版本（去重）。
    P2 G-02：有 task_id 时默认读 agent_runs + run_metrics；仅 source=langfuse 时扫 Langfuse。
    """
    use_langfuse = (source or "").strip().lower() == "langfuse"
    try:
        question_texts: Optional[Dict[int, str]] = None
        if task_id:
            qres = await get_task_questions(task_id, session)
            question_texts = {int(q["id"]): q["text"] for q in qres.get("questions", [])}
            if not question_texts:
                question_texts = await load_question_texts_for_task(session, task_id)

        if task_id and not use_langfuse:
            facets = await build_facets_from_agent_runs(
                session,
                task_id=task_id,
                question_texts=question_texts,
            )
            run_count = facets.get("trace_count", 0)
            return {
                **facets,
                "scan": {
                    "scanned": run_count,
                    "source": "agent_runs",
                    "truncated": False,
                },
                "task_id": task_id,
                **_facets_project_meta(project),
            }

        if not LANGFUSE_ENABLED:
            raise HTTPException(
                503,
                "Langfuse 未配置；指定 task_id 时可从 agent_runs 事实层读取 Facets",
            )

        registry = get_registry()
        cfg = registry.get_config(project)
        client = _client_for_project(project)
        combined_tags = _build_tags(tags=tags, task_id=task_id)
        traces, scan_meta = await scan_traces(
            client,
            tags=combined_tags,
        )
        if task_id:
            traces = [
                t
                for t in traces
                if (t.get("sessionId") or "").startswith(f"{task_id}:")
                or _meta_str(t, "onto_eval_task_id") == task_id
            ]
            scan_meta = {**scan_meta, "scanned": len(traces), "source": "langfuse"}

        lid_to_name, _ = await build_agent_identity_maps(session)
        facets = build_facets_from_traces(traces, question_texts, lid_to_name=lid_to_name)
        return {
            **facets,
            "scan": {**scan_meta, "source": "langfuse"},
            "task_id": task_id,
            "project": cfg.id,
            "project_name": cfg.name,
        }
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        _raise_from_langfuse(e)


@trace_router.get("/traces")
async def list_traces(
    project: Optional[str] = Query(None, description="onto-eval 项目 id"),
    limit: int = Query(TRACE_LIST_PAGE_SIZE_DEFAULT, ge=1, le=TRACE_LIST_PAGE_SIZE_MAX),
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    session_id: Optional[str] = Query(None, description="Langfuse sessionId，如 task_id:question_id"),
    tags: Optional[str] = Query(None, description="标签筛选"),
    task_id: Optional[str] = Query(None, description="评测任务 ID，映射为 tag task:{id}"),
    question_id: Optional[int] = Query(None, description="问题序号（0 起）"),
    agent_name: Optional[str] = Query(None, description="Agent 名称（业务展示名）"),
    logical_agent_id: Optional[str] = Query(None, description="逻辑 Agent ID（兼容旧参数，优先使用 agent_name）"),
    agent_version: Optional[str] = Query(None, description="Agent 版本"),
    question_keyword: Optional[str] = Query(None, description="题干关键词（需配合 task_id）"),
    from_timestamp: Optional[str] = Query(None),
    to_timestamp: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
    source: Optional[str] = Query(
        None,
        description="列表数据源：有 task_id 时默认 agent_runs；langfuse 扫 Langfuse",
    ),
    session: AsyncSession = Depends(get_session),
):
    use_langfuse = (source or "").strip().lower() == "langfuse"
    question_texts: Optional[Dict[int, str]] = None
    if task_id and question_keyword:
        qres = await get_task_questions(task_id, session)
        question_texts = {int(q["id"]): q["text"] for q in qres.get("questions", [])}

    if task_id and not use_langfuse:
        return await list_traces_from_agent_runs(
            session,
            task_id=task_id,
            question_id=question_id,
            agent_name=agent_name,
            agent_version=agent_version,
            question_keyword=question_keyword,
            question_texts=question_texts,
            limit=limit,
            page=page,
        )

    if not LANGFUSE_ENABLED:
        if task_id:
            return await list_traces_from_agent_runs(
                session,
                task_id=task_id,
                question_id=question_id,
                agent_name=agent_name,
                agent_version=agent_version,
                question_keyword=question_keyword,
                question_texts=question_texts,
                limit=limit,
                page=page,
            )
        raise HTTPException(
            503,
            "Langfuse 未配置；指定 task_id 可从 agent_runs 事实层列出 Trace",
        )
    try:
        _, name_to_lid = await build_agent_identity_maps(session)
        tag_lid = logical_agent_id
        if agent_name and agent_name.strip():
            tag_lid = name_to_lid.get(agent_name.strip()) or tag_lid
        combined_tags = _build_tags(
            tags=tags,
            task_id=task_id,
            logical_agent_id=tag_lid,
            agent_version=agent_version,
        )

        if task_id and question_id is not None and not session_id:
            session_id = build_eval_session_id(task_id, question_id)

        client = _client_for_project(project)
        data = await client.list_traces(
            limit=limit,
            page=page,
            session_id=session_id,
            tags=combined_tags,
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            name=name,
        )

        if task_id and not session_id:
            traces = data.get("data", [])
            filtered = [
                t for t in traces
                if (t.get("sessionId") or "").startswith(f"{task_id}:")
            ]
            data = {**data, "data": filtered}

        filtered = _apply_trace_filters(
            data,
            question_id=question_id,
            agent_name=agent_name,
            logical_agent_id=logical_agent_id if not (agent_name and agent_name.strip()) else None,
            agent_version=agent_version,
            question_keyword=question_keyword,
            question_texts=question_texts,
            name_to_lid=name_to_lid,
        )
        return filtered
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        _raise_from_langfuse(e)


@trace_router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    project: Optional[str] = Query(None, description="onto-eval 项目 id"),
    source: Optional[str] = Query(
        None,
        description="详情数据源：默认 Langfuse；fact 强制读事实层",
    ),
    session: AsyncSession = Depends(get_session),
):
    use_fact = (source or "").strip().lower() in ("fact", "agent_runs", "fact_layer")
    if use_fact:
        detail = await build_trace_detail_from_fact_layer(session, trace_id)
        if detail:
            return detail
        raise HTTPException(404, f"未在事实层找到 trace/run: {trace_id}")

    if not LANGFUSE_ENABLED:
        detail = await build_trace_detail_from_fact_layer(session, trace_id)
        if detail:
            return detail
        raise HTTPException(
            503,
            "Langfuse 未配置；可传 source=fact 从 agent_runs 读取已物化详情",
        )
    try:
        return await _client_for_project(project).get_trace(trace_id)
    except RuntimeError as e:
        detail = await build_trace_detail_from_fact_layer(session, trace_id)
        if detail:
            return detail
        raise HTTPException(503, str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        detail = await build_trace_detail_from_fact_layer(session, trace_id)
        if detail:
            return detail
        _raise_from_langfuse(e)


@trace_router.get("/tasks/{task_id}/questions")
async def get_task_questions(
    task_id: str,
    session: AsyncSession = Depends(get_session),
):
    """按任务 ID 返回数据集题干，供 Trace 探索左栏展示"""
    result = await session.execute(
        select(EvaluationTask).where(EvaluationTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")

    dataset_result = await session.execute(
        select(Dataset).where(Dataset.id == task.dataset_id)
    )
    dataset = dataset_result.scalar_one_or_none()
    questions = []
    if dataset and dataset.data:
        try:
            items = json.loads(dataset.data)
            for idx, item in enumerate(items):
                if isinstance(item, str):
                    text = item
                elif isinstance(item, dict):
                    text = (
                        item.get("question")
                        or item.get("q")
                        or item.get("input")
                        or item.get("prompt")
                        or ""
                    )
                else:
                    text = str(item)
                questions.append({"id": idx, "text": str(text)})
        except (json.JSONDecodeError, TypeError):
            pass

    by_id: Dict[int, str] = {int(q["id"]): q["text"] for q in questions}
    result_rows = await session.execute(
        select(EvaluationResult.question_id, EvaluationResult.question).where(
            EvaluationResult.task_id == task_id,
            EvaluationResult.question.isnot(None),
        )
    )
    for qid, qtext in result_rows.all():
        if qid is None or not str(qtext or "").strip():
            continue
        key = int(qid)
        prev = by_id.get(key, "")
        if len(str(qtext)) > len(prev):
            by_id[key] = str(qtext)

    questions = [{"id": k, "text": v} for k, v in sorted(by_id.items())]
    return {"task_id": task_id, "questions": questions}


@trace_router.get("/sessions")
async def list_session_traces(
    task_id: str = Query(..., description="评测任务 ID"),
    project: Optional[str] = Query(None, description="onto-eval 项目 id"),
    question_id: Optional[int] = Query(None, description="问题索引，不传则匹配 task 下所有 session"),
    agent_name: Optional[str] = Query(None, description="Agent 名称"),
    logical_agent_id: Optional[str] = Query(None, description="逻辑 Agent ID（兼容）"),
    agent_version: Optional[str] = Query(None, description="Agent 版本"),
    question_keyword: Optional[str] = Query(None, description="题干关键词"),
    limit: int = Query(TRACE_LIST_PAGE_SIZE_DEFAULT, ge=1, le=TRACE_LIST_PAGE_SIZE_MAX),
    page: int = Query(1, ge=1),
    source: Optional[str] = Query(
        None,
        description="列表数据源：默认 agent_runs；langfuse 扫 Langfuse",
    ),
    session: AsyncSession = Depends(get_session),
):
    """按 v2.1 session_id 规范查询：{task_id}:{question_id}"""
    use_langfuse = (source or "").strip().lower() == "langfuse"
    question_texts: Optional[Dict[int, str]] = None
    if question_keyword:
        qres = await get_task_questions(task_id, session)
        question_texts = {int(q["id"]): q["text"] for q in qres.get("questions", [])}

    if not use_langfuse:
        return await list_traces_from_agent_runs(
            session,
            task_id=task_id,
            question_id=question_id,
            agent_name=agent_name,
            agent_version=agent_version,
            question_keyword=question_keyword,
            question_texts=question_texts,
            limit=limit,
            page=page,
        )

    if not LANGFUSE_ENABLED:
        return await list_traces_from_agent_runs(
            session,
            task_id=task_id,
            question_id=question_id,
            agent_name=agent_name,
            agent_version=agent_version,
            question_keyword=question_keyword,
            question_texts=question_texts,
            limit=limit,
            page=page,
        )

    _, name_to_lid = await build_agent_identity_maps(session)
    tag_lid = logical_agent_id
    if agent_name and agent_name.strip():
        tag_lid = name_to_lid.get(agent_name.strip()) or tag_lid
    session_id = (
        build_eval_session_id(task_id, question_id) if question_id is not None else None
    )
    tags = _build_tags(
        tags=None,
        task_id=task_id,
        logical_agent_id=tag_lid,
        agent_version=agent_version,
    )
    try:
        client = _client_for_project(project)
        if session_id:
            data = await client.list_traces(
                limit=limit, page=page, session_id=session_id, tags=tags
            )
        else:
            data = await client.list_traces(
                limit=limit, page=page, tags=tags
            )
            traces = data.get("data", [])
            filtered = [
                t for t in traces
                if (t.get("sessionId") or "").startswith(f"{task_id}:")
            ]
            data = {**data, "data": filtered}

        question_texts: Optional[Dict[int, str]] = None
        if question_keyword:
            qres = await get_task_questions(task_id, session)
            question_texts = {int(q["id"]): q["text"] for q in qres.get("questions", [])}

        return _apply_trace_filters(
            data,
            question_id=question_id,
            agent_name=agent_name,
            logical_agent_id=logical_agent_id if not (agent_name and agent_name.strip()) else None,
            agent_version=agent_version,
            question_keyword=question_keyword,
            question_texts=question_texts,
            name_to_lid=name_to_lid,
        )
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        _raise_from_langfuse(e)



@trace_router.get("/sync-jobs")
async def get_trace_sync_jobs(
    task_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """查询 Trace Sync 任务队列（运维/调试）。"""
    jobs = await list_trace_sync_jobs(session, task_id=task_id, status=status, limit=limit)
    return {
        "items": [
            {
                "id": j.id,
                "agent_run_id": j.agent_run_id,
                "task_id": j.task_id,
                "trace_id": j.trace_id,
                "langfuse_project": j.langfuse_project,
                "status": j.status,
                "attempts": j.attempts,
                "last_error": j.last_error,
                "failure_category": j.failure_category,
                "created_at": j.created_at.isoformat() if j.created_at else None,
                "processed_at": j.processed_at.isoformat() if j.processed_at else None,
            }
            for j in jobs
        ],
        "count": len(jobs),
    }


@trace_router.get("/sync-jobs/stats")
async def get_trace_sync_queue_stats_endpoint(
    session: AsyncSession = Depends(get_session),
):
    """Trace Sync 队列聚合统计（P2 I-05）。"""
    stats = await get_trace_sync_queue_stats(session)
    return stats.as_dict()


@trace_router.get("/observability/legacy-aggregate")
async def get_legacy_aggregate_report(
    task_id: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """legacy:type:name aggregate_key 占比（P2 I-06）。"""
    report = await report_legacy_aggregate_ratio(session, task_id=task_id)
    return report.as_dict()


@trace_router.post("/sync-jobs/drain")
async def drain_trace_sync_jobs_endpoint(
    limit: int = Query(20, ge=1, le=100),
):
    """手动触发处理 pending SyncJob（与后台 Worker 相同逻辑）。"""
    processed = await drain_pending_trace_sync_jobs(limit=limit)
    return {"processed": processed, "limit": limit}


@trace_router.post("/sync-jobs/retry")
async def retry_failed_trace_sync_jobs_endpoint(
    task_id: str = Query(..., description="评测任务 ID"),
    session: AsyncSession = Depends(get_session),
):
    """重置指定任务下物化失败的 SyncJob，重新解析 langfuse_project 后入队重试。

    先对 failure_category=trace_not_found 的 failed jobs 重新从 onto-agent
    解析 langfuse_project（agent 侧改了 Langfuse 配置但 eval 未同步的场景），
    回写 Agent/AgentRun/Job 三表后重置为 pending；再对剩余 failed jobs
    （transient 等其他类别）走原有 enqueue 逻辑。
    """
    from services.backfill_trace_sync_service import (
        enqueue_backfill_for_task,
        re_resolve_langfuse_project_for_failed_jobs,
    )

    # 1. 先批量重解析 langfuse_project（trace_not_found 类别）
    reresolve_stats = await re_resolve_langfuse_project_for_failed_jobs(
        session, task_id
    )

    # 2. 再处理剩余 failed jobs + synced_incomplete（物化不完整）一起重试
    enqueue_stats = await enqueue_backfill_for_task(
        session,
        task_id,
        statuses=["failed", "synced_incomplete"],
        force=True,
    )
    await session.commit()
    return {
        "reresolve": reresolve_stats.as_dict(),
        "enqueue": enqueue_stats.as_dict(),
    }


@trace_router.get("/langfuse-source/preview")
async def preview_langfuse_source(
    project: Optional[str] = Query(None, description="onto-eval 项目 id"),
    source_task_id: Optional[str] = Query(None, description="来源评测任务 ID"),
    tags: Optional[str] = Query(None, description="按标签筛选"),
    session_id: Optional[str] = Query(None, description="按 session ID 筛选"),
    agent_name: Optional[str] = Query(None, description="按 Agent 名称筛选"),
    session: AsyncSession = Depends(get_session),
):
    """预览从 Langfuse 获取的历史答案映射，用于离线评估前确认。"""
    from services.langfuse_answer_source import fetch_langfuse_answer_map

    if not LANGFUSE_ENABLED:
        raise HTTPException(503, "Langfuse 未配置")

    answer_map, trace_meta = await fetch_langfuse_answer_map(
        project=project,
        source_task_id=source_task_id,
        tags=tags,
        session_id=session_id,
        agent_name=agent_name,
        session=session,
    )

    return {
        "answer_map": {str(k): v[:500] for k, v in answer_map.items()},
        "traces": trace_meta,
        "total_answers": len(answer_map),
        "total_traces": len(trace_meta),
    }


@trace_router.get("/langfuse-source/tasks")
async def list_langfuse_source_tasks(
    project: Optional[str] = Query(None, description="onto-eval 项目 id"),
    session: AsyncSession = Depends(get_session),
):
    """列出可作为 Langfuse 答案源的评测任务列表。

    候选 run 包含两类：
    1. 主：已成功同步进 fact_layer —— ``trace_sync_status='synced'`` 且
       ``trace_id`` 非空。下游按 trace_id 走 Langfuse 答案映射能直接命中。
    2. 兜底：onto-agent 端配了 Langfuse（``langfuse_project`` 非空）但
       onto-eval 端没有成功同步（常见于 agent run 失败 / 异常退出导致
       ``trace_id`` 为空、``trace_sync_status='skipped'``）。这种情况下
       下游 ``fetch_langfuse_answer_map`` 的时间窗 fallback 会按
       ``agent_runs.started_at`` 主动到 Langfuse 按时间窗拉业务 trace。

    这样「列表里有、Langfuse 里没有」对不上的现象仍能挡住（没有
    Langfuse 配置的任务不会进），但 onto-agent 端 trace 已上报的
    失败 run 也能作为答案源候选。
    """
    # 解析 project 参数为规范 ID，与 AgentRun.langfuse_project 对齐
    resolved_project: Optional[str] = None
    if project:
        try:
            from services.langfuse_project_registry import get_registry
            resolved_project = get_registry().resolve_project_id(project)
        except Exception:
            resolved_project = project

    # 子查询：统计每个 task 同步成功的 trace 数
    synced_conds = [
        AgentRun.task_id.isnot(None),
        AgentRun.trace_id.isnot(None),
        AgentRun.langfuse_project.isnot(None),
        AgentRun.trace_sync_status == "synced",
    ]
    if resolved_project:
        synced_conds.append(AgentRun.langfuse_project == resolved_project)
    synced_count_subq = (
        select(
            AgentRun.task_id.label("task_id"),
            func.count(AgentRun.id).label("synced_count"),
        )
        .where(*synced_conds)
        .group_by(AgentRun.task_id)
        .subquery()
    )

    # EXISTS：候选 run 至少满足「已 synced」或「onto-agent 端配了 Langfuse +
    # 有 started_at」其一，详见上方 docstring。
    usable_conds = [
        AgentRun.task_id == EvaluationTask.id,
        AgentRun.langfuse_project.isnot(None),
        AgentRun.started_at.isnot(None),
        or_(
            # 已成功同步
            (
                (AgentRun.trace_sync_status == "synced")
                & AgentRun.trace_id.isnot(None)
            ),
            # 兜底：onto-agent 端配了 Langfuse + 有时间戳，让时间窗
            # fallback 主动去 Langfuse 按时间窗拉业务 trace
            AgentRun.trace_id.is_(None),
        ),
    ]
    if resolved_project:
        usable_conds.append(AgentRun.langfuse_project == resolved_project)
    has_usable_trace_subq = (
        select(AgentRun.id)
        .where(*usable_conds)
        .exists()
    )

    tasks_result = await session.execute(
        select(EvaluationTask, synced_count_subq.c.synced_count)
        .outerjoin(
            synced_count_subq,
            synced_count_subq.c.task_id == EvaluationTask.id,
        )
        .where(
            EvaluationTask.agent_id.isnot(None),
            EvaluationTask.status == "completed",
            EvaluationTask.evaluation_mode == "online",
            has_usable_trace_subq,
        )
        .order_by(EvaluationTask.created_at.desc())
        .limit(50)
    )
    rows = tasks_result.all()

    task_list = []
    for t, synced_count in rows:
        task_list.append({
            "task_id": t.id,
            "dataset_id": t.dataset_id,
            "agent_id": t.agent_id,
            "created_at": _format_dt_utc(t.created_at),
            "completed_at": _format_dt_utc(t.completed_at),
            "evaluation_mode": getattr(t, "evaluation_mode", "online"),
            "synced_trace_count": int(synced_count or 0),
        })

    return {"tasks": task_list, "count": len(task_list)}
