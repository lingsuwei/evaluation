"""场景子步骤版本对比融合 Tab — 需求验收用例（P0–P3）。"""
from __future__ import annotations

from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Agent, AgentRun, Base, Dataset, EvaluationResult, EvaluationTask, get_session
from main import app
from services.cross_task_cockpit_service import CrossTaskCockpitError
from services.diff_summary_service import get_task_diff_highlights
from services.eval_context import build_eval_context
from services.run_comparison_service import compare_runs
from services.task_scenario_matrix_service import build_task_scenario_matrix
from tests.scenario_test_helpers import SAMPLE_A, SAMPLE_Q, seed_item_template
from tests.test_p3_diff_calculators import _seed_runs

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


@pytest.fixture
async def api_client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session

    async with factory() as session:
        await seed_item_template(session, dataset_id="ds_acc", dataset_name="Acc")
        session.add(
            EvaluationTask(
                id="t_acc",
                dataset_id="ds_acc",
                status="completed",
                agent_id="ag_a",
                completed_at=datetime.utcnow(),
            )
        )
        now = datetime.utcnow()
        session.add_all(
            [
                AgentRun(
                    id="run_acc_a",
                    origin="evaluation",
                    eval_context=build_eval_context(
                        task_id="t_acc",
                        dataset_id="ds_acc",
                        question_id=0,
                        question_key="qk_acc",
                    ),
                    task_id="t_acc",
                    dataset_id="ds_acc",
                    question_id=0,
                    agent_id="ag_a",
                    status="success",
                    started_at=now,
                    agent_snapshot="{}",
                ),
            ]
        )
        session.add(
            EvaluationResult(
                task_id="t_acc",
                question_id=0,
                metric_name="correctness",
                score=0.5,
                passed=True,
                agent_answer=SAMPLE_A,
                ground_truth=SAMPLE_A,
                question=SAMPLE_Q,
                agent_run_id="run_acc_a",
            )
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_accept_p3_task_detail_no_experiment_report(api_client):
    """P3：任务详情不再返回 experiment_report。"""
    res = await api_client.get("/tasks/t_acc/detail")
    assert res.status_code == 200
    body = res.json()
    assert "experiment_report" not in body
    assert "checkpoint_report" in body


@pytest.mark.asyncio
async def test_accept_p2_matrix_includes_diff_highlight(mem_session):
    """P2：matrix API 按题附带 diff_highlight 与 comparison_count。"""
    await _seed_runs(mem_session)
    session = mem_session
    await seed_item_template(session, dataset_id="d1", dataset_name="D")
    session.add_all(
        [
            Agent(id="ag_a", name="A", version="1", inference_config="{}"),
            Agent(id="ag_b", name="B", version="2", inference_config="{}"),
        ]
    )
    session.add(
        EvaluationTask(
            id="t1",
            dataset_id="d1",
            status="completed",
            agent_id="ag_a",
            completed_at=datetime.utcnow(),
        )
    )
    await session.commit()

    await compare_runs(
        session,
        left_run_id="run_left",
        right_run_id="run_right",
        diff_types=["semantic_diff", "structural_diff", "outcome_diff"],
    )
    await session.commit()

    payload = await build_task_scenario_matrix(session, "t1")
    assert payload["comparison_count"] >= 1
    row = payload["questions"][0]
    assert "diff_highlight" in row
    hl = row["diff_highlight"]
    assert hl is not None
    assert hl.get("comparison_id")
    assert hl.get("semantic") or hl.get("structural") or hl.get("outcome")


@pytest.mark.asyncio
async def test_accept_p1_matrix_two_versions_have_composite(mem_session):
    """P1：多版本 matrix 返回 L1 综合分与 member 列。"""
    session = mem_session
    sk = await seed_item_template(session, dataset_id="ds_intra", dataset_name="Intra")
    session.add_all(
        [
            Agent(id="ag_i1", name="Demo", version="1.0", inference_config="{}"),
            Agent(id="ag_i2", name="Demo", version="2.0", inference_config="{}"),
        ]
    )
    now = datetime.utcnow()
    session.add(
        EvaluationTask(
            id="t_intra",
            dataset_id="ds_intra",
            status="completed",
            agent_id="ag_i1",
            completed_at=now,
        )
    )
    ditem_id = "ditem_test01"
    qk = "qk_intra"
    session.add_all(
        [
            AgentRun(
                id="run_i1",
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t_intra",
                    dataset_id="ds_intra",
                    question_id=0,
                    question_key=qk,
                    dataset_item_id=ditem_id,
                    scenario_key=sk,
                ),
                task_id="t_intra",
                dataset_id="ds_intra",
                question_id=0,
                agent_id="ag_i1",
                status="success",
                started_at=now,
                agent_snapshot="{}",
            ),
            AgentRun(
                id="run_i2",
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id="t_intra",
                    dataset_id="ds_intra",
                    question_id=0,
                    question_key=qk,
                    dataset_item_id=ditem_id,
                    scenario_key=sk,
                ),
                task_id="t_intra",
                dataset_id="ds_intra",
                question_id=0,
                agent_id="ag_i2",
                status="success",
                started_at=now,
                agent_snapshot="{}",
            ),
        ]
    )
    session.add_all(
        [
            EvaluationResult(
                task_id="t_intra",
                question_id=0,
                metric_name="correctness",
                score=0.5,
                passed=True,
                agent_answer=SAMPLE_A,
                ground_truth=SAMPLE_A,
                question=SAMPLE_Q,
                agent_run_id="run_i1",
            ),
            EvaluationResult(
                task_id="t_intra",
                question_id=0,
                metric_name="correctness",
                score=0.5,
                passed=True,
                agent_answer=SAMPLE_A,
                ground_truth=SAMPLE_A,
                question=SAMPLE_Q,
                agent_run_id="run_i2",
            ),
        ]
    )
    await session.commit()

    payload = await build_task_scenario_matrix(session, "t_intra", scenario_key=sk)
    assert len(payload["member_ids"]) == 2
    row = payload["questions"][0]
    assert row["cells"]["ag_i1"]["composite_score"] is not None
    assert row["cells"]["ag_i2"]["composite_score"] is not None
    assert row["winner_label"]


