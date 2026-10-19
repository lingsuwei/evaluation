"""P3 Diff Calculator 单测（主流程 + 异常）。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import (
    AgentRun,
    Base,
    CheckpointResult,
    EvaluationResult,
    RunSpanNode,
)
from services.diff.checkpoint_diff_calculator import CheckpointDiffCalculator
from services.diff.cost_diff_calculator import CostDiffCalculator
from services.diff.diff_registry import resolve_diff_types
from services.diff.event_agg_diff_calculator import EventAggDiffCalculator
from services.diff.outcome_diff_calculator import OutcomeDiffCalculator
from services.eval_context import build_eval_context

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


async def _seed_runs(session: AsyncSession):
    started = datetime.utcnow()
    ended = started + timedelta(seconds=3)
    session.add_all(
        [
            AgentRun(
                id="run_left",
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t1", dataset_id="d1", question_id=0, run_index=0
                ),
                task_id="t1",
                dataset_id="d1",
                question_id=0,
                agent_id="ag_a",
                status="success",
                started_at=started,
                ended_at=ended,
                tokens_total=100,
                agent_snapshot="{}",
                trace_sync_status="synced",
            ),
            AgentRun(
                id="run_right",
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t1", dataset_id="d1", question_id=0, run_index=0
                ),
                task_id="t1",
                dataset_id="d1",
                question_id=0,
                agent_id="ag_b",
                status="success",
                started_at=started,
                ended_at=started + timedelta(seconds=5),
                tokens_total=80,
                agent_snapshot="{}",
                trace_sync_status="synced",
            ),
        ]
    )
    session.add_all(
        [
            EvaluationResult(
                task_id="t1",
                question_id=0,
                model="m",
                metric_name="correctness",
                score=0.7,
                avg_score=0.7,
                passed=True,
                agent_run_id="run_left",
            ),
            EvaluationResult(
                task_id="t1",
                question_id=0,
                model="m",
                metric_name="correctness",
                score=0.9,
                avg_score=0.9,
                passed=True,
                agent_run_id="run_right",
            ),
            RunSpanNode(
                id="sn_l1",
                agent_run_id="run_left",
                observation_id="o1",
                aggregate_key="step:plan",
                semantic_role="planning",
                summary_text="left plan summary",
                latency_ms=200,
                tokens=10,
                sibling_order=0,
                step_index=0,
            ),
            RunSpanNode(
                id="sn_l2",
                agent_run_id="run_left",
                observation_id="o2",
                aggregate_key="tool:search",
                semantic_role="tool_call",
                summary_text="search left",
                latency_ms=300,
                tokens=0,
                sibling_order=1,
                step_index=1,
            ),
            RunSpanNode(
                id="sn_r1",
                agent_run_id="run_right",
                observation_id="o3",
                aggregate_key="step:plan",
                semantic_role="planning",
                summary_text="right plan summary",
                latency_ms=250,
                tokens=12,
                sibling_order=0,
                step_index=0,
            ),
            CheckpointResult(
                id="cp_l",
                agent_run_id="run_left",
                checkpoint_id="chk_1",
                passed=True,
            ),
            CheckpointResult(
                id="cp_r",
                agent_run_id="run_right",
                checkpoint_id="chk_1",
                passed=False,
            ),
        ]
    )
    await session.commit()


@pytest.mark.asyncio
async def test_outcome_diff_calculator(mem_session):
    await _seed_runs(mem_session)
    calc = OutcomeDiffCalculator()
    payload = await calc.compute(
        mem_session, left_run_id="run_left", right_run_id="run_right"
    )
    assert payload["metrics"]["correctness"]["left"] == pytest.approx(0.7)
    assert payload["metrics"]["correctness"]["right"] == pytest.approx(0.9)
    assert payload["metrics"]["correctness"]["delta"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_cost_diff_calculator(mem_session):
    await _seed_runs(mem_session)
    calc = CostDiffCalculator()
    payload = await calc.compute(
        mem_session, left_run_id="run_left", right_run_id="run_right"
    )
    assert payload["run_level"]["tokens_total"]["delta"] == -20
    keys = {row["aggregate_key"] for row in payload["by_aggregate_key"]}
    assert "step:plan" in keys
    assert "tool:search" in keys


@pytest.mark.asyncio
async def test_cost_diff_run_not_found(mem_session):
    calc = CostDiffCalculator()
    with pytest.raises(ValueError, match="not found"):
        await calc.compute(
            mem_session, left_run_id="missing", right_run_id="also_missing"
        )


@pytest.mark.asyncio
async def test_event_agg_diff_calculator(mem_session):
    await _seed_runs(mem_session)
    calc = EventAggDiffCalculator()
    payload = await calc.compute(
        mem_session, left_run_id="run_left", right_run_id="run_right"
    )
    events = {e["aggregate_key"]: e for e in payload["events"]}
    assert events["tool:search"]["left"]["count"] == 1
    assert events["tool:search"]["right"]["count"] == 0


@pytest.mark.asyncio
async def test_checkpoint_diff_calculator(mem_session):
    await _seed_runs(mem_session)
    calc = CheckpointDiffCalculator()
    payload = await calc.compute(
        mem_session, left_run_id="run_left", right_run_id="run_right"
    )
    assert payload["checkpoints"]["chk_1"]["passed_left"] is True
    assert payload["checkpoints"]["chk_1"]["passed_right"] is False
    assert payload["checkpoints"]["chk_1"]["same"] is False


def test_resolve_diff_types_unknown():
    with pytest.raises(ValueError, match="unknown diff_type"):
        resolve_diff_types(["outcome_diff", "bad_type"])


def test_resolve_experiment_diff_types_default():
    from services.diff.diff_registry import resolve_experiment_diff_types
    from services.diff.diff_types import VERSION_EXPERIMENT_DIFF_TYPES

    assert resolve_experiment_diff_types(None) == list(VERSION_EXPERIMENT_DIFF_TYPES)
    assert resolve_experiment_diff_types(["outcome_diff"]) == ["outcome_diff"]
