"""P3-B Experiment API 单测。"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base, get_session
from experiment_routes import experiment_router
from tests.test_experiment_service import _seed_multi_agent_task

pytestmark = pytest.mark.p3


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def app(mem_session):
    app = FastAPI()
    app.include_router(experiment_router, prefix="/api")

    async def _override():
        yield mem_session

    app.dependency_overrides[get_session] = _override
    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_experiment_run_flow(client, mem_session):
    await _seed_multi_agent_task(mem_session)
    create_resp = await client.post(
        "/api/experiments/task/t_exp",
        json={"name": "版本对比", "baseline_agent_id": "ag_v1"},
    )
    assert create_resp.status_code == 200
    exp_id = create_resp.json()["id"]
    run_resp = await client.post(f"/api/experiments/{exp_id}/run", json={})
    assert run_resp.status_code == 200
    body = run_resp.json()
    assert body["status"] == "completed"
    assert len(body["summaries"]) == 1
    assert "semantic" in body["summaries"][0]["highlights"]
    assert "structural" in body["summaries"][0]["highlights"]


@pytest.mark.asyncio
async def test_task_version_cockpit(client, mem_session):
    await _seed_multi_agent_task(mem_session)
    await mem_session.commit()
    resp = await client.get("/api/experiments/task/t_exp/cockpit?question_id=0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == "t_exp"
    assert len(body["l1"]["rows"]) == 1
    assert body["l2"]["question_id"] == 0
    assert any(f["factor_key"] == "correctness" for f in body["l2"]["factors"])
