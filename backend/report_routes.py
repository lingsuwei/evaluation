"""评测报告归档 API：生成并落库评测报告(xlsx) / 版本对比报告(html)，提供列表/预览/下载/删除。

- POST /reports/comparison  生成并归档版本对比报告(HTML)
- POST /reports/evaluation  生成并归档评测报告(xlsx)
- GET  /reports             分页列表
- GET  /reports/{id}        单条元数据
- GET  /reports/{id}/preview  HTML 内联预览
- GET  /reports/{id}/download 下载
- DELETE /reports/{id}      删除
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from services.dataset_io_service import content_disposition_attachment
from services.report_archive_service import (
    build_comparison_title,
    build_evaluation_title,
    delete_report,
    fetch_evaluation_report_meta,
    fetch_workspace_name_for_agents,
    get_report_content,
    get_report_meta,
    list_reports,
    save_report,
)

report_router = APIRouter(prefix="/reports", tags=["Reports"])


class ComparisonReportRequest(BaseModel):
    task_ids: List[str] = Field(..., min_length=2, max_length=6)
    baseline_task_id: Optional[str] = None
    title: Optional[str] = Field(default=None, description="自定义报告标题；留空则自动生成")


class EvaluationReportRequest(BaseModel):
    task_ids: List[str] = Field(..., min_length=1)
    title: Optional[str] = Field(default=None, description="自定义报告标题；留空则自动生成")


def _ext_for(fmt: str) -> str:
    return "html" if fmt == "html" else "xlsx"


def _mime_for(fmt: str) -> str:
    if fmt == "html":
        return "text/html; charset=utf-8"
    if fmt == "xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return "application/octet-stream"


@report_router.post("/comparison")
async def post_comparison_report(
    body: ComparisonReportRequest,
    session: AsyncSession = Depends(get_session),
):
    """生成版本对比报告(HTML)并归档。"""
    from services.cross_task_scenario_service import (
        CrossTaskCockpitError,
        build_cross_task_scenario_matrix,
    )
    from services.comparison_report_html_service import build_comparison_html

    try:
        payload = await build_cross_task_scenario_matrix(
            session,
            body.task_ids,
            baseline_task_id=body.baseline_task_id,
            use_cache=True,  # 复用矩阵展示的计算结果，避免重复计算
        )
    except CrossTaskCockpitError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        import logging
        logging.exception("对比报告生成失败")
        raise HTTPException(500, f"报告生成失败: {e}") from e

    members = payload.get("members") or []
    agent_ids = [m.get("agent_id") for m in members if m.get("agent_id")]
    workspace_name = await fetch_workspace_name_for_agents(session, agent_ids)
    dataset_name = payload.get("dataset_name")
    task_count = len(payload.get("task_ids") or body.task_ids)

    title = body.title or build_comparison_title(
        dataset_name=dataset_name,
        workspace_name=workspace_name,
        task_count=task_count,
    )

    content = build_comparison_html(payload, title=title)

    verdict = payload.get("overall_verdict") or {}
    meta = {
        "member_count": len(members),
        "members": [
            {"label": m.get("label"), "is_baseline": m.get("is_baseline")}
            for m in members
        ],
        "scenario_key": payload.get("scenario_key"),
        "dataset_id": payload.get("dataset_id"),
    }

    report = await save_report(
        session,
        report_type="comparison",
        title=title,
        fmt="html",
        content=content,
        task_ids=payload.get("task_ids") or body.task_ids,
        baseline_task_id=payload.get("baseline_member_id") or body.baseline_task_id,
        dataset_id=payload.get("dataset_id"),
        dataset_name=dataset_name,
        workspace_name=workspace_name,
        verdict_status=verdict.get("status"),
        score=verdict.get("score"),
        meta=meta,
    )
    return await get_report_meta(session, report.id)


@report_router.post("/evaluation")
async def post_evaluation_report(
    body: EvaluationReportRequest,
    session: AsyncSession = Depends(get_session),
):
    """生成评测报告(xlsx)并归档。"""
    from services.export_report_service import build_export_report

    try:
        content = await build_export_report(session, body.task_ids)
    except Exception as e:
        import logging
        logging.exception("评测报告生成失败")
        raise HTTPException(500, f"报告生成失败: {e}") from e
    if content is None:
        raise HTTPException(404, "未找到可导出的任务")

    meta_info = await fetch_evaluation_report_meta(session, body.task_ids)
    title = body.title or build_evaluation_title(
        dataset_name=meta_info.get("dataset_name"),
        agent_name=meta_info.get("agent_name"),
        workspace_name=meta_info.get("workspace_name"),
        task_count=meta_info.get("task_count") or len(body.task_ids),
    )

    report = await save_report(
        session,
        report_type="evaluation",
        title=title,
        fmt="xlsx",
        content=content,
        task_ids=body.task_ids,
        dataset_id=meta_info.get("dataset_id"),
        dataset_name=meta_info.get("dataset_name"),
        agent_name=meta_info.get("agent_name"),
        workspace_name=meta_info.get("workspace_name"),
        meta={"task_count": meta_info.get("task_count") or len(body.task_ids)},
    )
    return await get_report_meta(session, report.id)


@report_router.get("")
async def get_reports(
    report_type: Optional[str] = Query(None, description="evaluation | comparison"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    return await list_reports(
        session, report_type=report_type, page=page, page_size=page_size
    )


@report_router.get("/{report_id}")
async def get_report_detail(
    report_id: str,
    session: AsyncSession = Depends(get_session),
):
    meta = await get_report_meta(session, report_id)
    if not meta:
        raise HTTPException(404, "报告不存在")
    return meta


@report_router.get("/{report_id}/preview")
async def preview_report(
    report_id: str,
    session: AsyncSession = Depends(get_session),
):
    """HTML 报告内联预览（浏览器内打开）。"""
    data = await get_report_content(session, report_id)
    if not data:
        raise HTTPException(404, "报告不存在")
    content, fmt, _title = data
    if fmt != "html":
        raise HTTPException(400, "仅 HTML 报告支持预览")
    return Response(
        content=content,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": "inline"},
    )


@report_router.get("/{report_id}/download")
async def download_report(
    report_id: str,
    session: AsyncSession = Depends(get_session),
):
    """下载报告文件。"""
    data = await get_report_content(session, report_id)
    if not data:
        raise HTTPException(404, "报告不存在")
    content, fmt, title = data
    filename = f"{title or 'report'}.{_ext_for(fmt)}"
    return Response(
        content=content,
        media_type=_mime_for(fmt),
        headers={"Content-Disposition": content_disposition_attachment(filename)},
    )


@report_router.delete("/{report_id}")
async def delete_report_endpoint(
    report_id: str,
    session: AsyncSession = Depends(get_session),
):
    ok = await delete_report(session, report_id)
    if not ok:
        raise HTTPException(404, "报告不存在")
    return {"ok": True}
