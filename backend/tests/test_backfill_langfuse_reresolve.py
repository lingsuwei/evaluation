"""retry 端点 langfuse_project 重解析机制单测。

覆盖 onto-agent 侧改了 Langfuse 配置但 eval 未同步的场景：
retry 时从 Agent.inference_config 重新解析 langfuse_project，
回写 Agent/AgentRun/Job 三表后重置为 pending。
"""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database import Agent, AgentRun, Base, TraceSyncJob
from services.backfill_trace_sync_service import (
    re_resolve_langfuse_project_for_failed_jobs,
)
from services.eval_context import build_eval_context


@pytest.fixture
async def mem_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _make_agent(
    agent_id: str,
    *,
    inference_config: str | None = json.dumps(
        {"url": "http://onto-agent:5055", "workspace_id": "ws1"}
    ),
    langfuse_project: str = "old-project",
) -> Agent:
    return Agent(
        id=agent_id,
        name=f"agent_{agent_id}",
        inference_config=inference_config,
        langfuse_project=langfuse_project,
        agent_kind="single",
    )


def _make_run(
    run_id: str,
    task_id: str,
    agent_id: str,
    *,
    trace_id: str = "tr_1",
    langfuse_project: str = "old-project",
    trace_sync_status: str = "failed",
) -> AgentRun:
    return AgentRun(
        id=run_id,
        origin="evaluation",
        eval_context=build_eval_context(
            task_id=task_id, dataset_id="d1", question_id=0, run_index=0
        ),
        task_id=task_id,
        dataset_id="d1",
        question_id=0,
        agent_id=agent_id,
        status="success",
        trace_id=trace_id,
        langfuse_project=langfuse_project,
        trace_sync_status=trace_sync_status,
        started_at=datetime.utcnow(),
        agent_snapshot="{}",
    )


def _make_failed_job(
    job_id: str,
    run_id: str,
    task_id: str,
    *,
    trace_id: str = "tr_1",
    langfuse_project: str = "old-project",
    failure_category: str = "trace_not_found",
    attempts: int = 5,
) -> TraceSyncJob:
    return TraceSyncJob(
        id=job_id,
        agent_run_id=run_id,
        task_id=task_id,
        trace_id=trace_id,
        langfuse_project=langfuse_project,
        status="failed",
        attempts=attempts,
        last_error="Langfuse 资源不存在",
        failure_category=failure_category,
        created_at=datetime.utcnow(),
    )


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reresolve_normal_case(mem_session):
    """onto-agent 返回新 project，Agent/AgentRun/Job 三表都更新，job 重置为 pending。"""
    agent = _make_agent("ag1", langfuse_project="old-project")
    run = _make_run("run1", "task1", "ag1", langfuse_project="old-project")
    job = _make_failed_job("tsj1", "run1", "task1", langfuse_project="old-project")
    mem_session.add_all([agent, run, job])
    await mem_session.commit()

    mock_resolve = AsyncMock(
        return_value={"status": "matched", "langfuse_project": "new-project"}
    )
    with patch(
        "services.agent_probe_service.resolve_langfuse_project_from_inference_config",
        mock_resolve,
    ):
        stats = await re_resolve_langfuse_project_for_failed_jobs(
            mem_session, "task1"
        )

    assert stats.matched == 1
    assert stats.re_resolved == 1
    assert stats.fetch_failed == 0

    # 验证三表回写（expire 后重新查 DB）
    mem_session.expire_all()
    agent_db = (
        await mem_session.execute(select(Agent).where(Agent.id == "ag1"))
    ).scalar_one()
    run_db = (
        await mem_session.execute(select(AgentRun).where(AgentRun.id == "run1"))
    ).scalar_one()
    job_db = (
        await mem_session.execute(select(TraceSyncJob).where(TraceSyncJob.id == "tsj1"))
    ).scalar_one()
    assert agent_db.langfuse_project == "new-project"
    assert run_db.langfuse_project == "new-project"
    assert job_db.langfuse_project == "new-project"

    # 验证 job 重置
    assert job.status == "pending"
    assert job.attempts == 0
    assert job.last_error is None
    assert job.failure_category is None
    assert run.trace_sync_status == "pending_sync"

    # 验证 resolve 被调用且传入正确 config
    mock_resolve.assert_called_once()
    call_args = mock_resolve.call_args[0][0]
    assert call_args["url"] == "http://onto-agent:5055"


