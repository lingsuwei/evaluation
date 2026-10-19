"""
过程点配置 API（v2.1 M2）
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import LANGFUSE_ENABLED
from database import Agent, ProcessCheckpointTemplate, get_session
from schemas import (
    CheckpointPreviewRequest,
    CheckpointPreviewMatchRequest,
    CheckpointBatchPreviewRequest,
    CheckpointBatchPreviewResponse,
    CheckpointNormalizeWeightsRequest,
    CheckpointNormalizeWeightsResponse,
    CheckpointReferenceCatalogResponse,
    CheckpointSampleRunsResponse,
    CheckpointReorderRequest,
    CheckpointSeedFromReferenceRequest,
    CheckpointSeedFromReferenceResponse,
    CheckpointTemplateCreateRequest,
    CheckpointTemplateResponse,
    CheckpointTemplateUpdateRequest,
)
from services.checkpoint_reference_service import (
    get_onto_agent_reference_catalog,
    seed_onto_agent_reference_for_agent_name,
)
from services.checkpoint_weight_service import normalize_checkpoint_weights_for_agent
from services.checkpoint_batch_service import clear_checkpoint_template_cache
from services.checkpoint_verify_service import (
    batch_preview_checkpoints_for_agent,
    list_sample_runs_for_agent,
    preview_checkpoint_on_trace,
)
from services.checkpoint_match_rules import (
    MatchRulesValidationError,
    evaluate_checkpoint_on_trace,
    validate_match_rules,
)
from services.agent_identity import (
    build_agent_identity_maps,
    list_distinct_agent_names,
    resolve_logical_agent_id_by_name,
)
from services.langfuse_errors import LangfuseApiError
from services.agent_run_trace_list import load_observations_for_preview
from trace_routes import _client_for_project

checkpoint_router = APIRouter(prefix="/checkpoints", tags=["Checkpoints"])


async def _resolve_preview_project(
    session: AsyncSession,
    template: ProcessCheckpointTemplate,
    body_project: Optional[str],
) -> Optional[str]:
    """预览 Langfuse 项目：请求体优先，否则取同 logical_agent_id 下 Agent 绑定。"""
    explicit = (body_project or "").strip()
    if explicit:
        return explicit
    lid = (template.logical_agent_id or "").strip()
    if not lid:
        return None
    q = select(Agent.langfuse_project).where(Agent.logical_agent_id == lid)
    for proj in (await session.execute(q)).scalars().all():
        pid = (proj or "").strip()
        if pid:
            return pid
    return None


def _template_to_response(
    row: ProcessCheckpointTemplate,
    lid_to_name: Optional[Dict[str, str]] = None,
) -> CheckpointTemplateResponse:
    rules = json.loads(row.match_rules or "{}")
    lid = row.logical_agent_id or ""
    agent_name = (lid_to_name or {}).get(lid, lid)
    return CheckpointTemplateResponse(
        id=row.id,
        logical_agent_id=lid,
        agent_name=agent_name,
        name=row.name,
        description=row.description,
        order=row.order,
        enforce_order=bool(getattr(row, "enforce_order", False)),
        required=bool(row.required),
        match_rules=rules,
        score_weight=float(row.score_weight or 1.0),
        match_mode=row.match_mode or "rule",
        langfuse_evaluator_id=row.langfuse_evaluator_id,
        template_set_name=row.template_set_name,
        enabled=bool(row.enabled),
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


async def _ensure_unique_name(
    session: AsyncSession,
    logical_agent_id: str,
    name: str,
    exclude_id: Optional[str] = None,
) -> None:
    q = select(ProcessCheckpointTemplate).where(
        ProcessCheckpointTemplate.logical_agent_id == logical_agent_id,
        ProcessCheckpointTemplate.name == name,
    )
    result = await session.execute(q)
    for row in result.scalars().all():
        if exclude_id and row.id == exclude_id:
            continue
        raise HTTPException(409, f"Agent「{logical_agent_id}」下已存在过程点: {name}")


async def _next_order_for_logical_agent(session: AsyncSession, logical_agent_id: str) -> int:
    """同 Agent 下已有最大 order + 1；无已有顺序时从 1 开始。"""
    result = await session.execute(
        select(ProcessCheckpointTemplate.order).where(
            ProcessCheckpointTemplate.logical_agent_id == logical_agent_id,
            ProcessCheckpointTemplate.order.is_not(None),
        )
    )
    orders = [o for o in result.scalars().all() if isinstance(o, int) and o >= 1]
    return (max(orders) + 1) if orders else 1


async def _resolve_template_logical_id(
    session: AsyncSession,
    *,
    agent_name: Optional[str],
    logical_agent_id: Optional[str],
) -> str:
    if agent_name and str(agent_name).strip():
        return await resolve_logical_agent_id_by_name(session, str(agent_name))
    lid = (logical_agent_id or "").strip()
    if lid:
        return lid
    raise HTTPException(400, "请提供 agent_name 或 logical_agent_id")


@checkpoint_router.get("/agent-names")
async def list_checkpoint_agent_names(session: AsyncSession = Depends(get_session)):
    """供筛选下拉：已注册 Agent 的业务名称（每个逻辑 Agent 一条）。"""
    names = await list_distinct_agent_names(session)
    return {"agent_names": names}


@checkpoint_router.get("/logical-agents")
async def list_logical_agents(session: AsyncSession = Depends(get_session)):
    """兼容旧前端：请改用 GET /checkpoints/agent-names。"""
    lid_to_name, _ = await build_agent_identity_maps(session)
    return {"logical_agent_ids": sorted(lid_to_name.keys()), "agent_names": sorted(lid_to_name.values())}


@checkpoint_router.get(
    "/reference-templates/onto-agent",
    response_model=CheckpointReferenceCatalogResponse,
)
async def get_onto_agent_reference_templates():
    """onto-agent M2 识别过程点参考目录（供 Agent 过程点配置页初始化参考）。"""
    return get_onto_agent_reference_catalog()


@checkpoint_router.post(
    "/templates/seed-from-reference",
    response_model=CheckpointSeedFromReferenceResponse,
)
async def seed_checkpoints_from_reference(
    body: CheckpointSeedFromReferenceRequest,
    session: AsyncSession = Depends(get_session),
):
    """按 M2 参考模板为 Agent 补缺过程点（不覆盖已有同名项）。"""
    result = await seed_onto_agent_reference_for_agent_name(
        session,
        agent_name=body.agent_name.strip(),
        template_names=body.template_names,
    )
    await session.commit()
    clear_checkpoint_template_cache()
    return CheckpointSeedFromReferenceResponse(**result)


@checkpoint_router.post(
    "/templates/normalize-weights",
    response_model=CheckpointNormalizeWeightsResponse,
)
async def normalize_checkpoint_template_weights(
    body: CheckpointNormalizeWeightsRequest,
    session: AsyncSession = Depends(get_session),
):
    """将 Agent 启用过程点 score_weight 归一化为合计 1.0（对齐场景子步骤）。"""
    try:
        result = await normalize_checkpoint_weights_for_agent(
            session, agent_name=body.agent_name.strip()
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await session.commit()
    clear_checkpoint_template_cache()
    return CheckpointNormalizeWeightsResponse(**result)


@checkpoint_router.get("/sample-runs", response_model=CheckpointSampleRunsResponse)
async def get_checkpoint_sample_runs(
    agent_name: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=30),
    session: AsyncSession = Depends(get_session),
):
    """过程点配置页：该 Agent 最近评测 Run（用于选 Trace 批量验证）。"""
    return await list_sample_runs_for_agent(session, agent_name=agent_name.strip(), limit=limit)


@checkpoint_router.post("/templates/batch-preview", response_model=CheckpointBatchPreviewResponse)
async def batch_preview_checkpoint_templates(
    body: CheckpointBatchPreviewRequest,
    session: AsyncSession = Depends(get_session),
):
    """对一条 trace 批量验证 Agent 全部过程点命中情况。"""
    try:
        result = await batch_preview_checkpoints_for_agent(
            session,
            agent_name=body.agent_name.strip(),
            trace_id=body.trace_id.strip(),
            project=body.project,
            source=body.source,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return CheckpointBatchPreviewResponse(**result)


@checkpoint_router.get("/templates", response_model=List[CheckpointTemplateResponse])
async def list_checkpoint_templates(
    agent_name: Optional[str] = Query(None, description="Agent 名称"),
    logical_agent_id: Optional[str] = Query(None, description="逻辑 Agent ID（兼容）"),
    enabled_only: bool = Query(False),
    template_set_name: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    lid_to_name, name_to_lid = await build_agent_identity_maps(session)
    filter_lid = logical_agent_id
    if agent_name and agent_name.strip():
        filter_lid = name_to_lid.get(agent_name.strip())
        if not filter_lid:
            return []
    q = select(ProcessCheckpointTemplate)
    if filter_lid:
        q = q.where(ProcessCheckpointTemplate.logical_agent_id == filter_lid)
    if enabled_only:
        q = q.where(ProcessCheckpointTemplate.enabled.is_(True))
    if template_set_name:
        q = q.where(ProcessCheckpointTemplate.template_set_name == template_set_name)
    q = q.order_by(
        ProcessCheckpointTemplate.logical_agent_id,
        ProcessCheckpointTemplate.order,
        ProcessCheckpointTemplate.name,
    )
    result = await session.execute(q)
    return [_template_to_response(r, lid_to_name) for r in result.scalars().all()]


@checkpoint_router.post("/templates", response_model=CheckpointTemplateResponse)
async def create_checkpoint_template(
    body: CheckpointTemplateCreateRequest,
    session: AsyncSession = Depends(get_session),
):
    if body.match_mode == "llm_judge":
        raise HTTPException(400, "M2 暂仅支持 match_mode=rule，llm_judge 将于后续里程碑支持")
    try:
        rules = validate_match_rules(body.match_rules)
    except MatchRulesValidationError as e:
        raise HTTPException(400, str(e)) from e

    lid = await _resolve_template_logical_id(
        session, agent_name=body.agent_name, logical_agent_id=body.logical_agent_id
    )
    await _ensure_unique_name(session, lid, body.name)

    order = body.order
    if order is None:
        order = await _next_order_for_logical_agent(session, lid)

    now = datetime.utcnow()
    row = ProcessCheckpointTemplate(
        id=f"cp_{uuid.uuid4().hex[:12]}",
        logical_agent_id=lid,
        name=body.name.strip(),
        description=body.description,
        order=order,
        enforce_order=body.enforce_order,
        required=body.required,
        match_rules=json.dumps(rules, ensure_ascii=False),
        score_weight=body.score_weight,
        match_mode=body.match_mode,
        langfuse_evaluator_id=body.langfuse_evaluator_id,
        template_set_name=body.template_set_name,
        enabled=body.enabled,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    clear_checkpoint_template_cache()
    await session.refresh(row)
    lid_to_name, _ = await build_agent_identity_maps(session)
    return _template_to_response(row, lid_to_name)


@checkpoint_router.put("/templates/reorder")
async def reorder_checkpoint_templates(
    body: CheckpointReorderRequest,
    session: AsyncSession = Depends(get_session),
):
    """按给定 id 顺序重排同一 Agent 下过程点的执行顺序（1..n）。"""
    agent_name = body.agent_name.strip()
    if not agent_name:
        raise HTTPException(400, "agent_name 不能为空")

    lid = await resolve_logical_agent_id_by_name(session, agent_name)
    ids = [i.strip() for i in body.template_ids if i.strip()]
    if not ids:
        raise HTTPException(400, "template_ids 不能为空")
    if len(set(ids)) != len(ids):
        raise HTTPException(400, "template_ids 含重复项")

    result = await session.execute(
        select(ProcessCheckpointTemplate).where(
            ProcessCheckpointTemplate.logical_agent_id == lid,
            ProcessCheckpointTemplate.id.in_(ids),
        )
    )
    rows = {r.id: r for r in result.scalars().all()}
    if len(rows) != len(ids):
        raise HTTPException(400, "部分 template_id 不存在或不属于该 Agent")

    now = datetime.utcnow()
    for idx, tid in enumerate(ids, start=1):
        rows[tid].order = idx
        rows[tid].updated_at = now
    await session.commit()
    clear_checkpoint_template_cache()
    return {"ok": True, "agent_name": agent_name, "count": len(ids)}


@checkpoint_router.get("/templates/{template_id}", response_model=CheckpointTemplateResponse)
async def get_checkpoint_template(
    template_id: str,
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(ProcessCheckpointTemplate, template_id)
    if not row:
        raise HTTPException(404, "过程点模板不存在")
    lid_to_name, _ = await build_agent_identity_maps(session)
    return _template_to_response(row, lid_to_name)


@checkpoint_router.put("/templates/{template_id}", response_model=CheckpointTemplateResponse)
async def update_checkpoint_template(
    template_id: str,
    body: CheckpointTemplateUpdateRequest,
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(ProcessCheckpointTemplate, template_id)
    if not row:
        raise HTTPException(404, "过程点模板不存在")

    if body.match_mode == "llm_judge":
        raise HTTPException(400, "M2 暂仅支持 match_mode=rule")

    if body.name and body.name.strip() != row.name:
        await _ensure_unique_name(
            session, row.logical_agent_id, body.name.strip(), exclude_id=template_id
        )
        row.name = body.name.strip()

    if body.description is not None:
        row.description = body.description
    if body.order is not None:
        row.order = body.order
    if body.enforce_order is not None:
        row.enforce_order = body.enforce_order
    if body.required is not None:
        row.required = body.required
    if body.match_rules is not None:
        try:
            rules = validate_match_rules(body.match_rules)
        except MatchRulesValidationError as e:
            raise HTTPException(400, str(e)) from e
        row.match_rules = json.dumps(rules, ensure_ascii=False)
    if body.score_weight is not None:
        row.score_weight = body.score_weight
    if body.match_mode is not None:
        row.match_mode = body.match_mode
    if body.langfuse_evaluator_id is not None:
        row.langfuse_evaluator_id = body.langfuse_evaluator_id
    if body.template_set_name is not None:
        row.template_set_name = body.template_set_name
    if body.enabled is not None:
        row.enabled = body.enabled

    row.updated_at = datetime.utcnow()
    await session.commit()
    clear_checkpoint_template_cache()
    await session.refresh(row)
    lid_to_name, _ = await build_agent_identity_maps(session)
    return _template_to_response(row, lid_to_name)


@checkpoint_router.delete("/templates/{template_id}")
async def delete_checkpoint_template(
    template_id: str,
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(ProcessCheckpointTemplate, template_id)
    if not row:
        raise HTTPException(404, "过程点模板不存在")
    if row.enabled:
        raise HTTPException(
            409,
            f"过程点「{row.name}」当前为启用状态，无法删除。请先禁用后再删除。",
        )
    await session.delete(row)
    await session.commit()
    clear_checkpoint_template_cache()
    return {"ok": True, "id": template_id}


@checkpoint_router.post("/templates/preview-match")
async def preview_checkpoint_match_draft(
    body: CheckpointPreviewMatchRequest,
    session: AsyncSession = Depends(get_session),
):
    """新增过程点未保存时，用表单规则对 trace 试跑匹配。"""
    try:
        result = await preview_checkpoint_on_trace(
            session,
            trace_id=body.trace_id.strip(),
            match_rules=body.match_rules,
            checkpoint_name=(body.checkpoint_name or "未命名过程点").strip(),
            order=body.order,
            required=body.required,
            project=body.project,
            source=body.source,
            agent_name=body.agent_name.strip(),
            client_for_project=_client_for_project,
        )
    except MatchRulesValidationError as e:
        raise HTTPException(400, str(e)) from e
    except ValueError as e:
        msg = str(e)
        status = 503 if "Langfuse 未配置" in msg else 400
        raise HTTPException(status, msg) from e
    return result


@checkpoint_router.post("/templates/{template_id}/preview")
async def preview_checkpoint_template(
    template_id: str,
    body: CheckpointPreviewRequest,
    session: AsyncSession = Depends(get_session),
):
    """对一条 trace 试跑匹配（M2 预览）；默认优先读物化 Span Tree。"""
    row = await session.get(ProcessCheckpointTemplate, template_id)
    if not row:
        raise HTTPException(404, "过程点模板不存在")

    if row.match_mode != "rule":
        return {
            "trace_id": body.trace_id,
            "template_id": template_id,
            "results": [
                {
                    "template_id": template_id,
                    "template_name": row.name,
                    "skipped": True,
                    "skip_reason": "M2 暂仅支持 rule 模式预览",
                    "hit": False,
                    "required": bool(row.required),
                    "matched_count": 0,
                    "evidence": {},
                }
            ],
        }

    try:
        rules = (
            validate_match_rules(body.match_rules)
            if body.match_rules is not None
            else json.loads(row.match_rules or "{}")
        )
        result = await preview_checkpoint_on_trace(
            session,
            trace_id=body.trace_id.strip(),
            match_rules=rules,
            checkpoint_name=row.name,
            order=row.order,
            required=bool(row.required),
            project=body.project,
            source=body.source,
            template=row,
            client_for_project=_client_for_project,
        )
    except MatchRulesValidationError as e:
        raise HTTPException(400, str(e)) from e
    except ValueError as e:
        msg = str(e)
        status = 503 if "Langfuse 未配置" in msg else 400
        raise HTTPException(status, msg) from e
    except LangfuseApiError as e:
        msg = e.message
        if e.http_status == 404 and not (body.project or "").strip():
            resolved = await _resolve_preview_project(session, row, body.project)
            msg = (
                f"{msg} 未指定项目 id 时已尝试 Agent 绑定的 Langfuse 项目"
                f"（{resolved or '默认项目'}）；请在预览中填写正确项目 id。"
            )
        raise HTTPException(e.http_status, msg) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"拉取 trace 失败: {e}") from e

    result["template_id"] = template_id
    if result.get("results"):
        result["results"][0]["template_id"] = template_id
    return result


@checkpoint_router.get("/historical-performance")
async def get_checkpoint_historical_performance(
    logical_agent_id: str = Query(..., min_length=1),
    dataset_id: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """过程点历史效能 P75/P90（P2：配置页参考）。"""
    from services.checkpoint_historical_perf_service import (
        build_checkpoint_historical_performance,
    )

    payload = await build_checkpoint_historical_performance(
        session,
        logical_agent_id,
        dataset_id=dataset_id,
    )
    if payload is None:
        raise HTTPException(404, "未找到启用的过程点模板")
    return payload


@checkpoint_router.post("/templates/validate-rules")
async def validate_rules_endpoint(body: Dict[str, Any]):
    """仅校验 match_rules JSON，不落库。"""
    try:
        normalized = validate_match_rules(body)
        from services.checkpoint_batch_service import warn_legacy_name_only_rules

        warning = warn_legacy_name_only_rules(normalized)
        payload: Dict[str, Any] = {"valid": True, "match_rules": normalized}
        if warning:
            payload["warning"] = warning
        return payload
    except MatchRulesValidationError as e:
        raise HTTPException(400, str(e)) from e
