"""backend_client.py 单测。"""
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib import error

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _make_config(backend_url="http://test:8000", token="", **extra):
    cfg = {
        "backend_url": backend_url,
        "token": token,
        "username": "",
        "password": "",
        "token_expiry_skew_seconds": 60,
        "request_timeout": 200,
    }
    cfg.update(extra)
    return cfg


def _mock_urlopen(response_body: dict, status=200):
    """返回一个 MagicMock，模拟 urllib.urlopen 上下文管理器。"""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.read = MagicMock(return_value=json.dumps(response_body).encode("utf-8"))
    ctx.status = status
    return ctx


def test_backend_client_post_normal():
    """正常 POST 透传 + JSON 解析。"""
    from backend_client import BackendClient

    cfg = _make_config()
    client = BackendClient.from_config(cfg)

    expected = {"score": 0.85, "passed": True}
    with patch("backend_client.request.urlopen", return_value=_mock_urlopen(expected)) as mock_urlopen:
        result = client.post("/api/v1/evaluate/single", {"question": "q", "answer": "a", "metric": "faithfulness"})

    assert result == expected
    # 验证请求 URL 和方法
    called_req = mock_urlopen.call_args[0][0]
    assert called_req.full_url == "http://test:8000/api/v1/evaluate/single"
    assert called_req.get_method() == "POST"


def test_backend_client_token_header_sent():
    """config 有 token 时，请求带 X-Eval-Token header（非 Authorization）。"""
    from backend_client import BackendClient

    cfg = _make_config(token="my-secret-token")
    client = BackendClient.from_config(cfg)

    expected = {"score": 0.85, "passed": True}
    with patch("backend_client.request.urlopen", return_value=_mock_urlopen(expected)) as mock_urlopen:
        client.post("/api/v1/evaluate/single", {"question": "q"})

    called_req = mock_urlopen.call_args[0][0]
    # urllib 用 str.capitalize() 规范化 header 名，大小写不敏感比较
    headers_lower = {k.lower(): v for k, v in called_req.headers.items()}
    assert "x-eval-token" in headers_lower
    assert headers_lower["x-eval-token"] == "my-secret-token"
    # 不应再发 Authorization header
    assert "authorization" not in headers_lower


def test_backend_client_401_token_invalid_passthrough():
    """401（token 校验失败）→ 透传后端错误，不重试。"""
    from backend_client import BackendClient

    cfg = _make_config(token="wrong-token")
    client = BackendClient.from_config(cfg)

    http_err = error.HTTPError(
        url="http://test:8000/api/v1/evaluate/single",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=MagicMock(),
    )
    http_err.read = MagicMock(return_value='{"error": "invalid_token", "message": "X-Eval-Token 校验失败"}'.encode("utf-8"))

    call_count = {"urlopen": 0}

    def fake_urlopen(req, timeout=None):
        call_count["urlopen"] += 1
        raise http_err

    with patch("backend_client.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(RuntimeError, match="HTTP 401"):
            client.post("/api/v1/evaluate/single", {"question": "q"})

    # token 静态，不刷新，只调一次
    assert call_count["urlopen"] == 1


def test_backend_client_connection_error_friendly():
    """后端不可达 → 友好错误提示检查 backend_url。"""
    from backend_client import BackendClient

    cfg = _make_config(backend_url="http://nonexistent:9999")
    client = BackendClient.from_config(cfg)

    with patch("backend_client.request.urlopen", side_effect=error.URLError("Connection refused")):
        with pytest.raises(RuntimeError, match="无法连接 onto-eval 后端"):
            client.get("/api/v1/metrics")


def test_backend_client_get_normal():
    """正常 GET 透传。"""
    from backend_client import BackendClient

    cfg = _make_config()
    client = BackendClient.from_config(cfg)

    expected = {"builtin": [{"key": "faithfulness"}]}
    with patch("backend_client.request.urlopen", return_value=_mock_urlopen(expected)) as mock_urlopen:
        result = client.get("/api/v1/metrics")

    assert result == expected
    called_req = mock_urlopen.call_args[0][0]
    assert called_req.get_method() == "GET"