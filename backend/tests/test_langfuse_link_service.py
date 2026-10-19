"""Langfuse 深链单测。"""
import pytest

from services import langfuse_link_service
from services.langfuse_project_registry import LangfuseProjectConfig


@pytest.fixture(autouse=True)
def _lf_host(monkeypatch):
    monkeypatch.setattr(langfuse_link_service, "LANGFUSE_HOST", "http://lf.example.com")
    # 每个测试前清空 project_id 缓存，避免用例间污染
    langfuse_link_service._project_id_cache.clear()


def test_build_trace_urls_with_project_id():
    links = langfuse_link_service.build_trace_urls_sync(
        trace_id="trace-abc",
        langfuse_project_id="proj-123",
        observation_id="obs-456",
    )
    assert links["trace_url"] == "http://lf.example.com/project/proj-123/traces/trace-abc"
    assert "observation=obs-456" in (links["observation_url"] or "")


def test_build_trace_urls_fallback_short_path():
    links = langfuse_link_service.build_trace_urls_sync(
        trace_id="trace-xyz",
        langfuse_project_id=None,
    )
    assert links["trace_url"] == "http://lf.example.com/trace/trace-xyz"


# ---- resolve_langfuse_project_id：配置中的 langfuse_project_id 不可信 ----


def _make_cfg(langfuse_project_id="197_project"):
    """构造一个带不可信 langfuse_project_id 的配置（模拟线上 197_project 场景）。"""
    return LangfuseProjectConfig(
        id="197_project",
        name="197项目",
        public_key="pk-test",
        secret_key="sk-test",
        host="http://lf.example.com",
        langfuse_project_id=langfuse_project_id,
    )


@pytest.mark.asyncio
async def test_resolve_project_id_ignores_config_short_circuit(monkeypatch):
    """配置中 langfuse_project_id='197_project'（非 cxxx 格式）时，
    resolve_langfuse_project_id 必须调用 _fetch_meta_for_config 取真实 cxxx ID，
    而非直接返回配置值。
    """
    real_cxxx_id = "cmsg4yyyg000go307t7vyudbr"

    cfg = _make_cfg(langfuse_project_id="197_project")
    fake_registry = type("R", (), {"get_config": staticmethod(lambda key=None: cfg)})()
    monkeypatch.setattr(langfuse_link_service, "get_registry", lambda: fake_registry)

    fetch_called = {"count": 0}

    async def fake_fetch(c):
        fetch_called["count"] += 1
        return real_cxxx_id, "197项目", None

    monkeypatch.setattr(langfuse_link_service, "_fetch_meta_for_config", fake_fetch)

    result = await langfuse_link_service.resolve_langfuse_project_id(None)

    assert fetch_called["count"] == 1, "必须调用 _fetch_meta_for_config 取真实 ID"
    assert result == real_cxxx_id, f"应返回真实 cxxx ID，而非配置值 '197_project'"
    assert result != "197_project", "不得直接返回配置中的 langfuse_project_id"


@pytest.mark.asyncio
async def test_resolve_project_id_caches_real_id(monkeypatch):
    """resolve_langfuse_project_id 缓存的是 _fetch_meta_for_config 返回的真实 ID，
    而非配置值。第二次调用不应再调 _fetch_meta_for_config。
    """
    real_cxxx_id = "cmsg4yyyg000go307t7vyudbr"

    cfg = _make_cfg(langfuse_project_id="197_project")
    fake_registry = type("R", (), {"get_config": staticmethod(lambda key=None: cfg)})()
    monkeypatch.setattr(langfuse_link_service, "get_registry", lambda: fake_registry)

    fetch_called = {"count": 0}

    async def fake_fetch(c):
        fetch_called["count"] += 1
        return real_cxxx_id, "197项目", None

    monkeypatch.setattr(langfuse_link_service, "_fetch_meta_for_config", fake_fetch)

    r1 = await langfuse_link_service.resolve_langfuse_project_id(None)
    r2 = await langfuse_link_service.resolve_langfuse_project_id(None)

    assert fetch_called["count"] == 1, "第二次应命中缓存，不再调 API"
    assert r1 == r2 == real_cxxx_id
