"""P4 Run / Comparison Span Tree API 单测。"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from comparison_routes import comparison_router
from database import Base, get_session
from run_routes import run_router
from services.run_comparison_service import compare_runs
from tests.test_p3_diff_calculators import _seed_runs

pytestmark = pytest.mark.p4


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
    app.include_router(run_router, prefix="/api")
    app.include_router(comparison_router, prefix="/api")

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
async def test_get_run_span_tree_api(client, mem_session):
    await _seed_runs(mem_session)
    resp = await client.get("/api/runs/run_left/span-tree")
    assert resp.status_code == 200
    assert resp.json()["node_count"] == 2


@pytest.mark.asyncio
async def test_get_run_checkpoints_api(client, mem_session):
    from tests.test_checkpoint_result_service import _seed_checkpoint_data

    await _seed_checkpoint_data(mem_session)
    resp = await client.get("/api/runs/run_cp1/checkpoints")
    assert resp.status_code == 200
    body = resp.json()
    assert body["checkpoint_passed"] == 1
    assert len(body["items"]) == 2
    assert body["items"][0]["name"] == "规划"


@pytest.mark.asyncio
async def test_get_comparison_span_trees_api(client, mem_session):
    await _seed_runs(mem_session)
    cmp = await compare_runs(
        mem_session, left_run_id="run_left", right_run_id="run_right"
    )
    resp = await client.get(f"/api/comparisons/{cmp['id']}/span-trees")
    assert resp.status_code == 200
    body = resp.json()
    assert body["comparison_id"] == cmp["id"]
    assert len(body["align_aggregate_keys"]) >= 1
