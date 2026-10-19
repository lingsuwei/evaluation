"""
Trace Sync 失败分类（P2 I-05+）：把 trace_sync_jobs.last_error 归类成有限类别，
让前端告警横条能区分「trace 已删除（重试无意义）」和「瞬时故障（可重试）」。

两类入口：
- ``classify_failure(exc_or_str)``：写时用，优先看异常类型（LangfuseApiError.http_status、
  ProjectNotFoundError、ValueError），退化到字符串匹配。
- ``derive_category_from_error_str(error_str)``：读时用，给 ``failure_category`` 为 NULL
  的历史 job 做 lazy 推导。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Union

from services.langfuse_errors import LangfuseApiError, is_transient_langfuse_failure

# 失败类别 code → 元数据。retryable=False 的类别重试无意义，前端应禁用/警告。
FAILURE_CATEGORY_META: Dict[str, Dict[str, Any]] = {
    "trace_not_found": {
        "label": "Trace 已删除",
        "retryable": False,
        "color": "#721c24",
        "bg": "#fff5f5",
    },
    "ownership_mismatch": {
        "label": "Trace 归属不匹配",
        "retryable": False,
        "color": "#721c24",
        "bg": "#fff5f5",
    },
    "agent_run_missing": {
        "label": "Run 记录缺失",
        "retryable": False,
        "color": "#856404",
        "bg": "#fff3cd",
    },
    "config_error": {
        "label": "配置错误",
        "retryable": False,
        "color": "#856404",
        "bg": "#fff3cd",
    },
    "auth_error": {
        "label": "鉴权失败",
        "retryable": False,
        "color": "#721c24",
        "bg": "#fff5f5",
    },
    "transient": {
        "label": "瞬时故障",
        "retryable": True,
        "color": "#155724",
        "bg": "#d4edda",
    },
    "incomplete_trace": {
        "label": "Trace 未完整",
        "retryable": True,
        "color": "#856404",
        "bg": "#fff3cd",
    },
    "unknown": {
        "label": "未知错误",
        "retryable": True,
        "color": "#383d41",
        "bg": "#e2e3e5",
    },
}

PERMANENT_CATEGORIES = frozenset(
    k for k, v in FAILURE_CATEGORY_META.items() if not v["retryable"]
)


def is_retryable_category(category: Optional[str]) -> bool:
    """该类别的失败是否值得重试。None / unknown / transient → True。"""
    if not category:
        return True
    meta = FAILURE_CATEGORY_META.get(category)
    return bool(meta and meta["retryable"]) if meta else True


# 字符串匹配规则（按优先级，first match wins）。小写比较。
_STRING_RULES = (
    (("资源不存在", "not found", "tracecontroller", "no trace found"), "trace_not_found"),
    (("归属不匹配", "ownership"), "ownership_mismatch"),
    (("agent_run 不存在", "agent_run_missing", "run 不存在"), "agent_run_missing"),
    (("未配置", "项目未注册", "project not found"), "config_error"),
    (("api key 无效", "访问被拒绝", "api key", "401", "403"), "auth_error"),
    (
        (
            "超时",
            "timeout",
            "服务暂不可用",
            "数据库尚未就绪",
            "请求过于频繁",
            "429",
            "503",
            "502",
            "500",
            "connecterror",
            "connecttimeout",
            "readtimeout",
            "server disconnected",
        ),
        "transient",
    ),
    (("stability_check", "incomplete", "未完整"), "incomplete_trace"),
)


def _match_string(text: str) -> str:
    if not text:
        return "unknown"
    lower = text.lower()
    for markers, category in _STRING_RULES:
        for m in markers:
            if m in lower:
                return category
    return "unknown"


def derive_category_from_error_str(error_str: str) -> str:
    """读时用：从 last_error 字符串推导类别（给 failure_category 为 NULL 的历史 job 用）。"""
    return _match_string(error_str or "")


def classify_failure(exc_or_str: Union[Exception, str, None]) -> str:
    """写时用：从异常对象或字符串判定类别。

    优先看异常类型：
    - LangfuseApiError → 按 http_status（404→trace_not_found, 401/403→auth_error,
      429/5xx→transient）
    - ProjectNotFoundError → config_error
    - ValueError 含「归属不匹配」→ ownership_mismatch
    - httpx 超时/连接类 → transient
    - 其余 → 退化到字符串匹配 ``str(exc)``
    """
    if exc_or_str is None:
        return "unknown"
    if isinstance(exc_or_str, str):
        return _match_string(exc_or_str)

    exc = exc_or_str

    # LangfuseApiError：按 http_status 精确分类
    if isinstance(exc, LangfuseApiError):
        status = exc.http_status
        body = exc.raw or ""
        if status == 404:
            return "trace_not_found"
        if status in (401, 403):
            return "auth_error"
        if status == 429:
            return "transient"
        if status >= 500 or is_transient_langfuse_failure(status, body):
            return "transient"
        # 其他 HTTP 状态码：退化到字符串匹配
        return _match_string(str(exc) or exc.message or "")

    # ProjectNotFoundError：项目未注册 → 配置错误
    if exc.__class__.__name__ == "ProjectNotFoundError":
        return "config_error"

    # ValueError：可能是 runtime_fact_builder 抛的归属不匹配
    if isinstance(exc, ValueError):
        msg = str(exc)
        if "归属不匹配" in msg or "ownership" in msg.lower():
            return "ownership_mismatch"
        return _match_string(msg)

    # httpx 网络类异常 → transient
    cls_name = exc.__class__.__name__.lower()
    if any(k in cls_name for k in ("timeout", "connecterror", "readerror", "disconnected")):
        return "transient"

    # 兜底：字符串匹配
    return _match_string(str(exc) or "")
