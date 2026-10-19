"""
历史任务 backfill（A6 / TD-P2-08）：对旧 AgentRun 重跑 Builder（Span + Metric + Checkpoint）。

用法（命令行）：
    cd backend
    python -m services.backfill_span_metrics [--agent-id AGENT_ID] [--batch-size 50] [--dry-run]

功能：
- 查询指定条件下的 AgentRun（trace_sync_status != synced 或指定 agent_id）
- 对每条 Run 通过 internal_sync 拉取 Langfuse trace
- 重跑 build_facts_for_trace（Span + Metric + Checkpoint + eval.operation.id 注入）
- 更新 trace_sync_status = synced

设计原则：
- 限流：batch_size 控制每批处理数量
- 幂等：Builder 本身幂等，重复执行结果一致
- dry-run：仅打印待处理 Run，不执行物化
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import List, Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, async_session
from logger import log
from services.runtime_fact_builder import build_facts_for_trace


async def _fetch_trace_from_langfuse(run: AgentRun) -> Optional[dict]:
    """从 Langfuse API 拉取 trace 数据。"""
    from services.langfuse_client import get_langfuse_client

    trace_id = run.trace_id
    project = run.langfuse_project
    if not trace_id:
        return None

    try:
        client = get_langfuse_client(project)
        trace_data = await client.fetch_trace_detail(trace_id)
        return trace_data
    except Exception as e:
        log(
            "WARNING",
            "BACKFILL",
            "Langfuse trace 拉取失败",
            agent_run_id=run.id,
            trace_id=trace_id,
            error=str(e),
        )
        return None


async def backfill_runs(
    session: AsyncSession,
    *,
    agent_id: Optional[str] = None,
    logical_agent_id: Optional[str] = None,
    batch_size: int = 50,
    dry_run: bool = False,
    status_filter: Optional[str] = None,
) -> dict:
    """对符合条件的 AgentRun 重跑 Builder。

    Args:
        agent_id: 过滤指定 Agent ID
        logical_agent_id: 过滤指定 logical_agent_id
        batch_size: 每批处理数量
        dry_run: 仅打印，不执行
        status_filter: 过滤 trace_sync_status（默认未 synced 的）

    Returns:
        统计字典：total / success / failed / skipped
    """
    stmt = select(AgentRun).where(AgentRun.trace_id.isnot(None))
    if agent_id:
        stmt = stmt.where(AgentRun.agent_id == agent_id)
    if logical_agent_id:
        stmt = stmt.where(AgentRun.logical_agent_id == logical_agent_id)
    if status_filter:
        stmt = stmt.where(AgentRun.trace_sync_status == status_filter)
    else:
        # 默认：处理未 synced 的 Run
        stmt = stmt.where(
            (AgentRun.trace_sync_status != "synced")
            | (AgentRun.trace_sync_status.is_(None))
        )
    stmt = stmt.limit(batch_size)

    result = await session.execute(stmt)
    runs = list(result.scalars().all())

    stats = {"total": len(runs), "success": 0, "failed": 0, "skipped": 0}

    if dry_run:
        for run in runs:
            log(
                "INFO",
                "BACKFILL_DRY_RUN",
                "待处理 AgentRun",
                agent_run_id=run.id,
                trace_id=run.trace_id,
                agent_id=run.agent_id,
                sync_status=run.trace_sync_status,
            )
        stats["skipped"] = len(runs)
        return stats

    for run in runs:
        try:
            trace = await _fetch_trace_from_langfuse(run)
            if trace is None:
                stats["skipped"] += 1
                log(
                    "WARNING",
                    "BACKFILL",
                    "trace 拉取为空，跳过",
                    agent_run_id=run.id,
                )
                continue

            result = await build_facts_for_trace(session, run, trace)
            if result.get("incomplete_materialization"):
                run.trace_sync_status = "synced_incomplete"
            else:
                run.trace_sync_status = "synced"
            await session.flush()
            stats["success"] += 1

            log(
                "INFO",
                "BACKFILL",
                "backfill 成功",
                agent_run_id=run.id,
                tokens_total=result.get("tokens_total"),
                span_nodes=result.get("span_nodes"),
                injected_ops=result.get("injected_operation_ids"),
            )
        except Exception as e:
            stats["failed"] += 1
            log(
                "ERROR",
                "BACKFILL",
                "backfill 失败",
                agent_run_id=run.id,
                error=str(e),
            )
            await session.rollback()

    return stats


async def main(args: Optional[List[str]] = None) -> None:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(description="历史任务 backfill：重跑 Builder")
    parser.add_argument("--agent-id", help="过滤指定 Agent ID")
    parser.add_argument("--logical-agent-id", help="过滤指定 logical_agent_id")
    parser.add_argument("--batch-size", type=int, default=50, help="每批处理数量")
    parser.add_argument("--dry-run", action="store_true", help="仅打印待处理 Run")
    parser.add_argument(
        "--status",
        dest="status_filter",
        help="过滤 trace_sync_status（默认未 synced）",
    )
    parsed = parser.parse_args(args)

    async with async_session() as session:
        stats = await backfill_runs(
            session,
            agent_id=parsed.agent_id,
            logical_agent_id=parsed.logical_agent_id,
            batch_size=parsed.batch_size,
            dry_run=parsed.dry_run,
            status_filter=parsed.status_filter,
        )
        log("INFO", "BACKFILL", "backfill 完成", **stats)
        print(f"Backfill 完成: {stats}")


if __name__ == "__main__":
    asyncio.run(main())
