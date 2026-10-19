"""场景评测 API 单测。"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base, get_session
from scenario_routes import scenario_router
from tests.test_scenario_eval_service import _seed_generic_run
from services.scenario_eval_service import GENERIC_L0_SCENARIO_KEY

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
    app.include_router(scenario_router, prefix="/api")

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
async def test_task_scenario_matrix_api(client, mem_session):
    from tests.test_task_scenario_matrix import _seed_intra_task_two_agents

    sk = await _seed_intra_task_two_agents(mem_session)
    res = await client.get(
        f"/api/scenario-eval/tasks/t_intra/matrix?scenario_key={sk}"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["comparison_mode"] == "intra_task_scenario"
    assert len(body["member_ids"]) == 2


@pytest.mark.asyncio
async def test_task_scenario_matrix_api_readable_500(client, monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("模拟内部错误")

    monkeypatch.setattr(
        "scenario_routes.build_task_scenario_matrix",
        _boom,
    )
    res = await client.get("/api/scenario-eval/tasks/t_x/matrix")
    assert res.status_code == 500
    assert "场景子步骤矩阵加载失败" in res.json()["detail"]
    assert "模拟内部错误" in res.json()["detail"]


@pytest.mark.asyncio
async def test_task_scenario_matrix_api_business_error_400(client, monkeypatch):
    from services.cross_task_cockpit_service import CrossTaskCockpitError

    async def _biz(*args, **kwargs):
        raise CrossTaskCockpitError("任务不存在")

    monkeypatch.setattr(
        "scenario_routes.build_task_scenario_matrix",
        _biz,
    )
    res = await client.get("/api/scenario-eval/tasks/t_missing/matrix")
    assert res.status_code == 400
    assert res.json()["detail"] == "任务不存在"


@pytest.mark.asyncio
async def test_task_report_generic_l0(client, mem_session):
    await _seed_generic_run(mem_session)
    await mem_session.commit()

    report = await client.get(
        f"/api/scenario-eval/tasks/t_scen/report?scenario_key={GENERIC_L0_SCENARIO_KEY}"
    )
    assert report.status_code == 200
    body = report.json()
    assert body["scenario_key"] == GENERIC_L0_SCENARIO_KEY
    assert len(body["runs"]) == 1
