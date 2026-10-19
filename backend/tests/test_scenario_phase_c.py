"""阶段 C：配置体验、权重归一化、trace 绑定补全。"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base, Dataset, ScenarioEvalStep, ScenarioEvalTemplate
from services.dataset_item_scenario_service import (
    auto_extract_steps_for_dataset,
    backfill_item_step_trace_bindings,
    build_default_match_rules,
    create_item_step,
    ensure_step_match_rules,
    list_dataset_item_configs,
    normalize_item_step_weights,
    sync_dataset_item_scenario_keys,
)
from services.scenario_step_trace_service import get_step_trace_binding


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


async def _seed_dataset(session: AsyncSession) -> tuple[str, str]:
    items = [
        {
            "question": "Q1",
            "model_answer": "1、步骤甲\n2、步骤乙",
            "ground_truth": "A",
        },
    ]
    ds = Dataset(
        id=f"ds_{uuid.uuid4().hex[:8]}",
        name="test-ds",
        data=json.dumps(items, ensure_ascii=False),
        item_count=1,
    )
    session.add(ds)
    await session.flush()
    await auto_extract_steps_for_dataset(session, ds.id)
    ditem_id = json.loads(ds.data)[0]["id"]
    return ds.id, ditem_id


@pytest.mark.asyncio
async def test_ensure_step_match_rules_fills_trace_binding():
    rules = ensure_step_match_rules(
        match_rules=None,
        gt_expected={"keywords": ["ECO", "映射"]},
        name="步骤1",
    )
    tb = rules["trace_binding"]
    assert "llm:main_iteration" in tb["span_name_patterns"][0] or "tool:" in str(
        tb["span_name_patterns"]
    )
    assert "ECO" in tb["output_keywords"]


@pytest.mark.asyncio
async def test_get_step_trace_binding_per_item_from_gt_criteria():
    binding = get_step_trace_binding(
        "ditem_abc",
        "S1",
        {
            "name": "判断",
            "gt_expected": {},
            "gt_criteria": ["第一层判断", "报废"],
        },
    )
    assert binding.get("output_keywords")


@pytest.mark.asyncio
async def test_normalize_item_step_weights(session: AsyncSession):
    ds_id, ditem_id = await _seed_dataset(session)
    tpl = (
        await session.execute(
            select(ScenarioEvalTemplate).where(
                ScenarioEvalTemplate.dataset_item_id == ditem_id
            )
        )
    ).scalar_one()
    steps = (
        await session.execute(
            select(ScenarioEvalStep).where(ScenarioEvalStep.template_id == tpl.id)
        )
    ).scalars().all()
    steps[0].weight = 0.2
    steps[1].weight = 0.2
    await session.flush()
    result = await normalize_item_step_weights(session, ditem_id)
    assert result["weight_summary"]["normalized"] is True
    assert abs(result["weight_summary"]["weight_sum"] - 1.0) <= 0.01


@pytest.mark.asyncio
async def test_sync_dataset_scenario_keys(session: AsyncSession):
    ds_id, ditem_id = await _seed_dataset(session)
    ds = await session.get(Dataset, ds_id)
    items = json.loads(ds.data)
    items[0]["scenario_key"] = "wrong_key"
    ds.data = json.dumps(items, ensure_ascii=False)
    await session.flush()
    out = await sync_dataset_item_scenario_keys(session, ds_id)
    assert out["synced_count"] == 1
    items2 = json.loads((await session.get(Dataset, ds_id)).data)
    assert items2[0]["scenario_key"] == f"ditem_{ditem_id}"


@pytest.mark.asyncio
async def test_list_config_includes_weight_summary(session: AsyncSession):
    ds_id, _ditem_id = await _seed_dataset(session)
    cfg = await list_dataset_item_configs(session, ds_id)
    assert cfg["items"][0]["weight_summary"]
    assert cfg["items"][0]["scenario_key_in_sync"] is True


@pytest.mark.asyncio
async def test_backfill_trace_bindings(session: AsyncSession):
    ds_id, ditem_id = await _seed_dataset(session)
    tpl = (
        await session.execute(
            select(ScenarioEvalTemplate).where(
                ScenarioEvalTemplate.dataset_item_id == ditem_id
            )
        )
    ).scalar_one()
    steps = (
        await session.execute(
            select(ScenarioEvalStep).where(ScenarioEvalStep.template_id == tpl.id)
        )
    ).scalars().all()
    for s in steps:
        s.match_rules = None
    await session.flush()
    out = await backfill_item_step_trace_bindings(session, ditem_id)
    assert out["updated_step_count"] >= 1
    row = await session.get(ScenarioEvalStep, steps[0].id)
    assert row.match_rules.get("trace_binding")


@pytest.mark.asyncio
async def test_create_item_step_auto_match_rules(session: AsyncSession):
    ds_id, ditem_id = await _seed_dataset(session)
    row = await create_item_step(
        session,
        ditem_id,
        step_key="SX",
        name="附加",
        weight=0.1,
        gt_expected={"keywords": ["附加关键词"]},
    )
    assert row["match_rules"]["trace_binding"]["output_keywords"]
