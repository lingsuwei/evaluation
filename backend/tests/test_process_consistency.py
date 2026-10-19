"""过程一致性总览读模型单测。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, CheckpointResult, ProcessCheckpointTemplate
from services.eval_context import build_eval_context
from services.process_consistency_service import (
    build_process_consistency,
    score_metrics_from_consistency_payload,
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


async def _seed_multi_run_task(session: AsyncSession):
    session.add_all(
        [
            ProcessCheckpointTemplate(
                id="tpl_plan",
                logical_agent_id="logical-a",
                name="规划",
                order=1,
                required=True,
                enforce_order=True,
                match_rules='{"mode":"any","rules":[]}',
                enabled=True,
            ),
            ProcessCheckpointTemplate(
                id="tpl_tool",
                logical_agent_id="logical-a",
                name="检索",
                order=2,
                required=True,
                match_rules='{"mode":"any","rules":[]}',
                enabled=True,
            ),
        ]
    )
    for idx, run_id in enumerate(["run_q0_r0", "run_q0_r1"]):
        session.add(
            AgentRun(
                id=run_id,
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t_pc", dataset_id="d1", question_id=0, run_index=idx
                ),
                task_id="t_pc",
                dataset_id="d1",
                question_id=0,
                run_index=idx,
                agent_id="ag1",
                agent_name="Agent A",
                logical_agent_id="logical-a",
                status="success",
                started_at=datetime.utcnow(),
                trace_sync_status="synced",
                agent_snapshot="{}",
            )
        )
    session.add_all(
        [
            CheckpointResult(
                id="cpr_r0_plan",
                agent_run_id="run_q0_r0",
                checkpoint_id="tpl_plan",
                passed=True,
                evidence={"hit": True, "order_ok": True},
            ),
            CheckpointResult(
                id="cpr_r0_tool",
                agent_run_id="run_q0_r0",
                checkpoint_id="tpl_tool",
                passed=True,
                evidence={"hit": True, "order_ok": True},
            ),
            CheckpointResult(
                id="cpr_r1_plan",
                agent_run_id="run_q0_r1",
                checkpoint_id="tpl_plan",
                passed=True,
                evidence={"hit": True, "order_ok": True},
            ),
            CheckpointResult(
                id="cpr_r1_tool",
                agent_run_id="run_q0_r1",
                checkpoint_id="tpl_tool",
                passed=False,
                evidence={"hit": True, "order_ok": False},
            ),
        ]
    )
    await session.commit()


@pytest.mark.asyncio
async def test_build_process_consistency_chain_status(mem_session):
    await _seed_multi_run_task(mem_session)
    payload = await build_process_consistency(mem_session, "t_pc")

    assert len(payload["questions"]) == 1
    q = payload["questions"][0]
    assert q["is_multi_run"] is True
    assert q["required_status"] == "red"
    assert q["chain_status"] == "有轮未过"
    assert q["rounds_all_ok"] == 1
    assert q["round_count"] == 2
    assert q["all_rounds_ok"] is False
    assert "structure_status" not in q
    assert "step_keys" not in q["runs"][0]

    summary = payload["summary"]
    assert summary["multi_run_questions"] == 1
    assert summary["chain_pass_rate"] == 0.0
    assert summary["questions_all_rounds_ok"] == 0
    assert summary["question_count"] == 1
    assert summary["red"] == 1


@pytest.mark.asyncio
async def test_process_score_aligns_with_summary_buckets(mem_session):
    await _seed_multi_run_task(mem_session)
    payload = await build_process_consistency(mem_session, "t_pc")
    metrics = score_metrics_from_consistency_payload(payload)
    summary = payload["summary"]
    n = summary["green"] + summary["yellow"] + summary["red"]
    assert n > 0
    expected = (
        summary["green"] * 1.0 + summary["yellow"] * 0.75 + summary["red"] * 0.0
    ) / n
    assert metrics["process_score"] == pytest.approx(expected)


@pytest.mark.asyncio
async def test_build_process_consistency_empty_task(mem_session):
    payload = await build_process_consistency(mem_session, "missing")
    assert payload["questions"] == []
    assert payload["summary"]["total_runs"] == 0
    assert payload["summary"]["chain_pass_rate"] is None


@pytest.mark.asyncio
async def test_cancelled_task_runs_without_checkpoint_results(mem_session):
    """任务被取消：Run 存在但无 CheckpointResult，不应视为"必达全通过"。

    回归 bug：items 为空时 _chain_ok_for_run 返回 True、required_all_passed 为 True，
    导致 required_status=green、chain_pass_rate=1.0、verdict=pass，与前端 0% 通过率矛盾。
    """
    # 复用模板但不写 CheckpointResult（模拟取消）
    session = mem_session
    session.add_all(
        [
            ProcessCheckpointTemplate(
                id="tpl_plan",
                logical_agent_id="logical-a",
                name="规划",
                order=1,
                required=True,
                enforce_order=True,
                match_rules='{"mode":"any","rules":[]}',
                enabled=True,
            ),
            ProcessCheckpointTemplate(
                id="tpl_tool",
                logical_agent_id="logical-a",
                name="检索",
                order=2,
                required=True,
                match_rules='{"mode":"any","rules":[]}',
                enabled=True,
            ),
        ]
    )
    for idx, run_id in enumerate(["run_q0_r0", "run_q0_r1"]):
        session.add(
            AgentRun(
                id=run_id,
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t_cancel", dataset_id="d1", question_id=0, run_index=idx
                ),
                task_id="t_cancel",
                dataset_id="d1",
                question_id=0,
                run_index=idx,
                agent_id="ag1",
                agent_name="Agent A",
                logical_agent_id="logical-a",
                status="cancelled",
                started_at=datetime.utcnow(),
                trace_sync_status="pending",
                agent_snapshot="{}",
            )
        )
    await session.commit()

    payload = await build_process_consistency(session, "t_cancel")
    assert len(payload["questions"]) == 1
    q = payload["questions"][0]
    # 无过程点证据 → 不能判绿
    assert q["required_status"] == "red"
    assert q["chain_pass_rate"] == 0.0
    assert q["all_rounds_ok"] is False

    summary = payload["summary"]
    assert summary["red"] == 1
    assert summary["green"] == 0
    assert summary["chain_pass_rate"] == 0.0
    assert summary["questions_all_rounds_ok"] == 0

    metrics = score_metrics_from_consistency_payload(payload)
    assert metrics["process_score"] is None or metrics["process_score"] == 0.0
