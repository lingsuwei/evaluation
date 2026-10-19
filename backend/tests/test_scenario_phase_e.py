"""阶段 E：自动场景打分、过程点导入、span_refs。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import (
    AgentRun,
    Base,
    Dataset,
    EvaluationResult,
    EvaluationTask,
    ProcessCheckpointTemplate,
)
from services.checkpoint_to_scenario_service import (
    checkpoint_match_rules_to_scenario,
    draft_steps_from_checkpoints,
    import_checkpoints_to_item_template,
)
from services.dataset_item_scenario_service import auto_extract_steps_for_dataset
from services.dataset_items import ensure_dataset_item_id
from services.eval_context import build_eval_context
from services.scenario_post_eval_service import score_scenario_for_task
from services.scenario_step_span_ref_service import list_scenario_step_span_refs


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest.mark.asyncio
async def test_checkpoint_rules_to_scenario_trace_binding():
    raw = json.dumps(
        {
            "mode": "any",
            "rules": [{"type": "observation_name", "op": "contains", "value": "retrieve"}],
        }
    )
    out = checkpoint_match_rules_to_scenario(raw)
    assert "retrieve" in out["trace_binding"]["span_name_patterns"]


@pytest.mark.asyncio
async def test_draft_steps_from_checkpoints():
    tpl = ProcessCheckpointTemplate(
        id="cp1",
        logical_agent_id="ag-test",
        name="检索上下文",
        description="拉取本体",
        order=1,
        required=True,
        match_rules=json.dumps(
            {
                "mode": "any",
                "rules": [{"type": "observation_name", "op": "contains", "value": "tool:"}],
            }
        ),
        score_weight=0.5,
        enabled=True,
    )
    steps = draft_steps_from_checkpoints([tpl])
    assert len(steps) == 1
    assert steps[0]["match_rules"].get("trace_binding")


@pytest.mark.asyncio
async def test_score_scenario_for_task(session: AsyncSession):
    item = {
        "question": "Q1",
        "model_answer": "1、甲\n2、乙",
        "ground_truth": "A",
    }
    ditem_id = ensure_dataset_item_id(item)
    ds = Dataset(
        id=f"ds_{uuid.uuid4().hex[:8]}",
        name="ds",
        data=json.dumps([item], ensure_ascii=False),
        item_count=1,
    )
    session.add(ds)
    await session.flush()
    await auto_extract_steps_for_dataset(session, ds.id)
    task_id = f"t_{uuid.uuid4().hex[:8]}"
    run_id = f"run_{uuid.uuid4().hex[:6]}"
    session.add(
        EvaluationTask(
            id=task_id,
            dataset_id=ds.id,
            models="[]",
            status="completed",
            auto_scenario_score=True,
        )
    )
    session.add(
        AgentRun(
            id=run_id,
            origin="evaluation",
            eval_context=build_eval_context(
                task_id=task_id,
                dataset_id=ds.id,
                question_id=0,
                dataset_item_id=ditem_id,
                scenario_key=f"ditem_{ditem_id}",
            ),
            task_id=task_id,
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
            task_id=task_id,
            question_id=0,
            model="m",
            metric_name="c",
            score=0.5,
            avg_score=0.5,
            passed=True,
            agent_run_id=run_id,
            question="Q1",
            ground_truth="A",
            agent_answer="甲 乙",
        )
    )
    await session.flush()
    out = await score_scenario_for_task(session, task_id)
    assert out["scored_count"] == 1
    refs = await list_scenario_step_span_refs(session, run_id)
    assert isinstance(refs, list)


@pytest.mark.asyncio
async def test_import_checkpoints_to_item(session: AsyncSession):
    item = {"question": "Q", "model_answer": "x", "ground_truth": "y"}
    ditem_id = ensure_dataset_item_id(item)
    ds = Dataset(
        id=f"ds_{uuid.uuid4().hex[:8]}",
        name="ds",
        data=json.dumps([item], ensure_ascii=False),
        item_count=1,
    )
    session.add(ds)
    session.add(
        ProcessCheckpointTemplate(
            id="cp2",
            logical_agent_id="lid-1",
            name="推理",
            order=1,
            required=True,
            match_rules="{}",
            score_weight=1.0,
            enabled=True,
        )
    )
    await session.flush()
    payload = await import_checkpoints_to_item_template(
        session,
        dataset_id=ds.id,
        dataset_item_id=ditem_id,
        logical_agent_id="lid-1",
    )
    assert payload["step_count"] == 1
    assert payload["scenario_key"].startswith("ditem_")
