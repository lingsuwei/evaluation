"""onto-agent run_id → Langfuse trace_id 推导（与 exporter 稳定 id 一致）。"""
from __future__ import annotations

import uuid

import pytest

from services.onto_agent_trace_id import (
    resolve_trace_id_from_agent_response,
    stable_langfuse_trace_id,
)

pytestmark = pytest.mark.p3


def test_stable_trace_id_matches_onto_agent_exporter():
    expected = uuid.uuid5(
        uuid.NAMESPACE_DNS, "session-stable:run-stable"
    ).hex
    assert stable_langfuse_trace_id("session-stable", "run-stable") == expected


def test_resolve_prefers_explicit_trace_id():
    body = {
        "run_id": "run-1",
        "session_id": "sess-1",
        "trace_id": "explicit-trace",
        "final_answer": "ok",
    }
    assert resolve_trace_id_from_agent_response(body) == "explicit-trace"


def test_resolve_derives_from_run_and_session():
    body = {
        "run_id": "c8774ff6-edf1-4e66-9c78-b2c3dfdfe0f7",
        "session_id": "sync_d10af000",
        "final_answer": "answer",
        "status": "success",
    }
    tid = resolve_trace_id_from_agent_response(body)
    assert tid == stable_langfuse_trace_id("sync_d10af000", "c8774ff6-edf1-4e66-9c78-b2c3dfdfe0f7")
    assert len(tid) == 32


def test_resolve_empty_without_run_id():
    assert resolve_trace_id_from_agent_response({"final_answer": "x"}) is None
