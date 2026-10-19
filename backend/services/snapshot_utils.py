"""有界 snapshot 工具（P2 B-01 / §11.3.1，≤8KB）。"""
from __future__ import annotations

import json
from typing import Any, Optional

MAX_SNAPSHOT_BYTES = 8192


def bounded_snapshot_json(data: Any, *, max_bytes: int = MAX_SNAPSHOT_BYTES) -> Optional[Any]:
    """
    序列化为 JSON 并截断至 max_bytes 以内。
    返回 dict/list 或 None；超长时保留可解析前缀并附加 _truncated 标记。
    """
    if data is None:
        return None
    if isinstance(data, str):
        if len(data.encode("utf-8")) <= max_bytes:
            return data
        return data.encode("utf-8")[: max_bytes - 32].decode("utf-8", errors="ignore") + "…"
    try:
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raw = json.dumps({"_error": "snapshot_not_serializable"}, ensure_ascii=False)
    encoded = raw.encode("utf-8")
    if len(encoded) <= max_bytes:
        return data
    # 尽量保留结构化摘要
    if isinstance(data, dict):
        slim = {k: v for k, v in list(data.items())[:20]}
        slim["_truncated"] = True
        slim_raw = json.dumps(slim, ensure_ascii=False, separators=(",", ":"))
        if len(slim_raw.encode("utf-8")) <= max_bytes:
            return slim
    return {"_truncated": True, "preview": raw[: max_bytes // 2]}
