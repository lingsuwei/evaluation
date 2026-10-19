"""DEF-P1-01/02：恢复时复用 AgentRun、跳过已完成 metric"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Agent, AgentRun, Base, Dataset, EvaluationResult, EvaluationTask
from services.evaluation_pipeline import (
    compute_online_progress_total,
    execute_online_inference_with_run,
    load_completed_metrics,
    load_task_resume_context,
    resume_evaluation_pipeline,
    run_online_evaluation_pipeline,
)


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
def sample_agent():
    return Agent(
        id="ag_resume",
        name="ResumeAgent",
        logical_agent_id="la_resume",
        version="1.0",
        inference_config='{"model": "test"}',
        created_at=datetime.utcnow(),
    )


@pytest.mark.asyncio
async def test_compute_online_progress_total_includes_inference():
    assert (
        compute_online_progress_total(3, ["a", "b"], 1) == 3 * (1 + 2)
    )
    assert compute_online_progress_total(0, ["a"], 1) == 0


@pytest.mark.asyncio
async def test_inference_reuses_run_without_calling_api(mem_session, sample_agent):
    mem_session.add(sample_agent)
    now = datetime.utcnow()
    run = AgentRun(
        id="run_reuse",
        origin="evaluation",
        task_id="task_r",
        dataset_id="ds_r",
        question_id=0,
        run_index=0,
        agent_id=sample_agent.id,
        status="success",
        started_at=now,
        ended_at=now,
        agent_snapshot="{}",
        created_at=now,
    )
    mem_session.add(run)
    mem_session.add(
        EvaluationResult(
            task_id="task_r",
            question_id=0,
            model="glm",
            metric_name="faithfulness",
            score=0.9,
            threshold=0.8,
            passed=True,
            agent_answer="cached answer",
            inference_duration_ms=42,
            agent_run_id=run.id,
        )
    )
    await mem_session.commit()

    with patch(
        "services.evaluation_pipeline.InferenceService.call_inference",
        new=AsyncMock(),
    ) as mock_infer:
        out = await execute_online_inference_with_run(
            mem_session,
            task_id="task_r",
            dataset_id="ds_r",
            question_id=0,
            run_index=0,
            question="q?",
            ground_truth="gt",
            agent=sample_agent,
            inference_config={"model": "test"},
            emit_inference_events=False,
        )
    mock_infer.assert_not_called()
    assert out.reused is True
    assert out.agent_run_id == "run_reuse"
    assert out.model_answer == "cached answer"

    count = await mem_session.scalar(select(func.count()).select_from(AgentRun))
    assert count == 1


@pytest.mark.asyncio
async def test_pipeline_resume_skips_done_metric_and_reuses_run(mem_session, sample_agent):
    mem_session.add(sample_agent)
    now = datetime.utcnow()
    mem_session.add(
        Dataset(
            id="ds_resume",
            name="Resume DS",
            data='[{"question": "q0?", "ground_truth": "gt0"}]',
            item_count=1,
            created_at=now,
        )
    )
    mem_session.add(
        EvaluationTask(
            id="task_resume",
            dataset_id="ds_resume",
            models='["default"]',
            status="paused",
            progress_current=2,
            progress_total=10,
            agent_id=sample_agent.id,
            resume_from=0,
            run_count=1,
            llm_model="glm",
        )
    )
    mem_session.add(
        AgentRun(
            id="run_q0",
            origin="evaluation",
            task_id="task_resume",
            dataset_id="ds_resume",
            question_id=0,
            run_index=0,
            agent_id=sample_agent.id,
            status="success",
            started_at=now,
            ended_at=now,
            agent_snapshot="{}",
            created_at=now,
        )
    )
    mem_session.add(
        EvaluationResult(
            task_id="task_resume",
            question_id=0,
            model="glm",
            metric_name="faithfulness",
            score=0.8,
            threshold=0.8,
            passed=True,
            agent_answer="ans0",
            inference_duration_ms=10,
            agent_run_id="run_q0",
        )
    )
    await mem_session.commit()

    evaluate_calls: list[str] = []

    async def _fake_evaluate_single(
        task_id, question_id, model_name, metric_name, *args, **kwargs
    ):
        evaluate_calls.append(metric_name)
        return {"score": 0.85, "reason": "ok", "duration_ms": 5}

    with (
        patch(
            "services.evaluation_pipeline.InferenceService.call_inference",
            new=AsyncMock(),
        ) as mock_infer,
        patch(
            "tasks.evaluate_single",
            new=AsyncMock(side_effect=_fake_evaluate_single),
        ),
        patch("tasks.evaluate_with_script", new=AsyncMock()),
        patch("tasks.check_task_paused", new=AsyncMock(return_value=False)),
        patch("tasks.emit_event", new=AsyncMock()),
        patch("tasks.refresh_llm_models_cache_task", new=AsyncMock()),
        patch(
            "evaluators.llm_client.generate_evaluation_summary",
            new=AsyncMock(return_value="summary"),
        ),
        patch(
            "services.evaluation_pipeline.async_session",
        ) as mock_async_session,
    ):
        mock_async_session.return_value.__aenter__ = AsyncMock(return_value=mem_session)
        mock_async_session.return_value.__aexit__ = AsyncMock(return_value=None)

        await run_online_evaluation_pipeline(
            task_id="task_resume",
            dataset_id="ds_resume",
            agent_id=sample_agent.id,
            resume_from=0,
            selected_metrics=["faithfulness", "answer_relevancy"],
        )

    mock_infer.assert_not_called()
    assert evaluate_calls == ["answer_relevancy"]

    done = await load_completed_metrics(mem_session, "task_resume", 0)
    assert "faithfulness" in done
    assert "answer_relevancy" in done

    run_count = await mem_session.scalar(
        select(func.count()).select_from(AgentRun).where(AgentRun.task_id == "task_resume")
    )
    assert run_count == 1

    task = (
        await mem_session.execute(
            select(EvaluationTask).where(EvaluationTask.id == "task_resume")
        )
    ).scalar_one()
    # 1 题 × (1 推理 + 2 指标) = 3
    assert task.progress_total == 3


@pytest.mark.asyncio
async def test_load_task_resume_context_reads_persisted_options(mem_session, sample_agent):
    now = datetime.utcnow()
    mem_session.add(
        Dataset(
            id="ds_ctx",
            name="Ctx",
            data='[{"question": "q"}]',
            item_count=1,
            created_at=now,
        )
    )
    mem_session.add(
        EvaluationTask(
            id="task_ctx",
            dataset_id="ds_ctx",
            models='["default"]',
            status="paused",
            resume_from=2,
            run_count=3,
            inference_rounds=2,
            llm_model="glm",
            agent_id=sample_agent.id,
            use_bm25=True,
            use_doc_library=True,
            selected_metrics='["faithfulness"]',
            selected_indices='[0, 1]',
            custom_scripts='{"0": "s1"}',
            created_at=now,
        )
    )
    await mem_session.commit()

    ctx = await load_task_resume_context(mem_session, "task_ctx")
    assert ctx.task_id == "task_ctx"
    assert ctx.resume_from == 2
    assert ctx.inference_rounds == 2
    assert ctx.use_bm25 is True
    assert ctx.use_doc_library is True
    assert ctx.selected_metrics == ["faithfulness"]
    assert ctx.selected_indices == [0, 1]
    assert ctx.custom_scripts == {"0": "s1"}
    assert ctx.is_online is True


@pytest.mark.asyncio
async def test_resume_context_stability_disabled_keeps_rounds(mem_session, sample_agent):
    """enable_stability=False 不再强制单轮，inference_rounds 保持用户配置。"""
    now = datetime.utcnow()
    mem_session.add(
        Dataset(
            id="ds_stab_off",
            name="StabOff",
            data='[{"question": "q"}]',
            item_count=1,
            created_at=now,
        )
    )
    mem_session.add(
        EvaluationTask(
            id="task_stab_off",
            dataset_id="ds_stab_off",
            models='["default"]',
            status="paused",
            resume_from=0,
            inference_rounds=3,  # 用户设 3 轮
            llm_model="glm",
            agent_id=sample_agent.id,
            enable_stability=False,  # 关闭稳定性
            created_at=now,
        )
    )
    await mem_session.commit()

    ctx = await load_task_resume_context(mem_session, "task_stab_off")
    # 稳定性关闭不影响推理轮数，多轮质量平均仍可用
    assert ctx.inference_rounds == 3
    assert ctx.task_id == "task_stab_off"


@pytest.mark.asyncio
async def test_resume_evaluation_pipeline_uses_task_bm25_flags(mem_session, sample_agent):
    """H-04：恢复时 BM25 选项从 task 行读取，而非 resume API 参数。"""
    now = datetime.utcnow()
    mem_session.add(
        Dataset(
            id="ds_bm25",
            name="BM25",
            data='[{"question": "q", "ground_truth": "gt", "model_answer": "a"}]',
            item_count=1,
            created_at=now,
        )
    )
    mem_session.add(
        EvaluationTask(
            id="task_bm25",
            dataset_id="ds_bm25",
            models='["default"]',
            status="paused",
            resume_from=0,
            run_count=1,
            llm_model="glm",
            use_bm25=True,
            use_doc_library=True,
            selected_metrics='["faithfulness"]',
        )
    )
    await mem_session.commit()

    captured: dict = {}

    async def _fake_evaluate_single(*args, **kwargs):
        captured.update(kwargs)
        return {"score": 0.9, "reason": "ok", "duration_ms": 1}

    with (
        patch("services.evaluation_pipeline.async_session") as mock_async_session,
        patch("tasks.async_session") as mock_tasks_session,
        patch("tasks.check_task_paused", new=AsyncMock(return_value=False)),
        patch(
            "tasks.evaluate_single",
            new=AsyncMock(side_effect=_fake_evaluate_single),
        ),
        patch("tasks.refresh_llm_models_cache_task", new=AsyncMock()),
        patch("tasks.emit_event", new=AsyncMock()),
        patch(
            "evaluators.llm_client.generate_evaluation_summary",
            new=AsyncMock(return_value="summary"),
        ),
    ):
        mock_async_session.return_value.__aenter__ = AsyncMock(return_value=mem_session)
        mock_async_session.return_value.__aexit__ = AsyncMock(return_value=None)
        mock_tasks_session.return_value.__aenter__ = AsyncMock(return_value=mem_session)
        mock_tasks_session.return_value.__aexit__ = AsyncMock(return_value=None)

        await resume_evaluation_pipeline(task_id="task_bm25")

    assert captured.get("use_bm25") is True
    assert captured.get("use_doc_library") is True


@pytest.mark.asyncio
async def test_check_task_paused_returns_true_for_cancelled(mem_session, sample_agent):
    """取消（status=CANCELLED，未写 paused_at）也应让 check_task_paused 返回 True，使 pipeline 退出。"""
    now = datetime.utcnow()
    mem_session.add(
        EvaluationTask(
            id="task_cancelled",
            dataset_id="ds_c",
            models='["default"]',
            status="cancelled",
            run_count=1,
            inference_rounds=1,
            llm_model="glm",
            agent_id=sample_agent.id,
            created_at=now,
        )
    )
    await mem_session.commit()

    with patch("tasks.async_session") as mock_tasks_session:
        mock_tasks_session.return_value.__aenter__ = AsyncMock(return_value=mem_session)
        mock_tasks_session.return_value.__aexit__ = AsyncMock(return_value=None)
        from tasks import check_task_paused

        assert await check_task_paused("task_cancelled") is True


@pytest.mark.asyncio
async def test_persist_pause_checkpoint_keeps_cancelled_status(mem_session, sample_agent):
    """取消任务在检查点退出时，persist_pause_checkpoint 不应把 status 覆盖为 PAUSED，也不写 paused_at。"""
    from services.evaluation_pipeline import persist_pause_checkpoint

    now = datetime.utcnow()
    mem_session.add(
        EvaluationTask(
            id="task_cancelled2",
            dataset_id="ds_c2",
            models='["default"]',
            status="cancelled",
            run_count=1,
            inference_rounds=1,
            llm_model="glm",
            agent_id=sample_agent.id,
            created_at=now,
        )
    )
    await mem_session.commit()

    await persist_pause_checkpoint(mem_session, "task_cancelled2", question_id=3)
    await mem_session.commit()

    result = await mem_session.execute(
        select(EvaluationTask).where(EvaluationTask.id == "task_cancelled2")
    )
    task = result.scalar_one()
    assert task.status == "cancelled"  # 保持取消，未被覆盖为 paused
    assert task.paused_at is None  # 不写 paused_at，避免前端误判为可恢复
    assert task.resume_from == 3  # 仍记录断点留痕
