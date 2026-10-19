"""单 Agent 在线评测：按题号恢复断点。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Agent, Dataset, EvaluationTask
from services.evaluation_pipeline import run_online_evaluation_pipeline

pytestmark = pytest.mark.p3


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        from database import Base

        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_resume_from_question_index(mem_session):
    now = datetime.utcnow()
    mem_session.add(
        Dataset(
            id="ds_cp",
            name="CP",
            data=(
                '[{"question":"Q0","ground_truth":"A0"},'
                '{"question":"Q1","ground_truth":"A1"}]'
            ),
            item_count=2,
            created_at=now,
        )
    )
    mem_session.add(
        Agent(
            id="ag1",
            name="D",
            version="1.0",
            inference_config='{"url": "http://x", "method": "POST"}',
        )
    )
    mem_session.add(
        EvaluationTask(
            id="task_cp",
            dataset_id="ds_cp",
            models='["j"]',
            status="paused",
            agent_id="ag1",
            resume_from=1,
            run_count=1,
            llm_model="glm",
            progress_current=3,
            progress_total=6,
        )
    )
    await mem_session.commit()

    infer_questions: list[str] = []

    async def _infer(**kwargs):
        infer_questions.append(kwargs.get("question"))
        return {"model_answer": "ans", "duration_ms": 10, "tokens_total": 5}

    async def _eval(*_a, **_k):
        return {"score": 0.9, "reason": "ok", "duration_ms": 5}

    with (
        patch(
            "services.evaluation_pipeline.InferenceService.call_inference",
            new=AsyncMock(side_effect=_infer),
        ),
        patch("tasks.evaluate_single", new=AsyncMock(side_effect=_eval)),
        patch("tasks.evaluate_with_script", new=AsyncMock()),
        patch("tasks.check_task_paused", new=AsyncMock(return_value=False)),
        patch("tasks.emit_event", new=AsyncMock()),
        patch("tasks.refresh_llm_models_cache_task", new=AsyncMock()),
        patch(
            "evaluators.llm_client.generate_evaluation_summary",
            new=AsyncMock(return_value="s"),
        ),
        patch("services.evaluation_pipeline.async_session") as mock_sess,
    ):
        mock_sess.return_value.__aenter__ = AsyncMock(return_value=mem_session)
        mock_sess.return_value.__aexit__ = AsyncMock(return_value=None)
        await run_online_evaluation_pipeline(
            task_id="task_cp",
            dataset_id="ds_cp",
            agent_id="ag1",
            resume_from=1,
            selected_metrics=["correctness"],
        )

    assert infer_questions == ["Q1"]
    task = (
        await mem_session.execute(
            select(EvaluationTask).where(EvaluationTask.id == "task_cp")
        )
    ).scalar_one()
    assert task.status == "completed"
    assert task.resume_from == 0
