"""H-03 Agent 注册 sync 健康预检单测。"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from services.agent_probe_service import (
    probe_agent_inference_config,
    resolve_langfuse_project_from_system_config,
)
from services.langfuse_project_registry import LangfuseProjectConfig, LangfuseProjectRegistry


def _mock_response(status: int, payload: dict | str):
    resp = AsyncMock()
    resp.status = status
    if isinstance(payload, dict):
        resp.text = AsyncMock(return_value=json.dumps(payload))
        resp.json = AsyncMock(return_value=payload)
    else:
        resp.text = AsyncMock(return_value=payload)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _session_get_post(workspaces_status: int, workspaces_body: dict, sync_status: int, sync_body: dict, obs_status: int = 200, obs_body: dict | None = None):
    """mock session：get 调用顺序 = [workspaces, observability-config, health]; post = sync。

    新接口下 langfuse 探测依赖 workspace_id，故 workspaces 在前、observability-config 在后。
    health 第 3 次 get 抛 ClientError 以走 sync ping 分支。
    """
    inner = MagicMock()
    inner.get = MagicMock(side_effect=[
        _mock_response(workspaces_status, workspaces_body),
        _mock_response(obs_status, obs_body or {"enabled": False}),
        aiohttp.ClientError("health unavailable"),
        aiohttp.ClientError("api/health unavailable"),
    ])
    inner.post = MagicMock(return_value=_mock_response(sync_status, sync_body))

    outer = MagicMock()
    outer.__aenter__ = AsyncMock(return_value=inner)
    outer.__aexit__ = AsyncMock(return_value=None)
    return outer


@pytest.mark.asyncio
async def test_probe_missing_url():
    result = await probe_agent_inference_config({"url": ""})
    assert result["ok"] is False
    assert result["checks"][0]["name"] == "url"


@pytest.mark.asyncio
async def test_probe_workspace_not_found():
    cfg = {
        "url": "http://localhost:5055",
        "auth": {"type": "bearer", "token": "tok"},
        "workspace_id": "missing-ws",
        "endpoint": "/agentic/runs/sync",
    }
    ws_body = {
        "workspaces": [{"id": "zcproject", "name": "ZC", "is_default": True}],
        "default_workspace_id": "zcproject",
    }
    session = _session_get_post(200, ws_body, 200, {"status": "ok"})
    with patch("services.agent_probe_service.aiohttp.ClientSession", return_value=session):
        with patch("services.agent_probe_service.resolve_agent_base_url", return_value="http://localhost:5055"):
            result = await probe_agent_inference_config(cfg)

    assert result["ok"] is False
    ws_check = next(c for c in result["checks"] if c["name"] == "workspace")
    assert ws_check["ok"] is False
    assert "missing-ws" in ws_check["message"]


@pytest.mark.asyncio
async def test_probe_all_pass_with_sync_usage():
    cfg = {
        "url": "http://localhost:5055",
        "auth": {"type": "bearer", "token": "tok"},
        "workspace_id": "zcproject",
        "endpoint": "/agentic/runs/sync",
    }
    ws_body = {
        "workspaces": [{"id": "zcproject", "name": "ZC", "is_default": True}],
        "default_workspace_id": "zcproject",
    }
    sync_body = {
        "status": "completed",
        "run_id": "run_1",
        "usage": {"tokens_total": 42, "duration_ms": 100},
    }
    session = _session_get_post(200, ws_body, 200, sync_body)
    with patch("services.agent_probe_service.aiohttp.ClientSession", return_value=session):
        with patch("services.agent_probe_service.resolve_agent_base_url", return_value="http://localhost:5055"):
            result = await probe_agent_inference_config(cfg)

    assert result["ok"] is True
    assert result["workspace_id"] == "zcproject"
    sync_check = next(c for c in result["checks"] if c["name"] == "sync")
    assert sync_check["ok"] is True
    assert sync_check["has_usage"] is True


@pytest.mark.asyncio
async def test_probe_basic_auth_login_required():
    cfg = {
        "url": "http://localhost:5055",
        "auth": {"type": "basic_auth", "username": "", "password": ""},
    }
    with patch("services.agent_probe_service.resolve_agent_base_url", return_value="http://localhost:5055"):
        result = await probe_agent_inference_config(cfg)

    assert result["ok"] is False
    auth_check = next(c for c in result["checks"] if c["name"] == "auth")
    assert auth_check["ok"] is False


def test_resolve_langfuse_project_not_configured():
    resolved = resolve_langfuse_project_from_system_config({"observability": {"enabled": False}})
    assert resolved["status"] == "not_configured"
    assert resolved["langfuse_project"] is None


def test_resolve_langfuse_project_matched(monkeypatch):
    registry = LangfuseProjectRegistry(
        [
            LangfuseProjectConfig(
                id="proj-a",
                name="Project A",
                public_key="pk-test",
                secret_key="sk-test",
            )
        ]
    )
    monkeypatch.setattr("config.LANGFUSE_ENABLED", True)
    monkeypatch.setattr(
        "services.langfuse_project_registry.find_project_by_public_key",
        registry.find_by_public_key,
    )
    resolved = resolve_langfuse_project_from_system_config(
        {
            "observability": {
                "enabled": True,
                "langfuse": {"public_key": "pk-test", "has_secret_key": True},
            }
        }
    )
    assert resolved["status"] == "matched"
    assert resolved["langfuse_project"] == "proj-a"


def test_resolve_langfuse_project_matched_workspace_level(monkeypatch):
    """新接口（工作空间级 observability-config）直接返回观测配置，无 observability 外层。"""
    registry = LangfuseProjectRegistry(
        [
            LangfuseProjectConfig(
                id="proj-a",
                name="Project A",
                public_key="pk-test",
                secret_key="sk-test",
            )
        ]
    )
    monkeypatch.setattr("config.LANGFUSE_ENABLED", True)
    monkeypatch.setattr(
        "services.langfuse_project_registry.find_project_by_public_key",
        registry.find_by_public_key,
    )
    resolved = resolve_langfuse_project_from_system_config(
        {"enabled": True, "langfuse": {"public_key": "pk-test", "has_secret_key": True}}
    )
    assert resolved["status"] == "matched"
    assert resolved["langfuse_project"] == "proj-a"


def test_warn_check_severity_for_not_registered():
    checks: list = []
    suggestions: list = []
    from services.agent_probe_service import _append_langfuse_probe_check

    _append_langfuse_probe_check(
        checks,
        suggestions,
        {
            "status": "not_registered",
            "public_key_preview": "pk-lf-test",
        },
    )
    lf = next(c for c in checks if c["name"] == "langfuse")
    assert lf["ok"] is True
    assert lf["severity"] == "warning"
    assert suggestions


@pytest.mark.asyncio
async def test_probe_langfuse_not_configured_does_not_fail_probe():
    cfg = {
        "url": "http://localhost:5055",
        "auth": {"type": "bearer", "token": "tok"},
        "workspace_id": "zcproject",
        "endpoint": "/agentic/runs/sync",
    }
    ws_body = {
        "workspaces": [{"id": "zcproject", "name": "ZC", "is_default": True}],
        "default_workspace_id": "zcproject",
    }
    sync_body = {"status": "completed", "run_id": "run_1"}
    session = _session_get_post(200, ws_body, 200, sync_body, obs_body={"enabled": False})
    with patch("services.agent_probe_service.aiohttp.ClientSession", return_value=session):
        with patch("services.agent_probe_service.resolve_agent_base_url", return_value="http://localhost:5055"):
            result = await probe_agent_inference_config(cfg)

    assert result["ok"] is True
    lf_check = next(c for c in result["checks"] if c["name"] == "langfuse")
    assert lf_check["ok"] is True
    assert lf_check.get("severity") == "warning"
    assert result.get("has_warnings") is True
    assert result.get("langfuse_project_status") == "not_configured"
    assert result.get("langfuse_project") in (None, "")


@pytest.mark.asyncio
async def test_probe_langfuse_fallback_to_system_config(monkeypatch):
    """旧版 onto-agent：工作空间级 observability-config 404，回退 system-config 旧格式。"""
    registry = LangfuseProjectRegistry(
        [
            LangfuseProjectConfig(
                id="proj-a",
                name="Project A",
                public_key="pk-test",
                secret_key="sk-test",
            )
        ]
    )
    monkeypatch.setattr("config.LANGFUSE_ENABLED", True)
    monkeypatch.setattr(
        "services.langfuse_project_registry.find_project_by_public_key",
        registry.find_by_public_key,
    )

    cfg = {
        "url": "http://localhost:5055",
        "auth": {"type": "bearer", "token": "tok"},
        "workspace_id": "zcproject",
        "endpoint": "/agentic/runs/sync",
    }
    ws_body = {
        "workspaces": [{"id": "zcproject", "name": "ZC", "is_default": True}],
        "default_workspace_id": "zcproject",
    }
    sync_body = {"status": "completed", "run_id": "run_1"}
    sys_body = {
        "observability": {
            "enabled": True,
            "langfuse": {"public_key": "pk-test", "has_secret_key": True},
        }
    }

    inner = MagicMock()
    inner.get = MagicMock(side_effect=[
        _mock_response(200, ws_body),                                   # workspaces
        _mock_response(404, {"error": "not found"}),                    # observability-config (404)
        _mock_response(200, sys_body),                                  # system-config (回退)
        aiohttp.ClientError("health unavailable"),                      # /health
        aiohttp.ClientError("api/health unavailable"),                  # /api/health 回退
    ])
    inner.post = MagicMock(return_value=_mock_response(200, sync_body))
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=inner)
    session.__aexit__ = AsyncMock(return_value=None)

    with patch("services.agent_probe_service.aiohttp.ClientSession", return_value=session):
        with patch("services.agent_probe_service.resolve_agent_base_url", return_value="http://localhost:5055"):
            result = await probe_agent_inference_config(cfg)

    assert result["ok"] is True
    assert result.get("langfuse_project") == "proj-a"
    assert result.get("langfuse_project_status") == "matched"
