"""事实摄入服务（ADR-027）：fetch_trace_from_langfuse 单测。"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services.fact_ingest_service import fetch_trace_from_langfuse


@pytest.mark.asyncio
async def test_fetch_trace_from_langfuse_returns_trace():
    mock_trace = {"id": "tr_1", "observations": []}
    with patch(
        "services.fact_ingest_service.get_langfuse_client",
        return_value=AsyncMock(get_trace=AsyncMock(return_value=mock_trace)),
    ), patch("services.fact_ingest_service.LANGFUSE_ENABLED", True):
        result = await fetch_trace_from_langfuse("tr_1")
    assert result == mock_trace


@pytest.mark.asyncio
async def test_fetch_trace_from_langfuse_returns_none_when_disabled():
    with patch("services.fact_ingest_service.LANGFUSE_ENABLED", False):
        result = await fetch_trace_from_langfuse("tr_1")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_trace_from_langfuse_returns_none_on_error():
    with patch(
        "services.fact_ingest_service.get_langfuse_client",
        return_value=AsyncMock(get_trace=AsyncMock(side_effect=Exception("fail"))),
    ), patch("services.fact_ingest_service.LANGFUSE_ENABLED", True):
        result = await fetch_trace_from_langfuse("tr_1")
    assert result is None
