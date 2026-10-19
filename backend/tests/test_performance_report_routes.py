"""运行效能报告 HTTP API 单测（P0）。"""
from __future__ import annotations

from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Agent, AgentRun, Base, Dataset, EvaluationResult, EvaluationTask, get_session
from main import app
from services.runtime_fact_builder import apply_runtime_facts_from_trace

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
                id="ds_p0",
                name="ds",
                data='[{"question":"q"}]',
                created_at=datetime.utcnow(),
            )
        )
        session.add(
            Agent(
                id="ag_p0",
                name="agent",
                logical_agent_id="logic_p0",
                version="1.0",
                inference_config='{"url":"http://x"}',
                created_at=datetime.utcnow(),
            )
        )
        session.add(
            EvaluationTask(
                id="task_p0",
                dataset_id="ds_p0",
                agent_id="ag_p0",
                models='["default"]',
                status="completed",
                created_at=datetime.utcnow(),
            )
        )
        for idx, rid in enumerate(("run_p0_a", "run_p0_b")):
            session.add(
                AgentRun(
                    id=rid,
                    origin="evaluation",
                    task_id="task_p0",
                    dataset_id="ds_p0",
                    question_id=1,
                    run_index=idx,
                    agent_id="ag_p0",
                    status="success",
                    started_at=datetime.utcnow(),
                    trace_id=f"tr_{idx}",
                    agent_snapshot="{}",
                    created_at=datetime.utcnow(),
                )
            )
        session.add(
            EvaluationResult(
                task_id="task_p0",
                question_id=1,
                model="runtime",
                metric_name="inference_latency",
                score=1.0,
                threshold=60000,
                passed=True,
                eval_method="runtime",
                agent_run_id="run_p0_a",
                question="q1",
                ground_truth="gt",
                agent_answer="a",
                inference_duration_ms=5000,
            )
        )
        await session.flush()

        trace_a = {
            "observations": [
                {"id": "a1", "type": "GENERATION", "metadata": {"agent.step.id": "plan"}},
                {
                    "id": "a2",
                    "type": "TOOL",
                    "metadata": {"agent.tool.name": "kb_search"},
                },
                {"id": "a3", "type": "GENERATION", "metadata": {"agent.step.id": "answer"}},
            ]
        }
        trace_b = {
            "observations": [
                {"id": "b1", "type": "GENERATION", "metadata": {"agent.step.id": "plan"}},
                {"id": "b2", "type": "GENERATION", "metadata": {"agent.step.id": "answer"}},
            ]
        }
        run_a = await session.get(AgentRun, "run_p0_a")
        run_b = await session.get(AgentRun, "run_p0_b")
        await apply_runtime_facts_from_trace(session, run_a, trace_a)
        await apply_runtime_facts_from_trace(session, run_b, trace_b)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_performance_report_route(api_client):
    res = await api_client.get("/tasks/task_p0/performance-report")
    assert res.status_code == 200
    body = res.json()
    assert body["task_id"] == "task_p0"
    assert body["summary"]["llm_call_count"] >= 3
    assert body["summary"]["tool_call_count"] >= 1
    assert len(body["run_rows"]) == 2
    assert body["focus_questions"]["slowest"]


@pytest.mark.asyncio
async def test_run_step_compare_route(api_client):
    res = await api_client.get(
        "/tasks/task_p0/run-step-compare",
        params={"left_run_id": "run_p0_a", "right_run_id": "run_p0_b"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["question_id"] == 1
    assert any(item["type"] == "removed" for item in body["diff_summary"])
    assert len(body["aligned_steps"]) >= 1
    assert len(body["round_stability"]["rows"]) >= 1


@pytest.mark.asyncio
async def test_run_step_compare_rejects_foreign_task_run(api_client):
    res = await api_client.get(
        "/tasks/task_p0/run-step-compare",
        params={"left_run_id": "run_p0_a", "right_run_id": "run_missing"},
    )
    assert res.status_code == 404
