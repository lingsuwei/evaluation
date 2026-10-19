"""跨任务（跨版本）单题 Step 序列对比（P1）。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, EvaluationTask
from services.cross_task_step_compare_service import build_cross_task_step_compare
from services.eval_context import build_eval_context
from services.runtime_fact_builder import apply_runtime_facts_from_trace

pytestmark = pytest.mark.p3

QK = "qk_cross"


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _run(run_id: str, task_id: str, agent_id: str, trace_id: str) -> AgentRun:
    return AgentRun(
        id=run_id,
        origin="evaluation",
        eval_context=build_eval_context(
            task_id=task_id,
            dataset_id="ds_x",
            question_id=0,
            question_key=QK,
            dataset_item_id="ditem_x",
        ),
        task_id=task_id,
        dataset_id="ds_x",
        question_id=0,
        run_index=0,
        agent_id=agent_id,
        status="success",
        started_at=datetime.utcnow(),
        agent_snapshot="{}",
        trace_id=trace_id,
    )


async def _seed(session: AsyncSession) -> None:
    session.add_all(
        [
            EvaluationTask(
                id="t_a", dataset_id="ds_x", status="completed", agent_id="ag_a",
                completed_at=datetime.utcnow(),
            ),
            EvaluationTask(
                id="t_b", dataset_id="ds_x", status="completed", agent_id="ag_b",
                completed_at=datetime.utcnow(),
            ),
        ]
    )
    run_a = _run("run_a", "t_a", "ag_a", "tr_a")
    run_b = _run("run_b", "t_b", "ag_b", "tr_b")
    session.add_all([run_a, run_b])
    await session.flush()

    trace_a = {
        "observations": [
            {"id": "a1", "type": "GENERATION", "name": "plan", "metadata": {"agent.step.id": "plan"}},
            {"id": "a2", "type": "TOOL", "name": "kb", "metadata": {"agent.tool.name": "kb_search"}},
            {"id": "a3", "type": "GENERATION", "name": "answer", "metadata": {"agent.step.id": "answer"}},
        ]
    }
    trace_b = {
        "observations": [
            {"id": "b1", "type": "GENERATION", "name": "plan", "metadata": {"agent.step.id": "plan"}},
            {"id": "b2", "type": "GENERATION", "name": "answer", "metadata": {"agent.step.id": "answer"}},
        ]
    }
    await apply_runtime_facts_from_trace(session, run_a, trace_a)
    await apply_runtime_facts_from_trace(session, run_b, trace_b)
    await session.commit()


@pytest.mark.asyncio
async def test_cross_task_step_compare(mem_session):
    await _seed(mem_session)
    payload = await build_cross_task_step_compare(
        mem_session,
        question_key=QK,
        baseline_task_id="t_a",
        compare_task_id="t_b",
    )
    assert payload is not None
    assert payload["question_key"] == QK
    assert payload["baseline_task_id"] == "t_a"
    assert payload["compare_task_id"] == "t_b"
    assert payload["left"]["trace_id"] == "tr_a"
    assert payload["right"]["trace_id"] == "tr_b"
    # t_a 比 t_b 多一个 TOOL 步骤，应识别为 removed
    assert any(b["type"] == "removed" for b in payload["diff_summary"])
    assert len(payload["aligned_steps"]) >= 2
    # 跨任务下清空多轮稳定性
    assert payload["round_stability"] == {"columns": [], "rows": []}
    assert payload["structure_summary"] is None


@pytest.mark.asyncio
async def test_cross_task_step_compare_missing_run(mem_session):
    await _seed(mem_session)
    payload = await build_cross_task_step_compare(
        mem_session,
        question_key="qk_not_exist",
        baseline_task_id="t_a",
        compare_task_id="t_b",
    )
    assert payload is None
