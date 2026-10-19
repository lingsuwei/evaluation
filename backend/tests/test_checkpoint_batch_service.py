"""过程点批量评估单测（P2 E-01～E-03）。"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database import AgentRun, Base, CheckpointResult, EvaluationTask, ProcessCheckpointTemplate, RunSpanNode
from services.checkpoint_batch_service import (
    batch_evaluate_checkpoints_for_run,
    compute_checkpoint_passed,
    evaluate_template_on_nodes,
    warn_legacy_name_only_rules,
)
from services.eval_context import build_eval_context
from services.runtime_fact_builder import build_facts_for_trace


@pytest.fixture
async def mem_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def test_compute_checkpoint_passed():
    assert compute_checkpoint_passed(hit=True, required=True, order_ok=True) is True
    assert compute_checkpoint_passed(hit=False, required=True, order_ok=None) is False
    assert compute_checkpoint_passed(hit=False, required=False, order_ok=None) is True
    assert compute_checkpoint_passed(hit=True, required=True, order_ok=False) is False


def test_warn_legacy_name_only_rules():
    msg = warn_legacy_name_only_rules(
        {"mode": "any", "rules": [{"type": "observation_name", "op": "eq", "value": "x"}]}
    )
    assert msg is not None
    assert "jsonpath" in msg


@pytest.mark.asyncio
async def test_evaluate_template_jsonpath_on_span_attributes(mem_session):
    tpl = ProcessCheckpointTemplate(
        id="tpl_json",
        logical_agent_id="la_cp",
        name="检索步骤",
        match_rules=json.dumps(
            {
                "mode": "any",
                "rules": [
                    {
                        "type": "jsonpath",
                        "path": "$.attributes.agent.step.id",
                        "op": "eq",
                        "value": "retrieve",
                    }
                ],
            }
        ),
        enabled=True,
        required=True,
    )
    mem_session.add(tpl)
    await mem_session.flush()

    nodes = [
        RunSpanNode(
            id="span_1",
            agent_run_id="run_cp",
            observation_id="obs_1",
            sibling_order=0,
            step_index=0,
            node_type="span",
            name="internal",
            attributes={"agent.step.id": "retrieve"},
        ),
        RunSpanNode(
            id="span_2",
            agent_run_id="run_cp",
            observation_id="obs_2",
            sibling_order=0,
            step_index=1,
            node_type="generation",
            name="answer",
            attributes={},
        ),
    ]
    ev = evaluate_template_on_nodes(tpl, nodes)
    assert ev["hit"] is True
    assert ev["passed"] is True
    assert "obs_1" in (ev.get("evidence") or {}).get("matched_ids", [])


@pytest.mark.asyncio
async def test_batch_persist_checkpoint_results(mem_session):
    run = AgentRun(
        id="run_cp_batch",
        origin="evaluation",
        eval_context=build_eval_context(
            task_id="t1", dataset_id="d1", question_id=0, run_index=0
        ),
        task_id="t1",
        dataset_id="d1",
        question_id=0,
        run_index=0,
        agent_id="ag1",
        logical_agent_id="la_cp",
        status="success",
        started_at=datetime.utcnow(),
        agent_snapshot="{}",
    )
    mem_session.add(run)
    mem_session.add(
        ProcessCheckpointTemplate(
            id="tpl_hit",
            logical_agent_id="la_cp",
            name="must_search",
            match_rules=json.dumps(
                {
                    "mode": "any",
                    "rules": [
                        {
                            "type": "jsonpath",
                            "path": "$.attributes.agent.tool.name",
                            "op": "eq",
                            "value": "kb",
                        }
                    ],
                }
            ),
            enabled=True,
            required=True,
        )
    )
    await mem_session.flush()

    nodes = [
        RunSpanNode(
            id="span_kb",
            agent_run_id="run_cp_batch",
            observation_id="obs_kb",
            sibling_order=0,
            step_index=0,
            attributes={"agent.tool.name": "kb"},
        )
    ]
    evals = await batch_evaluate_checkpoints_for_run(mem_session, run, nodes)
    await mem_session.commit()

    assert len(evals) == 1
    assert evals[0]["passed"] is True
    rows = (await mem_session.execute(select(CheckpointResult))).scalars().all()
    assert len(rows) == 1
    assert rows[0].checkpoint_id == "tpl_hit"
    assert rows[0].passed is True


@pytest.mark.asyncio
async def test_build_facts_includes_checkpoint_batch(mem_session):
    run = AgentRun(
        id="run_facts_cp",
        origin="evaluation",
        eval_context=build_eval_context(
            task_id="t2", dataset_id="d1", question_id=0, run_index=0
        ),
        task_id="t2",
        dataset_id="d1",
        question_id=0,
        run_index=0,
        agent_id="ag1",
        logical_agent_id="la_facts",
        status="success",
        started_at=datetime.utcnow(),
        trace_id="tr_cp",
        agent_snapshot="{}",
    )
    mem_session.add(run)
    mem_session.add(
        ProcessCheckpointTemplate(
            id="tpl_facts",
            logical_agent_id="la_facts",
            name="step_main",
            match_rules=json.dumps(
                {
                    "mode": "any",
                    "rules": [
                        {
                            "type": "jsonpath",
                            "path": "$.attributes.agent.step.id",
                            "op": "eq",
                            "value": "main",
                        }
                    ],
                }
            ),
            enabled=True,
            required=True,
        )
    )
    await mem_session.commit()

    trace = {
        "id": "tr_cp",
        "observations": [
            {
                "id": "obs_m",
                "name": "s",
                "type": "SPAN",
                "metadata": {"agent.step.id": "main"},
            },
        ],
    }
    out = await build_facts_for_trace(mem_session, run, trace)
    await mem_session.commit()

    assert out["checkpoints"] == 1
    cps = (
        await mem_session.execute(
            select(CheckpointResult).where(CheckpointResult.agent_run_id == "run_facts_cp")
        )
    ).scalars().all()
    assert len(cps) == 1
    assert cps[0].passed is True


@pytest.mark.asyncio
async def test_build_facts_skips_checkpoint_when_process_disabled(mem_session):
    """enable_process_consistency=False → build_facts_for_trace 不物化 CheckpointResult。"""
    mem_session.add(
        EvaluationTask(
            id="t_pc_off",
            dataset_id="d1",
            models='["glm"]',
            status="completed",
            evaluation_mode="online",
            agent_id="ag1",
            enable_process_consistency=False,
        )
    )
    run = AgentRun(
        id="run_pc_off",
        origin="evaluation",
        eval_context=build_eval_context(
            task_id="t_pc_off", dataset_id="d1", question_id=0, run_index=0
        ),
        task_id="t_pc_off",
        dataset_id="d1",
        question_id=0,
        run_index=0,
        agent_id="ag1",
        logical_agent_id="la_facts",
        status="success",
        started_at=datetime.utcnow(),
        trace_id="tr_pc_off",
        agent_snapshot="{}",
    )
    mem_session.add(run)
    mem_session.add(
        ProcessCheckpointTemplate(
            id="tpl_pc_off",
            logical_agent_id="la_facts",
            name="step_main",
            match_rules=json.dumps(
                {
                    "mode": "any",
                    "rules": [
                        {
                            "type": "jsonpath",
                            "path": "$.attributes.agent.step.id",
                            "op": "eq",
                            "value": "main",
                        }
                    ],
                }
            ),
            enabled=True,
            required=True,
        )
    )
    await mem_session.commit()

    trace = {
        "id": "tr_pc_off",
        "observations": [
            {
                "id": "obs_m",
                "name": "s",
                "type": "SPAN",
                "metadata": {"agent.step.id": "main"},
            },
        ],
    }
    out = await build_facts_for_trace(mem_session, run, trace)
    await mem_session.commit()

    # checkpoints 统计应为 0
    assert out["checkpoints"] == 0
    # 不应物化任何 CheckpointResult
    cps = (
        await mem_session.execute(
            select(CheckpointResult).where(CheckpointResult.agent_run_id == "run_pc_off")
        )
    ).scalars().all()
    assert len(cps) == 0
