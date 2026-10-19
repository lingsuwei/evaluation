"""Pipeline 与 Trace Sync 集成（TS-INT-*）"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Agent, AgentRun, Base, TraceSyncJob
from services.onto_agent_trace_id import stable_langfuse_trace_id


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
        id="ag_pipe",
        name="PipeAgent",
        logical_agent_id="la_pipe",
        version="1.0",
        inference_config='{"model": "test"}',
        created_at=datetime.utcnow(),
    )


@pytest.mark.asyncio
async def test_pipeline_enqueues_job_when_trace_id_present(mem_session, sample_agent):
    """推理完成后自动入队 Trace Sync Worker，状态为 pending_sync。"""
    mem_session.add(sample_agent)
    await mem_session.commit()

    mock_inference = {
        "model_answer": "answer",
        "trace_id": "trace-from-inference",
        "tokens_total": 12,
    }

    with patch(
        "services.evaluation_pipeline.InferenceService.call_inference",
        new=AsyncMock(return_value=mock_inference),
    ):
        from services.evaluation_pipeline import execute_online_inference_with_run

        await execute_online_inference_with_run(
            mem_session,
            task_id="task_pipe",
            dataset_id="ds_pipe",
            question_id=0,
            run_index=0,
            question="q?",
            ground_truth="gt",
            agent=sample_agent,
            inference_config={
                "provider": "DeepSeek",
                "model": "deepseek-chat",
                "model_version": "v3",
            },
            emit_inference_events=False,
            dataset_item_id="ditem_test_01",
            question_key="qk_test",
        )
        await mem_session.commit()

    # Worker 入队：应有一条 pending job
    jobs = (
        await mem_session.execute(select(TraceSyncJob))
    ).scalars().all()
    assert len(jobs) == 1

    run = (await mem_session.execute(select(AgentRun))).scalar_one()
    assert run.trace_sync_status == "pending_sync"
    assert run.trace_id == "trace-from-inference"
    assert run.root_trace_id == "trace-from-inference"
    # COUP-C04：Pipeline 不再写 tokens_total 头表，物化前为 None
    assert run.tokens_total is None
    # tokens_total 信息仍保留在 output_snapshot 中，供 Builder 合并
    assert run.eval_context is not None
    assert run.eval_context.get("task_id") == "task_pipe"
    assert run.eval_context.get("dataset_item_id")
    assert run.origin == "evaluation"
    assert run.input_snapshot is not None
    assert run.input_snapshot.get("question") == "q?"
    assert run.output_snapshot is not None
    assert "answer" in (run.output_snapshot.get("model_answer") or "")
    assert run.inference_model_provider == "DeepSeek"
    assert run.inference_model_name == "deepseek-chat"
    assert run.inference_model_version == "v3"


@pytest.mark.asyncio
async def test_pipeline_derives_trace_id_from_onto_agent_run_and_session(
    mem_session, sample_agent
):
    """sync 响应含 run_id/session_id 时，按 onto-agent 稳定规则写入 trace_id。"""
    mem_session.add(sample_agent)
    await mem_session.commit()

    run_id = "c8774ff6-edf1-4e66-9c78-b2c3dfdfe0f7"
    session_id = "sync_d10af000"
    expected_trace = stable_langfuse_trace_id(session_id, run_id)

    with patch(
        "services.evaluation_pipeline.InferenceService.call_inference",
        new=AsyncMock(
            return_value={
                "model_answer": "answer",
                "onto_agent_run_id": run_id,
                "onto_agent_session_id": session_id,
                "trace_id": expected_trace,
            }
        ),
    ):
        from services.evaluation_pipeline import execute_online_inference_with_run

        await execute_online_inference_with_run(
            mem_session,
            task_id="task_derive",
            dataset_id="ds",
            question_id=0,
            run_index=0,
            question="q",
            ground_truth=None,
            agent=sample_agent,
            inference_config={},
            emit_inference_events=False,
        )
        await mem_session.commit()

    run = (await mem_session.execute(select(AgentRun))).scalar_one()
    assert run.trace_id == expected_trace
    assert run.root_trace_id == expected_trace
    assert run.trace_sync_status == "pending_sync"


@pytest.mark.asyncio
async def test_pipeline_skips_enqueue_without_trace_id(mem_session, sample_agent):
    mem_session.add(sample_agent)
    await mem_session.commit()

    with patch(
        "services.evaluation_pipeline.InferenceService.call_inference",
        new=AsyncMock(return_value={"model_answer": "", "error": "no trace"}),
    ):
        from services.evaluation_pipeline import execute_online_inference_with_run

        await execute_online_inference_with_run(
            mem_session,
            task_id="task_pipe2",
            dataset_id="ds_pipe",
            question_id=1,
            run_index=0,
            question="q2",
            ground_truth=None,
            agent=sample_agent,
            inference_config={},
            emit_inference_events=False,
        )
        await mem_session.commit()

    jobs = (await mem_session.execute(select(TraceSyncJob))).scalars().all()
    assert len(jobs) == 0

    runs = (await mem_session.execute(select(AgentRun))).scalars().all()
    assert runs[0].trace_sync_status == "skipped"


@pytest.mark.asyncio
async def test_pipeline_skips_langfuse_wrapper_by_default(mem_session, sample_agent):
    """默认 EVAL_LANGFUSE_WRAPPER_ENABLED=false，不向 InferenceService 传 trace_context。"""
    mem_session.add(sample_agent)
    await mem_session.commit()

    mock_inference = AsyncMock(
        return_value={"model_answer": "answer", "trace_id": "trace-agent-only"}
    )
    with patch(
        "services.evaluation_pipeline.InferenceService.call_inference",
        new=mock_inference,
    ):
        from services.evaluation_pipeline import execute_online_inference_with_run

        await execute_online_inference_with_run(
            mem_session,
            task_id="task_no_wrapper",
            dataset_id="ds",
            question_id=0,
            run_index=0,
            question="q",
            ground_truth=None,
            agent=sample_agent,
            inference_config={},
            emit_inference_events=False,
        )

    mock_inference.assert_called_once()
    assert mock_inference.call_args.kwargs.get("trace_context") is None


@pytest.mark.asyncio
async def test_pipeline_passes_trace_context_when_wrapper_enabled(mem_session, sample_agent):
    mem_session.add(sample_agent)
    await mem_session.commit()

    mock_inference = AsyncMock(
        return_value={"model_answer": "answer", "trace_id": "trace-agent-only"}
    )
    with patch(
        "services.evaluation_pipeline.EVAL_LANGFUSE_WRAPPER_ENABLED",
        True,
    ), patch(
        "services.evaluation_pipeline.InferenceService.call_inference",
        new=mock_inference,
    ):
        from services.evaluation_pipeline import execute_online_inference_with_run

        await execute_online_inference_with_run(
            mem_session,
            task_id="task_wrapper",
            dataset_id="ds",
            question_id=0,
            run_index=0,
            question="q",
            ground_truth=None,
            agent=sample_agent,
            inference_config={},
            emit_inference_events=False,
        )

    mock_inference.assert_called_once()
    trace_ctx = mock_inference.call_args.kwargs.get("trace_context")
    assert trace_ctx is not None
    assert trace_ctx.task_id == "task_wrapper"
    assert trace_ctx.agent_name == "PipeAgent"
