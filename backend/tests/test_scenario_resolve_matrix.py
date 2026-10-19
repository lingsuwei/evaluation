"""场景解析与矩阵按题模板（阶段 A/B）。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import AgentRun, Base, Dataset, EvaluationResult
from services.dataset_item_scenario_service import auto_extract_steps_for_dataset
from services.eval_context import build_eval_context
from services.scenario_eval_service import (
    GENERIC_L0_SCENARIO_KEY,
    resolve_scenario_for_run,
    score_run_against_scenario,
)
from services.scenario_template_utils import filter_enabled_steps, template_with_active_steps_only
from services.task_scenario_matrix_service import build_task_scenario_matrix


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


async def _seed_eco_task(session: AsyncSession) -> tuple[str, str, str]:
    items = [
        {
            "question": "问题1：ECO-001",
            "model_answer": "1、定位ECO-001\n2、EPart映射\n3、MPart映射",
            "ground_truth": "A001",
        },
        {
            "question": "问题4：成本",
            "model_answer": "步骤 1：判断\n步骤 2：计算",
            "ground_truth": "450 元",
        },
    ]
    ds = Dataset(
        id=f"ds_{uuid.uuid4().hex[:8]}",
        name="onto-eval-12-with-context",
        data=json.dumps(items, ensure_ascii=False),
        item_count=2,
    )
    session.add(ds)
    await session.flush()
    await auto_extract_steps_for_dataset(session, ds.id)
    task_id = f"t_{uuid.uuid4().hex[:8]}"
    started = datetime.utcnow()
    run_ids = []
    for q_idx in range(2):
        rid = f"run_{q_idx}_{uuid.uuid4().hex[:6]}"
        run_ids.append(rid)
        ditem_id = json.loads(ds.data)[q_idx]["id"]
        session.add(
            AgentRun(
                id=rid,
                origin="evaluation",
                eval_context=build_eval_context(
                    task_id=task_id,
                    dataset_id=ds.id,
                    question_id=q_idx,
                    run_index=0,
                    dataset_item_id=ditem_id,
                    scenario_key=f"ditem_{ditem_id}",
                ),
                task_id=task_id,
                dataset_id=ds.id,
                question_id=q_idx,
                agent_id="ag1",
                agent_name="Agent",
                status="success",
                started_at=started,
                agent_snapshot="{}",
            )
        )
        session.add(
            EvaluationResult(
                task_id=task_id,
                question_id=q_idx,
                model="m",
                metric_name="correctness",
                score=0.5,
                avg_score=0.5,
                passed=True,
                agent_run_id=rid,
                question=items[q_idx]["question"],
                ground_truth=items[q_idx]["ground_truth"],
                agent_answer="定位ECO-001 EPart MPart 450",
            )
        )
    await session.flush()
    from database import EvaluationTask

    session.add(
        EvaluationTask(
            id=task_id,
            dataset_id=ds.id,
            models="[]",
            agent_id="ag1",
            status="completed",
            progress_current=2,
            progress_total=2,
        )
    )
    await session.flush()
    return task_id, ds.id, run_ids[0]


@pytest.mark.asyncio
async def test_resolve_eco_run_uses_ditem_not_generic(session: AsyncSession):
    task_id, _ds_id, run_id = await _seed_eco_task(session)
    key = await resolve_scenario_for_run(session, run_id)
    assert key
    assert key.startswith("ditem_")
    assert key != GENERIC_L0_SCENARIO_KEY


@pytest.mark.asyncio
async def test_pirate_gt_does_not_resolve_builtin_demo(session: AsyncSession):
    items = [
        {
            "question": "已识别3艘海盗大飞T001",
            "ground_truth": "T001 B006",
            "model_answer": "x",
        },
    ]
    ds = Dataset(
        id=f"ds_{uuid.uuid4().hex[:8]}",
        name="zc",
        data=json.dumps(items, ensure_ascii=False),
        item_count=1,
    )
    session.add(ds)
    await session.flush()
    await auto_extract_steps_for_dataset(session, ds.id)
    ditem_id = json.loads(ds.data)[0]["id"]
    run_id = f"run_pirate_{uuid.uuid4().hex[:6]}"
    session.add(
        AgentRun(
            id=run_id,
            origin="evaluation",
            eval_context=build_eval_context(
                task_id="t1",
                dataset_id=ds.id,
                question_id=0,
                dataset_item_id=ditem_id,
            ),
            task_id="t1",
            dataset_id=ds.id,
            question_id=0,
            agent_id="ag1",
            status="success",
            started_at=datetime.utcnow(),
            agent_snapshot="{}",
        )
    )
    session.add(
        EvaluationResult(
            task_id="t1",
            question_id=0,
            model="m",
            metric_name="c",
            score=0,
            avg_score=0,
            passed=False,
            agent_run_id=run_id,
            question=items[0]["question"],
            ground_truth=items[0]["ground_truth"],
            agent_answer="",
        )
    )
    await session.flush()
    key = await resolve_scenario_for_run(session, run_id)
    assert key.startswith("ditem_")


@pytest.mark.asyncio
async def test_disabled_steps_excluded_from_scoring_template(session: AsyncSession):
    task_id, _ds_id, run_id = await _seed_eco_task(session)
    from database import ScenarioEvalStep, ScenarioEvalTemplate
    from sqlalchemy import select

    key = await resolve_scenario_for_run(session, run_id)
    tpl_row = (
        await session.execute(
            select(ScenarioEvalTemplate).where(
                ScenarioEvalTemplate.scenario_key == key
            )
        )
    ).scalar_one()
    steps = (
        await session.execute(
            select(ScenarioEvalStep).where(ScenarioEvalStep.template_id == tpl_row.id)
        )
    ).scalars().all()
    steps[0].enabled = False
    await session.flush()
    tpl = await score_run_against_scenario(session, run_id, persist=False)
    assert tpl is not None
    assert len(tpl["steps"]) == len(steps) - 1
    assert all(s.get("enabled", True) for s in tpl["template"]["steps"])


@pytest.mark.asyncio
async def test_matrix_auto_mode_per_question_columns(session: AsyncSession):
    task_id, _ds_id, _run_id = await _seed_eco_task(session)
    matrix = await build_task_scenario_matrix(session, task_id, scenario_key=None)
    assert matrix["scenario_mode"] == "auto"
    assert len(matrix["questions"]) == 2
    cols0 = matrix["questions"][0].get("step_columns") or []
    cols1 = matrix["questions"][1].get("step_columns") or []
    assert len(cols0) == 3
    assert len(cols1) == 2
    assert matrix["questions"][0]["scenario_key"] != matrix["questions"][1]["scenario_key"]


@pytest.mark.asyncio
async def test_filter_enabled_steps_helper():
    steps = [
        {"step_key": "S1", "enabled": True},
        {"step_key": "S2", "enabled": False},
    ]
    assert len(filter_enabled_steps(steps)) == 1
    tpl = template_with_active_steps_only({"steps": steps})
    assert len(tpl["steps"]) == 1
