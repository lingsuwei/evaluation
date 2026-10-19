"""D-07：任务详情 API 返回 trace_sync_status"""
from __future__ import annotations

from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, Dataset, EvaluationResult, EvaluationTask, RunMetric, get_session
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
                data='[{"question":"q","model_answer":"a"}]',
                created_at=datetime.utcnow(),
            )
        )
        session.add(
            EvaluationTask(
                id="task1",
                dataset_id="ds1",
                models='["default"]',
                status="completed",
                evaluation_mode="online",
                agent_id="ag1",
                created_at=datetime.utcnow(),
            )
        )
        session.add(
            AgentRun(
                id="run1",
                origin="evaluation",
                task_id="task1",
                dataset_id="ds1",
                question_id=0,
                run_index=0,
                agent_id="ag1",
                status="success",
                started_at=datetime.utcnow(),
                ended_at=datetime.utcnow(),
                tokens_total=15000,
                inference_model_provider="智谱",
                inference_model_name="glm-4.7",
                inference_model_version="2026-05",
                agent_snapshot="{}",
                trace_sync_status="pending_sync",
                created_at=datetime.utcnow(),
            )
        )
        session.add(
            EvaluationResult(
                task_id="task1",
                question_id=0,
                model="glm",
                metric_name="faithfulness",
                score=0.9,
                threshold=0.8,
                passed=True,
                agent_run_id="run1",
                question="q",
                ground_truth="gt",
                agent_answer="a",
            )
        )
        session.add_all(
            [
                EvaluationResult(
                    task_id="task1",
                    question_id=0,
                    model="runtime",
                    metric_name="inference_latency",
                    score=1.0,
                    avg_score=1.0,
                    threshold=10000,
                    passed=True,
                    eval_method="runtime",
                    agent_run_id="run1",
                    question="q",
                    ground_truth="gt",
                    agent_answer="a",
                    inference_duration_ms=8000,
                ),
                EvaluationResult(
                    task_id="task1",
                    question_id=0,
                    model="runtime",
                    metric_name="tokens_total",
                    score=0.875,
                    avg_score=0.875,
                    threshold=12000,
                    passed=False,
                    eval_method="runtime",
                    agent_run_id="run1",
                    question="q",
                    ground_truth="gt",
                    agent_answer="a",
                    inference_duration_ms=8000,
                ),
            ]
        )
        session.add_all(
            [
                RunMetric(
                    id="rm_tokens_llm",
                    agent_run_id="run1",
                    metric_key="runtime.tokens_llm",
                    source="runtime",
                    value_num=14000,
                    dims_hash="",
                ),
                RunMetric(
                    id="rm_tokens_tool",
                    agent_run_id="run1",
                    metric_key="runtime.tokens_tool",
                    source="runtime",
                    value_num=1000,
                    dims_hash="",
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
async def test_task_detail_includes_sync_fields(api_client):
    res = await api_client.get("/tasks/task1/detail")
    assert res.status_code == 200
    body = res.json()
    assert body["sync_summary"]["pending_sync"] == 1
    assert body["fact_materialization_status"] == "in_progress"
    assert body["agent_run_count"] == 1
    faith = body["metrics_data"]["faithfulness"]["results"][0]
    assert faith["trace_sync_status"] == "pending_sync"
    assert faith["agent_run_id"] == "run1"
    assert "analytics_rows" in body
    assert isinstance(body["analytics_rows"], list)
    assert "dimension_verdicts" in body
    assert body["dimension_verdicts"]["quality"]["status"] in ("pass", "warn", "fail", "na")
    model_summary = body["task"]["inference_model_summary"]
    assert model_summary["display"] == "智谱 · glm-4.7 · 2026-05"
    assert model_summary["count"] == 1

    tasks = await api_client.get("/tasks")
    task = tasks.json()["tasks"][0]
    assert task["inference_model_summary"]["display"] == "智谱 · glm-4.7 · 2026-05"


@pytest.mark.asyncio
async def test_result_endpoint_splits_runtime_details(api_client):
    res = await api_client.get("/result/task1")
    assert res.status_code == 200
    body = res.json()
    runtime_details = body["runtime_details"]
    assert runtime_details[0]["inference_duration_ms"] == 8000
    assert runtime_details[0]["tokens_total"] == 15000
    assert runtime_details[0]["tokens_llm"] == 14000
    assert runtime_details[0]["tokens_tool"] == 1000
    metric_names = [row["metrics"][0]["name"] for row in body["details"]]
    assert "inference_latency" in metric_names
    assert body["quality_score"]["total_score"] is not None
    assert body["summary"]["quality_score"]["total_score"] == body["quality_score"]["total_score"]
    assert "dimension_verdicts" in body["summary"]
    assert body["summary"]["dimension_verdicts"]["quality"]["headline"]
    assert body["dimension_verdicts"]["task_id"] == "task1"
    runtime_summary = body["summary"]["runtime_summary"]
    assert runtime_summary["inference_latency"]["threshold"] == 10000
    assert runtime_summary["inference_latency"]["pass_rate"] == 100
    assert runtime_summary["inference_latency"]["runtime_target_source"] == "system_default"


@pytest.mark.asyncio
async def test_task_detail_uses_runtime_token_metrics(api_client):
    res = await api_client.get("/tasks/task1/detail")
    assert res.status_code == 200
    body = res.json()

    tokens_row = body["metrics_data"]["tokens_total"]["results"][0]
    assert tokens_row["tokens_total"] == 15000
    assert tokens_row["tokens_llm"] == 14000
    assert tokens_row["tokens_tool"] == 1000

    inference_run = body["inference_runs_by_question"]["0"][0]
    assert inference_run["tokens_llm"] == 14000
    assert inference_run["tokens_tool"] == 1000


@pytest.mark.asyncio
async def test_update_runtime_targets_recalculates_runtime_scores(api_client):
    res = await api_client.patch(
        "/tasks/task1/runtime-targets",
        json={"inference_latency_ms": 5000, "tokens_total": 20000},
    )
    assert res.status_code == 200
    assert res.json()["updated_count"] == 2

    detail = await api_client.get("/tasks/task1/detail")
    body = detail.json()
    summaries = {m["metric_name"]: m for m in body["metrics_summary"]}
    latency = summaries["inference_latency"]
    tokens = summaries["tokens_total"]
    assert latency["threshold"] == 5000
    assert latency["pass_rate"] == 0
    assert latency["runtime_target_source"] == "task_custom"
    assert tokens["threshold"] == 20000
    assert tokens["pass_rate"] == 100
    assert tokens["runtime_target_source"] == "task_custom"
