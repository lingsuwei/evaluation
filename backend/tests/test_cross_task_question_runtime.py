"""单题运行效能跨版本对比（P0）。"""
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, EvaluationResult, RunMetric
from services.cross_task_scenario_service import build_cross_task_scenario_matrix
from tests.scenario_test_helpers import seed_two_tasks_with_item_template

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


async def _seed_runtime(session: AsyncSession) -> None:
    """为两个任务的 Run 补充 tokens/时延/调用次数事实。"""
    runs = (await session.execute(select(AgentRun))).scalars().all()
    for r in runs:
        r.tokens_total = 1000 if r.task_id == "t_s1" else 1500
    results = (await session.execute(select(EvaluationResult))).scalars().all()
    for er in results:
        er.inference_duration_ms = 2000 if er.task_id == "t_s1" else 3200
    session.add_all(
        [
            RunMetric(
                id="m_s1_llm",
                agent_run_id="run_s1",
                metric_key="runtime.llm_call_count",
                value_num=3,
            ),
            RunMetric(
                id="m_s1_tool",
                agent_run_id="run_s1",
                metric_key="runtime.tool_call_count",
                value_num=2,
            ),
            RunMetric(
                id="m_s2_llm",
                agent_run_id="run_s2",
                metric_key="runtime.llm_call_count",
                value_num=5,
            ),
            RunMetric(
                id="m_s2_tool",
                agent_run_id="run_s2",
                metric_key="runtime.tool_call_count",
                value_num=4,
            ),
        ]
    )
    await session.commit()


@pytest.mark.asyncio
async def test_question_runtime_summary(mem_session):
    await seed_two_tasks_with_item_template(mem_session)
    await _seed_runtime(mem_session)

    payload = await build_cross_task_scenario_matrix(mem_session, ["t_s1", "t_s2"])
    summary = payload.get("question_runtime_summary")
    assert summary is not None
    assert summary["baseline_task_id"] == "t_s1"
    assert summary["has_runtime"] is True
    assert summary["member_ids"] == ["t_s1", "t_s2"]
    assert len(summary["rows"]) >= 1

    row = summary["rows"][0]
    base = row["cells"]["t_s1"]
    other = row["cells"]["t_s2"]

    assert base["missing"] is False
    assert base["latency_ms"] == 2000
    assert base["tokens_total"] == 1000
    assert base["llm_call_count"] == 3
    assert base["tool_call_count"] == 2
    assert base["trace_id"] is None or isinstance(base["trace_id"], str)
    assert base["vs_baseline"] is None

    assert other["latency_ms"] == 3200
    assert other["tokens_total"] == 1500
    assert other["llm_call_count"] == 5
    assert other["tool_call_count"] == 4
    assert other["vs_baseline"]["latency_delta_ms"] == 1200
    assert other["vs_baseline"]["tokens_delta"] == 500
    assert other["vs_baseline"]["llm_delta"] == 2
    assert other["vs_baseline"]["tool_delta"] == 2


@pytest.mark.asyncio
async def test_question_runtime_handles_missing_runtime(mem_session):
    await seed_two_tasks_with_item_template(mem_session)

    payload = await build_cross_task_scenario_matrix(mem_session, ["t_s1", "t_s2"])
    summary = payload.get("question_runtime_summary")
    assert summary is not None
    assert summary["has_runtime"] is False
    row = summary["rows"][0]
    assert row["cells"]["t_s1"]["missing"] is False
    assert row["cells"]["t_s1"]["latency_ms"] is None
