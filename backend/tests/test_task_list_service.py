"""任务列表 API 读模型单测。"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Agent, Base, Dataset, EvaluationResult, EvaluationTask
from services.task_list_service import serialize_tasks_for_list

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


@pytest.mark.asyncio
async def test_serialize_tasks_for_list_enrichment(mem_session):
    mem_session.add(Dataset(id="ds1", name="带状通道数据集"))
    mem_session.add(
        Agent(
            id="ag1",
            name="防御 Agent v2",
            langfuse_project="onto-eval",
            inference_config='{"workspace_id":"ws-demo-1","base_url":"http://127.0.0.1:9","auth":{"api_token":"t"}}',
            is_active=True,
        )
    )
    mem_session.add(
        EvaluationTask(
            id="task_list_1",
            dataset_id="ds1",
            models='["glm"]',
            status="completed",
            agent_id="ag1",
        )
    )
    mem_session.add(
        EvaluationResult(
            task_id="task_list_1",
            question_id=0,
            model="glm",
            metric_name="correctness",
            score=0.8,
            avg_score=0.8,
            threshold=0.5,
            passed=True,
        )
    )
    mem_session.add(
        EvaluationResult(
            task_id="task_list_1",
            question_id=0,
            model="glm",
            metric_name="faithfulness",
            score=0.6,
            avg_score=0.6,
            threshold=0.5,
            passed=True,
        )
    )
    await mem_session.flush()

    task = (
        await mem_session.get(EvaluationTask, "task_list_1")
    )
    rows = await serialize_tasks_for_list(mem_session, [task])
    assert len(rows) == 1
    row = rows[0]
    assert row["dataset_name"] == "带状通道数据集"
    assert row["dataset_id"] == "ds1"
    assert row["agent_name"] == "防御 Agent v2"
    assert row["overall_avg_score"] == pytest.approx(0.7)
    assert row["id"] == "task_list_1"
    assert row["agent_workspace_name"] == "ws-demo-1"


@pytest.mark.asyncio
async def test_serialize_tasks_for_list_hides_scores_while_processing(mem_session):
    mem_session.add(Dataset(id="ds1", name="测试集"))
    mem_session.add(
        EvaluationTask(
            id="task_processing",
            dataset_id="ds1",
            models='["glm"]',
            status="processing",
            agent_id="ag1",
        )
    )
    mem_session.add(
        EvaluationResult(
            task_id="task_processing",
            question_id=0,
            model="glm",
            metric_name="correctness",
            score=1.0,
            avg_score=1.0,
            threshold=0.5,
            passed=True,
        )
    )
    await mem_session.flush()

    task = await mem_session.get(EvaluationTask, "task_processing")
    rows = await serialize_tasks_for_list(mem_session, [task])
    row = rows[0]

    assert row["status"] == "processing"
    assert row["overall_avg_score"] is None
    assert row["total_score"] is None
    assert row["result_score"] is None
    assert row["process_score"] is None
    assert row["quality_score"] == {}
