"""Langfuse UI 深链构造（trace / observation）。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from config import LANGFUSE_HOST, LANGFUSE_WRITE_PROJECT
from database import Agent, AgentRun
from services.langfuse_project_meta import _fetch_meta_for_config
from services.langfuse_project_registry import get_registry

_project_id_cache: Dict[str, Optional[str]] = {}


async def resolve_langfuse_project_id(project_key: Optional[str]) -> Optional[str]:
    """onto-eval 项目 key → Langfuse UI project id（cxxx）。"""
    key = (project_key or LANGFUSE_WRITE_PROJECT or "").strip() or None
    cache_key = key or "__default__"
    if cache_key in _project_id_cache:
        return _project_id_cache[cache_key]
    try:
        cfg = get_registry().get_config(key)
    except Exception:
        _project_id_cache[cache_key] = None
        return None
    # 始终调用 _fetch_meta_for_config 取 Langfuse 真实 cxxx ID。
    # 配置中的 langfuse_project_id 可能是人工填写的名称（如 "197_project"），
    # 而非 Langfuse 内部 ID（cxxx 格式），直接用于 URL 深链会打不开 trace。
    # _fetch_meta_for_config 内部会用项目 Key 调 Langfuse API 拉取真实项目列表，
    # 并按 cfg.langfuse_project_id 匹配或取唯一项目返回真实 ID。
    lf_id, _, _err = await _fetch_meta_for_config(cfg)
    _project_id_cache[cache_key] = lf_id
    return lf_id


async def resolve_run_langfuse_project_key(
    session: AsyncSession, run: AgentRun
) -> Optional[str]:
    direct = (run.langfuse_project or "").strip()
    if direct:
        return direct
    if run.agent_id:
        agent = await session.get(Agent, run.agent_id)
        if agent and (agent.langfuse_project or "").strip():
            return str(agent.langfuse_project).strip()
    return (LANGFUSE_WRITE_PROJECT or "").strip() or None


def resolve_langfuse_host(project_key: Optional[str] = None) -> Optional[str]:
    """从项目配置获取 host，回退到全局 LANGFUSE_HOST。

    项目级 host（langfuse.projects.json 中每项的 host 字段）优先于
    全局 LANGFUSE_HOST 环境变量。这确保 trace 深链使用项目实际所属的
    Langfuse 实例地址，而非 .env 中的占位值。
    """
    key = (project_key or "").strip() or None
    if key:
        try:
            cfg = get_registry().get_config(key)
            if cfg.host:
                return cfg.host.rstrip("/")
        except Exception:
            pass
    return (LANGFUSE_HOST or "").rstrip("/") or None


def build_trace_urls_sync(
    *,
    trace_id: str,
    langfuse_project_id: Optional[str],
    observation_id: Optional[str] = None,
    host: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    effective_host = (host or LANGFUSE_HOST or "").rstrip("/")
    if not effective_host or not trace_id:
        return {
            "langfuse_host": effective_host or None,
            "trace_id": trace_id or None,
            "trace_url": None,
            "observation_url": None,
            "langfuse_project_id": langfuse_project_id,
        }
    if langfuse_project_id:
        trace_url = f"{effective_host}/project/{langfuse_project_id}/traces/{trace_id}"
    else:
        trace_url = f"{effective_host}/trace/{trace_id}"
    observation_url = trace_url
    if observation_id:
        observation_url = f"{trace_url}?observation={observation_id}"
    return {
        "langfuse_host": effective_host,
        "trace_id": trace_id,
        "trace_url": trace_url,
        "observation_url": observation_url,
        "langfuse_project_id": langfuse_project_id,
    }


async def build_langfuse_links_for_run(
    session: AsyncSession,
    run: AgentRun,
    *,
    observation_id: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    trace_id = (run.trace_id or run.root_trace_id or "").strip()
    project_key = await resolve_run_langfuse_project_key(session, run)
    project_id = await resolve_langfuse_project_id(project_key)
    host = resolve_langfuse_host(project_key)
    links = build_trace_urls_sync(
        trace_id=trace_id,
        langfuse_project_id=project_id,
        observation_id=observation_id,
        host=host,
    )
    links["langfuse_project_key"] = project_key
    links["langfuse_host"] = host
    return links
