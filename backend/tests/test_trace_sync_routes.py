"""Trace Sync HTTP API（TS-API-*）"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database import AgentRun, Base, TraceSyncJob, get_session
import trace_routes


@pytest.fixture
async def sync_api_env():
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
    app.include_router(trace_routes.trace_router, prefix="/api")
    app.dependency_overrides[get_session] = _session_override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield {"client": client, "session_factory": session_factory}

    await engine.dispose()


async def _seed_jobs(session_factory):
    async with session_factory() as session:
        run = AgentRun(
            id="run_api",
            task_id="task_a",
            dataset_id="ds1",
            question_id=0,
            run_index=0,
            agent_id="ag1",
            status="success",
            started_at=datetime.utcnow(),
            trace_id="t1",
            agent_snapshot="{}",
            created_at=datetime.utcnow(),
        )
        session.add(run)
        session.add_all(
            [
                TraceSyncJob(
                    id="tsj_ok",
                    agent_run_id=run.id,
                    task_id="task_a",
                    trace_id="t1",
                    status="success",
                    attempts=1,
                    created_at=datetime.utcnow(),
                ),
                TraceSyncJob(
                    id="tsj_pend",
                    agent_run_id=run.id,
                    task_id="task_b",
                    trace_id="t2",
                    status="pending",
                    attempts=0,
                    created_at=datetime.utcnow(),
                ),
            ]
        )
        await session.commit()


@pytest.mark.asyncio
async def test_list_sync_jobs(sync_api_env):
    await _seed_jobs(sync_api_env["session_factory"])
    client = sync_api_env["client"]
    res = await client.get("/api/trace/sync-jobs")
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 2
    assert "agent_run_id" in body["items"][0]


@pytest.mark.asyncio
async def test_list_sync_jobs_filter_status(sync_api_env):
    await _seed_jobs(sync_api_env["session_factory"])
    res = await sync_api_env["client"].get(
        "/api/trace/sync-jobs", params={"status": "pending"}
    )
    assert res.status_code == 200
    assert res.json()["count"] == 1
    assert res.json()["items"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_drain_endpoint(sync_api_env):
    await _seed_jobs(sync_api_env["session_factory"])
    with patch(
        "trace_routes.drain_pending_trace_sync_jobs",
        return_value=1,
    ) as mock_drain:
        res = await sync_api_env["client"].post(
            "/api/trace/sync-jobs/drain", params={"limit": 5}
        )
    assert res.status_code == 200
    assert res.json() == {"processed": 1, "limit": 5}
    mock_drain.assert_awaited_once_with(limit=5)


@pytest.mark.asyncio
async def test_drain_endpoint(sync_api_env, monkeypatch):
    with patch(
        "trace_routes.drain_pending_trace_sync_jobs",
        return_value=0,
    ):
        res = await sync_api_env["client"].post("/api/trace/sync-jobs/drain")
    assert res.status_code == 200
    assert res.json()["processed"] == 0


@pytest.mark.asyncio
async def test_trace_sync_stats_endpoint(sync_api_env):
    await _seed_jobs(sync_api_env["session_factory"])
    res = await sync_api_env["client"].get("/api/trace/sync-jobs/stats")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] >= 1
    assert "pending" in body
    assert "failure_rate" in body
