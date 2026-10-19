"""langfuse_project_meta 单测：多 Langfuse 实例场景下的 ID 解析安全性。"""
import pytest

from services import langfuse_project_meta
from services.langfuse_project_registry import LangfuseProjectConfig


class _FakeClient:
    """替身 LangfuseClient，捕获构造参数 + 返回预设项目列表。"""

    def __init__(self, *, host=None, public_key=None, secret_key=None, project_key=None):
        self.host = host
        self.public_key = public_key
        self.secret_key = secret_key
        self.project_key = project_key
        # 测试用：只要 key 有值就 enabled，聚焦 host 传递逻辑
        self.enabled = bool(public_key and secret_key)
        self._projects_payload = _FakeClient._next_payload

    async def list_projects(self):
        return self._projects_payload

    # 测试用：通过类变量注入下一次构造 FakeClient 时返回的 payload
    _next_payload = {"data": []}


def _cfg(
    *,
    id="test-env",
    host="http://120.25.178.197:20116",
    langfuse_project_id=None,
    public_key="pk-lf-test",
    secret_key="sk-lf-test",
):
    return LangfuseProjectConfig(
        id=id,
        name=id,
        public_key=public_key,
        secret_key=secret_key,
        host=host,
        langfuse_project_id=langfuse_project_id,
    )


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeClient._next_payload = {"data": []}
    yield


@pytest.mark.asyncio
async def test_fetch_meta_uses_project_host_not_global(monkeypatch):
    """_fetch_meta_for_config 必须用 cfg.host 创建 client，
    而非全局 LANGFUSE_HOST——多 Langfuse 实例各项目 host 不同。"""
    captured = {}

    original_init = _FakeClient.__init__

    def capturing_init(self, **kwargs):
        captured.update(kwargs)
        original_init(self, **kwargs)

    monkeypatch.setattr(langfuse_project_meta, "LangfuseClient", _FakeClient)
    _FakeClient.__init__ = capturing_init
    _FakeClient._next_payload = {
        "data": [{"id": "cmsg4yyyg000go307t7vyudbr", "name": "test-env"}]
    }

    cfg = _cfg(host="http://175.178.171.155:13001")
    pid, _, _ = await langfuse_project_meta._fetch_meta_for_config(cfg)

    _FakeClient.__init__ = original_init

    assert captured.get("host") == "http://175.178.171.155:13001", (
        "必须传 cfg.host 给 LangfuseClient，多实例场景下各项目 host 不同"
    )
    assert pid == "cmsg4yyyg000go307t7vyudbr"


@pytest.mark.asyncio
async def test_fetch_meta_single_project_returns_its_id(monkeypatch):
    """项目级 API Key 只返回 1 个项目时，返回该项目的真实 cxxx ID。"""
    monkeypatch.setattr(langfuse_project_meta, "LangfuseClient", _FakeClient)
    _FakeClient._next_payload = {
        "data": [{"id": "cmsg4aaa", "name": "测试环境"}]
    }

    cfg = _cfg(langfuse_project_id="test-env")  # 人工名，非 cxxx
    pid, name, err = await langfuse_project_meta._fetch_meta_for_config(cfg)

    assert pid == "cmsg4aaa"
    assert name == "测试环境"
    assert err is None


@pytest.mark.asyncio
async def test_fetch_meta_multi_projects_matches_by_name(monkeypatch):
    """多项目返回时，cfg.langfuse_project_id 是项目名（非 cxxx ID），
    应按 item["name"] 匹配返回对应项目的 ID。"""
    monkeypatch.setattr(langfuse_project_meta, "LangfuseClient", _FakeClient)
    _FakeClient._next_payload = {
        "data": [
            {"id": "cxxx-aaa", "name": "bq-project"},
            {"id": "cxxx-bbb", "name": "bq_Agent"},
        ]
    }

    cfg = _cfg(langfuse_project_id="bq_Agent")
    pid, name, err = await langfuse_project_meta._fetch_meta_for_config(cfg)

    assert pid == "cxxx-bbb", "应按 name 匹配到 bq_Agent 的 cxxx ID"
    assert name == "bq_Agent"
    assert err is None


@pytest.mark.asyncio
async def test_fetch_meta_multi_projects_no_guess_when_unmatched(monkeypatch):
    """多项目返回且 cfg.langfuse_project_id 既不匹配 id 也不匹配 name 时，
    不取 parsed[0] 猜测，返回 None（安全失败）。"""
    monkeypatch.setattr(langfuse_project_meta, "LangfuseClient", _FakeClient)
    _FakeClient._next_payload = {
        "data": [
            {"id": "cxxx-aaa", "name": "项目A"},
            {"id": "cxxx-bbb", "name": "项目B"},
        ]
    }

    cfg = _cfg(langfuse_project_id="不存在的项目名")
    pid, name, err = await langfuse_project_meta._fetch_meta_for_config(cfg)

    assert pid is None, "匹配不上时不得取 parsed[0] 猜测"
    assert err is not None, "应返回错误信息说明无法确定目标项目"
