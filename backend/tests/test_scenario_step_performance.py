"""scenario_step_performance_service 单测。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import (
    AgentRun,
    Base,
    RunSpanNode,
    ScenarioEvalStep,
    ScenarioStepSpanRef,
)
from services.eval_context import build_eval_context
from services.scenario_step_performance_service import build_scenario_step_performance_summary


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
async def test_step_performance_uses_bound_span_not_run_average(mem_session):
    """各子步骤应取绑定 Span 的耗时，而非整 Run 均摊。"""
    start = datetime(2026, 1, 1, 0, 0, 0)
    mem_session.add(
        AgentRun(
            id="run_perf",
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
            tokens_total=5000,
            agent_snapshot="{}",
        )
    )
    mem_session.add_all(
        [
            ScenarioEvalStep(
                id="step_a",
                template_id="tpl1",
                step_key="S1",
                name="步骤一",
                step_order=1,
                weight=1.0,
            ),
            ScenarioEvalStep(
                id="step_b",
                template_id="tpl1",
                step_key="S2",
                name="步骤二",
                step_order=2,
                weight=1.0,
            ),
        ]
    )
    mem_session.add_all(
        [
            RunSpanNode(
                id="sn_a",
                agent_run_id="run_perf",
                observation_id="obs_a",
                aggregate_key="tool:a",
                semantic_role="tool",
                sibling_order=0,
                step_index=0,
                latency_ms=1000,
                tokens=800,
            ),
            RunSpanNode(
                id="sn_b",
                agent_run_id="run_perf",
                observation_id="obs_b",
                aggregate_key="tool:b",
                semantic_role="tool",
                sibling_order=1,
                step_index=1,
                latency_ms=2000,
                tokens=1200,
            ),
        ]
    )
    mem_session.add_all(
        [
            ScenarioStepSpanRef(
                id="ref_a",
                agent_run_id="run_perf",
                step_id="step_a",
                observation_id="obs_a",
                span_node_id="sn_a",
            ),
            ScenarioStepSpanRef(
                id="ref_b",
                agent_run_id="run_perf",
                step_id="step_b",
                observation_id="obs_b",
                span_node_id="sn_b",
            ),
        ]
    )
    await mem_session.commit()

    question_rows = [
        {
            "cells": {
                "m1": {
                    "agent_run_id": "run_perf",
                    "steps": [
                        {"step_id": "step_a", "step_key": "S1", "name": "步骤一"},
                        {"step_id": "step_b", "step_key": "S2", "name": "步骤二"},
                    ],
                }
            }
        }
    ]
    summary = await build_scenario_step_performance_summary(
        mem_session, question_rows, ["m1"]
    )
    by_key = {row["step_key"]: row for row in summary}
    assert by_key["S1"]["avg_latency_ms"] == 1000.0
    assert by_key["S2"]["avg_latency_ms"] == 2000.0
    assert by_key["S1"]["avg_tokens"] == 800.0
    assert by_key["S2"]["avg_tokens"] == 1200.0
    assert by_key["S1"]["sample_count"] == 1
    assert by_key["S2"]["sample_count"] == 1


@pytest.mark.asyncio
async def test_step_performance_from_trace_binding_observation(mem_session):
    """无 span_ref 时回退 trace_binding.observation_id。"""
    mem_session.add(
        AgentRun(
            id="run_tb",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t1", dataset_id="d1", question_id=0, run_index=0
            ),
            task_id="t1",
            dataset_id="d1",
            question_id=0,
            agent_id="ag1",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    t0 = datetime(2026, 1, 1, 0, 0, 0)
    mem_session.add(
        RunSpanNode(
            id="sn_x",
            agent_run_id="run_tb",
            observation_id="obs_x",
            sibling_order=0,
            step_index=0,
            latency_ms=None,
            tokens=99,
            started_at=t0,
            ended_at=t0 + timedelta(milliseconds=640),
        )
    )
    await mem_session.commit()

    summary = await build_scenario_step_performance_summary(
        mem_session,
        [
            {
                "cells": {
                    "m1": {
                        "agent_run_id": "run_tb",
                        "steps": [
                            {
                                "step_key": "SX",
                                "name": "绑定步",
                                "trace_binding": {"observation_id": "obs_x"},
                            }
                        ],
                    }
                }
            }
        ],
        ["m1"],
    )
    assert len(summary) == 1
    assert summary[0]["avg_latency_ms"] == 640.0
    assert summary[0]["avg_tokens"] == 99.0
