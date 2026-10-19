"""EVAL-UX B1：进行中任务 partial result API。"""
from __future__ import annotations

from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base, Dataset, EvaluationResult, EvaluationTask, get_session
from main import app


@pytest.fixture
async def api_client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session

    async with factory() as session:
        session.add(
            Dataset(
                id="ds1",
                name="test",
                data='[{"question":"q1","model_answer":"a1"}]',
                created_at=datetime.utcnow(),
            )
        )
        session.add(
            EvaluationTask(
                id="task_partial",
                dataset_id="ds1",
                models='["glm"]',
                status="processing",
                progress_current=1,
                progress_total=3,
                evaluation_mode="offline",
                agent_id=None,
                created_at=datetime.utcnow(),
            )
        )
        session.add(
            EvaluationResult(
                task_id="task_partial",
                question_id=0,
                model="glm",
                metric_name="faithfulness",
                score=0.9,
                avg_score=0.9,
                threshold=0.8,
                passed=True,
                duration_ms=10,
            )
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_result_without_partial_is_minimal(api_client):
    res = await api_client.get("/result/task_partial")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "processing"
    assert "details" not in body


@pytest.mark.asyncio
async def test_result_partial_returns_saved_rows(api_client):
    res = await api_client.get("/result/task_partial", params={"partial": True})
    assert res.status_code == 200
    body = res.json()
    assert body["partial"] is True
    assert body["status"] == "processing"
    assert len(body["details"]) == 1
    assert body["details"][0]["metrics"][0]["name"] == "faithfulness"
    assert body["completed_question_ids"] == [0]
    assert body["partial_quality_score"]["metric_result_count"] == 1
