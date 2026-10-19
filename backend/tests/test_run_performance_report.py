"""run_call_stats_service / run_step_diff_service 单测。"""
from __future__ import annotations

from datetime import datetime

import pytest

from database import RunSpanNode
from services.run_call_stats_service import (
    build_step_sequence,
    derive_step_key,
    summarize_call_counts_from_nodes,
)
from services.run_step_diff_service import (
    align_steps,
    build_diff_summary,
    build_round_stability_matrix,
)


def _node(**kwargs) -> RunSpanNode:
    defaults = dict(
        id="n1",
        agent_run_id="run1",
        sibling_order=0,
        attributes={},
        created_at=datetime.utcnow(),
    )
    defaults.update(kwargs)
    return RunSpanNode(**defaults)


def test_summarize_call_counts():
    nodes = [
        _node(id="n1", semantic_role="llm", node_type="generation", step_index=0),
        _node(
            id="n2",
            semantic_role="tool",
            node_type="tool",
            step_index=1,
            attributes={"agent.tool.name": "kb_search"},
        ),
        _node(id="n3", semantic_role="llm", node_type="generation", step_index=2),
    ]
    llm, tool, by_tool = summarize_call_counts_from_nodes(nodes)
    assert llm == 2
    assert tool == 1
    assert by_tool["kb_search"] == 1


def test_build_step_sequence_ordered():
    nodes = [
        _node(id="n2", semantic_role="tool", step_index=1, aggregate_key="tool:kb"),
        _node(id="n1", semantic_role="llm", step_index=0, aggregate_key="llm:plan"),
    ]
    seq = build_step_sequence(nodes)
    assert len(seq) == 2
    assert seq[0]["step_key"] == "llm:plan"
    assert seq[1]["kind"] == "tool"


def test_align_steps_removed_and_added():
    left = [
        {"step_key": "llm:a", "kind": "llm", "label": "a", "latency_ms": 100, "tokens": 10},
        {"step_key": "tool:kb", "kind": "tool", "label": "kb", "latency_ms": 200, "tokens": 5},
    ]
    right = [
        {"step_key": "llm:a", "kind": "llm", "label": "a", "latency_ms": 120, "tokens": 12},
    ]
    rows = align_steps(left, right)
    assert len(rows) == 2
    assert any(r["status"] == "removed" and r["step_key"] == "tool:kb" for r in rows)
    assert any(r["status"] in ("changed", "same") and r["step_key"] == "llm:a" for r in rows)


def test_align_steps_shows_all_when_identical():
    steps = [
        {"step_key": "llm:0", "kind": "llm", "label": "plan", "latency_ms": 100, "tokens": 50},
        {"step_key": "tool:kb", "kind": "tool", "label": "search", "latency_ms": 200, "tokens": 10},
        {"step_key": "llm:answer", "kind": "llm", "label": "answer", "latency_ms": 300, "tokens": 80},
    ]
    rows = align_steps(steps, list(steps))
    assert len(rows) == 3
    assert all(r["status"] == "same" for r in rows)
    assert [r["index"] for r in rows] == [1, 2, 3]


def test_align_steps_order_change():
    left = [
        {"step_key": "llm:a", "kind": "llm", "label": "a", "latency_ms": 100, "tokens": 10},
        {"step_key": "tool:b", "kind": "tool", "label": "b", "latency_ms": 200, "tokens": 5},
        {"step_key": "llm:c", "kind": "llm", "label": "c", "latency_ms": 300, "tokens": 8},
    ]
    right = [
        {"step_key": "llm:a", "kind": "llm", "label": "a", "latency_ms": 100, "tokens": 10},
        {"step_key": "llm:c", "kind": "llm", "label": "c", "latency_ms": 310, "tokens": 8},
        {"step_key": "tool:b", "kind": "tool", "label": "b", "latency_ms": 200, "tokens": 5},
    ]
    rows = align_steps(left, right)
    assert len(rows) >= 3
    assert any(r["status"] == "order" for r in rows)


def test_build_diff_summary_no_bullet_when_all_same():
    aligned = align_steps(
        [{"step_key": "llm:a", "kind": "llm", "label": "a", "latency_ms": 100, "tokens": 10}],
        [{"step_key": "llm:a", "kind": "llm", "label": "a", "latency_ms": 110, "tokens": 12}],
    )
    bullets = build_diff_summary(
        aligned,
        left_meta={"llm_call_count": 1, "tool_call_count": 0, "step_count": 1},
        right_meta={"llm_call_count": 1, "tool_call_count": 0, "step_count": 1},
    )
    assert bullets == []


def test_build_diff_summary_removed():
    aligned = [
        {
            "step_key": "tool:geo",
            "status": "removed",
            "left": {"label": "geo_mapper"},
            "right": None,
        }
    ]
    bullets = build_diff_summary(
        aligned,
        left_meta={"llm_call_count": 4, "tool_call_count": 3},
        right_meta={"llm_call_count": 4, "tool_call_count": 2},
    )
    assert any(b["type"] == "removed" for b in bullets)
    assert any(b["type"] == "changed" and "工具" in b["text"] for b in bullets)


def test_round_stability_matrix():
    seqs = [
        ("r0", 0, [{"step_key": "tool:geo", "latency_ms": 100}]),
        ("r1", 1, []),
        ("r2", 2, [{"step_key": "tool:geo", "latency_ms": 600}]),
    ]
    matrix = build_round_stability_matrix(seqs)
    assert len(matrix["columns"]) == 3
    geo = next(r for r in matrix["rows"] if r["step_key"] == "tool:geo")
    assert geo["stability"] == "unstable"
