"""P3 RunComparison 服务与 API 单测。"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from comparison_routes import comparison_router
from database import AgentRun, Base, get_session
from services.eval_context import build_eval_context
from tests.test_p3_diff_calculators import _seed_runs

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
async def test_cross_task_from_tasks_api(client, mem_session):
    from tests.test_cross_task_cockpit import _seed_two_tasks

    await _seed_two_tasks(mem_session)
    resp = await client.post(
        "/api/comparisons/from-tasks",
        json={"task_ids": ["t_v1", "t_v2"], "baseline_task_id": "t_v1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["comparison_mode"] == "cross_task"
    assert len(body["agents"]) == 2


@pytest.mark.asyncio
async def test_compare_runs_service(mem_session):
    from services.run_comparison_service import compare_runs

    await _seed_runs(mem_session)
    out = await compare_runs(
        mem_session, left_run_id="run_left", right_run_id="run_right"
    )
    assert out["id"].startswith("cmp_")
    assert len(out["diffs"]) == 4
    types = {d["diff_type"] for d in out["diffs"]}
    assert types == {
        "outcome_diff",
        "cost_diff",
        "checkpoint_diff",
        "event_agg_diff",
    }


@pytest.mark.asyncio
async def test_compare_same_run_rejected(mem_session):
    from services.run_comparison_service import RunComparisonError, compare_runs

    mem_session.add(
        AgentRun(
            id="run_x",
            origin="evaluation",
            task_id="t",
            agent_id="a",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    await mem_session.commit()
    with pytest.raises(RunComparisonError, match="must differ"):
        await compare_runs(mem_session, left_run_id="run_x", right_run_id="run_x")


@pytest.mark.asyncio
async def test_compare_api(client, mem_session):
    await _seed_runs(mem_session)
    resp = await client.post(
        "/api/comparisons/compare",
        json={"left_run_id": "run_left", "right_run_id": "run_right"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["left_run_id"] == "run_left"
    assert len(body["diffs"]) == 4


@pytest.mark.asyncio
async def test_get_comparison_api(client, mem_session):
    from services.run_comparison_service import compare_runs

    await _seed_runs(mem_session)
    created = await compare_runs(
        mem_session, left_run_id="run_left", right_run_id="run_right"
    )
    resp = await client.get(f"/api/comparisons/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


@pytest.mark.asyncio
async def test_list_comparisons_api(client, mem_session):
    from services.run_comparison_service import compare_runs

    await _seed_runs(mem_session)
    await compare_runs(mem_session, left_run_id="run_left", right_run_id="run_right")
    resp = await client.get("/api/comparisons", params={"task_id": "t1"})
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