@pytest.mark.asyncio
async def test_reresolve_onto_agent_unreachable(mem_session):
    """onto-agent 不可达：resolve 返回 fetch_failed，不更新任何表，job 保持 failed。"""
    agent = _make_agent("ag1")
    run = _make_run("run1", "task1", "ag1")
    job = _make_failed_job("tsj1", "run1", "task1")
    mem_session.add_all([agent, run, job])
    await mem_session.commit()

    with patch(
        "services.agent_probe_service.resolve_langfuse_project_from_inference_config",
        AsyncMock(return_value={"status": "fetch_failed", "langfuse_project": None}),
    ):
        stats = await re_resolve_langfuse_project_for_failed_jobs(
            mem_session, "task1"
        )

    assert stats.matched == 1
    assert stats.fetch_failed == 1
    assert stats.re_resolved == 0

    # 验证未回写
    mem_session.expire_all()
    job_db = (
        await mem_session.execute(select(TraceSyncJob).where(TraceSyncJob.id == "tsj1"))
    ).scalar_one()
    assert job_db.status == "failed"
    assert job_db.attempts == 5
    assert job_db.langfuse_project == "old-project"
    assert job_db.failure_category == "trace_not_found"


@pytest.mark.asyncio
async def test_reresolve_inference_config_missing(mem_session):
    """Agent.inference_config 为空：fetch_failed。"""
    agent = _make_agent("ag1", inference_config=None)
    run = _make_run("run1", "task1", "ag1")
    job = _make_failed_job("tsj1", "run1", "task1")
    mem_session.add_all([agent, run, job])
    await mem_session.commit()

    mock_resolve = AsyncMock()
    with patch(
        "services.agent_probe_service.resolve_langfuse_project_from_inference_config",
        mock_resolve,
    ):
        stats = await re_resolve_langfuse_project_for_failed_jobs(
            mem_session, "task1"
        )

    assert stats.fetch_failed == 1
    assert stats.re_resolved == 0
    # inference_config 为空时不应调用 onto-agent
    mock_resolve.assert_not_called()


@pytest.mark.asyncio
async def test_reresolve_inference_config_invalid_json(mem_session):
    """inference_config JSON 解析失败：fetch_failed。"""
    agent = _make_agent("ag1", inference_config="{invalid json")
    run = _make_run("run1", "task1", "ag1")
    job = _make_failed_job("tsj1", "run1", "task1")
    mem_session.add_all([agent, run, job])
    await mem_session.commit()

    mock_resolve = AsyncMock()
    with patch(
        "services.agent_probe_service.resolve_langfuse_project_from_inference_config",
        mock_resolve,
    ):
        stats = await re_resolve_langfuse_project_for_failed_jobs(
            mem_session, "task1"
        )

    assert stats.fetch_failed == 1
    mock_resolve.assert_not_called()


@pytest.mark.asyncio
async def test_reresolve_same_project(mem_session):
    """重解析结果与旧值相同：unchanged 计数+1，仍重置 attempts。"""
    agent = _make_agent("ag1", langfuse_project="same-project")
    run = _make_run("run1", "task1", "ag1", langfuse_project="same-project")
    job = _make_failed_job(
        "tsj1", "run1", "task1", langfuse_project="same-project"
    )
    mem_session.add_all([agent, run, job])
    await mem_session.commit()

    with patch(
        "services.agent_probe_service.resolve_langfuse_project_from_inference_config",
        AsyncMock(
            return_value={"status": "matched", "langfuse_project": "same-project"}
        ),
    ):
        stats = await re_resolve_langfuse_project_for_failed_jobs(
            mem_session, "task1"
        )

    assert stats.unchanged == 1
    assert stats.re_resolved == 0

    # 仍应重置 job
    mem_session.expire_all()
    job_db = (
        await mem_session.execute(select(TraceSyncJob).where(TraceSyncJob.id == "tsj1"))
    ).scalar_one()
    assert job_db.status == "pending"
    assert job_db.attempts == 0
    assert job_db.last_error is None


