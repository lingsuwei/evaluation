"""过程点配置 API 契约测试"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database import Agent, Base, get_session
import checkpoint_routes


@pytest.fixture
async def checkpoint_env():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session_override():
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.include_router(checkpoint_routes.checkpoint_router, prefix="/api")
    app.dependency_overrides[get_session] = _session_override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield {"client": client, "session_factory": session_factory}

    await engine.dispose()


async def _seed_agent(session_factory, agent: Agent) -> None:
    async with session_factory() as session:
        session.add(agent)
        await session.commit()


@pytest.mark.asyncio
async def test_crud_and_validate_rules(checkpoint_env):
    client = checkpoint_env["client"]
    await _seed_agent(
        checkpoint_env["session_factory"],
        Agent(
            id="agent_demo",
            name="演示 Agent",
            logical_agent_id="onto-agent-demo",
            version="v1",
        ),
    )

    rules = {
        "mode": "any",
        "rules": [{"type": "observation_type", "op": "eq", "value": "GENERATION"}],
    }
    create_res = await client.post(
        "/api/checkpoints/templates",
        json={
            "agent_name": "演示 Agent",
            "name": "生成回答",
            "match_rules": rules,
            "order": 2,
        },
    )
    assert create_res.status_code == 200, create_res.text
    tid = create_res.json()["id"]
    assert create_res.json()["agent_name"] == "演示 Agent"

    list_res = await client.get(
        "/api/checkpoints/templates",
        params={"agent_name": "演示 Agent"},
    )
    assert list_res.status_code == 200
    assert len(list_res.json()) == 1

    dup_res = await client.post(
        "/api/checkpoints/templates",
        json={
            "agent_name": "演示 Agent",
            "name": "生成回答",
            "match_rules": rules,
        },
    )
    assert dup_res.status_code == 409

    validate_res = await client.post(
        "/api/checkpoints/templates/validate-rules",
        json={
            "mode": "any",
            "rules": [{"type": "observation_name", "op": "eq", "value": "x"}],
        },
    )
    assert validate_res.status_code == 200
    assert validate_res.json()["valid"] is True
    assert "warning" in validate_res.json()

    validate_res2 = await client.post(
        "/api/checkpoints/templates/validate-rules",
        json=rules,
    )
    assert validate_res2.status_code == 200
    assert validate_res2.json()["valid"] is True
    assert "warning" not in validate_res2.json()

    names_res = await client.get("/api/checkpoints/agent-names")
    assert names_res.status_code == 200
    assert "演示 Agent" in names_res.json()["agent_names"]

    delete_res = await client.put(f"/api/checkpoints/templates/{tid}", json={"enabled": False})
    assert delete_res.status_code == 200, delete_res.text
    delete_res = await client.delete(f"/api/checkpoints/templates/{tid}")
    assert delete_res.status_code == 200


@pytest.mark.asyncio
async def test_delete_blocked_when_enabled(checkpoint_env):
    client = checkpoint_env["client"]
    await _seed_agent(
        checkpoint_env["session_factory"],
        Agent(
            id="agent_del",
            name="删除测试 Agent",
            logical_agent_id="la-del",
            version="v1",
        ),
    )
    rules = {
        "mode": "any",
        "rules": [{"type": "observation_name", "op": "contains", "value": "x"}],
    }
    create_res = await client.post(
        "/api/checkpoints/templates",
        json={"agent_name": "删除测试 Agent", "name": "启用中", "match_rules": rules},
    )
    assert create_res.status_code == 200, create_res.text
    tid = create_res.json()["id"]
    assert create_res.json()["enabled"] is True

    blocked = await client.delete(f"/api/checkpoints/templates/{tid}")
    assert blocked.status_code == 409
    assert "启用" in blocked.json()["detail"]


@pytest.mark.asyncio
async def test_create_auto_assigns_order(checkpoint_env):
    client = checkpoint_env["client"]
    await _seed_agent(
        checkpoint_env["session_factory"],
        Agent(id="agent_ord", name="顺序 Agent", logical_agent_id="la-ord", version="v1"),
    )
    rules = {
        "mode": "any",
        "rules": [{"type": "observation_name", "op": "contains", "value": "a"}],
    }
    first = await client.post(
        "/api/checkpoints/templates",
        json={"agent_name": "顺序 Agent", "name": "步骤1", "match_rules": rules, "order": 2},
    )
    assert first.status_code == 200, first.text

    second = await client.post(
        "/api/checkpoints/templates",
        json={"agent_name": "顺序 Agent", "name": "步骤2", "match_rules": rules},
    )
    assert second.status_code == 200, second.text
    assert second.json()["order"] == 3


@pytest.mark.asyncio
async def test_reorder_templates(checkpoint_env):
    client = checkpoint_env["client"]
    await _seed_agent(
        checkpoint_env["session_factory"],
        Agent(id="agent_re", name="重排 Agent", logical_agent_id="la-re", version="v1"),
    )
    rules = {
        "mode": "any",
        "rules": [{"type": "observation_name", "op": "contains", "value": "x"}],
    }
    ids = []
    for name, order in [("步骤A", 1), ("步骤B", 2), ("步骤C", 3)]:
        res = await client.post(
            "/api/checkpoints/templates",
            json={"agent_name": "重排 Agent", "name": name, "match_rules": rules, "order": order},
        )
        assert res.status_code == 200, res.text
        ids.append(res.json()["id"])

    reorder_res = await client.put(
        "/api/checkpoints/templates/reorder",
        json={"agent_name": "重排 Agent", "template_ids": [ids[2], ids[0], ids[1]]},
    )
    assert reorder_res.status_code == 200, reorder_res.text

    list_res = await client.get(
        "/api/checkpoints/templates",
        params={"agent_name": "重排 Agent"},
    )
    assert list_res.status_code == 200
    by_id = {item["id"]: item for item in list_res.json()}
    assert by_id[ids[2]]["order"] == 1
    assert by_id[ids[0]]["order"] == 2
    assert by_id[ids[1]]["order"] == 3


@pytest.mark.asyncio
async def test_preview_mock_langfuse(checkpoint_env, monkeypatch):
    client = checkpoint_env["client"]
    monkeypatch.setattr(checkpoint_routes, "LANGFUSE_ENABLED", True)

    await _seed_agent(
        checkpoint_env["session_factory"],
        Agent(id="agent_x", name="Agent X", logical_agent_id="agent-x", version="v1"),
    )

    rules = {
        "mode": "any",
        "rules": [{"type": "observation_name", "op": "contains", "value": "gen"}],
    }
    create_res = await client.post(
        "/api/checkpoints/templates",
        json={
            "agent_name": "Agent X",
            "name": "步骤A",
            "match_rules": rules,
        },
    )
    tid = create_res.json()["id"]

    mock_client = AsyncMock()
    mock_client.get_trace = AsyncMock(
        return_value={
            "id": "trace-1",
            "observations": [
                {"id": "obs-1", "name": "llm_generation", "type": "GENERATION"},
            ],
        }
    )
    monkeypatch.setattr(
        checkpoint_routes,
        "_client_for_project",
        lambda project=None: mock_client,
    )

    preview_res = await client.post(
        f"/api/checkpoints/templates/{tid}/preview",
        json={"trace_id": "trace-1", "project": "default"},
    )
    assert preview_res.status_code == 200, preview_res.text
    body = preview_res.json()
    assert body["results"][0]["hit"] is True


@pytest.mark.asyncio
async def test_preview_match_draft_without_save(checkpoint_env, monkeypatch):
    client = checkpoint_env["client"]
    monkeypatch.setattr(checkpoint_routes, "LANGFUSE_ENABLED", True)

    await _seed_agent(
        checkpoint_env["session_factory"],
        Agent(id="agent_draft", name="Draft Agent", logical_agent_id="draft-agent", version="v1"),
    )

    mock_client = AsyncMock()
    mock_client.get_trace = AsyncMock(
        return_value={
            "id": "trace-draft",
            "observations": [
                {"id": "obs1", "name": "tool_search", "type": "SPAN", "metadata": {}},
            ],
        }
    )
    monkeypatch.setattr(checkpoint_routes, "_client_for_project", lambda project=None: mock_client)

    rules = {
        "mode": "any",
        "rules": [{"type": "observation_name", "op": "contains", "value": "search"}],
    }
    preview_res = await client.post(
        "/api/checkpoints/templates/preview-match",
        json={
            "trace_id": "trace-draft",
            "agent_name": "Draft Agent",
            "checkpoint_name": "手工新增",
            "match_rules": rules,
        },
    )
    assert preview_res.status_code == 200, preview_res.text
    body = preview_res.json()
    assert body["hit"] is True
    assert body["results"][0]["matched_observation_name"] == "tool_search"


@pytest.mark.asyncio
async def test_preview_prefers_fact_layer_spans(checkpoint_env, monkeypatch):
    from datetime import datetime

    from database import AgentRun, RunSpanNode

    client = checkpoint_env["client"]
    factory = checkpoint_env["session_factory"]
    monkeypatch.setattr(checkpoint_routes, "LANGFUSE_ENABLED", True)

    await _seed_agent(
        factory,
        Agent(id="agent_f", name="Agent F", logical_agent_id="agent-f", version="v1"),
    )

    async with factory() as session:
        session.add(
            AgentRun(
                id="run_cp_prev",
                origin="evaluation",
                task_id="t_prev",
                question_id=0,
                agent_id="agent_f",
                trace_id="tr_fact",
                status="success",
                started_at=datetime.utcnow(),
                agent_snapshot="{}",
            )
        )
        session.add(
            RunSpanNode(
                id="span_prev",
                agent_run_id="run_cp_prev",
                observation_id="obs_gen",
                sibling_order=0,
                step_index=0,
                node_type="generation",
                name="llm_generation",
            )
        )
        await session.commit()

    rules = {
        "mode": "any",
        "rules": [{"type": "observation_name", "op": "contains", "value": "gen"}],
    }
    create_res = await client.post(
        "/api/checkpoints/templates",
        json={"agent_name": "Agent F", "name": "步骤F", "match_rules": rules},
    )
    tid = create_res.json()["id"]

    mock_client = AsyncMock()
    mock_client.get_trace = AsyncMock(
        return_value={
            "id": "tr_fact",
            "observations": [{"id": "obs-lf", "name": "other", "type": "SPAN"}],
        }
    )
    monkeypatch.setattr(checkpoint_routes, "_client_for_project", lambda project=None: mock_client)

    preview_res = await client.post(
        f"/api/checkpoints/templates/{tid}/preview",
        json={"trace_id": "tr_fact", "project": "default"},
    )
    assert preview_res.status_code == 200, preview_res.text
    body = preview_res.json()
    assert body["observation_source"] == "fact_layer"
    assert body["results"][0]["hit"] is True
    mock_client.get_trace.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_resolves_agent_langfuse_project(checkpoint_env, monkeypatch):
    client = checkpoint_env["client"]
    monkeypatch.setattr(checkpoint_routes, "LANGFUSE_ENABLED", True)

    await _seed_agent(
        checkpoint_env["session_factory"],
        Agent(
            id="agent_lf",
            name="LF Agent",
            logical_agent_id="lf-agent",
            version="v1",
            langfuse_project="cc-consume-2.0",
        ),
    )

    rules = {
        "mode": "any",
        "rules": [{"type": "observation_type", "op": "eq", "value": "GENERATION"}],
    }
    create_res = await client.post(
        "/api/checkpoints/templates",
        json={"agent_name": "LF Agent", "name": "生成", "match_rules": rules},
    )
    tid = create_res.json()["id"]

    captured: dict = {}

    def _mock_client(project=None):
        captured["project"] = project
        mock = AsyncMock()
        mock.get_trace = AsyncMock(
            return_value={
                "id": "trace-lf",
                "observations": [{"id": "obs-1", "name": "gen", "type": "GENERATION"}],
            }
        )
        return mock

    monkeypatch.setattr(checkpoint_routes, "_client_for_project", _mock_client)

    preview_res = await client.post(
        f"/api/checkpoints/templates/{tid}/preview",
        json={"trace_id": "trace-lf", "project": None, "source": "langfuse"},
    )
    assert preview_res.status_code == 200, preview_res.text
    assert captured["project"] == "cc-consume-2.0"
    assert preview_res.json()["project"] == "cc-consume-2.0"


@pytest.mark.asyncio
async def test_reference_catalog_and_seed_from_reference(checkpoint_env):
    client = checkpoint_env["client"]
    await _seed_agent(
        checkpoint_env["session_factory"],
        Agent(
            id="agent_ref_route",
            name="M2 参考 Agent",
            logical_agent_id="la_m2_ref",
            version="v1",
        ),
    )

    catalog_res = await client.get("/api/checkpoints/reference-templates/onto-agent")
    assert catalog_res.status_code == 200, catalog_res.text
    catalog = catalog_res.json()
    assert catalog["count"] == 10
    assert len(catalog["templates"]) == 10

    seed_res = await client.post(
        "/api/checkpoints/templates/seed-from-reference",
        json={"agent_name": "M2 参考 Agent"},
    )
    assert seed_res.status_code == 200, seed_res.text
    body = seed_res.json()
    assert body["created"] == 10
    assert body["logical_agent_id"] == "la_m2_ref"
    assert len(body["created_names"]) == 10

    list_res = await client.get(
        "/api/checkpoints/templates",
        params={"agent_name": "M2 参考 Agent"},
    )
    assert list_res.status_code == 200
    assert len(list_res.json()) == 10

    seed_again = await client.post(
        "/api/checkpoints/templates/seed-from-reference",
        json={"agent_name": "M2 参考 Agent"},
    )
    assert seed_again.status_code == 200
    assert seed_again.json()["created"] == 0
    assert len(seed_again.json()["skipped_names"]) == 10


@pytest.mark.asyncio
async def test_normalize_checkpoint_weights(checkpoint_env):
    client = checkpoint_env["client"]
    await _seed_agent(
        checkpoint_env["session_factory"],
        Agent(
            id="agent_norm",
            name="权重 Agent",
            logical_agent_id="la_norm",
            version="v1",
        ),
    )

    rules = {
        "mode": "any",
        "rules": [{"type": "observation_name", "op": "contains", "value": "tool:"}],
    }
    for idx, (name, weight, enabled) in enumerate(
        [("步骤 A", 2.0, True), ("步骤 B", 2.0, True), ("步骤 C", 1.0, False)], start=1
    ):
        res = await client.post(
            "/api/checkpoints/templates",
            json={
                "agent_name": "权重 Agent",
                "name": name,
                "match_rules": rules,
                "order": idx,
                "score_weight": weight,
                "enabled": enabled,
            },
        )
        assert res.status_code == 200, res.text

    norm_res = await client.post(
        "/api/checkpoints/templates/normalize-weights",
        json={"agent_name": "权重 Agent"},
    )
    assert norm_res.status_code == 200, norm_res.text
    body = norm_res.json()
    assert body["weight_summary"]["normalized"] is True
    assert abs(body["weight_summary"]["weight_sum"] - 1.0) <= 0.01
    assert body["weight_summary"]["enabled_count"] == 2

    list_res = await client.get(
        "/api/checkpoints/templates",
        params={"agent_name": "权重 Agent"},
    )
    weights = {
        row["name"]: row["score_weight"]
        for row in list_res.json()
        if row["enabled"]
    }
    assert abs(sum(weights.values()) - 1.0) <= 0.01
    assert weights["步骤 A"] == weights["步骤 B"]


@pytest.mark.asyncio
async def test_sample_runs_and_batch_preview(checkpoint_env):
    client = checkpoint_env["client"]
    from datetime import datetime

    from database import AgentRun, RunSpanNode

    await _seed_agent(
        checkpoint_env["session_factory"],
        Agent(
            id="agent_verify",
            name="验证 Agent",
            logical_agent_id="la_verify",
            version="v1",
        ),
    )

    rules = {
        "mode": "any",
        "rules": [{"type": "observation_name", "op": "contains", "value": "tool:"}],
    }
    create_res = await client.post(
        "/api/checkpoints/templates",
        json={"agent_name": "验证 Agent", "name": "工具调用", "match_rules": rules, "order": 1},
    )
    assert create_res.status_code == 200

    async with checkpoint_env["session_factory"]() as session:
        session.add(
            AgentRun(
                id="run_verify_1",
                agent_id="agent_verify",
                logical_agent_id="la_verify",
                agent_name="验证 Agent",
                trace_id="trace_verify_1",
                status="success",
                started_at=datetime.utcnow(),
                agent_snapshot="{}",
            )
        )
        session.add(
            RunSpanNode(
                id="span_v1",
                agent_run_id="run_verify_1",
                observation_id="obs_tool_1",
                name="tool:ontology_context",
                node_type="TOOL",
                step_index=0,
                sibling_order=0,
                attributes={"tool_name": "ontology_context"},
            )
        )
        await session.commit()

    runs_res = await client.get(
        "/api/checkpoints/sample-runs",
        params={"agent_name": "验证 Agent"},
    )
    assert runs_res.status_code == 200, runs_res.text
    assert runs_res.json()["count"] >= 1

    preview_res = await client.post(
        "/api/checkpoints/templates/batch-preview",
        json={"agent_name": "验证 Agent", "trace_id": "trace_verify_1"},
    )
    assert preview_res.status_code == 200, preview_res.text
    body = preview_res.json()
    assert body["observation_source"] == "fact_layer"
    assert body["enabled_total"] >= 1
