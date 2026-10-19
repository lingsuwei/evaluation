"""Agent 注册预置 onto-agent 过程点单测。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Agent, Base, ProcessCheckpointTemplate
from services.checkpoint_seed_service import (
    is_onto_agent_inference_config,
    logical_agent_has_checkpoints,
    seed_onto_agent_baseline_checkpoints,
)


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def test_is_onto_agent_inference_config():
    assert is_onto_agent_inference_config({"endpoint": "/agentic/runs/sync", "url": "http://x"})
    assert not is_onto_agent_inference_config({"endpoint": "/v1/chat", "url": "http://x"})
    assert not is_onto_agent_inference_config(None)


@pytest.mark.asyncio
async def test_seed_baseline_on_new_agent(mem_session):
    agent = Agent(
        id="agent_seed1",
        name="测试 onto-agent",
        inference_config=json.dumps(
            {"url": "http://127.0.0.1:5055", "endpoint": "/agentic/runs/sync"}
        ),
    )
    mem_session.add(agent)
    await mem_session.flush()

    result = await seed_onto_agent_baseline_checkpoints(mem_session, agent)
    assert result["created"] == 4
    assert result["skipped"] is False

    lid = agent.logical_agent_id
    assert lid
    assert await logical_agent_has_checkpoints(mem_session, lid)

    rows = (
        await mem_session.execute(
            ProcessCheckpointTemplate.__table__.select().where(
                ProcessCheckpointTemplate.logical_agent_id == lid
            )
        )
    ).fetchall()
    assert len(rows) == 4


@pytest.mark.asyncio
async def test_seed_skips_when_checkpoints_exist(mem_session):
    agent = Agent(
        id="agent_seed2",
        name="已有过程点",
        logical_agent_id="la_existing",
        inference_config=json.dumps({"endpoint": "/agentic/runs/sync"}),
    )
    mem_session.add(agent)
    mem_session.add(
        ProcessCheckpointTemplate(
            id="cp_existing",
            logical_agent_id="la_existing",
            name="主 Agent 循环",
            match_rules='{"mode":"any","rules":[]}',
        )
    )
    await mem_session.flush()

    result = await seed_onto_agent_baseline_checkpoints(mem_session, agent)
    assert result["created"] == 0
    assert result["reason"] == "checkpoints_exist"


@pytest.mark.asyncio
async def test_seed_skips_non_onto_agent(mem_session):
    agent = Agent(
        id="agent_seed3",
        name="自定义 API",
        inference_config=json.dumps({"endpoint": "/chat/completions"}),
    )
    mem_session.add(agent)
    await mem_session.flush()

    result = await seed_onto_agent_baseline_checkpoints(mem_session, agent)
    assert result["created"] == 0
    assert result["reason"] == "not_onto_agent_endpoint"


@pytest.mark.asyncio
async def test_reference_catalog_has_nine_templates():
    from services.checkpoint_reference_service import get_onto_agent_reference_catalog

    catalog = get_onto_agent_reference_catalog()
    assert catalog["id"] == "onto-agent"
    assert catalog["count"] == 9
    assert len(catalog["templates"]) == 9
    names = {t["name"] for t in catalog["templates"]}
    assert "主 Agent 循环" in names
    assert "子 Agent 编排" in names


@pytest.mark.asyncio
async def test_seed_reference_merges_missing_only(mem_session):
    from services.checkpoint_reference_service import seed_onto_agent_reference_for_agent_name

    agent = Agent(
        id="agent_ref1",
        name="参考初始化 Agent",
        logical_agent_id="la_ref1",
    )
    mem_session.add(agent)
    mem_session.add(
        ProcessCheckpointTemplate(
            id="cp_existing_ref",
            logical_agent_id="la_ref1",
            name="主 Agent 循环",
            match_rules='{"mode":"any","rules":[]}',
        )
    )
    await mem_session.flush()

    result = await seed_onto_agent_reference_for_agent_name(
        mem_session, agent_name="参考初始化 Agent"
    )
    assert result["created"] == 8
    assert "主 Agent 循环" in result["skipped_names"]

    rows = (
        await mem_session.execute(
            ProcessCheckpointTemplate.__table__.select().where(
                ProcessCheckpointTemplate.logical_agent_id == "la_ref1"
            )
        )
    ).fetchall()
    assert len(rows) == 9
