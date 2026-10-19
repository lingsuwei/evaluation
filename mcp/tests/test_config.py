"""config.py 配置加载单测。"""
import json
import sys
from pathlib import Path

import pytest

# 让 mcp 包可导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _write_config(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_load_config_normal(tmp_path):
    """正常配置加载返回 dict 含 backend_url。"""
    from config import load_config

    p = _write_config(tmp_path, {
        "backend_url": "http://localhost:8000",
        "token": "",
        "request_timeout": 200,
    })
    cfg = load_config(p)
    assert cfg["backend_url"] == "http://localhost:8000"
    assert cfg["request_timeout"] == 200
    assert cfg["token"] == ""


def test_load_config_missing_backend_url_raises(tmp_path):
    """缺 backend_url 抛 ValueError。"""
    from config import load_config

    p = _write_config(tmp_path, {"token": ""})
    with pytest.raises(ValueError, match="backend_url"):
        load_config(p)


def test_load_config_defaults(tmp_path):
    """缺 request_timeout 时默认 200。"""
    from config import load_config

    p = _write_config(tmp_path, {"backend_url": "http://x:8000"})
    cfg = load_config(p)
    assert cfg["request_timeout"] == 200
    assert cfg["token"] == ""