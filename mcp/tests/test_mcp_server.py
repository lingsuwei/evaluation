"""mcp_server.py 工具调用单测。"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _make_config():
    return {
        "backend_url": "http://test:8000",
        "token": "",
        "username": "",
        "password": "",
        "request_timeout": 200,
        "default_metric": "faithfulness",
        "default_threshold": None,
    }


@pytest.mark.asyncio
async def test_call_evaluate_answer():
    """call_tool('evaluate_answer', ...) 调 backend_client 并透传结果。"""
    from mcp.types import TextContent

    backend_resp = {
        "metric": "faithfulness",
        "score": 0.85,
        "passed": True,
        "threshold": 0.8,
        "reason": "回答忠实于上下文",
        "duration_ms": 1200,
        "skipped": False,
        "eval_method": "llm",
    }

    mock_client = MagicMock()
    mock_client.post = MagicMock(return_value=backend_resp)

    with patch("mcp_server.load_config", return_value=_make_config()), \
         patch("mcp_server.BackendClient.from_config", return_value=mock_client):
        from mcp_server import call_tool

        result = await call_tool("evaluate_answer", {
            "question": "什么是物料A？",
            "answer": "物料A是零件。",
            "metric": "faithfulness",
            "context": "物料A是结构件。",
        })

    assert len(result) == 1
    assert isinstance(result[0], TextContent)
    parsed = json.loads(result[0].text)
    assert parsed["score"] == 0.85
    assert parsed["passed"] is True
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "/api/v1/evaluate/single"
    assert call_args[0][1]["metric"] == "faithfulness"


@pytest.mark.asyncio
async def test_call_list_metrics():
    """call_tool('list_metrics', ...) 调 backend_client 并透传结果。"""
    from mcp.types import TextContent

    backend_resp = {
        "builtin": [
            {"key": "faithfulness", "requires": ["question", "answer", "context"]},
        ],
    }

    mock_client = MagicMock()
    mock_client.get = MagicMock(return_value=backend_resp)

    with patch("mcp_server.load_config", return_value=_make_config()), \
         patch("mcp_server.BackendClient.from_config", return_value=mock_client):
        from mcp_server import call_tool

        result = await call_tool("list_metrics", {})

    assert len(result) == 1
    parsed = json.loads(result[0].text)
    assert parsed["builtin"][0]["key"] == "faithfulness"
    mock_client.get.assert_called_once_with("/metrics")


@pytest.mark.asyncio
async def test_call_unknown_tool_raises():
    """未知工具名抛 ValueError。"""
    with patch("mcp_server.load_config", return_value=_make_config()), \
         patch("mcp_server.BackendClient.from_config", return_value=MagicMock()):
        from mcp_server import call_tool

        with pytest.raises(ValueError, match="Unknown tool"):
            await call_tool("nonexistent", {})