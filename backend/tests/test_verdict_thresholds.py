"""P3：结论阈值配置与历史徽章。"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Base, EvaluationResult, EvaluationTask
from services.evaluation_mode import EVALUATION_MODE_OFFLINE, EVALUATION_MODE_ONLINE
from services.task_dimension_verdict_service import (
    build_task_dimension_verdicts,
    compact_dimension_verdict_badges,
)
from services.task_list_service import serialize_tasks_for_list
from services.verdict_threshold_service import (
    VerdictThresholds,
    resolve_verdict_thresholds,
    threshold_meta,
    validate_threshold_patch,
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


async def _seed_offline_task(session: AsyncSession, task_id: str = "t_thr"):
    session.add(
        EvaluationTask(
            id=task_id,
            dataset_id="ds1",
            models='["glm"]',
            status="completed",
            evaluation_mode=EVALUATION_MODE_OFFLINE,
            created_at=datetime.utcnow(),
        )
    )
    session.add(
        EvaluationResult(
            task_id=task_id,
            question_id=0,
            model="glm",
            metric_name="faithfulness",
            score=0.95,
            threshold=0.8,
            passed=True,
            duration_ms=1,
            avg_score=0.95,
        )
    )
    await session.commit()


def test_validate_threshold_patch_rejects_out_of_range():
    with pytest.raises(ValueError, match="quality_pass_rate_pass"):
        validate_threshold_patch({"quality_pass_rate_pass": 1.5})


def test_resolve_custom_thresholds():
    task = EvaluationTask(
        id="x",
        verdict_thresholds=json.dumps({"quality_total_pass": 0.95}),
    )
    thr = resolve_verdict_thresholds(task)
    assert thr.quality_total_pass == 0.95
    assert thr.quality_total_warn == VerdictThresholds.defaults().quality_total_warn
    meta = threshold_meta(task)
    assert meta["source"] == "task_custom"
    assert "quality_total_pass" in meta["custom_keys"]


async def test_custom_threshold_changes_quality_verdict(mem_session: AsyncSession):
    await _seed_offline_task(mem_session)
    base = await build_task_dimension_verdicts(mem_session, "t_thr")
    assert base is not None
    assert base["quality"]["status"] == "pass"

    task = await mem_session.get(EvaluationTask, "t_thr")
    task.verdict_thresholds = json.dumps({"quality_total_pass": 0.99})
    await mem_session.commit()

    strict = await build_task_dimension_verdicts(mem_session, "t_thr")
    assert strict is not None
    assert strict["quality"]["status"] in ("warn", "fail")
    assert strict["thresholds"]["source"] == "task_custom"


async def test_list_includes_dimension_badges(mem_session: AsyncSession):
    """历史列表为已完成任务返回四维徽章（compact status 形状）。"""
    await _seed_offline_task(mem_session, "t_list_badges")
    task = await mem_session.get(EvaluationTask, "t_list_badges")
    items = await serialize_tasks_for_list(mem_session, [task])
    assert len(items) == 1
    badges = items[0].get("dimension_verdict_badges")
    assert badges is not None
    assert set(badges.keys()) == {
        "quality",
        "process_consistency",
        "scenario_delivery",
        "runtime_performance",
    }


def test_compact_badges_shape():
    payload = {
        "quality": {"status": "pass"},
        "process_consistency": {"status": "warn"},
        "scenario_delivery": {"status": "na"},
        "runtime_stability": {
            "structural": {"status": "consistent"},
            "performance": {"status": "within_target"},
        },
    }
    badges = compact_dimension_verdict_badges(payload)
    assert badges == {
        "quality": "pass",
        "process_consistency": "warn",
        "scenario_delivery": "na",
        "runtime_performance": "within_target",
    }
