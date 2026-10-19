"""过程点合规读模型单测（M3 Tab）。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, CheckpointResult, ProcessCheckpointTemplate
from services.checkpoint_result_service import (
    build_run_checkpoint_report,
    build_task_checkpoint_report,
    compute_checkpoint_outcome,
)
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


async def _seed_checkpoint_data(session: AsyncSession):
    session.add(
        AgentRun(
            id="run_cp1",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t_cp", dataset_id="d1", question_id=0, run_index=0
            ),
            task_id="t_cp",
            dataset_id="d1",
            question_id=0,
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
            ProcessCheckpointTemplate(
                id="tpl_plan",
                logical_agent_id="logical-a",
                name="规划",
                order=1,
                required=True,
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
            ProcessCheckpointTemplate(
                id="tpl_opt",
                logical_agent_id="logical-a",
                name="可选增强",
                order=3,
                required=False,
                match_rules='{"mode":"any","rules":[]}',
                enabled=True,
            ),
        ]
    )
    session.add_all(
        [
            CheckpointResult(
                id="cpr1",
                agent_run_id="run_cp1",
                checkpoint_id="tpl_tool",
                passed=False,
                evidence={"hit": True, "order_ok": False, "matched_ids": ["o2"]},
            ),
            CheckpointResult(
                id="cpr2",
                agent_run_id="run_cp1",
                checkpoint_id="tpl_plan",
                passed=True,
                evidence={"hit": True, "order_ok": True, "matched_ids": ["o1"]},
            ),
            CheckpointResult(
                id="cpr3",
                agent_run_id="run_cp1",
                checkpoint_id="tpl_opt",
                passed=True,
                evidence={"hit": False, "matched_ids": []},
            ),
        ]
    )
    await session.commit()


@pytest.mark.asyncio
async def test_build_run_checkpoint_report_sorted_by_order(mem_session):
    await _seed_checkpoint_data(mem_session)
    report = await build_run_checkpoint_report(mem_session, "run_cp1")
    assert report is not None
    assert report["checkpoint_total"] == 3
    assert report["checkpoint_passed"] == 2
    assert report["required_total"] == 2
    assert report["required_passed"] == 1
    assert report["required_compliance_rate"] == 0.5
    assert report["compliance_rate"] == 0.5
    assert report["optional_total"] == 1
    assert report["optional_hit"] == 0
    assert report["optional_hit_rate"] == 0.0
    names = [i["name"] for i in report["items"]]
    assert names == ["规划", "检索", "可选增强"]
    assert report["items"][1]["order_ok"] is False
    assert report["items"][2]["outcome"] == "optional_skip"


@pytest.mark.asyncio
async def test_build_task_checkpoint_report(mem_session):
    await _seed_checkpoint_data(mem_session)
    report = await build_task_checkpoint_report(mem_session, "t_cp")
    assert report["summary"]["run_count"] == 1
    assert report["summary"]["runs_with_checkpoints"] == 1
    assert report["summary"]["avg_compliance_rate"] == 0.5
    assert report["summary"]["avg_required_compliance_rate"] == 0.5
    assert report["summary"]["avg_optional_hit_rate"] == 0.0
    assert len(report["runs"]) == 1


def test_compute_checkpoint_outcome_optional_skip():
    assert compute_checkpoint_outcome(required=False, hit=False, passed=True) == "optional_skip"
    assert compute_checkpoint_outcome(required=True, hit=False, passed=False) == "required_miss"
