"""离线评估能力边界：仅质量指标，不含 runtime / 场景 / AgentRun stub。"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import ENABLED_METRICS, METRICS_CONFIG
from database import AgentRun, Base, Dataset, EvaluationResult, EvaluationTask
from services.evaluation_mode import (
    EVALUATION_MODE_OFFLINE,
    EVALUATION_MODE_ONLINE,
    get_task_capabilities,
    validate_offline_evaluate_request,
)
from services.evaluation_pipeline import compute_offline_progress_total, run_offline_evaluation_pipeline
from services.quality_score_service import compute_task_quality_score


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session, factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_validate_offline_rejects_runtime_metrics():
    with pytest.raises(ValueError, match="运行效能"):
        validate_offline_evaluate_request(
            selected_metrics=["faithfulness", "inference_latency"],
            metrics_config=METRICS_CONFIG,
            enabled_metrics=ENABLED_METRICS,
            inference_rounds=2,
        )


@pytest.mark.asyncio
async def test_compute_offline_progress_total_excludes_runtime():
    total = compute_offline_progress_total(
        3,
        ["faithfulness", "inference_latency", "correctness"],
    )
    assert total == 3 * 2 * 1


@pytest.mark.asyncio
async def test_offline_task_capabilities():
    task = EvaluationTask(
        id="task_off",
        dataset_id="ds1",
        models='["glm"]',
        status="completed",
        evaluation_mode=EVALUATION_MODE_OFFLINE,
        agent_id=None,
    )
    assert get_task_capabilities(task) == ["quality"]


@pytest.mark.asyncio
async def test_offline_pipeline_skips_runtime_scenario_and_agent_run(mem_session):
    session, factory = mem_session
    session.add(
        Dataset(
            id="ds_off",
            name="offline-ds",
            item_count=1,
            data='[{"question":"Q1","answer":"A1","ground_truth":"GT1"}]',
            created_at=datetime.utcnow(),
        )
    )
    session.add(
        EvaluationTask(
            id="task_off_pipe",
            dataset_id="ds_off",
            models='["glm"]',
            status="pending",
            evaluation_mode=EVALUATION_MODE_OFFLINE,
            auto_scenario_score=False,
            agent_id=None,
            selected_metrics='["faithfulness"]',
            llm_model="glm",
            created_at=datetime.utcnow(),
        )
    )
    await session.commit()

    async def fake_evaluate_single(*args, **kwargs):
        return {
            "score": 0.9,
            "passed": True,
            "reason": "ok",
            "duration_ms": 10,
            "cot": "ok",
        }

    with patch("services.evaluation_pipeline.async_session") as mock_async_session, patch(
        "tasks.async_session",
    ) as mock_tasks_session, patch(
        "tasks.evaluate_single",
        new=AsyncMock(side_effect=fake_evaluate_single),
    ), patch(
        "tasks.refresh_llm_models_cache_task",
        new=AsyncMock(),
    ), patch(
        "tasks.emit_event",
        new=AsyncMock(),
    ), patch(
        "evaluators.llm_client.generate_evaluation_summary",
        new=AsyncMock(return_value="summary"),
    ):
        mock_async_session.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_async_session.return_value.__aexit__ = AsyncMock(return_value=None)
        mock_tasks_session.return_value.__aenter__ = AsyncMock(return_value=session)
        mock_tasks_session.return_value.__aexit__ = AsyncMock(return_value=None)

        await run_offline_evaluation_pipeline(
            task_id="task_off_pipe",
            dataset_id="ds_off",
            selected_metrics=["faithfulness", "inference_latency"],
            llm_model="glm",
        )

    run_count = await session.scalar(select(func.count()).select_from(AgentRun))
    assert run_count == 0

    metrics = (
        await session.execute(
            select(EvaluationResult.metric_name).where(
                EvaluationResult.task_id == "task_off_pipe"
            )
        )
    ).scalars().all()
    assert metrics == ["faithfulness"]

    task = await session.get(EvaluationTask, "task_off_pipe")
    assert task.status == "completed"


@pytest.mark.asyncio
async def test_offline_quality_score_result_only(mem_session):
    session, _factory = mem_session
    session.add(
        EvaluationTask(
            id="task_qs",
            dataset_id="ds1",
            models='["glm"]',
            status="completed",
            evaluation_mode=EVALUATION_MODE_OFFLINE,
            agent_id=None,
            created_at=datetime.utcnow(),
        )
    )
    session.add(
        EvaluationResult(
            task_id="task_qs",
            question_id=0,
            model="glm",
            metric_name="faithfulness",
            score=0.9,
            threshold=0.8,
            passed=True,
            duration_ms=1,
            avg_score=0.9,
        )
    )
    await session.commit()

    score = await compute_task_quality_score(session, "task_qs")
    assert score["evaluation_mode"] == "offline"
    assert score["process_score"] is None
    assert score["process_weight"] == 0.0
    assert score["total_score"] == score["result_score"]
    # 离线 + 单轮：stability_score=None，权重全归结果分
    assert score["stability_score"] is None
    assert score["stability_weight"] == pytest.approx(0.0)
    assert score["result_weight"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_compute_offline_progress_total_with_process_eval():
    total = compute_offline_progress_total(
        3,
        ["faithfulness", "correctness"],
        has_process_eval=True,
    )
    assert total == 3 * 2 * 1 + 3


@pytest.mark.asyncio
async def test_compute_offline_progress_total_without_process_eval():
    total = compute_offline_progress_total(
        3,
        ["faithfulness", "correctness"],
        has_process_eval=False,
    )
    assert total == 3 * 2 * 1


@pytest.mark.asyncio
async def test_offline_capabilities_with_process_eval_completed():
    task = EvaluationTask(
        id="task_pe",
        dataset_id="ds1",
        models='["glm"]',
        status="completed",
        evaluation_mode=EVALUATION_MODE_OFFLINE,
        agent_id=None,
        process_eval_completed=True,
    )
    caps = get_task_capabilities(task)
    assert "scenario" in caps
    assert "checkpoint" in caps


@pytest.mark.asyncio
async def test_offline_capabilities_without_process_eval():
    task = EvaluationTask(
        id="task_no_pe",
        dataset_id="ds1",
        models='["glm"]',
        status="completed",
        evaluation_mode=EVALUATION_MODE_OFFLINE,
        agent_id=None,
        process_eval_completed=False,
    )
    caps = get_task_capabilities(task)
    assert "scenario" not in caps
    assert "checkpoint" not in caps


@pytest.mark.asyncio
async def test_online_capabilities_respect_dimension_flags():
    """维度开关关闭时，对应 capability 被剔除。"""
    task = EvaluationTask(
        id="task_flags",
        dataset_id="ds1",
        models='["glm"]',
        status="completed",
        evaluation_mode=EVALUATION_MODE_ONLINE,
        agent_id="ag1",
    )
    # 默认全开
    caps = get_task_capabilities(task)
    assert "scenario" in caps
    assert "checkpoint" in caps
    assert "runtime" in caps

    # 关闭场景
    task.auto_scenario_score = False
    caps = get_task_capabilities(task)
    assert "scenario" not in caps
    assert "checkpoint" in caps
    assert "runtime" in caps

    # 关闭过程一致性
    task.auto_scenario_score = True
    task.enable_process_consistency = False
    caps = get_task_capabilities(task)
    assert "scenario" in caps
    assert "checkpoint" not in caps
    assert "runtime" in caps

    # 关闭稳定性：runtime 不再受 enable_stability 控制（运行效能独立）
    task.enable_process_consistency = True
    task.enable_stability = False
    caps = get_task_capabilities(task)
    assert "scenario" in caps
    assert "checkpoint" in caps
    assert "runtime" in caps


@pytest.mark.asyncio
async def test_build_eval_context_includes_dataset_item_id():
    from services.offline_process_eval_service import _build_eval_context_for_offline_run

    task = EvaluationTask(
        id="task_ctx",
        dataset_id="ds1",
        models='["glm"]',
        status="completed",
        evaluation_mode=EVALUATION_MODE_OFFLINE,
        agent_id=None,
    )
    ctx = _build_eval_context_for_offline_run(
        task=task,
        question_id=0,
        run_index=0,
        dataset_item_id="ditem_abc123",
        scenario_key="generic_l0",
    )
    assert ctx["dataset_item_id"] == "ditem_abc123"
    assert ctx["scenario_key"] == "generic_l0"


@pytest.mark.asyncio
async def test_build_eval_context_omits_empty_optional_fields():
    from services.offline_process_eval_service import _build_eval_context_for_offline_run

    task = EvaluationTask(
        id="task_ctx2",
        dataset_id="ds1",
        models='["glm"]',
        status="completed",
        evaluation_mode=EVALUATION_MODE_OFFLINE,
        agent_id=None,
    )
    ctx = _build_eval_context_for_offline_run(
        task=task,
        question_id=0,
        run_index=0,
    )
    assert "dataset_item_id" not in ctx
    assert "scenario_key" not in ctx


@pytest.mark.asyncio
async def test_clear_old_offline_stubs(mem_session):
    session, _factory = mem_session
    task = EvaluationTask(
        id="task_clean",
        dataset_id="ds1",
        models='["glm"]',
        status="completed",
        evaluation_mode=EVALUATION_MODE_OFFLINE,
        agent_id=None,
    )
    session.add(task)
    now = datetime.utcnow()
    old_run_1 = AgentRun(
        id="offline_run_old_1",
        origin="evaluation",
        evaluation_mode="offline",
        task_id="task_clean",
        question_id=0,
        status="completed",
        agent_id="agent_stub",
        started_at=now,
        agent_snapshot="{}",
    )
    old_run_2 = AgentRun(
        id="offline_run_old_2",
        origin="evaluation",
        evaluation_mode="offline",
        task_id="task_clean",
        question_id=0,
        status="completed",
        agent_id="agent_stub",
        started_at=now,
        agent_snapshot="{}",
    )
    other_run = AgentRun(
        id="offline_run_other",
        origin="evaluation",
        evaluation_mode="offline",
        task_id="task_clean",
        question_id=1,
        status="completed",
        agent_id="agent_stub",
        started_at=now,
        agent_snapshot="{}",
    )
    session.add_all([old_run_1, old_run_2, other_run])
    await session.commit()

    from services.offline_process_eval_service import _clear_old_offline_stubs

    cleared = await _clear_old_offline_stubs(session, "task_clean", 0)
    assert cleared == 2

    remaining = (
        await session.execute(select(AgentRun.id))
    ).scalars().all()
    assert remaining == ["offline_run_other"]


@pytest.mark.asyncio
async def test_load_offline_agent_answer(mem_session):
    session, _factory = mem_session
    task = EvaluationTask(
        id="task_ans",
        dataset_id="ds1",
        models='["glm"]',
        status="completed",
        evaluation_mode=EVALUATION_MODE_OFFLINE,
        agent_id=None,
    )
    session.add(task)
    session.add(
        EvaluationResult(
            task_id="task_ans",
            question_id=0,
            model="glm",
            metric_name="faithfulness",
            score=0.9,
            threshold=0.8,
            passed=True,
            duration_ms=1,
            agent_answer="这是模型的回答",
        )
    )
    await session.commit()

    from services.offline_process_eval_service import _load_offline_agent_answer

    answer = await _load_offline_agent_answer(session, task, 0)
    assert answer == "这是模型的回答"


@pytest.mark.asyncio
async def test_load_offline_agent_answer_missing(mem_session):
    session, _factory = mem_session
    task = EvaluationTask(
        id="task_no_ans",
        dataset_id="ds1",
        models='["glm"]',
        status="completed",
        evaluation_mode=EVALUATION_MODE_OFFLINE,
        agent_id=None,
    )
    session.add(task)
    await session.commit()

    from services.offline_process_eval_service import _load_offline_agent_answer

    answer = await _load_offline_agent_answer(session, task, 0)
    assert answer == ""