@pytest.mark.asyncio
async def test_reresolve_agent_missing(mem_session):
    """Agent 表无对应记录：agent_missing。"""
    run = _make_run("run1", "task1", "ghost_agent")
    job = _make_failed_job("tsj1", "run1", "task1")
    mem_session.add_all([run, job])
    await mem_session.commit()

    stats = await re_resolve_langfuse_project_for_failed_jobs(mem_session, "task1")

    assert stats.agent_missing == 1
    assert stats.re_resolved == 0


@pytest.mark.asyncio
async def test_reresolve_multiple_jobs_same_agent(mem_session):
    """多个 failed jobs 属于同一 agent：只探测一次 onto-agent，复用缓存。"""
    agent = _make_agent("ag1", langfuse_project="old-project")
    run = _make_run("run1", "task1", "ag1", langfuse_project="old-project")
    job1 = _make_failed_job("tsj1", "run1", "task1", langfuse_project="old-project")
    job2 = _make_failed_job("tsj2", "run1", "task1", langfuse_project="old-project")
    job3 = _make_failed_job("tsj3", "run1", "task1", langfuse_project="old-project")
    mem_session.add_all([agent, run, job1, job2, job3])
    await mem_session.commit()

    mock_resolve = AsyncMock(
        return_value={"status": "matched", "langfuse_project": "new-project"}
    )
    with patch(
        "services.agent_probe_service.resolve_langfuse_project_from_inference_config",
        mock_resolve,
    ):
        stats = await re_resolve_langfuse_project_for_failed_jobs(
            mem_session, "task1"
        )

    assert stats.matched == 3
    assert stats.re_resolved == 3
    # 只调用一次 onto-agent（缓存复用）
    mock_resolve.assert_called_once()


@pytest.mark.asyncio
async def test_reresolve_filters_by_failure_category(mem_session):
    """只处理 failure_category=trace_not_found，transient/unknown 不处理。"""
    agent = _make_agent("ag1")
    run = _make_run("run1", "task1", "ag1")
    job_tnf = _make_failed_job(
        "tsj_tnf", "run1", "task1", failure_category="trace_not_found"
    )
    job_transient = _make_failed_job(
        "tsj_tr", "run1", "task1", failure_category="transient"
    )
    job_unknown = _make_failed_job(
        "tsj_unk", "run1", "task1", failure_category="unknown"
    )
    mem_session.add_all([agent, run, job_tnf, job_transient, job_unknown])
    await mem_session.commit()

    mock_resolve = AsyncMock(
        return_value={"status": "matched", "langfuse_project": "new-project"}
    )
    with patch(
        "services.agent_probe_service.resolve_langfuse_project_from_inference_config",
        mock_resolve,
    ):
        stats = await re_resolve_langfuse_project_for_failed_jobs(
            mem_session, "task1"
        )

    # 只匹配 trace_not_found
    assert stats.matched == 1
    assert stats.re_resolved == 1

    # transient/unknown 的 job 保持 failed
    mem_session.expire_all()
    job_tr_db = (
        await mem_session.execute(select(TraceSyncJob).where(TraceSyncJob.id == "tsj_tr"))
    ).scalar_one()
    job_unk_db = (
        await mem_session.execute(select(TraceSyncJob).where(TraceSyncJob.id == "tsj_unk"))
    ).scalar_one()
    assert job_tr_db.status == "failed"
    assert job_unk_db.status == "failed"


@pytest.mark.asyncio
async def test_reresolve_no_matching_jobs(mem_session):
    """无符合条件的 failed jobs：matched=0，无副作用。"""
    stats = await re_resolve_langfuse_project_for_failed_jobs(
        mem_session, "empty_task"
    )
    assert stats.matched == 0
    assert stats.re_resolved == 0
