"""observation_latency / span_tree_builder 逐步耗时补全单测。"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta

from services.observation_latency import (
    latency_ms_from_interval,
    resolve_observation_latency_ms,
    resolve_span_node_latency_ms,
)
from services.span_tree_builder import build_span_node_rows


def test_latency_from_start_end_iso():
    obs = {
        "startTime": "2026-01-01T00:00:00.000Z",
        "endTime": "2026-01-01T00:00:01.250Z",
    }
    assert resolve_observation_latency_ms(obs) == 1250


def test_latency_langfuse_seconds_fraction():
    obs = {"latency": 0.581}
    assert resolve_observation_latency_ms(obs) == 581


def test_latency_explicit_ms_field():
    obs = {"latencyMs": 432}
    assert resolve_observation_latency_ms(obs) == 432


def test_latency_large_integer_as_ms():
    obs = {"latency": 1200}
    assert resolve_observation_latency_ms(obs) == 1200


def test_latency_small_integer_as_seconds():
    obs = {"latency": 3}
    assert resolve_observation_latency_ms(obs) == 3000


def test_start_end_takes_precedence_over_wrong_latency():
    obs = {
        "startTime": "2026-01-01T00:00:00Z",
        "endTime": "2026-01-01T00:00:02Z",
        "latency": 999999,
    }
    assert resolve_observation_latency_ms(obs) == 2000


def test_resolve_span_node_from_stored_times():
    start = datetime(2026, 1, 1, 0, 0, 0)
    end = start + timedelta(milliseconds=850)
    assert (
        resolve_span_node_latency_ms(
            latency_ms=None,
            started_at=start,
            ended_at=end,
        )
        == 850
    )


def test_build_span_node_rows_persists_latency_and_times():
    trace = {
        "observations": [
            {
                "id": "gen1",
                "name": "answer",
                "type": "GENERATION",
                "startTime": "2026-05-01T10:00:00.000Z",
                "endTime": "2026-05-01T10:00:00.750Z",
                "usage": {"total": 12},
            },
            {
                "id": "tool1",
                "name": "search",
                "type": "SPAN",
                "latency": 0.42,
                "metadata": {"agent.tool.name": "kb_search"},
            },
        ]
    }
    rows = build_span_node_rows(trace, "run_lat", allow_legacy_aggregate_key=False)
    by_obs = {r["observation_id"]: r for r in rows}
    assert by_obs["gen1"]["latency_ms"] == 750
    assert isinstance(by_obs["gen1"]["started_at"], datetime)
    assert isinstance(by_obs["gen1"]["ended_at"], datetime)
    assert by_obs["tool1"]["latency_ms"] == 420


def test_latency_ms_from_interval_helper():
    assert latency_ms_from_interval(None, None) is None
    assert (
        latency_ms_from_interval(
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00.100Z",
        )
        == 100
    )


@pytest.mark.asyncio
async def test_span_tree_read_backfills_latency_from_times():
    """已物化节点 latency_ms 为空时，读 API 应从 started_at/ended_at 补全。"""
    from datetime import datetime, timedelta

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from database import AgentRun, Base, RunSpanNode
    from services.eval_context import build_eval_context
    from services.span_tree_service import build_span_tree_for_run

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        start = datetime(2026, 1, 1, 0, 0, 0)
        session.add(
            AgentRun(
                id="run_backfill",
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t1", dataset_id="d1", question_id=0, run_index=0
                ),
                task_id="t1",
                dataset_id="d1",
                question_id=0,
                agent_id="ag1",
                status="success",
                started_at=start,
                trace_id="tr_bf",
                agent_snapshot="{}",
            )
        )
        session.add(
            RunSpanNode(
                id="sn_bf",
                agent_run_id="run_backfill",
                observation_id="obs_bf",
                aggregate_key="llm:plan",
                semantic_role="llm",
                sibling_order=0,
                step_index=0,
                latency_ms=None,
                started_at=start,
                ended_at=start + timedelta(milliseconds=640),
            )
        )
        await session.commit()
        tree = await build_span_tree_for_run(session, "run_backfill")
        assert tree is not None
        assert tree["nodes"][0]["latency_ms"] == 640
    await engine.dispose()
