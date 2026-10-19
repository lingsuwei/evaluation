"""P2 I-06：legacy aggregate_key 占比。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, RunSpanNode
from services.legacy_aggregate_report import report_legacy_aggregate_ratio


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            AgentRun(
                id="run_l1",
                origin="evaluation",
                task_id="task_l",
                dataset_id="ds",
                question_id=0,
                agent_id="ag1",
                status="success",
                started_at=datetime.utcnow(),
                agent_snapshot="{}",
            )
        )
        session.add_all(
            [
                RunSpanNode(
                    id="s1",
                    agent_run_id="run_l1",
                    observation_id="o1",
                    sibling_order=0,
                    step_index=0,
                    aggregate_key="agent.step:main",
                ),
                RunSpanNode(
                    id="s2",
                    agent_run_id="run_l1",
                    observation_id="o2",
                    sibling_order=1,
                    step_index=1,
                    aggregate_key="legacy:span:gen",
                ),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_ratio_global(mem_session):
    report = await report_legacy_aggregate_ratio(mem_session)
    assert report.total_spans == 2
    assert report.legacy_spans == 1
    assert report.legacy_ratio == 0.5
    d = report.as_dict()
    assert d["legacy_ratio_pct"] == 50.0
    assert d["alert"] is False  # 样本 < 10 不告警


@pytest.mark.asyncio
async def test_legacy_ratio_by_task(mem_session):
    report = await report_legacy_aggregate_ratio(mem_session, task_id="task_l")
    assert report.legacy_spans == 1
    assert report.task_id == "task_l"
