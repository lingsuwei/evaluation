"""
从 Langfuse Public API 拉取项目展示信息（与配置解耦，失败时回退本地 name）。

仅依赖稳定字段：data[].id、data[].name。Langfuse 升级若变更结构，只需改 parse 函数。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from config import LANGFUSE_SYNC_PROJECT_NAMES
from logger import log
from services.langfuse_client import LangfuseClient, _format_langfuse_error
from services.langfuse_errors import LangfuseApiError
from services.langfuse_project_registry import LangfuseProjectConfig, LangfuseProjectRegistry


@dataclass(frozen=True)
class ProjectPublicView:
    id: str
    name: str
    langfuse_project_id: Optional[str]
    name_source: str  # "config" | "langfuse" | "config_fallback"
    sync_error: Optional[str] = None


def parse_langfuse_projects_payload(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """解析 GET /api/public/projects 响应，隔离 API 结构变化。"""
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    out: List[Dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("id") or "").strip()
        name = str(row.get("name") or "").strip()
        if pid and name:
            out.append({"id": pid, "name": name})
    return out


async def _fetch_meta_for_config(cfg: LangfuseProjectConfig) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """用该项目 Key 调 Langfuse，返回 (langfuse_project_id, display_name, error)。

    必须传 cfg.host 给 LangfuseClient —— 多 Langfuse 实例部署时各项目 host 不同，
    若回退到全局 LANGFUSE_HOST 会连到错误实例导致 API Key 认证失败。
    """
    client = LangfuseClient(
        host=cfg.host,
        public_key=cfg.public_key,
        secret_key=cfg.secret_key,
        project_key=cfg.id,
    )
    if not client.enabled:
        return None, None, "API Key 未配置"
    try:
        payload = await client.list_projects()
        parsed = parse_langfuse_projects_payload(payload)
        if not parsed:
            return None, None, "Langfuse 未返回项目列表"
        if cfg.langfuse_project_id:
            # 先按 cxxx ID 精确匹配
            for item in parsed:
                if item["id"] == cfg.langfuse_project_id:
                    return item["id"], item["name"], None
            # 再按项目名匹配（cfg.langfuse_project_id 可能是人工填写的项目名）
            for item in parsed:
                if item["name"] == cfg.langfuse_project_id:
                    return item["id"], item["name"], None
        # 项目级 API Key 通常只返回 1 个项目，直接取
        if len(parsed) == 1:
            return parsed[0]["id"], parsed[0]["name"], None
        # 多项目且无法匹配：安全失败，不取 parsed[0] 猜测
        return None, None, f"Langfuse 返回 {len(parsed)} 个项目，无法确定目标项目，请在配置中填正确的 langfuse_project_id"
    except LangfuseApiError as e:
        return None, None, e.message
    except Exception as e:
        return None, None, _format_langfuse_error(e)


_enrich_cache: Dict[str, Tuple[float, List[ProjectPublicView]]] = {}
_CACHE_KEY = "all"


async def build_public_project_views(
    registry: LangfuseProjectRegistry,
    *,
    force_refresh: bool = False,
    ttl_seconds: float = 600,
) -> List[ProjectPublicView]:
    """合并本地配置与 Langfuse 远程名称（可选）。"""
    if not LANGFUSE_SYNC_PROJECT_NAMES:
        return [
            ProjectPublicView(
                id=p.id,
                name=p.name,
                langfuse_project_id=p.langfuse_project_id,
                name_source="config",
            )
            for p in sorted(registry.all_configs(), key=lambda x: x.name)
        ]

    now = time.monotonic()
    cached = _enrich_cache.get(_CACHE_KEY)
    if not force_refresh and cached and (now - cached[0]) < ttl_seconds:
        return cached[1]

    configs = registry.all_configs()
    if not configs:
        return []

    results = await asyncio.gather(*[_fetch_meta_for_config(c) for c in configs])
    views: List[ProjectPublicView] = []
    for cfg, (lf_id, lf_name, err) in zip(configs, results):
        langfuse_id = lf_id or cfg.langfuse_project_id
        if lf_name:
            display = lf_name
            source = "langfuse"
        else:
            display = cfg.name
            source = "config_fallback" if err else "config"
        if err:
            log(
                "WARN",
                "LANGFUSE",
                "项目名同步失败，使用配置名",
                project=cfg.id,
                error=err,
            )
        views.append(
            ProjectPublicView(
                id=cfg.id,
                name=display,
                langfuse_project_id=langfuse_id,
                name_source=source,
                sync_error=err,
            )
        )

    views.sort(key=lambda x: x.name)
    _enrich_cache[_CACHE_KEY] = (now, views)
    return views


def invalidate_project_meta_cache() -> None:
    _enrich_cache.clear()
