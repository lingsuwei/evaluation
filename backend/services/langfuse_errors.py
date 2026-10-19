"""
Langfuse Public API 错误解析：将 Prisma/数据库不可用等误判的 401 转为可读提示。
"""
from __future__ import annotations

import json
from typing import Tuple

# Langfuse 在 DB 未就绪时校验 API Key 可能返回 401，正文含 Prisma/Postgres 信息
_DB_UNAVAILABLE_MARKERS = (
    "database system is not yet accepting connections",
    "consistent recovery state has not been yet reached",
    "error in connector",
    "error querying the database",
    "prisma.apikey",
    "prisma.",
    "connection refused",
    "econnrefused",
    "too many connections",
    "the database system is in recovery mode",
)


class LangfuseApiError(Exception):
    """BFF 层向上抛出的 Langfuse 错误（含建议 HTTP 状态码）。"""

    def __init__(self, http_status: int, message: str, *, raw: str = ""):
        self.http_status = http_status
        self.message = message
        self.raw = raw
        super().__init__(message)

    def __str__(self) -> str:
        return self.message


def _extract_message_body(text: str) -> str:
    """从 Langfuse JSON 响应中提取 message 字段。"""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            msg = data.get("message") or data.get("error") or data.get("detail")
            if msg is not None:
                return str(msg)
    except json.JSONDecodeError:
        pass
    return text


def is_transient_langfuse_failure(status_code: int, body: str) -> bool:
    """是否适合短暂重试（数据库恢复、上游 5xx、429 限流等）。"""
    combined = f"{body} {_extract_message_body(body)}".lower()
    if any(m in combined for m in _DB_UNAVAILABLE_MARKERS):
        return True
    if status_code >= 500:
        return True
    if status_code == 429:
        return True
    return False


def interpret_langfuse_http_error(status_code: int, body: str) -> Tuple[int, str]:
    """
    将 Langfuse 原始 HTTP 错误映射为 (BFF状态码, 用户可读中文说明)。
    """
    raw = (body or "")[:800]
    combined = f"{raw} {_extract_message_body(raw)}".lower()

    if any(m in combined for m in _DB_UNAVAILABLE_MARKERS):
        return (
            503,
            "Langfuse 服务端数据库尚未就绪或正在恢复（PostgreSQL 未接受连接）。"
            "这通常不是 onto-eval 的 API Key 配置问题。"
            "请等待 1–2 分钟后点击「查询」重试，或检查 Langfuse 的 PostgreSQL 是否已启动完成。",
        )

    if status_code == 401:
        return (
            401,
            "Langfuse API Key 无效或已吊销。请检查 langfuse.projects.json（或环境变量）"
            "中当前项目的 public_key / secret_key 是否与 Langfuse 控制台一致。",
        )
    if status_code == 403:
        return (403, "Langfuse 访问被拒绝，请检查项目权限与 API Key 范围。")
    if status_code == 404:
        return (404, "Langfuse 资源不存在，请确认 langfuse.projects.json 中项目的 host 与 Public API 可用。")
    if status_code == 429:
        return (429, "Langfuse 请求过于频繁，请稍后重试。")
    if status_code >= 500:
        return (503, f"Langfuse 服务暂不可用（HTTP {status_code}），请稍后重试。")

    return (502, f"Langfuse 请求失败（HTTP {status_code}）。")
