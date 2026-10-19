"""Langfuse 客户端网络错误映射单测。"""
from __future__ import annotations

import httpx
import pytest

from services.langfuse_client import LangfuseClient
from services.langfuse_errors import LangfuseApiError


@pytest.mark.asyncio
async def test_request_maps_connect_error_to_langfuse_api_error(monkeypatch):
    client = LangfuseClient(
        host="http://langfuse.invalid",
        public_key="pk",
        secret_key="sk",
        project_key="test-proj",
    )

    async def _boom(*_args, **_kwargs):
        raise httpx.ConnectError("[Errno -2] Name or service not known")

    monkeypatch.setattr(httpx.AsyncClient, "request", _boom)

    with pytest.raises(LangfuseApiError) as exc_info:
        await client.get_trace("trace_abc")

    assert exc_info.value.http_status == 503
    assert "无法连接 Langfuse" in exc_info.value.message
