"""config.json 加载与校验。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DEFAULTS = {
    "token": "",
    "request_timeout": 200,
    "default_metric": "faithfulness",
    "default_threshold": None,
}


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """加载并校验 config.json。

    Args:
        config_path: 配置文件路径。None 时用同目录默认 config.json。

    Returns:
        校验后的配置 dict，必含 backend_url / request_timeout / token。
    """
    if config_path is None:
        config_path = Path(__file__).resolve().parent / "config.json"
    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"config.json not found: {config_path}")

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"config must be a JSON object: {config_path}")

    cfg = dict(_DEFAULTS)
    cfg.update(raw)

    backend_url = str(cfg.get("backend_url", "") or "").strip()
    if not backend_url:
        raise ValueError("config.json missing required value: backend_url")

    cfg["backend_url"] = backend_url
    cfg["request_timeout"] = int(cfg["request_timeout"])
    return cfg