@pytest.mark.asyncio
async def test_accept_negative_matrix_empty_task(mem_session):
    """反向：无 Run 任务返回 400 类错误。"""
    mem_session.add(
        EvaluationTask(
            id="t_empty",
            dataset_id="ds_x",
            status="completed",
            completed_at=datetime.utcnow(),
        )
    )
    await mem_session.commit()
    with pytest.raises(CrossTaskCockpitError, match="无可用"):
        await build_task_scenario_matrix(mem_session, "t_empty")


@pytest.mark.asyncio
async def test_accept_negative_diff_highlight_null_without_comparison(mem_session):
    """反向：未跑版本对比时 diff_highlight 为 null。"""
    session = mem_session
    await seed_item_template(session, dataset_id="ds_solo", dataset_name="Solo")
    session.add(Agent(id="ag_s", name="S", version="1", inference_config="{}"))
    now = datetime.utcnow()
    session.add(
        EvaluationTask(
            id="t_solo",
            dataset_id="ds_solo",
            status="completed",
            agent_id="ag_s",
            completed_at=now,
        )
    )
    session.add(
        AgentRun(
            id="run_solo",
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t_solo",
                dataset_id="ds_solo",
                question_id=0,
                question_key="qk_solo",
            ),
            task_id="t_solo",
            dataset_id="ds_solo",
            question_id=0,
            agent_id="ag_s",
            status="success",
            started_at=now,
            agent_snapshot="{}",
        )
    )
    session.add(
        EvaluationResult(
            task_id="t_solo",
            question_id=0,
            metric_name="correctness",
            score=0.5,
            passed=True,
            agent_answer=SAMPLE_A,
            ground_truth=SAMPLE_A,
            question=SAMPLE_Q,
            agent_run_id="run_solo",
        )
    )
    await session.commit()

    highlights = await get_task_diff_highlights(session, "t_solo")
    assert highlights == []

    payload = await build_task_scenario_matrix(session, "t_solo")
    assert payload["comparison_count"] == 0
    assert payload["questions"][0]["diff_highlight"] is None
