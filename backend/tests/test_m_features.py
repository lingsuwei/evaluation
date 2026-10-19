"""M-01 / M-02 中期能力单测。"""
from __future__ import annotations

from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Agent, AgentRun, Base, Dataset, EvaluationResult, EvaluationTask, get_session
from main import app
from services.runtime_target_suggestion_service import suggest_runtime_targets

pytestmark = pytest.mark.p3


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
                id="ds_hist",
                name="hist",
                data='[{"question":"q"}]',
                created_at=datetime.utcnow(),
            )
        )
        session.add(
            Agent(
                id="ag_v1",
                name="demo",
                logical_agent_id="logic_demo",
                version="1.0",
                inference_config='{"url":"http://agent"}',
                created_at=datetime.utcnow(),
            )
        )
        session.add(
            EvaluationTask(
                id="task_cur",
                dataset_id="ds_hist",
                agent_id="ag_v1",
                models='["default"]',
                status="completed",
                created_at=datetime.utcnow(),
            )
        )
        session.add(
            AgentRun(
                id="run_hist_1",
                origin="evaluation",
                task_id="task_old",
                dataset_id="ds_hist",
                logical_agent_id="logic_demo",
                question_id=0,
                run_index=0,
                agent_id="ag_v1",
                status="success",
                started_at=datetime.utcnow(),
                ended_at=datetime.utcnow(),
                tokens_total=1000,
                agent_snapshot="{}",
                created_at=datetime.utcnow(),
            )
        )
        session.add(
            AgentRun(
                id="run_hist_2",
                origin="evaluation",
                task_id="task_old",
                dataset_id="ds_hist",
                logical_agent_id="logic_demo",
                question_id=0,
                run_index=0,
                agent_id="ag_v1",
                status="success",
                started_at=datetime.utcnow(),
                ended_at=datetime.utcnow(),
                tokens_total=3000,
                agent_snapshot="{}",
                created_at=datetime.utcnow(),
            )
        )
        session.add_all(
            [
                EvaluationResult(
                    task_id="task_old",
                    question_id=0,
                    model="runtime",
                    metric_name="inference_latency",
                    score=1.0,
                    threshold=10000,
                    passed=True,
                    eval_method="runtime",
                    agent_run_id="run_hist_1",
                    question="q",
                    ground_truth="gt",
                    agent_answer="a",
                    inference_duration_ms=2000,
                ),
                EvaluationResult(
                    task_id="task_old",
                    question_id=0,
                    model="runtime",
                    metric_name="inference_latency",
                    score=1.0,
                    threshold=10000,
                    passed=True,
                    eval_method="runtime",
                    agent_run_id="run_hist_2",
                    question="q",
                    ground_truth="gt",
                    agent_answer="a",
                    inference_duration_ms=4000,
                ),
            ]
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_experiment_report_endpoint_deprecated(api_client):
    res = await api_client.get("/tasks/task_cur/experiment-report")
    assert res.status_code == 200
    body = res.json()
    assert "diff_highlights" in body
    assert "comparison_count" in body


@pytest.mark.asyncio
async def test_runtime_target_suggestions_p75_p90(api_client):
    res = await api_client.get("/tasks/task_cur/runtime-target-suggestions")
    assert res.status_code == 200
    body = res.json()
    assert body["source"] == "historical"
    assert body["inference_latency_ms"]["p75"] == 3500
    assert body["inference_latency_ms"]["p90"] == 3800
    assert body["tokens_total"]["p75"] == 2500
