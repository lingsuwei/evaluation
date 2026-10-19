"""
Langfuse Public API 客户端（BFF 层）
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

from config import LANGFUSE_HOST, LANGFUSE_REQUEST_TIMEOUT
from logger import log
from services.langfuse_errors import (
    LangfuseApiError,
    interpret_langfuse_http_error,
    is_transient_langfuse_failure,
)

LANGFUSE_RETRY_ATTEMPTS = 5
LANGFUSE_RETRY_DELAY_SEC = 2.0
LANGFUSE_RETRY_DELAY_429_SEC = 5.0
# 网络错误指数退避上限
LANGFUSE_RETRY_MAX_DELAY_SEC = 30.0
LANGFUSE_MAX_PAGE_LIMIT = 100

# 后台 trace sync 超时——可通过 LANGFUSE_WORKER_TIMEOUT 环境变量调整
# 默认 60s 足够下载大 trace；服务器无响应时快速失败触发重试而非等 180s
LANGFUSE_WORKER_TIMEOUT = float(os.getenv("LANGFUSE_WORKER_TIMEOUT", "60"))
# 健康检查用短超时，快速判断可达性
LANGFUSE_HEALTH_TIMEOUT = 10.0
# 连接建立超时——短于 read 超时，不可达服务器快速失败而非等 180s
LANGFUSE_CONNECT_TIMEOUT_SEC = 15.0


def _format_langfuse_error(exc: BaseException, *, timeout: float = LANGFUSE_REQUEST_TIMEOUT) -> str:
    """httpx 超时类异常 str() 常为空，需显式拼装可读信息"""
    if isinstance(exc, LangfuseApiError):
        return exc.message
    if isinstance(exc, httpx.HTTPStatusError):
        body = (exc.response.text or "")[:500]
        _, msg = interpret_langfuse_http_error(exc.response.status_code, body)
        return msg
    if isinstance(exc, httpx.ConnectTimeout):
        return f"连接 Langfuse 超时（>{timeout}s），请检查 langfuse.projects.json 中项目的 host 与服务器防火墙/安全组"
    if isinstance(exc, httpx.ConnectError):
        return f"无法连接 Langfuse: {exc}"
    if isinstance(exc, httpx.ReadTimeout):
        return f"读取 Langfuse 响应超时（>{timeout}s）"
    msg = str(exc).strip()
    return msg or f"{type(exc).__name__}: {exc!r}"


class LangfuseClient:
    """通过 Langfuse Public API 读取 trace 数据（按项目 API Key 隔离）"""

    def __init__(
        self,
        *,
        host: Optional[str] = None,
        public_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        project_key: Optional[str] = None,
    ):
        self.host = (host or LANGFUSE_HOST or "").rstrip("/")
        self.public_key = (public_key or "").strip()
        self.secret_key = (secret_key or "").strip()
        self.project_key = project_key or ""
        self.enabled = bool(self.host and self.public_key and self.secret_key)
        self.base_url = f"{self.host}/api/public" if self.host else ""
        self._auth = (self.public_key, self.secret_key)

    def _ensure_enabled(self):
        if not self.enabled:
            raise RuntimeError(
                "Langfuse 未配置或项目 API Key 无效，请检查 langfuse.projects.json 中项目的 host / public_key / secret_key"
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        self._ensure_enabled()
        req_timeout = timeout if timeout is not None else LANGFUSE_REQUEST_TIMEOUT
        encoded_path = "/".join(quote(seg, safe="") for seg in path.split("/"))
        url = f"{self.base_url}{encoded_path}"
        last_error: Optional[LangfuseApiError] = None

        for attempt in range(LANGFUSE_RETRY_ATTEMPTS):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        connect=LANGFUSE_CONNECT_TIMEOUT_SEC,
                        read=req_timeout,
                        write=req_timeout,
                        pool=10.0,
                    )
                ) as client:
                    response = await client.request(
                        method,
                        url,
                        params=params,
                        auth=self._auth,
                    )
                    response.raise_for_status()
                    return response.json()
            except httpx.HTTPStatusError as e:
                body = e.response.text or ""
                bff_status, msg = interpret_langfuse_http_error(
                    e.response.status_code, body
                )
                last_error = LangfuseApiError(
                    bff_status, msg, raw=body[:500]
                )
                log(
                    "ERROR",
                    "LANGFUSE",
                    "Public API 请求失败",
                    project=self.project_key,
                    url=url,
                    status_code=e.response.status_code,
                    bff_status=bff_status,
                    attempt=attempt + 1,
                    detail=body[:500],
                )
                if (
                    attempt < LANGFUSE_RETRY_ATTEMPTS - 1
                    and is_transient_langfuse_failure(e.response.status_code, body)
                ):
                    _delay = (
                        LANGFUSE_RETRY_DELAY_429_SEC
                        if e.response.status_code == 429
                        else LANGFUSE_RETRY_DELAY_SEC
                    )
                    await asyncio.sleep(_delay)
                    continue
                raise last_error from e
            except LangfuseApiError:
                raise
            except httpx.RequestError as e:
                msg = _format_langfuse_error(e, timeout=req_timeout)
                log(
                    "WARN",
                    "LANGFUSE",
                    "Public API 网络请求失败，尝试重试",
                    project=self.project_key,
                    url=url,
                    error=msg,
                    attempt=attempt + 1,
                )
                if attempt < LANGFUSE_RETRY_ATTEMPTS - 1:
                    # 网络错误天然瞬时（Server disconnected / ReadTimeout /
                    # ConnectTimeout 等）——指数退避后重连重试
                    _delay = min(
                        LANGFUSE_RETRY_DELAY_SEC * (2 ** attempt),
                        LANGFUSE_RETRY_MAX_DELAY_SEC,
                    )
                    await asyncio.sleep(_delay)
                    continue
                raise LangfuseApiError(503, msg) from e
            except Exception as e:
                log(
                    "ERROR",
                    "LANGFUSE",
                    "Public API 请求异常",
                    project=self.project_key,
                    url=url,
                    error=str(e),
                )
                raise

        if last_error:
            raise last_error
        raise RuntimeError("Langfuse 请求未执行")

    async def health(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "status": "disabled", "project": self.project_key}
        probe_url = f"{self.base_url}/traces"
        try:
            await self.list_traces(limit=1, timeout=LANGFUSE_HEALTH_TIMEOUT)
            return {
                "enabled": True,
                "status": "ok",
                "host": self.host,
                "project": self.project_key,
                "probe_url": probe_url,
            }
        except Exception as e:
            log(
                "ERROR",
                "LANGFUSE",
                "健康检查失败",
                project=self.project_key,
                host=self.host,
                probe_url=probe_url,
                error=_format_langfuse_error(e, timeout=LANGFUSE_HEALTH_TIMEOUT),
            )
            return {
                "enabled": True,
                "status": "error",
                "host": self.host,
                "project": self.project_key,
                "probe_url": probe_url,
                "error_type": type(e).__name__,
                "message": _format_langfuse_error(e),
            }

    async def list_projects(self) -> Dict[str, Any]:
        """GET /api/public/projects — 项目级 Key 通常只返回当前可访问的 1 个项目。"""
        return await self._request("GET", "/projects")

    async def list_traces(
        self,
        *,
        limit: int = 50,
        page: int = 1,
        session_id: Optional[str] = None,
        tags: Optional[str] = None,
        from_timestamp: Optional[str] = None,
        to_timestamp: Optional[str] = None,
        name: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "page": max(1, page)}
        if session_id:
            params["sessionId"] = session_id
        if tags:
            params["tags"] = tags
        if from_timestamp:
            params["fromTimestamp"] = from_timestamp
        if to_timestamp:
            params["toTimestamp"] = to_timestamp
        if name:
            params["name"] = name
        return await self._request("GET", "/traces", params=params, timeout=timeout)

    async def get_trace(self, trace_id: str, *, timeout: Optional[float] = None) -> Dict[str, Any]:
        # 注意：Langfuse v2 public API 在带 fields 参数（如 "core,observations,..."）
        # 时会把 trace 的 input / output 字段过滤成 None（仅在 trace level 缺省时
        # 返回完整 IO）。为保证所有调用方（trace 同步、离线评估、answer 兜底等）
        # 都能拿到 output / input，这里 **不传 fields**，让 API 返回完整 trace。
        return await self._request("GET", f"/traces/{trace_id}", timeout=timeout)

    async def list_datasets(
        self,
        *,
        limit: int = 50,
        page: int = 1,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": min(limit, LANGFUSE_MAX_PAGE_LIMIT), "page": max(1, page)}
        return await self._request("GET", "/datasets", params=params)

    async def get_dataset(self, dataset_name: str) -> Dict[str, Any]:
        return await self._request("GET", f"/datasets/{dataset_name}")

    async def get_dataset_items(
        self,
        dataset_name: str,
        *,
        limit: int = 50,
        page: int = 1,
    ) -> Dict[str, Any]:
        try:
            params: Dict[str, Any] = {"limit": min(limit, LANGFUSE_MAX_PAGE_LIMIT), "page": max(1, page)}
            return await self._request(
                "GET", f"/datasets/{dataset_name}/items", params=params
            )
        except LangfuseApiError as e:
            if e.http_status == 404:
                dataset = await self.get_dataset(dataset_name)
                items = dataset.get("items") or []
                return {"data": items[:limit], "meta": {"page": page, "limit": limit, "totalItems": len(items)}}
            raise

    async def get_dataset_runs(
        self,
        dataset_name: str,
        *,
        limit: int = 50,
        page: int = 1,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "page": max(1, page)}
        return await self._request(
            "GET", f"/datasets/{dataset_name}/runs", params=params
        )


def get_langfuse_client(project: Optional[str] = None) -> LangfuseClient:
    """按 onto-eval 项目 id 获取 Langfuse 客户端；不传则用默认项目"""
    from services.langfuse_project_registry import get_registry

    return get_registry().get_client(project)
