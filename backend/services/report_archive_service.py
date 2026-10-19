"""评测报告归档服务：持久化导出的评测报告(xlsx) / 版本对比报告(html)。

仅负责存取（落库 / 列表 / 读取 / 删除）与命名元数据收集，不负责报告内容生成。
报告二进制以 PG BYTEA 存储，与现有全 PG 架构一致。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from database import Agent, Dataset, EvaluationReport, EvaluationTask
from services.agent_workspace_display import workspace_name_from_agent
from services.evaluation_mode import get_evaluation_mode
from services.evaluation_pipeline import parse_task_agent_ids


# ── 命名 ──

def _fmt_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M")


def build_evaluation_title(
    *,
    dataset_name: Optional[str],
    agent_name: Optional[str],
    workspace_name: Optional[str],
    task_count: int,
) -> str:
    parts = [
        "评测报告",
        dataset_name or "—",
        agent_name or "—",
        workspace_name or "—",
        f"{task_count}个任务",
        _fmt_now(),
    ]
    return " · ".join(parts)


def build_comparison_title(
    *,
    dataset_name: Optional[str],
    workspace_name: Optional[str],
    task_count: int,
) -> str:
    parts = [
        "版本对比",
        dataset_name or "—",
        f"{task_count}个任务",
        workspace_name or "—",
        _fmt_now(),
    ]
    return " · ".join(parts)


# ── 元数据收集 ──

async def fetch_evaluation_report_meta(
    session: AsyncSession, task_ids: List[str]
) -> Dict[str, Any]:
    """从任务列表收集评测报告命名/归档所需的元数据。

    - dataset_name：所有任务同评测集则取其名，否则"多个评测集"
    - agent_name / workspace_name：取第一个有 Agent 的在线任务；离线任务记"离线评估"
    """
    if not task_ids:
        return {}

    rows = (
        await session.execute(
            select(EvaluationTask).where(EvaluationTask.id.in_(task_ids))
        )
    ).scalars().all()

    dataset_ids = {r.dataset_id for r in rows if r.dataset_id}
    if len(dataset_ids) == 1:
        ds = await session.get(Dataset, next(iter(dataset_ids)))
        dataset_name = ds.name if ds else None
        dataset_id = next(iter(dataset_ids))
    else:
        dataset_name = "多个评测集" if len(dataset_ids) > 1 else None
        dataset_id = None

    agent_name: Optional[str] = None
    workspace_name: Optional[str] = None
    for task in rows:
        if get_evaluation_mode(task) == "offline":
            continue
        agent_ids = parse_task_agent_ids(task)
        aid = agent_ids[0] if agent_ids else task.agent_id
        if not aid:
            continue
        agent = await session.get(Agent, aid)
        if agent:
            agent_name = agent.name or agent_name
            ws = workspace_name_from_agent(agent)
            if ws:
                workspace_name = ws
            break

    if agent_name is None and any(get_evaluation_mode(t) == "offline" for t in rows):
        agent_name = "离线评估"

    return {
        "dataset_id": dataset_id,
        "dataset_name": dataset_name,
        "agent_name": agent_name,
        "workspace_name": workspace_name,
        "task_count": len(rows),
    }


async def fetch_workspace_name_for_agents(
    session: AsyncSession, agent_ids: List[str]
) -> Optional[str]:
    """从一组 Agent 中取第一个非空工作空间展示名。"""
    for aid in agent_ids:
        if not aid:
            continue
        agent = await session.get(Agent, aid)
        if agent:
            ws = workspace_name_from_agent(agent)
            if ws:
                return ws
    return None


# ── 存取 ──

async def save_report(
    session: AsyncSession,
    *,
    report_type: str,
    title: str,
    fmt: str,
    content: bytes,
    task_ids: Optional[List[str]] = None,
    baseline_task_id: Optional[str] = None,
    dataset_id: Optional[str] = None,
    dataset_name: Optional[str] = None,
    agent_name: Optional[str] = None,
    workspace_name: Optional[str] = None,
    verdict_status: Optional[str] = None,
    score: Optional[float] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> EvaluationReport:
    """落库一份报告，返回实体。"""
    report = EvaluationReport(
        id=str(uuid.uuid4()),
        report_type=report_type,
        title=title,
        format=fmt,
        task_ids=task_ids,
        baseline_task_id=baseline_task_id,
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        agent_name=agent_name,
        workspace_name=workspace_name,
        content=content,
        file_size=len(content) if content else 0,
        verdict_status=verdict_status,
        score=score,
        meta=meta,
        created_at=datetime.utcnow(),
    )
    session.add(report)
    await session.commit()
    await session.refresh(report)
    return report


def _row_to_meta(r: EvaluationReport) -> Dict[str, Any]:
    """实体 → 列表/详情元数据（不含 content 二进制）。"""
    return {
        "id": r.id,
        "report_type": r.report_type,
        "title": r.title,
        "format": r.format,
        "task_ids": r.task_ids,
        "baseline_task_id": r.baseline_task_id,
        "dataset_id": r.dataset_id,
        "dataset_name": r.dataset_name,
        "agent_name": r.agent_name,
        "workspace_name": r.workspace_name,
        "file_size": r.file_size,
        "verdict_status": r.verdict_status,
        "score": r.score,
        "meta": r.meta,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


async def list_reports(
    session: AsyncSession,
    *,
    report_type: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    """分页列表，按 created_at 倒序，不加载 content。"""
    stmt = select(EvaluationReport).options(defer(EvaluationReport.content))
    if report_type:
        stmt = stmt.where(EvaluationReport.report_type == report_type)
    stmt = stmt.order_by(EvaluationReport.created_at.desc())

    # 总数
    from sqlalchemy import func

    count_stmt = select(func.count()).select_from(EvaluationReport)
    if report_type:
        count_stmt = count_stmt.where(EvaluationReport.report_type == report_type)
    total = (await session.execute(count_stmt)).scalar() or 0

    stmt = stmt.limit(page_size).offset((max(page, 1) - 1) * page_size)
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_row_to_meta(r) for r in rows],
    }


async def get_report_meta(
    session: AsyncSession, report_id: str
) -> Optional[Dict[str, Any]]:
    """单条元数据（不含 content）。"""
    stmt = (
        select(EvaluationReport)
        .options(defer(EvaluationReport.content))
        .where(EvaluationReport.id == report_id)
    )
    r = (await session.execute(stmt)).scalar_one_or_none()
    return _row_to_meta(r) if r else None


async def get_report_content(
    session: AsyncSession, report_id: str
) -> Optional[Tuple[bytes, str, str]]:
    """返回 (content, format, title)，用于下载/预览。"""
    r = await session.get(EvaluationReport, report_id)
    if not r:
        return None
    return (r.content or b"", r.format or "bin", r.title or "report")


async def delete_report(session: AsyncSession, report_id: str) -> bool:
    """删除一份报告，返回是否命中。"""
    r = await session.get(EvaluationReport, report_id)
    if not r:
        return False
    await session.delete(r)
    await session.commit()
    return True
