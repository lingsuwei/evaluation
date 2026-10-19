"""P2 D-05：任务级物化状态计算。"""
from __future__ import annotations

from types import SimpleNamespace

from services.fact_materialization_status import (
    compute_fact_materialization_status,
    summarize_trace_sync_counts,
)


def _run(status: str):
    return SimpleNamespace(trace_sync_status=status)


def test_summarize_trace_sync_counts():
    summary = summarize_trace_sync_counts(
        [_run("synced"), _run("pending_sync"), _run(None)]
    )
    assert summary == {"synced": 1, "pending_sync": 1, "unknown": 1}


def test_materialization_complete():
    s = {"synced": 2, "skipped": 1}
    assert compute_fact_materialization_status(s, agent_run_count=3) == "complete"


def test_materialization_in_progress():
    s = {"synced": 1, "pending_sync": 2}
    assert compute_fact_materialization_status(s, agent_run_count=3) == "in_progress"


def test_materialization_failed():
    s = {"failed": 2}
    assert compute_fact_materialization_status(s, agent_run_count=2) == "failed"


def test_materialization_no_runs():
    assert compute_fact_materialization_status({}, agent_run_count=0) == "no_runs"


def test_materialization_partial():
    s = {"synced": 1, "failed": 1}
    assert compute_fact_materialization_status(s, agent_run_count=2) == "partial"


def test_materialization_in_progress_with_legacy_awaiting_push():
    """兼容历史数据：awaiting_push 仍被识别为 pending。"""
    s = {"synced": 1, "awaiting_push": 2}
    assert compute_fact_materialization_status(s, agent_run_count=3) == "in_progress"
