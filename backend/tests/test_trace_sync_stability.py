"""Trace Sync 观测数稳定性检查单测。

覆盖 _check_observation_stability() 的核心路径：
1. 首次拉取 → 总是 defer
2. obs_count 增长 → defer 并更新 prev_count / last_change_at
3. obs_count 稳定超过 STABILITY_WINDOW_SEC → 处理
4. MAX_WAIT_SEC 超时 → 强制处理
5. last_error JSON 损坏 → 当作首次检查
6. attempts 不因 stability defer 递增（在 worker 集成层验证）
7. 成功处理后状态清空（在 worker 集成层验证）
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from services.trace_sync_worker import (
    _STABILITY_PREFIX,
    _check_observation_stability,
    _parse_stability_state,
)


def _make_job(last_error: str | None = None, attempts: int = 1) -> object:
    """构造一个轻量 TraceSyncJob mock（无需数据库）。"""
    class _Job:
        def __init__(self):
            self.id = "tsj_test"
            self.trace_id = "trace_test"
            self.last_error = last_error
            self.attempts = attempts
            self.status = "running"
            self.failure_category = None
            self.processed_at = None
    return _Job()


def _make_trace(obs_count: int) -> dict:
    """构造一个包含 N 个 observations 的 trace dict。"""
    return {
        "id": "trace_test",
        "observations": [{"id": f"obs_{i}"} for i in range(obs_count)],
    }


def _encode_state(prev_obs_count: int, first_check_at: datetime, last_change_at: datetime) -> str:
    """编码稳定性状态到 last_error 格式。"""
    return f"{_STABILITY_PREFIX}{json.dumps({
        'prev_obs_count': prev_obs_count,
        'first_check_at': first_check_at.isoformat(),
        'last_change_at': last_change_at.isoformat(),
    }, separators=(',', ':'))}"


# ── 1. 首次拉取总是 defer ──────────────────────────────────────

def test_first_fetch_always_defers():
    """首次拉取，即使 obs_count > 1 也应 defer。"""
    job = _make_job(last_error=None)
    trace = _make_trace(obs_count=50)
    now = datetime.utcnow()

    should_defer, reason, metadata = _check_observation_stability(job, trace, now)

    assert should_defer is True
    assert metadata is not None
    assert metadata["prev_obs_count"] == 50
    assert metadata["first_check_at"] == now.isoformat()
    assert metadata["last_change_at"] == now.isoformat()
    assert "首次拉取" in reason


def test_first_fetch_defers_with_single_obs():
    """首次拉取只有 1 个 observation（root span）也应 defer。"""
    job = _make_job(last_error=None)
    trace = _make_trace(obs_count=1)
    now = datetime.utcnow()

    should_defer, reason, metadata = _check_observation_stability(job, trace, now)

    assert should_defer is True
    assert metadata["prev_obs_count"] == 1


# ── 2. obs_count 增长 → defer 并更新 ───────────────────────────

def test_obs_count_growing_defers():
    """第二次拉取 obs_count 增加 → 仍在上传，defer 并更新 prev_count。"""
    first_check = datetime.utcnow() - timedelta(seconds=20)
    last_change = first_check
    job = _make_job(
        last_error=_encode_state(prev_obs_count=1, first_check_at=first_check, last_change_at=last_change)
    )
    trace = _make_trace(obs_count=30)  # 从 1 增长到 30
    now = datetime.utcnow()

    should_defer, reason, metadata = _check_observation_stability(job, trace, now)

    assert should_defer is True
    assert metadata["prev_obs_count"] == 30  # 更新为最新值
    assert metadata["first_check_at"] == first_check.isoformat()  # 保持不变
    assert metadata["last_change_at"] == now.isoformat()  # 更新为当前时间
    assert "增长" in reason


# ── 3. obs_count 稳定超过窗口 → 处理 ───────────────────────────

def test_obs_count_stable_processes():
    """obs_count 不变且稳定时间 >= STABILITY_WINDOW_SEC → 处理。"""
    from config import TRACE_SYNC_STABILITY_WINDOW_SEC

    first_check = datetime.utcnow() - timedelta(seconds=TRACE_SYNC_STABILITY_WINDOW_SEC + 10)
    last_change = first_check  # 从首次检查起 obs_count 就没变
    job = _make_job(
        last_error=_encode_state(prev_obs_count=50, first_check_at=first_check, last_change_at=last_change)
    )
    trace = _make_trace(obs_count=50)  # 与 prev 相同
    now = datetime.utcnow()

    should_defer, reason, metadata = _check_observation_stability(job, trace, now)

    assert should_defer is False
    assert metadata is None
    assert "稳定" in reason


def test_obs_count_stable_but_window_not_met_defers():
    """obs_count 不变但稳定时间 < STABILITY_WINDOW_SEC → defer。"""
    from config import TRACE_SYNC_STABILITY_WINDOW_SEC

    first_check = datetime.utcnow() - timedelta(seconds=10)
    last_change = first_check
    job = _make_job(
        last_error=_encode_state(prev_obs_count=50, first_check_at=first_check, last_change_at=last_change)
    )
    trace = _make_trace(obs_count=50)
    now = datetime.utcnow()

    should_defer, reason, metadata = _check_observation_stability(job, trace, now)

    assert should_defer is True
    assert metadata is not None
    assert metadata["prev_obs_count"] == 50
    assert "defer" in reason or "未变" in reason


# ── 4. MAX_WAIT_SEC 超时 → 强制处理 ────────────────────────────

def test_max_wait_exceeded_forces_processing():
    """超过 MAX_WAIT_SEC → 即使 obs_count 仍在增长也强制处理。"""
    from config import TRACE_SYNC_MAX_WAIT_SEC

    first_check = datetime.utcnow() - timedelta(seconds=TRACE_SYNC_MAX_WAIT_SEC + 5)
    last_change = datetime.utcnow() - timedelta(seconds=5)  # 最近还在变化
    job = _make_job(
        last_error=_encode_state(prev_obs_count=50, first_check_at=first_check, last_change_at=last_change)
    )
    trace = _make_trace(obs_count=80)  # 仍在增长
    now = datetime.utcnow()

    should_defer, reason, metadata = _check_observation_stability(job, trace, now)

    assert should_defer is False
    assert metadata is None
    assert "强制" in reason or "最大等待" in reason


def test_max_wait_exceeded_with_stable_count_processes():
    """超过 MAX_WAIT_SEC 且 obs_count 不变 → 正常处理（非强制路径）。"""
    from config import TRACE_SYNC_MAX_WAIT_SEC

    first_check = datetime.utcnow() - timedelta(seconds=TRACE_SYNC_MAX_WAIT_SEC + 10)
    last_change = first_check
    job = _make_job(
        last_error=_encode_state(prev_obs_count=50, first_check_at=first_check, last_change_at=last_change)
    )
    trace = _make_trace(obs_count=50)
    now = datetime.utcnow()

    should_defer, reason, metadata = _check_observation_stability(job, trace, now)

    assert should_defer is False
    assert metadata is None


# ── 5. JSON 损坏 → 当作首次检查 ────────────────────────────────

def test_corrupted_last_error_treated_as_first_check():
    """last_error 不是 STABILITY_CHECK 格式 → 当作首次检查。"""
    job = _make_job(last_error="some random error message")
    trace = _make_trace(obs_count=10)
    now = datetime.utcnow()

    should_defer, reason, metadata = _check_observation_stability(job, trace, now)

    assert should_defer is True
    assert metadata is not None
    assert metadata["prev_obs_count"] == 10
    assert "首次拉取" in reason


def test_malformed_json_treated_as_first_check():
    """last_error 有 STABILITY_CHECK 前缀但 JSON 损坏 → 当作首次检查。"""
    job = _make_job(last_error=f"{_STABILITY_PREFIX}{{invalid json}}")
    trace = _make_trace(obs_count=5)
    now = datetime.utcnow()

    should_defer, reason, metadata = _check_observation_stability(job, trace, now)

    assert should_defer is True
    assert metadata is not None
    assert metadata["prev_obs_count"] == 5


def test_parse_stability_state_none_last_error():
    """_parse_stability_state 对 None 返回 None。"""
    assert _parse_stability_state(None) is None


def test_parse_stability_state_non_prefix():
    """_parse_stability_state 对非 STABILITY_CHECK 前缀返回 None。"""
    assert _parse_stability_state("some error") is None


def test_parse_stability_state_valid():
    """_parse_stability_state 正确解析有效 JSON。"""
    state = _encode_state(prev_obs_count=42, first_check_at=datetime.utcnow(), last_change_at=datetime.utcnow())
    result = _parse_stability_state(state)
    assert result is not None
    assert result["prev_obs_count"] == 42


# ── 6. 边界场景 ────────────────────────────────────────────────

def test_obs_count_decreased_treated_as_stable():
    """obs_count 减少（异常情况）→ 当作不变处理（不增长即不 defer 更新）。"""
    from config import TRACE_SYNC_STABILITY_WINDOW_SEC

    first_check = datetime.utcnow() - timedelta(seconds=TRACE_SYNC_STABILITY_WINDOW_SEC + 10)
    last_change = first_check
    job = _make_job(
        last_error=_encode_state(prev_obs_count=50, first_check_at=first_check, last_change_at=last_change)
    )
    trace = _make_trace(obs_count=40)  # 少于 prev（异常）
    now = datetime.utcnow()

    should_defer, reason, metadata = _check_observation_stability(job, trace, now)

    # count 没有增长，所以进入稳定性窗口检查
    assert should_defer is False
    assert metadata is None


def test_zero_observations_first_fetch_defers():
    """trace 有 0 个 observations → 首次拉取也应 defer。"""
    job = _make_job(last_error=None)
    trace = {"id": "trace_test", "observations": []}
    now = datetime.utcnow()

    should_defer, reason, metadata = _check_observation_stability(job, trace, now)

    assert should_defer is True
    assert metadata["prev_obs_count"] == 0


def test_metadata_round_trip():
    """状态 JSON 编码 → 解码 → 再次检查的一致性。"""
    now = datetime.utcnow()
    job = _make_job(last_error=None)
    trace = _make_trace(obs_count=5)

    # 首次检查
    _, _, metadata = _check_observation_stability(job, trace, now)
    assert metadata is not None

    # 模拟写入 last_error
    job.last_error = f"{_STABILITY_PREFIX}{json.dumps(metadata, separators=(',', ':'))}"

    # 第二次检查（obs_count 不变，但窗口未满）
    should_defer, _, metadata2 = _check_observation_stability(job, trace, now + timedelta(seconds=10))
    assert should_defer is True
    assert metadata2["prev_obs_count"] == 5
    assert metadata2["first_check_at"] == metadata["first_check_at"]
