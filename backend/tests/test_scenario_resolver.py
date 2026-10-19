"""场景解析：条目 scenario_key / L0 回退（ST-2-1 / ST-2-2）。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, Dataset, EvaluationResult
from services.eval_context import build_eval_context
from services.scenario_eval_service import (
    GENERIC_L0_SCENARIO_KEY,
    resolve_scenario_for_run,
)

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
async def test_resolve_item_scenario_key(mem_session):
    mem_session.add(
        Dataset(
            id="ds_sk",
            name="t",
            data='[{"question":"q","model_answer":"a"}]',
            item_count=1,
        )
    )
    mem_session.add(
        AgentRun(
            id="run_sk",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t1", dataset_id="ds_sk", question_id=0, run_index=0
            ),
            task_id="t1",
            dataset_id="ds_sk",
            question_id=0,
            agent_id="ag1",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    mem_session.add(
        EvaluationResult(
            task_id="t1",
            question_id=0,
            model="m",
            metric_name="correctness",
            score=0.5,
            avg_score=0.5,
            passed=True,
            agent_run_id="run_sk",
            question="普通问题",
            ground_truth="",
            agent_answer="",
        )
    )
    await mem_session.flush()
    key = await resolve_scenario_for_run(mem_session, "run_sk")
    assert key == GENERIC_L0_SCENARIO_KEY


@pytest.mark.asyncio
async def test_resolve_generic_l0_fallback(mem_session):
    mem_session.add(
        Dataset(
            id="ds_l0",
            name="t",
            data='[{"question":"什么是AI","model_answer":"a"}]',
            item_count=1,
        )
    )
    mem_session.add(
        AgentRun(
            id="run_l0",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t2", dataset_id="ds_l0", question_id=0, run_index=0
            ),
            task_id="t2",
            dataset_id="ds_l0",
            question_id=0,
            agent_id="ag1",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    mem_session.add(
        EvaluationResult(
            task_id="t2",
            question_id=0,
            model="m",
            metric_name="correctness",
            score=0.5,
            avg_score=0.5,
            passed=True,
            agent_run_id="run_l0",
            question="什么是人工智能",
            ground_truth="",
            agent_answer="人工智能是…",
        )
    )
    await mem_session.flush()
    key = await resolve_scenario_for_run(mem_session, "run_l0")
    assert key == GENERIC_L0_SCENARIO_KEY
