"""trace_sync_failure_category 分类器单测。

覆盖：
- 7 个类别的 exception 入口 + string 入口
- 5 条历史 failed job 的 last_error（"Langfuse 资源不存在"）→ trace_not_found
- 大小写不敏感、空串、None 兜底
- unknown 默认 retryable
"""
from __future__ import annotations

import pytest

from services.langfuse_errors import LangfuseApiError
from services.langfuse_project_registry import ProjectNotFoundError
from services.trace_sync_failure_category import (
    FAILURE_CATEGORY_META,
    PERMANENT_CATEGORIES,
    classify_failure,
    derive_category_from_error_str,
    is_retryable_category,
)


# ── trace_not_found ──────────────────────────────────────────

def test_trace_not_found_by_exception_404():
    exc = LangfuseApiError(404, "Langfuse 资源不存在")
    assert classify_failure(exc) == "trace_not_found"


def test_trace_not_found_by_string_资源不存在():
    # 5 条历史 job 的 last_error 正是这个串
    assert derive_category_from_error_str(
        "Langfuse 资源不存在，请确认 langfuse.projects.json 中项目的 host 与 Public API 可用。"
    ) == "trace_not_found"


def test_trace_not_found_by_string_not_found():
    assert derive_category_from_error_str("Trace not found within authorized project") == "trace_not_found"


# ── ownership_mismatch ───────────────────────────────────────

def test_ownership_mismatch_by_value_error():
    exc = ValueError(
        "trace 归属不匹配: trace.run_id=abc != run.onto_agent_run_id=xyz"
    )
    assert classify_failure(exc) == "ownership_mismatch"


def test_ownership_mismatch_by_string():
    assert derive_category_from_error_str("trace 归属不匹配: a != b") == "ownership_mismatch"


# ── agent_run_missing ────────────────────────────────────────

def test_agent_run_missing_by_string():
    assert derive_category_from_error_str("agent_run 不存在") == "agent_run_missing"


# ── config_error ─────────────────────────────────────────────

def test_config_error_by_langfuse_disabled_string():
    assert derive_category_from_error_str("Langfuse 未配置") == "config_error"


def test_config_error_by_project_not_registered_exception():
    exc = ProjectNotFoundError("unknown-proj")
    assert classify_failure(exc) == "config_error"


def test_config_error_by_project_not_registered_string():
    assert derive_category_from_error_str("Langfuse 项目未注册: some-proj") == "config_error"


# ── auth_error ───────────────────────────────────────────────

def test_auth_error_by_401_exception():
    exc = LangfuseApiError(401, "Langfuse API Key 无效或已吊销")
    assert classify_failure(exc) == "auth_error"


def test_auth_error_by_403_exception():
    exc = LangfuseApiError(403, "Langfuse 访问被拒绝")
    assert classify_failure(exc) == "auth_error"


def test_auth_error_by_string():
    assert derive_category_from_error_str("API Key 无效") == "auth_error"


# ── transient ────────────────────────────────────────────────

def test_transient_by_429_exception():
    exc = LangfuseApiError(429, "Langfuse 请求过于频繁")
    assert classify_failure(exc) == "transient"


def test_transient_by_5xx_exception():
    for status in (500, 502, 503, 504):
        exc = LangfuseApiError(status, f"HTTP {status}")
        assert classify_failure(exc) == "transient", f"status {status} should be transient"


def test_transient_by_db_unavailable_body():
    # is_transient_langfuse_failure 会命中 DB_UNAVAILABLE_MARKERS
    exc = LangfuseApiError(503, "服务暂不可用", raw="database system is not yet accepting connections")
    assert classify_failure(exc) == "transient"


def test_transient_by_timeout_string():
    assert derive_category_from_error_str("连接 Langfuse 超时（>60s）") == "transient"


def test_transient_by_rate_limit_string():
    assert derive_category_from_error_str("Langfuse 请求过于频繁，请稍后重试。") == "transient"


# ── unknown / 兜底 ───────────────────────────────────────────

def test_unknown_for_empty_string():
    assert derive_category_from_error_str("") == "unknown"


def test_unknown_for_none():
    assert classify_failure(None) == "unknown"


def test_unknown_for_unrecognized_string():
    assert derive_category_from_error_str("一些没见过的错误信息") == "unknown"


def test_unknown_for_unrecognized_exception():
    assert classify_failure(RuntimeError("weird error")) == "unknown"


# ── 大小写不敏感 ─────────────────────────────────────────────

def test_case_insensitive_trace_not_found():
    assert derive_category_from_error_str("LANGFUSE 资源不存在") == "trace_not_found"


def test_case_insensitive_config_error():
    assert derive_category_from_error_str("langfuse 未配置") == "config_error"


# ── 字符串入口（classify_failure 接受 str）────────────────

def test_classify_failure_accepts_string():
    assert classify_failure("agent_run 不存在") == "agent_run_missing"


# ── retryable 判定 ───────────────────────────────────────────

def test_permanent_categories_not_retryable():
    assert "trace_not_found" in PERMANENT_CATEGORIES
    assert "ownership_mismatch" in PERMANENT_CATEGORIES
    assert "config_error" in PERMANENT_CATEGORIES
    assert "auth_error" in PERMANENT_CATEGORIES
    assert "agent_run_missing" in PERMANENT_CATEGORIES


def test_transient_and_unknown_retryable():
    assert is_retryable_category("transient") is True
    assert is_retryable_category("unknown") is True


def test_permanent_categories_not_retryable_flag():
    assert is_retryable_category("trace_not_found") is False
    assert is_retryable_category("config_error") is False


def test_none_category_retryable():
    assert is_retryable_category(None) is True
    assert is_retryable_category("") is True


# ── 元数据完整性 ─────────────────────────────────────────────

def test_all_categories_have_meta():
    expected = {
        "trace_not_found", "ownership_mismatch", "agent_run_missing",
        "config_error", "auth_error", "transient", "incomplete_trace", "unknown",
    }
    assert set(FAILURE_CATEGORY_META.keys()) == expected


def test_all_categories_have_label_and_retryable():
    for code, meta in FAILURE_CATEGORY_META.items():
        assert "label" in meta, f"{code} missing label"
        assert "retryable" in meta, f"{code} missing retryable"
        assert isinstance(meta["retryable"], bool)
