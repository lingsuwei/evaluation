"""P0：inference_rounds Pipeline 单测。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Agent, AgentRun, Base
from services.evaluation_pipeline import (
    compute_online_progress_total,
    is_question_online_evaluation_complete,
)


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
def sample_agent():
    return Agent(
        id="ag_p0",
        name="P0Agent",
        logical_agent_id="la_p0",
        version="1.0",
        inference_config='{"model": "test"}',
        created_at=datetime.utcnow(),
    )


def test_progress_total_scales_with_inference_rounds():
    single = compute_online_progress_total(2, ["correctness"], 1)
    triple = compute_online_progress_total(2, ["correctness"], 3)
    assert single == 2 * (1 + 1)
    assert triple == 2 * (3 + 3)
    assert triple - single == 2 * (3 - 1) * 2


@pytest.mark.asyncio
async def test_is_question_complete_requires_all_inference_rounds(mem_session, sample_agent):
    now = datetime.utcnow()
    mem_session.add(sample_agent)
    for idx in range(2):
        mem_session.add(
            AgentRun(
                id=f"run_{idx}",
                origin="evaluation",
                task_id="t1",
                dataset_id="d1",
                question_id=0,
                run_index=idx,
                agent_id=sample_agent.id,
                status="success",
                started_at=now,
                agent_snapshot="{}",
            )
        )
    await mem_session.commit()

    done = await is_question_online_evaluation_complete(
        mem_session,
        task_id="t1",
        question_id=0,
        agent_id=sample_agent.id,
        metrics_to_run=["correctness"],
        inference_rounds=3,
    )
    assert done is False

    mem_session.add(
        AgentRun(
            id="run_2",
            origin="evaluation",
            task_id="t1",
            dataset_id="d1",
            question_id=0,
            run_index=2,
            agent_id=sample_agent.id,
            status="success",
            started_at=now,
            agent_snapshot="{}",
        )
    )
    await mem_session.commit()

    done2 = await is_question_online_evaluation_complete(
        mem_session,
        task_id="t1",
        question_id=0,
        agent_id=sample_agent.id,
        metrics_to_run=[],
        inference_rounds=3,
    )
    assert done2 is True
