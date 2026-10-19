"""P1：多轮稳定性 / structure_summary / enforce_order 单测。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from database import ProcessCheckpointTemplate, RunSpanNode
from services.checkpoint_batch_service import evaluate_template_on_nodes
from services.multi_run_aggregate_service import (
    aggregate_scenario_cell,
    aggregate_steps_across_runs,
)
from services.run_step_diff_service import build_structure_summary


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


def test_build_structure_summary_mixed_counts():
    seqs = [
        ("r0", 0, [{"step_key": f"s{i}"} for i in range(17)]),
        ("r1", 1, [{"step_key": f"s{i}"} for i in range(20)]),
        ("r2", 2, [{"step_key": f"s{i}"} for i in range(18)] + [{"step_key": "extra"}]),
    ]
    summary = build_structure_summary(seqs)
    assert summary["distinct_step_keys"] == 21
    assert len(summary["step_counts_by_run"]) == 3
    assert summary["step_counts_by_run"][0]["count"] == 17
    assert summary["step_counts_by_run"][1]["count"] == 20
    assert "17 / 20 / 19" in summary["headline"]
    assert any(x["step_key"] == "extra" for x in summary["sometimes_missing"])


def test_aggregate_steps_across_runs():
    scored = [
        {
            "agent_run_id": "a",
            "steps": [
                {"step_key": "S1", "name": "a", "required": True, "score": 1.0, "passed": True},
                {"step_key": "S2", "name": "b", "required": True, "score": 0.5, "passed": False},
            ],
        },
        {
            "agent_run_id": "b",
            "steps": [
                {"step_key": "S1", "name": "a", "required": True, "score": 0.8, "passed": True},
                {"step_key": "S2", "name": "b", "required": True, "score": 0.9, "passed": True},
            ],
        },
    ]
    out = aggregate_steps_across_runs(scored)
    s1 = next(s for s in out if s["step_key"] == "S1")
    s2 = next(s for s in out if s["step_key"] == "S2")
    assert s1["round_stats"]["pass_rate"] == "2/2"
    assert s2["round_stats"]["pass_rate"] == "1/2"
    assert s2["round_stats"]["mean_score"] == 0.7


def test_enforce_order_false_ignores_order():
    tpl = ProcessCheckpointTemplate(
        id="cp1",
        logical_agent_id="agent1",
        name="stepA",
        match_rules='{"mode":"any","rules":[{"type":"observation_name","op":"contains","value":"x"}]}',
        order=1,
        enforce_order=False,
        required=True,
        enabled=True,
    )
    nodes = [
        _node(id="o2", observation_id="o2", name="x-step", step_index=1),
        _node(id="o1", observation_id="o1", name="y", step_index=0),
    ]
    result = evaluate_template_on_nodes(tpl, nodes)
    assert result["hit"] is True
    assert result["passed"] is True
    assert result.get("order_ok") is None


def test_enforce_order_true_fails_wrong_order():
    tpl = ProcessCheckpointTemplate(
        id="cp2",
        logical_agent_id="agent1",
        name="stepA",
        match_rules='{"mode":"any","rules":[{"type":"observation_name","op":"contains","value":"x"}]}',
        order=1,
        enforce_order=True,
        required=True,
        enabled=True,
    )
    nodes = [
        _node(id="o2", observation_id="o2", name="x-step", step_index=1),
    ]
    result = evaluate_template_on_nodes(tpl, nodes)
    assert result["hit"] is True
    assert result["order_ok"] is False
    assert result["passed"] is False


def test_aggregate_scenario_cell():
    runs = [
        SimpleNamespace(id="a", run_index=0, status="success"),
        SimpleNamespace(id="b", run_index=1, status="success"),
    ]
    reps = [
        {"agent_run_id": "a", "composite_score": 0.8, "steps": []},
        {"agent_run_id": "b", "composite_score": 0.6, "steps": []},
    ]
    cell = aggregate_scenario_cell(runs, reps)
    assert cell["round_count"] == 2
    assert cell["composite_score"] == 0.7
    assert cell["stability"]["min"] == 0.6
