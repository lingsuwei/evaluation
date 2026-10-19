"""场景 QA 子步骤评测 API。"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_session
from services.cross_task_cockpit_service import CrossTaskCockpitError
from services.cross_task_scenario_service import build_cross_task_scenario_matrix
from services.scenario_eval_service import (
    GENERIC_L0_SCENARIO_KEY,
    ensure_generic_l0_template,
    get_template_by_key,
    resolve_scenario_for_run,
    score_run_against_scenario,
    score_task_scenario,
)
from services.checkpoint_to_scenario_service import (
    import_checkpoints_to_dataset,
    import_checkpoints_to_item_template,
)
from services.dataset_item_scenario_service import (
    auto_extract_steps_for_dataset,
    backfill_item_step_trace_bindings,
    create_item_step,
    delete_item_step,
    list_dataset_item_configs,
    normalize_item_step_weights,
    reorder_item_steps,
    seed_zc_sample_for_dataset,
    sync_dataset_item_scenario_keys,
    update_item_step,
)
from services.scenario_post_eval_service import score_scenario_for_task
from services.scenario_step_span_ref_service import list_scenario_step_span_refs
from services.scenario_gt_draft_service import draft_steps_from_markdown
from services.scenario_step_trace_service import build_run_scenario_steps_detail
from services.scenario_step_verify_service import (
    batch_preview_scenario_steps_for_item,
    preview_scenario_step_on_trace,
)
from services.task_scenario_matrix_service import build_task_scenario_matrix
from logger import log


def _scenario_matrix_http_error(exc: Exception) -> HTTPException:
    """场景矩阵接口：业务错误 400，其余返回可读 500。"""
    if isinstance(exc, CrossTaskCockpitError):
        return HTTPException(status_code=400, detail=str(exc))
    log(
        "ERROR",
        "SCENARIO_API",
        "场景矩阵加载失败",
        error=str(exc),
        error_type=type(exc).__name__,
    )
    message = str(exc).strip() or type(exc).__name__
    return HTTPException(
        status_code=500,
        detail=f"场景子步骤矩阵加载失败：{message}",
    )


class CrossTaskScenarioRequest(BaseModel):
    task_ids: List[str] = Field(..., min_length=2, max_length=6)
    scenario_key: Optional[str] = Field(
        default=None,
        description="省略或 auto 表示按 Run/按题模板解析；仅调试时指定固定 scenario_key",
    )
    baseline_task_id: Optional[str] = None


class DraftFromMarkdownRequest(BaseModel):
    markdown: str = Field(..., min_length=1)
    scenario_key: Optional[str] = None
    name: Optional[str] = None


class ScenarioItemStepCreateRequest(BaseModel):
    step_key: str = Field(..., min_length=1, max_length=32)
    name: str = Field(..., min_length=1, max_length=200)
    step_order: Optional[int] = None
    weight: float = Field(default=0.1, ge=0, le=1)
    required: bool = True
    enabled: bool = True
    enforce_order: bool = False
    gt_criteria: Optional[List[str]] = None
    gt_expected: Optional[dict] = None
    match_rules: Optional[dict] = None


class ScenarioItemStepUpdateRequest(BaseModel):
    name: Optional[str] = None
    step_order: Optional[int] = None
    weight: Optional[float] = Field(default=None, ge=0, le=1)
    required: Optional[bool] = None
    enabled: Optional[bool] = None
    enforce_order: Optional[bool] = None
    gt_criteria: Optional[List[str]] = None
    gt_expected: Optional[dict] = None
    match_rules: Optional[dict] = None


class ScenarioItemStepsReorderRequest(BaseModel):
    step_ids: List[str] = Field(..., min_length=1)


class ImportCheckpointsRequest(BaseModel):
    logical_agent_id: str = Field(..., min_length=1)
    template_set_name: Optional[str] = None


class ScenarioStepPreviewTraceRequest(BaseModel):
    trace_id: str = Field(..., min_length=1)
    agent_name: str = Field(..., min_length=1)
    step_key: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    project: Optional[str] = None
    source: Optional[str] = None
    gt_expected: Optional[dict] = None
    match_rules: Optional[dict] = None
    enabled: bool = True
    required: bool = True
    step_order: Optional[int] = None


class ScenarioStepsBatchPreviewRequest(BaseModel):
    trace_id: str = Field(..., min_length=1)
    agent_name: str = Field(..., min_length=1)
    project: Optional[str] = None
    source: Optional[str] = None


scenario_router = APIRouter(prefix="/scenario-eval", tags=["ScenarioEval"])


@scenario_router.get("/templates/{scenario_key}")
async def get_scenario_template(
    scenario_key: str,
    session: AsyncSession = Depends(get_session),
):
    detail = await get_template_by_key(session, scenario_key)
    if not detail:
        raise HTTPException(404, "scenario template not found")
    return detail


@scenario_router.get("/runs/{run_id}/steps-detail")
async def get_run_scenario_steps_detail(
    run_id: str,
    scenario_key: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """
    子步骤打分 + 绑定 observation 的 input/output + Langfuse 深链。
    """
    await ensure_generic_l0_template(session)
    key = scenario_key or await resolve_scenario_for_run(session, run_id)
    if not key:
        raise HTTPException(404, "no matching scenario for run")
    detail = await build_run_scenario_steps_detail(session, run_id, key)
    if not detail:
        raise HTTPException(404, "run or scenario not found")
    await session.commit()
    return detail


@scenario_router.get("/runs/{run_id}/resolved-scenario")
async def get_run_resolved_scenario(
    run_id: str,
    session: AsyncSession = Depends(get_session),
):
    """解析 Run 应使用的 scenario_key（条目 > 数据集默认 > L0）。"""
    await ensure_generic_l0_template(session)
    key = await resolve_scenario_for_run(session, run_id)
    if not key:
        raise HTTPException(404, "no matching scenario for run")
    return {"agent_run_id": run_id, "scenario_key": key}


@scenario_router.post("/templates/draft-from-markdown")
async def post_draft_from_markdown(body: DraftFromMarkdownRequest):
    """GT Markdown 半自动抽取子步骤草稿（ST-2-3）。"""
    return draft_steps_from_markdown(
        body.markdown,
        scenario_key=body.scenario_key,
        name=body.name,
    )


@scenario_router.get("/datasets/{dataset_id}/item-steps-config")
async def get_dataset_item_steps_config(
    dataset_id: str,
    session: AsyncSession = Depends(get_session),
):
    """按题列出场景子步骤配置（固定评测集 / 按 dataset_item）。"""
    try:
        return await list_dataset_item_configs(session, dataset_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@scenario_router.post("/datasets/{dataset_id}/auto-extract-steps")
async def post_auto_extract_steps(
    dataset_id: str,
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
):
    """按题从 model_answer 自动提炼场景子步骤。"""
    try:
        payload = await auto_extract_steps_for_dataset(
            session, dataset_id, force=force
        )
        if payload.get("conflict"):
            raise HTTPException(409, detail=payload)
        await session.commit()
        return payload
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@scenario_router.post("/tasks/{task_id}/score-scenario")
async def post_score_task_scenario(
    task_id: str,
    session: AsyncSession = Depends(get_session),
):
    """手动触发任务下全部 Run 的场景子步骤打分（E-1）。"""
    try:
        payload = await score_scenario_for_task(session, task_id)
        await session.commit()
        return payload
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@scenario_router.get("/runs/{run_id}/span-refs")
async def get_run_scenario_span_refs(
    run_id: str,
    session: AsyncSession = Depends(get_session),
):
    """子步骤物化 Span 绑定列表（E-3）。"""
    refs = await list_scenario_step_span_refs(session, run_id)
    return {"agent_run_id": run_id, "span_refs": refs}


@scenario_router.post("/items/{dataset_item_id}/import-from-checkpoints")
async def post_import_checkpoints_to_item(
    dataset_item_id: str,
    body: ImportCheckpointsRequest,
    dataset_id: str = Query(..., description="评测集 ID"),
    session: AsyncSession = Depends(get_session),
):
    """从 Agent 过程点导入本题场景子步骤草稿（E-2）。"""
    try:
        payload = await import_checkpoints_to_item_template(
            session,
            dataset_id=dataset_id,
            dataset_item_id=dataset_item_id,
            logical_agent_id=body.logical_agent_id.strip(),
            template_set_name=(body.template_set_name or "").strip() or None,
        )
        await session.commit()
        return payload
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@scenario_router.post("/datasets/{dataset_id}/import-from-checkpoints")
async def post_import_checkpoints_to_dataset(
    dataset_id: str,
    body: ImportCheckpointsRequest,
    session: AsyncSession = Depends(get_session),
):
    """从 Agent 过程点为评测集每题导入场景子步骤草稿（E-2）。"""
    try:
        payload = await import_checkpoints_to_dataset(
            session,
            dataset_id=dataset_id,
            logical_agent_id=body.logical_agent_id.strip(),
            template_set_name=(body.template_set_name or "").strip() or None,
        )
        await session.commit()
        return payload
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@scenario_router.post("/datasets/{dataset_id}/sync-scenario-keys")
async def post_sync_dataset_scenario_keys(
    dataset_id: str,
    session: AsyncSession = Depends(get_session),
):
    """将评测集条目 scenario_key 与按题模板 ditem_* 对齐（C-3）。"""
    try:
        payload = await sync_dataset_item_scenario_keys(session, dataset_id)
        await session.commit()
        return payload
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@scenario_router.post("/items/{dataset_item_id}/steps/normalize-weights")
async def post_normalize_item_step_weights(
    dataset_item_id: str,
    session: AsyncSession = Depends(get_session),
):
    """启用子步骤权重归一化为 1.0（C-2）。"""
    try:
        payload = await normalize_item_step_weights(session, dataset_item_id)
        await session.commit()
        return payload
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@scenario_router.post("/items/{dataset_item_id}/steps/backfill-trace-bindings")
async def post_backfill_item_trace_bindings(
    dataset_item_id: str,
    session: AsyncSession = Depends(get_session),
):
    """补全缺失的 match_rules.trace_binding（C-1）。"""
    try:
        payload = await backfill_item_step_trace_bindings(session, dataset_item_id)
        await session.commit()
        return payload
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@scenario_router.post("/datasets/{dataset_id}/seed-zc-sample")
async def post_seed_zc_sample(
    dataset_id: str,
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
):
    """兼容旧路径：等同 auto-extract-steps。"""
    try:
        payload = await auto_extract_steps_for_dataset(
            session, dataset_id, force=force
        )
        if payload.get("conflict"):
            raise HTTPException(409, detail=payload)
        await session.commit()
        return payload
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@scenario_router.post("/items/{dataset_item_id}/steps")
async def post_item_scenario_step(
    dataset_item_id: str,
    body: ScenarioItemStepCreateRequest,
    session: AsyncSession = Depends(get_session),
):
    try:
        row = await create_item_step(
            session,
            dataset_item_id,
            step_key=body.step_key,
            name=body.name,
            step_order=body.step_order,
            weight=body.weight,
            required=body.required,
            enabled=body.enabled,
            enforce_order=body.enforce_order,
            gt_criteria=body.gt_criteria,
            gt_expected=body.gt_expected,
            match_rules=body.match_rules,
        )
        await session.commit()
        return row
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@scenario_router.put("/steps/{step_id}")
async def put_scenario_step(
    step_id: str,
    body: ScenarioItemStepUpdateRequest,
    session: AsyncSession = Depends(get_session),
):
    patch = body.model_dump(exclude_unset=True)
    try:
        row = await update_item_step(session, step_id, patch)
        await session.commit()
        return row
    except ValueError as e:
        msg = str(e)
        status = 404 if "not found" in msg.lower() else 400
        raise HTTPException(status, msg) from e


@scenario_router.delete("/steps/{step_id}")
async def delete_scenario_step(
    step_id: str,
    session: AsyncSession = Depends(get_session),
):
    try:
        await delete_item_step(session, step_id)
        await session.commit()
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@scenario_router.put("/items/{dataset_item_id}/steps/reorder")
async def put_reorder_item_steps(
    dataset_item_id: str,
    body: ScenarioItemStepsReorderRequest,
    session: AsyncSession = Depends(get_session),
):
    try:
        rows = await reorder_item_steps(session, dataset_item_id, body.step_ids)
        await session.commit()
        return {"steps": rows}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@scenario_router.get("/runs/{run_id}/report")
async def get_run_scenario_report(
    run_id: str,
    scenario_key: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    key = scenario_key or await resolve_scenario_for_run(session, run_id)
    if not key:
        raise HTTPException(404, "no matching scenario for run")
    report = await score_run_against_scenario(session, run_id, key, persist=True)
    if not report:
        raise HTTPException(404, "run or scenario not found")
    await session.commit()
    return report


@scenario_router.get("/tasks/{task_id}/report")
async def get_task_scenario_report(
    task_id: str,
    scenario_key: Optional[str] = Query(
        None,
        description="省略或 auto：按各 Run 解析场景模板",
    ),
    session: AsyncSession = Depends(get_session),
):
    await ensure_generic_l0_template(session)
    sk = (scenario_key or "").strip()
    if sk and sk.lower() not in ("auto", "per_item", "resolve"):
        if sk == GENERIC_L0_SCENARIO_KEY:
            await ensure_generic_l0_template(session)
    else:
        sk = None
    report = await score_task_scenario(session, task_id, sk)
    await session.commit()
    return report


@scenario_router.post("/items/{dataset_item_id}/steps/batch-preview-trace")
async def post_item_steps_batch_preview_trace(
    dataset_item_id: str,
    body: ScenarioStepsBatchPreviewRequest,
    session: AsyncSession = Depends(get_session),
):
    """本题全部子步骤在一条 trace 上批量试跑 Span 绑定（配置页 Trace 验证）。"""
    try:
        return await batch_preview_scenario_steps_for_item(
            session,
            dataset_item_id=dataset_item_id,
            trace_id=body.trace_id,
            agent_name=body.agent_name,
            project=body.project,
            source=body.source,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@scenario_router.post("/items/{dataset_item_id}/steps/preview-trace")
async def post_item_step_preview_trace(
    dataset_item_id: str,
    body: ScenarioStepPreviewTraceRequest,
    session: AsyncSession = Depends(get_session),
):
    """单条子步骤（含未保存草稿）在 trace 上试跑 Span 绑定。"""
    try:
        return await preview_scenario_step_on_trace(
            session,
            dataset_item_id=dataset_item_id,
            trace_id=body.trace_id,
            agent_name=body.agent_name,
            step_key=body.step_key,
            name=body.name,
            gt_expected=body.gt_expected,
            match_rules=body.match_rules,
            enabled=body.enabled,
            required=body.required,
            step_order=body.step_order,
            project=body.project,
            source=body.source,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@scenario_router.get("/tasks/{task_id}/matrix")
async def get_task_scenario_matrix(
    task_id: str,
    scenario_key: Optional[str] = Query(
        None,
        description="省略或 auto：按题使用过程点配置中的场景子步骤模板",
    ),
    baseline_agent_id: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """单任务内多 Agent 版本场景子步骤矩阵。"""
    try:
        payload = await build_task_scenario_matrix(
            session,
            task_id,
            scenario_key=scenario_key,
            baseline_agent_id=baseline_agent_id,
        )
        await session.commit()
        return payload
    except HTTPException:
        await session.rollback()
        raise
    except Exception as e:
        await session.rollback()
        raise _scenario_matrix_http_error(e) from e


@scenario_router.post("/from-tasks")
async def post_cross_task_scenario_matrix(
    body: CrossTaskScenarioRequest,
    session: AsyncSession = Depends(get_session),
):
    """同评测集多任务：按 question_key 对齐的场景子步骤对比矩阵。"""
    try:
        return await build_cross_task_scenario_matrix(
            session,
            body.task_ids,
            scenario_key=body.scenario_key,
            baseline_task_id=body.baseline_task_id,
        )
    except HTTPException:
        await session.rollback()
        raise
    except Exception as e:
        await session.rollback()
        raise _scenario_matrix_http_error(e) from e


@scenario_router.get("/cross-task-step-compare")
async def get_cross_task_step_compare(
    question_key: str = Query(..., description="对齐的题目 question_key"),
    baseline_task_id: str = Query(..., description="基线版本任务 id"),
    compare_task_id: str = Query(..., description="对比版本任务 id"),
    session: AsyncSession = Depends(get_session),
):
    """跨版本同题 Step 序列对比：基线代表 Run vs 对比代表 Run。"""
    from services.cross_task_step_compare_service import (
        build_cross_task_step_compare,
    )

    payload = await build_cross_task_step_compare(
        session,
        question_key=question_key,
        baseline_task_id=baseline_task_id,
        compare_task_id=compare_task_id,
    )
    if payload is None:
        raise HTTPException(404, "该题目在所选版本中缺少可对比的 Run")
    return payload


@scenario_router.get("/cross-task-stability-compare")
async def get_cross_task_stability_compare(
    question_key: str = Query(..., description="对齐的题目 question_key"),
    baseline_task_id: str = Query(..., description="基线版本任务 id"),
    compare_task_id: str = Query(..., description="对比版本任务 id"),
    session: AsyncSession = Depends(get_session),
):
    """跨版本同题多轮稳定性对比：两版本均 ≥2 轮时返回结构/步骤稳定性。"""
    from services.cross_task_stability_service import (
        build_cross_task_stability_compare,
    )

    return await build_cross_task_stability_compare(
        session,
        question_key=question_key,
        baseline_task_id=baseline_task_id,
        compare_task_id=compare_task_id,
    )


# ============ 场景子步骤文件管理 API ============

import os

from services.step_asset_service import (
    save_asset,
    load_asset,
    delete_asset,
    list_assets,
    KIND_GROUND_TRUTH,
    KIND_LLM_PROMPT,
)
from services.dataset_item_scenario_service import item_scenario_key

_VALID_KINDS = {"ground_truth", "llm_prompt"}
_SUPPORTED_EXTS = {".txt", ".csv"}


@scenario_router.put("/step-assets/{dataset_id}/{scenario_key}/{step_key}/{kind}")
async def upload_step_asset(
    dataset_id: str,
    scenario_key: str,
    step_key: str,
    kind: str,
    files: List[UploadFile] = File(...),
):
    """上传参考答案 / 评测提示词文件（支持多文件 .txt / .csv）"""
    if kind not in _VALID_KINDS:
        raise HTTPException(400, f"kind 必须为 ground_truth 或 llm_prompt")
    asset_kind = KIND_GROUND_TRUTH if kind == "ground_truth" else KIND_LLM_PROMPT
    saved: List[dict] = []
    for file in files:
        filename = file.filename or "upload.txt"
        _, ext = os.path.splitext(filename)
        ext = ext.lower()
        if ext not in _SUPPORTED_EXTS:
            raise HTTPException(
                400,
                f"不支持的文件格式: {ext}。仅支持 .txt 和 .csv 文件",
            )
        content = await file.read()
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = content.decode("gbk")
            except UnicodeDecodeError:
                raise HTTPException(
                    400,
                    f"文件 {filename} 编码不支持，请使用 UTF-8 编码",
                )
        rel_path = save_asset(
            dataset_id, scenario_key, step_key, text, asset_kind,
            filename=filename,
        )
        saved.append({"filename": os.path.basename(rel_path), "path": rel_path, "size": len(text)})
    return {"success": True, "files": saved, "count": len(saved)}


@scenario_router.get("/step-assets/{dataset_id}/{scenario_key}/{step_key}/{kind}")
async def get_step_asset(
    dataset_id: str,
    scenario_key: str,
    step_key: str,
    kind: str,
    filename: Optional[str] = Query(None),
):
    """读取参考答案 / 评测提示词文件"""
    if kind not in _VALID_KINDS:
        raise HTTPException(400, f"kind 必须为 ground_truth 或 llm_prompt")
    asset_kind = KIND_GROUND_TRUTH if kind == "ground_truth" else KIND_LLM_PROMPT
    content = load_asset(dataset_id, scenario_key, step_key, asset_kind, filename=filename)
    if content is None:
        raise HTTPException(404, "文件不存在")
    return {"content": content, "exists": True}


@scenario_router.delete("/step-assets/{dataset_id}/{scenario_key}/{step_key}/{kind}")
async def remove_step_asset(
    dataset_id: str,
    scenario_key: str,
    step_key: str,
    kind: str,
    filename: Optional[str] = Query(None),
):
    """删除参考答案 / 评测提示词文件。

    filename 为空时删除该步骤的所有文件，为指定文件名时只删除该文件。
    """
    if kind not in _VALID_KINDS:
        raise HTTPException(400, f"kind 必须为 ground_truth 或 llm_prompt")
    asset_kind = KIND_GROUND_TRUTH if kind == "ground_truth" else KIND_LLM_PROMPT
    deleted = delete_asset(dataset_id, scenario_key, step_key, asset_kind, filename=filename or "")
    return {"success": deleted, "deleted": deleted}


@scenario_router.get("/step-assets/{dataset_id}/{scenario_key}/{step_key}/status")
async def check_step_assets(
    dataset_id: str,
    scenario_key: str,
    step_key: str,
):
    """检查步骤已有文件列表"""
    gt_files = list_assets(dataset_id, scenario_key, step_key, KIND_GROUND_TRUTH)
    llm_files = list_assets(dataset_id, scenario_key, step_key, KIND_LLM_PROMPT)
    return {
        "ground_truth": len(gt_files) > 0,
        "ground_truth_files": gt_files,
        "llm_prompt": len(llm_files) > 0,
        "llm_prompt_files": llm_files,
    }
