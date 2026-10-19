"""P2 ORM 与 snapshot 工具单测。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database import AgentRun, Base, CheckpointResult, RunMetric, RunSpanNode
from services.snapshot_utils import MAX_SNAPSHOT_BYTES, bounded_snapshot_json


@pytest.fixture
async def mem_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def test_bounded_snapshot_within_limit():
    data = {"question": "hello", "run_index": 0}
    out = bounded_snapshot_json(data)
    assert out == data
    assert len(json.dumps(out, ensure_ascii=False).encode("utf-8")) <= MAX_SNAPSHOT_BYTES


def test_bounded_snapshot_truncates_large_payload():
    data = {"question": "x" * 20_000}
    out = bounded_snapshot_json(data)
    assert out is not None
    assert out.get("_truncated") is True or isinstance(out, str)


@pytest.mark.asyncio
async def test_p2_models_persist(mem_session):
    run = AgentRun(
        id="run_p2_test",
        agent_id="agent_1",
        status="success",
        started_at=__import__("datetime").datetime.utcnow(),
        agent_snapshot="{}",
        input_snapshot={"question": "q1"},
        output_snapshot={"answer": "a1"},
    )
    mem_session.add(run)
    await mem_session.flush()

    span = RunSpanNode(
        id="span_1",
        agent_run_id=run.id,
        observation_id="obs_1",
        sibling_order=0,
        semantic_role="llm",
        summary_text="short",
    )
    metric = RunMetric(
        id="m_1",
        agent_run_id=run.id,
        metric_key="runtime.tokens_total",
        source="runtime",
        value_num=128.0,
    )
    cp = CheckpointResult(
        id="cp_1",
        agent_run_id=run.id,
        checkpoint_id="tpl_1",
        passed=True,
    )
    mem_session.add_all([span, metric, cp])
    await mem_session.commit()

    assert span.agent_run_id == run.id
    assert metric.metric_key == "runtime.tokens_total"
    assert cp.passed is True
