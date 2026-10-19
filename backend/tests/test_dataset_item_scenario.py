"""按题场景子步骤（ZC 样例）。"""
import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base, Dataset
from services.dataset_item_scenario_service import (
    auto_extract_steps_for_dataset,
    ensure_step_match_rules,
    list_dataset_item_configs,
    resolve_item_scenario_key,
)
from services.scenario_eval_service import get_template_by_key


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


def _zc_items():
    path = __file__
    # minimal inline ZC-like 4 items
    return [
        {
            "question": "针对带状防御区域，调度合理的快艇数量，请生成防御方案",
            "ground_truth": "PLAN_CORRIDOR_001",
            "model_answer": "x",
        },
        {
            "question": "已识别3艘海盗大飞T001、T002、T003，请派遣快艇B001",
            "ground_truth": "T001 B006",
            "model_answer": "x",
        },
        {
            "question": "针对已识别3艘海盗大飞T004、T005、T006",
            "ground_truth": "任务动作列表",
            "model_answer": "x",
        },
        {
            "question": "快艇B001生成执行方案",
            "ground_truth": "打击 FPV",
            "model_answer": "x",
        },
    ]


@pytest.mark.asyncio
async def test_auto_extract_from_model_answer(session: AsyncSession):
    items = [
        {
            "question": "问题1：ECO-001变更",
            "model_answer": "1、定位ECO-001受影响对象\n2、EPart版本映射\n3、MPart工艺关系映射",
            "ground_truth": "变更1：A001",
        },
        {
            "question": "问题4：报废成本",
            "model_answer": "步骤 1：第一层判断\n步骤 2：第二层判断\n步骤 3：计算成本",
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

    result = await auto_extract_steps_for_dataset(session, ds.id)
    assert result["seeded_item_count"] == 2
    assert result["source"] == "model_answer"

    cfg = await list_dataset_item_configs(session, ds.id)
    assert cfg["items"][0]["has_template"]
    assert len(cfg["items"][0]["template"]["steps"]) == 3
    assert "ECO" in cfg["items"][0]["template"]["steps"][0]["name"] or any(
        "ECO" in str(s.get("gt_expected")) for s in cfg["items"][0]["template"]["steps"]
    )
    assert len(cfg["items"][1]["template"]["steps"]) == 3
    assert "第一层" in cfg["items"][1]["template"]["steps"][0]["name"]

    key0 = await resolve_item_scenario_key(session, ds.id, 0)
    assert key0 and key0.startswith("ditem_")
    tpl = await get_template_by_key(session, key0)
    assert tpl is not None


# ── ensure_step_match_rules eval_config 默认值 ──────────────────────


def test_ensure_step_match_rules_adds_eval_config_default():
    """match_rules 为空时，补全 eval_config: {mode: "keyword"}。"""
    result = ensure_step_match_rules(
        match_rules=None,
        gt_expected={"keywords": ["海况"]},
        name="态势研判",
    )
    assert result["eval_config"] == {"mode": "keyword"}
    assert "trace_binding" in result


def test_ensure_step_match_rules_preserves_existing_eval_config():
    """已有 eval_config 时不覆盖。"""
    rules = {
        "trace_binding": {
            "span_name_patterns": ["llm:main"],
            "output_keywords": ["结论"],
        },
        "eval_config": {"mode": "llm_aggregate", "aggregate_prompt": "汇总"},
    }
    result = ensure_step_match_rules(match_rules=rules, name="测试")
    assert result["eval_config"]["mode"] == "llm_aggregate"
    assert result["eval_config"]["aggregate_prompt"] == "汇总"


def test_ensure_step_match_rules_no_eval_config_but_has_binding():
    """有 trace_binding 但无 eval_config 时，补充默认 eval_config。"""
    rules = {
        "trace_binding": {
            "span_name_patterns": ["llm:main"],
            "output_keywords": ["结论"],
        },
    }
    result = ensure_step_match_rules(match_rules=rules, name="测试")
    assert result["eval_config"] == {"mode": "keyword"}
