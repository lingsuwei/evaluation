"""Span Tree Builder 单测（P2 C-02 / I-01）。"""
from __future__ import annotations

from services.span_tree_builder import build_span_node_rows


def test_builds_parent_child_tree_and_step_index():
    trace = {
        "observations": [
            {
                "id": "root",
                "name": "agent",
                "type": "SPAN",
                "metadata": {"agent.step.id": "plan"},
            },
            {
                "id": "child1",
                "parentObservationId": "root",
                "name": "tool_a",
                "type": "SPAN",
                "metadata": {"agent.tool.name": "search"},
            },
            {
                "id": "child2",
                "parentObservationId": "root",
                "name": "llm",
                "type": "GENERATION",
                "metadata": {},
                "usage": {"total": 10},
                "output": "hello world",
            },
        ]
    }
    rows = build_span_node_rows(trace, "run_tree_1", allow_legacy_aggregate_key=False)
    assert len(rows) == 3

    by_obs = {r["observation_id"]: r for r in rows}
    root = by_obs["root"]
    assert root["parent_id"] is None
    assert root["depth"] == 0
    assert root["aggregate_key"] == "step:plan"

    c1 = by_obs["child1"]
    c2 = by_obs["child2"]
    assert c1["parent_id"] == root["id"]
    assert c2["parent_id"] == root["id"]
    assert c1["depth"] == 1
    assert c2["depth"] == 1
    assert c1["aggregate_key"] == "tool:search"
    assert c2["semantic_role"] == "llm"
    assert c2["summary_text"] == "hello world"

    step_indices = sorted(r["step_index"] for r in rows)
    assert step_indices == [0, 1, 2]


def test_l1_deterministic_io_fields_from_metadata():
    trace = {
        "observations": [
            {
                "id": "tool1",
                "name": "search",
                "type": "SPAN",
                "metadata": {
                    "agent.tool.name": "kb_search",
                    "agent.is_deterministic_io": True,
                    "agent.mock_response_payload": {"hits": 2},
                },
            },
        ]
    }
    rows = build_span_node_rows(trace, "run_l1", allow_legacy_aggregate_key=False)
    assert rows[0]["is_deterministic_io"] is True
    assert rows[0]["mock_response_payload"] == {"hits": 2}


def test_l1_mock_payload_json_string():
    trace = {
        "observations": [
            {
                "id": "tool1",
                "type": "SPAN",
                "metadata": {
                    "agent.is_deterministic_io": "true",
                    "agent.mock_response_payload": '{"ok": true}',
                },
            },
        ]
    }
    rows = build_span_node_rows(trace, "run_l1s", allow_legacy_aggregate_key=False)
    assert rows[0]["is_deterministic_io"] is True
    assert rows[0]["mock_response_payload"] == {"ok": True}


def test_legacy_aggregate_key_disabled():
    trace = {
        "observations": [
            {"id": "o1", "name": "x", "type": "SPAN", "metadata": {}},
        ]
    }
    rows = build_span_node_rows(trace, "run_x", allow_legacy_aggregate_key=False)
    assert rows[0]["aggregate_key"] == "legacy:unknown"


def test_idempotent_rebuild_same_structure():
    trace = {
        "observations": [
            {"id": "a", "name": "n1", "type": "SPAN", "metadata": {}},
            {"id": "b", "parentObservationId": "a", "name": "n2", "type": "SPAN", "metadata": {}},
        ]
    }
    first = build_span_node_rows(trace, "run_idem", allow_legacy_aggregate_key=True)
    second = build_span_node_rows(trace, "run_idem", allow_legacy_aggregate_key=True)
    assert len(first) == len(second) == 2
    assert [r["observation_id"] for r in first] == [r["observation_id"] for r in second]
    assert [r["depth"] for r in first] == [r["depth"] for r in second]
