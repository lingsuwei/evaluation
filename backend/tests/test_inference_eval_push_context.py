"""H-01 eval 推理请求注入 agent_run_id 单测。"""
from __future__ import annotations

from services.inference_service import _inject_eval_sync_context


def test_inject_eval_sync_context_merges_fields():
    payload = {"request": "q", "context": {"agent_mode": "thinking"}}
    _inject_eval_sync_context(
        payload,
        endpoint="agentic/runs/sync",
        agent_run_id="run_eval_abc",
        langfuse_project="proj",
        logical_agent_id="lid",
        agent_version="v1",
    )
    ctx = payload["context"]
    assert ctx["agent_run_id"] == "run_eval_abc"
    assert ctx["langfuse_project"] == "proj"
    assert ctx["logical_agent_id"] == "lid"
    assert ctx["agent_version"] == "v1"
    assert ctx["agent_mode"] == "thinking"


def test_inject_skips_non_sync_endpoint():
    payload = {"user_query": "q"}
    _inject_eval_sync_context(
        payload,
        endpoint="infer",
        agent_run_id="run_eval_abc",
    )
    assert "context" not in payload


def test_inject_eval_sync_context_for_agent_swarm_chat_sync():
    """agent_swarm /api/chat/sync 端点同样需要注入 eval 关联 context。"""
    payload = {"user_query": "q"}
    _inject_eval_sync_context(
        payload,
        endpoint="api/chat/sync",
        agent_run_id="run_eval_xyz",
        langfuse_project="proj2",
        logical_agent_id="lid2",
        agent_version="v2",
    )
    ctx = payload["context"]
    assert ctx["agent_run_id"] == "run_eval_xyz"
    assert ctx["langfuse_project"] == "proj2"
    assert ctx["logical_agent_id"] == "lid2"
    assert ctx["agent_version"] == "v2"


def test_build_payload_with_template_does_not_raise_on_inject():
    """payload_template 分支也必须能解析 endpoint_clean（回归 #endpoint_clean）。"""
    endpoint = "agentic/runs/sync"
    endpoint_clean = endpoint.lstrip("/")
    payload = {"request": "${question}", "context": {"agent_mode": "thinking"}}
    _inject_eval_sync_context(
        payload,
        endpoint=endpoint_clean,
        agent_run_id="run_x",
        langfuse_project="cc-consume-2.0",
    )
    assert payload["context"]["agent_run_id"] == "run_x"
