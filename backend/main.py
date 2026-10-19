import asyncio
import json
import os
import uuid
from typing import Any, Dict, List, Optional
from datetime import datetime

# DeepEval 超时配置：默认 per_task=180s 会被切成 per_attempt≈88.5s，对慢模型(如 glm-5.2 做工具参数判断)过紧，
# 会触发 RetryError[TimeoutError] 把单次评估记 0 分。延长外层预算到 600s，per_attempt 自动切成 ~296s。
# 必须在 deepeval 任何 import 之前设置（settings 在首次 import 时按环境变量构建）。
os.environ.setdefault("DEEPEVAL_PER_TASK_TIMEOUT_SECONDS_OVERRIDE", "600")

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks, Depends, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update, exists, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from services.analytics_service import aggregate_task_run_metrics
from services.checkpoint_result_service import build_task_checkpoint_report
from services.eval_metric_sync_service import sync_evaluation_result_to_run_metrics
from services.inference_model_display import (
    summarize_execution_modes_from_runs,
    summarize_inference_models_from_runs,
)
from services.quality_score_service import compute_task_quality_score
from services.run_analytics_view import fetch_run_analytics_rows_for_task
from services.fact_materialization_status import (
    compute_fact_materialization_status,
    summarize_trace_sync_counts,
    summarize_trace_sync_failure_categories,
)
from services.diff_summary_service import build_task_experiment_report
from services.inference_duration_service import (
    batch_load_trace_latency_ms,
    resolve_agent_e2e_duration_ms,
)
from services.task_performance_report_service import (
    build_run_step_compare,
    build_task_performance_report,
)
from services.process_consistency_service import build_process_consistency
from services.task_dimension_verdict_service import (
    build_task_dimension_verdicts,
)
from database import init_db, get_session, get_read_session, async_session, Dataset, Agent, AgentRun, EvaluationTask, EvaluationResult, EvaluationRound, LLMModel, EvaluationScript, RunMetric, CustomMetric, ChatSession, ChatMessage
from schemas import (
    DatasetItem, DatasetUploadResponse, UpdateDatasetRequest, AgentRegisterRequest, AgentUpdateRequest, AgentResponse, EvaluateRequest,
    EvaluateResponse, TaskStatus, LLMModelCreateRequest, LLMModelUpdateRequest, LLMModelResponse,
    AgentProbeRequest, AgentProbeResponse,
    ChatSessionCreateRequest, ChatDraftPatchRequest, ChatInterpretResponse,
    ChatSessionOut, ChatSessionListItem, ChatMessageOut, ChatMessageCreateRequest,
)
from tasks import run_evaluation_task, run_online_evaluation_task, resume_evaluation_task, get_task_queue
from extended_routes import extended_router
from trace_routes import trace_router
from checkpoint_routes import checkpoint_router
from comparison_routes import comparison_router
from experiment_routes import experiment_router
from scenario_routes import scenario_router
from run_routes import run_router
from report_routes import report_router
from logger import log
from config import (
    TRACE_SYNC_BATCH_SIZE,
    TRACE_SYNC_POLL_INTERVAL_SEC,
    TRACE_SYNC_WORKER_ENABLED,
)

app = FastAPI(title="本体评测系统", version="1.0.0")

_trace_sync_worker_task: Optional[asyncio.Task] = None

app.include_router(extended_router, prefix="/extended", tags=["扩展功能"])
# 与 Vite/nginx 代理一致：前端 /api/trace/* 会 rewrite 为 /trace/*
app.include_router(trace_router)
app.include_router(trace_router, prefix="/api")
# 与 Vite/nginx 代理一致：前端 /api/checkpoints/* 会 rewrite 为 /checkpoints/*
app.include_router(checkpoint_router)
app.include_router(checkpoint_router, prefix="/api")
app.include_router(comparison_router)
app.include_router(comparison_router, prefix="/api")
app.include_router(experiment_router)
app.include_router(experiment_router, prefix="/api")
app.include_router(scenario_router)
app.include_router(scenario_router, prefix="/api")
app.include_router(run_router)
app.include_router(run_router, prefix="/api")
app.include_router(report_router)
app.include_router(report_router, prefix="/api")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def disable_api_cache(request: Request, call_next):
    """API 响应禁止浏览器缓存，避免前端拿到过期数据（如过期 token）。"""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


class RuntimeMetricTargetsUpdate(BaseModel):
    inference_latency_ms: Optional[float] = Field(
        None,
        gt=0,
        description="本任务推理时延目标，单位毫秒",
    )
    tokens_total: Optional[float] = Field(
        None,
        gt=0,
        description="本任务 Tokens 总消耗目标",
    )


class VerdictThresholdsUpdate(BaseModel):
    quality_pass_rate_pass: Optional[float] = Field(None, ge=0, le=1)
    quality_pass_rate_warn: Optional[float] = Field(None, ge=0, le=1)
    quality_total_pass: Optional[float] = Field(None, ge=0, le=1)
    quality_total_warn: Optional[float] = Field(None, ge=0, le=1)
    process_chain_pass_warn: Optional[float] = Field(None, ge=0, le=1)
    scenario_process_pass: Optional[float] = Field(None, ge=0, le=1)
    scenario_process_warn: Optional[float] = Field(None, ge=0, le=1)
    scenario_required_pass: Optional[float] = Field(None, ge=0, le=1)
    scenario_required_warn: Optional[float] = Field(None, ge=0, le=1)
    scenario_score_std_volatile: Optional[float] = Field(None, ge=0, le=1)
    struct_drift_ratio_significant: Optional[float] = Field(None, gt=0, le=1)
    perf_pass_rate_within: Optional[float] = Field(None, ge=0, le=100)
    perf_pass_rate_elevated: Optional[float] = Field(None, ge=0, le=100)


async def _warmup_detail_cache() -> None:
    """启动后台预热：对最近若干个任务跑一次详情查询，把热数据页载入 PG 缓存。

    消除首次打开详情页时的冷启动延迟（冷缓冲 + 磁盘随机 I/O）。
    后台异步执行，失败静默，不影响服务可用性。
    可通过环境变量 DETAIL_WARMUP_COUNT 控制预热任务数（默认 3，<=0 关闭）。
    """
    import os as _os

    warmup_limit = int(_os.environ.get("DETAIL_WARMUP_COUNT", "3"))
    if warmup_limit <= 0:
        return
    try:
        async with async_session() as session:
            res = await session.execute(
                select(EvaluationTask.id)
                .where(
                    EvaluationTask.status.in_(
                        ["completed", "processing", "cancelled", "paused"]
                    )
                )
                .order_by(EvaluationTask.created_at.desc())
                .limit(warmup_limit)
            )
            task_ids = [row[0] for row in res.all()]
        if not task_ids:
            return
        log("INFO", "WARMUP", "开始预热详情缓存", task_count=len(task_ids))
        for tid in task_ids:
            try:
                async with async_session() as session:
                    await get_task_detail(tid, session)
            except HTTPException:
                pass
            except Exception as exc:
                log("WARN", "WARMUP", "预热单任务失败", task_id=tid, error=str(exc))
        log("INFO", "WARMUP", "详情缓存预热完成")
    except Exception as exc:
        log("WARN", "WARMUP", "详情缓存预热异常", error=str(exc))


@app.on_event("startup")
async def startup():
    # ⚠️ SSE 事件流使用进程内内存存储，不支持多 Worker 部署
    # 若检测到多 Worker 启动，打印警告
    import os, sys
    workers = os.environ.get("UVICORN_WORKERS", "1")
    if workers != "1" or "--workers" in " ".join(sys.argv):
        log("WARNING", "STARTUP",
            "检测到多 Worker 模式启动！SSE 实时事件流基于进程内内存，"
            "多 Worker 下会导致前端无法收到评测进度更新。"
            "请使用单 Worker (uvicorn main:app) 或改用 Redis Pub/Sub 方案。")

    await init_db()

    # 恢复孤儿任务：服务重启后，所有 status=processing 的任务其 in-memory
    # asyncio 协程已随旧进程丢失，DB 却仍为 processing，会导致前端永远显示
    # "运行中"且 SSE 无业务事件。统一标记为 failed，前端轮询下次即可看到终态。
    # PAUSED 任务保留——它有显式 resume 入口，用户可手动恢复。
    try:
        async with async_session() as session:
            result = await session.execute(
                update(EvaluationTask)
                .where(EvaluationTask.status == TaskStatus.PROCESSING)
                .values(
                    status=TaskStatus.FAILED,
                    error="服务重启时任务仍在运行中，in-memory 协程已丢失",
                )
            )
            affected = result.rowcount
            if affected > 0:
                await session.commit()
                log("WARN", "STARTUP", "恢复孤儿任务",
                    marked_failed=affected,
                    note="processing → failed（in-memory 协程随旧进程丢失）")
    except Exception as exc:
        log("ERROR", "STARTUP", "恢复孤儿任务失败", error=str(exc))

    # 清理重启前未完成的确认操作：_confirm_futures 是进程内 dict，旧进程死亡后
    # Future 已丢失，agent loop 协程也随旧进程死亡，pending_confirm 留在 DB 成孤儿。
    # 启动时清理：置空 pending_confirm，会话状态回退为 error。
    try:
        async with async_session() as session:
            result = await session.execute(
                update(ChatSession)
                .where(ChatSession.pending_confirm.isnot(None))
                .values(
                    pending_confirm=None,
                    status="error",
                    error_message="服务重启时确认操作中断",
                )
            )
            affected = result.rowcount
            if affected > 0:
                await session.commit()
                log("WARN", "STARTUP", "清理孤儿 pending_confirm",
                    cleaned=affected,
                    note="服务重启时 agent loop 协程已丢失，确认操作无法恢复")
    except Exception as exc:
        log("ERROR", "STARTUP", "清理孤儿 pending_confirm 失败", error=str(exc))

    # 启动时加载 LLM 模型配置到缓存
    async with async_session() as session:
        await refresh_llm_models_cache(session)
        try:
            from services.scenario_eval_service import (
                ensure_generic_l0_template,
                remove_corridor_demo_template,
            )

            removed = await remove_corridor_demo_template(session)
            if removed:
                log("INFO", "STARTUP", "已删除临时 corridor_defense 演示模板")
            await ensure_generic_l0_template(session)
            await session.commit()
            log("INFO", "STARTUP", "通用 L0 场景模板已就绪")
        except Exception as exc:
            log("WARN", "STARTUP", "场景模板初始化跳过", error=str(exc))
    log("INFO", "STARTUP", "LLM模型配置缓存加载完成")

    # 注册 DeepEval 声明式指标（可选依赖，未安装时静默跳过）
    try:
        from config import get_llm_config, get_default_model_key
        from evaluators.registry import register_deepeval_metrics

        default_key = get_default_model_key()
        llm_cfg = get_llm_config(default_key)
        model_config = {
            "model": llm_cfg.get("model", ""),
            "base_url": llm_cfg.get("base_url", ""),
            "api_key": llm_cfg.get("api_key", ""),
            "temperature": llm_cfg.get("temperature", 0.0),
            "provider": llm_cfg.get("provider"),
        }
        count = register_deepeval_metrics(model_config)
        if count > 0:
            log("INFO", "STARTUP", f"已注册 {count} 项 DeepEval 指标")
        else:
            log("INFO", "STARTUP", "deepeval 未安装或无可用指标，跳过高级指标注册")

        # 加载已配置的指标参数到进程缓存（json_correctness / prompt_alignment 等）
        try:
            from evaluators.registry import load_metric_params_cache
            async with async_session() as sess:
                await load_metric_params_cache(sess)
            log("INFO", "STARTUP", "指标参数缓存加载完成")
        except Exception as exc:
            log("WARN", "STARTUP", "指标参数缓存加载失败", error=str(exc))
    except Exception as exc:
        log("WARN", "STARTUP", "DeepEval 指标注册跳过", error=str(exc))

    global _trace_sync_worker_task
    if TRACE_SYNC_WORKER_ENABLED:
        from services.trace_sync_worker import drain_pending_trace_sync_jobs

        async def _trace_sync_poll_loop() -> None:
            while True:
                try:
                    n = await drain_pending_trace_sync_jobs(limit=TRACE_SYNC_BATCH_SIZE)
                    if n:
                        log("INFO", "TRACE_SYNC", "后台处理完成", processed=n)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log("ERROR", "TRACE_SYNC", "后台轮询异常", error=str(e))
                await asyncio.sleep(TRACE_SYNC_POLL_INTERVAL_SEC)

        _trace_sync_worker_task = asyncio.create_task(_trace_sync_poll_loop())
        log(
            "INFO",
            "STARTUP",
            "Trace Sync Worker 已启动",
            interval_sec=TRACE_SYNC_POLL_INTERVAL_SEC,
            batch_size=TRACE_SYNC_BATCH_SIZE,
        )

    # 后台预热详情缓存：对最近若干任务跑一次详情查询，把热数据页载入 PG 缓存，
    # 消除首次打开详情页时的冷启动延迟。失败静默，不阻塞服务。
    asyncio.create_task(_warmup_detail_cache())


@app.on_event("shutdown")
async def shutdown_trace_sync_worker() -> None:
    global _trace_sync_worker_task
    if _trace_sync_worker_task:
        _trace_sync_worker_task.cancel()
        try:
            await _trace_sync_worker_task
        except asyncio.CancelledError:
            pass
        _trace_sync_worker_task = None


@app.get("/models")
async def get_available_models():
    """获取所有可用的评估模型列表及当前默认模型"""
    from config import get_available_models, get_default_model_key
    return {
        "models": get_available_models(),
        "default_key": get_default_model_key(),
    }


@app.get("/metrics")
async def list_metrics(evaluation_mode: Optional[str] = None):
    """获取所有可选评测指标清单（内置 + DeepEval + 自定义），按 category 分组。

    返回结构：
    {
      "builtin": [{key, label, threshold}],
      "deepeval": [{key, label, category, category_label, requires, note, params, gate_threshold, lower_is_better}],
      "custom": [{id, key, name, kind, threshold, is_active}],
      "categories": {category: label}
    }

    查询参数 evaluation_mode=offline 时,剔除依赖 Agent 推理过程产物的指标
    (多轮对话类、需工具调用/轨迹/思考/检索上下文的 DeepEval 指标,以及多轮自定义指标),
    供创建离线评测任务时仅展示可用指标;指标库页不传参,仍返回全量。
    """
    from config import METRICS_CONFIG
    from evaluators.registry import DEEPEVAL_METRIC_KEYS

    offline_only = (evaluation_mode or "").strip().lower() == "offline"

    # 1. 内置指标（离线模式排除 runtime 指标，因其依赖 Agent 推理过程）
    _RUNTIME_KEYS = ("inference_latency", "tokens_total")
    _METRIC_META = {
        "faithfulness": {
            "category": "quality",
            "requires": ["question", "answer", "context"],
            "optional": [],
            "eval_methods": ["llm"],
            "description": "评估回答是否忠实于上下文，检测幻觉",
        },
        "answer_relevancy": {
            "category": "quality",
            "requires": ["question", "answer"],
            "optional": [],
            "eval_methods": ["llm"],
            "description": "评估回答是否紧扣问题",
        },
        "correctness": {
            "category": "quality",
            "requires": ["question", "answer", "ground_truth"],
            "optional": [],
            "eval_methods": ["llm"],
            "description": "评估回答与标准答案的一致程度",
        },
        "inference_latency": {
            "category": "runtime",
            "requires": [],
            "optional": [],
            "eval_methods": ["trace"],
            "description": "推理时延（毫秒）",
        },
        "tokens_total": {
            "category": "runtime",
            "requires": [],
            "optional": [],
            "eval_methods": ["trace"],
            "description": "Tokens 总消耗",
        },
    }
    builtin = [
        {
            "key": k,
            "label": k,
            "threshold": v,
            "is_runtime": k in _RUNTIME_KEYS,
            "category": _METRIC_META.get(k, {}).get("category", "quality"),
            "requires": _METRIC_META.get(k, {}).get("requires", []),
            "optional": _METRIC_META.get(k, {}).get("optional", []),
            "default_threshold": v,
            "eval_methods": _METRIC_META.get(k, {}).get("eval_methods", []),
            "description": _METRIC_META.get(k, {}).get("description", ""),
        }
        for k, v in METRICS_CONFIG.items()
        if not (offline_only and k in _RUNTIME_KEYS)
    ]

    # 2. DeepEval 指标
    deepeval = []
    categories = {}
    try:
        from evaluators.deepeval_metrics.deepeval_suite import (
            METRIC_SPECS, CATEGORY_LABELS, is_metric_offline_supported,
        )
        from evaluators.registry import _metric_params_cache
        categories = dict(CATEGORY_LABELS)
        for spec in METRIC_SPECS:
            if spec.key in ("faithfulness", "answer_relevancy", "golden_match", "hallucination"):
                continue  # 与原生同名或作用重叠，不重复展示（correctness 替代 golden_match，faithfulness 替代 hallucination）
            if offline_only and not is_metric_offline_supported(spec):
                continue  # 离线评估不支持依赖 Agent 推理过程产物的指标
            deepeval.append({
                "key": spec.key,
                "label": spec.label,
                "category": spec.category,
                "category_label": CATEGORY_LABELS.get(spec.category, spec.category),
                "requires": list(spec.requires),
                "note": spec.note,
                "params": [
                    {"name": p.name, "label": p.label, "type": p.type,
                     "required": p.required, "placeholder": p.placeholder}
                    for p in spec.params
                ],
                "param_values": _metric_params_cache.get(spec.key, {}),
                "gate_threshold": spec.gate_threshold,
                "lower_is_better": spec.lower_is_better,
            })
    except ImportError:
        pass  # deepeval 未安装，deepeval 列表为空

    # 3. 自定义指标
    custom = []
    async with async_session() as session:
        result = await session.execute(
            select(CustomMetric).where(CustomMetric.is_active == True)  # noqa: E712
        )
        for cm in result.scalars():
            if offline_only and cm.multi_turn:
                continue  # 离线评估不支持多轮对话指标(需对话回合轨迹)
            custom.append({
                "id": cm.id,
                "key": f"custom_{cm.id}",
                "name": cm.name,
                "kind": cm.kind,
                "category": cm.category,
                "threshold": cm.threshold,
                "is_active": cm.is_active,
            })

    return {
        "builtin": builtin,
        "deepeval": deepeval,
        "custom": custom,
        "categories": categories,
    }


class RuntimeThresholdUpdate(BaseModel):
    inference_latency_ms: Optional[float] = Field(None, gt=0, description="推理时延全局默认阈值，单位毫秒")
    tokens_total: Optional[float] = Field(None, gt=0, description="Tokens 总消耗全局默认阈值")


@app.get("/metrics/runtime-thresholds")
async def get_runtime_thresholds(
    session: AsyncSession = Depends(get_session),
):
    """获取运行时指标全局默认阈值。"""
    from services.system_config_service import get_all_runtime_thresholds

    thresholds = await get_all_runtime_thresholds(session)
    return {
        "inference_latency_ms": thresholds.get("inference_latency"),
        "tokens_total": thresholds.get("tokens_total"),
    }


@app.put("/metrics/runtime-thresholds")
async def update_runtime_thresholds(
    request: RuntimeThresholdUpdate,
    session: AsyncSession = Depends(get_session),
):
    """更新运行时指标全局默认阈值（对新创建的任务生效）。"""
    from services.system_config_service import set_runtime_thresholds

    updates: dict[str, float] = {}
    if request.inference_latency_ms is not None:
        updates["inference_latency"] = request.inference_latency_ms
    if request.tokens_total is not None:
        updates["tokens_total"] = request.tokens_total
    if not updates:
        raise HTTPException(400, "未提供任何更新字段")

    await set_runtime_thresholds(session, updates)
    await session.commit()

    from services.system_config_service import get_all_runtime_thresholds
    thresholds = await get_all_runtime_thresholds(session)
    return {
        "inference_latency_ms": thresholds.get("inference_latency"),
        "tokens_total": thresholds.get("tokens_total"),
        "message": "运行时指标全局默认阈值已更新，对新创建的任务生效。",
    }


# ==================== 指标参数配置（json_correctness / prompt_alignment 等）====================

class MetricParamsUpdate(BaseModel):
    """指标参数更新请求体：param_name → param_value。

    value 类型按 MetricSpec.params 中的 type 决定：
    - text: 字符串
    - list: 字符串数组（前端按行拆分）
    - json: JSON 对象
    """
    params: Dict[str, Any] = Field(default_factory=dict)


@app.get("/metrics/{metric_key}/params")
async def get_metric_params_api(
    metric_key: str,
    session: AsyncSession = Depends(get_session),
):
    """读取单个指标的用户配置参数。"""
    from services.system_config_service import get_metric_params
    return await get_metric_params(session, metric_key)


@app.put("/metrics/{metric_key}/params")
async def update_metric_params_api(
    metric_key: str,
    request: MetricParamsUpdate,
    session: AsyncSession = Depends(get_session),
):
    """更新单个指标的用户配置参数（整体覆盖），对新创建的任务生效。

    值类型按指标参数声明：
    - list 类型：前端按行拆分成数组
    - json 类型：直接传 JSON 对象
    - text 类型：字符串
    """
    from evaluators.deepeval_metrics.deepeval_suite import METRIC_SPECS
    from services.system_config_service import set_metric_params

    # 校验 metric_key 合法且有 params 声明
    spec = next((s for s in METRIC_SPECS if s.key == metric_key), None)
    if spec is None or not spec.params:
        raise HTTPException(404, f"指标 {metric_key} 不存在或不接受参数配置")

    # 按 spec.params 中的 type 规范化 value
    normalized: Dict[str, Any] = {}
    decl_by_name = {p.name: p for p in spec.params}
    for k, v in request.params.items():
        decl = decl_by_name.get(k)
        if decl is None:
            continue  # 忽略未声明的参数
        if decl.type == "list":
            if isinstance(v, str):
                v = [line.strip() for line in v.split("\n") if line.strip()]
            elif isinstance(v, list):
                v = [str(x).strip() for x in v if str(x).strip()]
        elif decl.type == "json":
            if isinstance(v, str):
                import json as _json
                try:
                    v = _json.loads(v)
                except _json.JSONDecodeError:
                    raise HTTPException(400, f"参数 {k}（{decl.label}）的 JSON 格式无效")
        normalized[k] = v

    await set_metric_params(session, metric_key, normalized)
    await session.commit()

    # 刷新进程缓存，对新创建任务立即生效
    from evaluators.registry import refresh_metric_params
    await refresh_metric_params(session, metric_key)

    return {
        "metric": metric_key,
        "params": normalized,
        "message": "指标参数已更新，对新创建的任务生效。",
    }


# ==================== 单题同步评测（供 MCP server 调用）====================

class EvaluateSingleRequest(BaseModel):
    question: str
    answer: str
    metric: str
    context: Optional[str] = None
    ground_truth: Optional[str] = None
    threshold: Optional[float] = None
    model_key: Optional[str] = None


@app.post("/api/v1/evaluate/single")
async def evaluate_single(req: EvaluateSingleRequest, http_request: Request):
    """单题同步评测，无状态，不落库。

    供 MCP server 调用。P0 只走 LLM 评估（faithfulness 不暴露 BM25 选项）。
    缺必要输入时返回 score=null, skipped=true（非错误）。

    鉴权：若设置了 EVAL_SINGLE_TOKEN 环境变量，请求须带 X-Eval-Token header
    匹配该值；未设置则开放（本地开发无负担）。
    """
    import os
    from fastapi.responses import JSONResponse

    # 0. token 校验（保护花钱的端点，未设环境变量则开放）
    expected_token = os.getenv("EVAL_SINGLE_TOKEN", "").strip()
    if expected_token:
        provided = (http_request.headers.get("X-Eval-Token", "") or "").strip()
        if provided != expected_token:
            return JSONResponse(
                status_code=401,
                content={"error": "invalid_token", "message": "X-Eval-Token 校验失败"},
            )

    from config import METRICS_CONFIG
    from evaluators.registry import METRICS_REGISTRY, get_metric

    # 1. 校验指标名（只允许原生质量指标，排除 runtime 指标）
    _QUALITY_METRICS = {"faithfulness", "answer_relevancy", "correctness"}
    if req.metric not in _QUALITY_METRICS:
        from fastapi.responses import JSONResponse
        available = [k for k in METRICS_REGISTRY.keys() if k in _QUALITY_METRICS]
        return JSONResponse(
            status_code=400,
            content={
                "error": "unknown_metric",
                "message": f"Unknown metric: {req.metric}. Available: {available}",
                "available_metrics": available,
            },
        )

    # 2. 取指标实例（P0 faithfulness 强制 LLM，不传 use_bm25）
    metric = get_metric(
        req.metric,
        model_key=req.model_key,
    )

    # 3. 评测（同步 await，不进任务队列）
    effective_threshold = (
        req.threshold if req.threshold is not None
        else METRICS_CONFIG.get(req.metric, 0.8)
    )
    try:
        result, passed = await metric.measure(
            question=req.question,
            answer=req.answer,
            context=req.context,
            ground_truth=req.ground_truth,
            threshold=effective_threshold,
        )
    except Exception as exc:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=503,
            content={
                "error": "evaluator_error",
                "message": str(exc),
                "metric": req.metric,
            },
        )

    # 4. 判断 skipped（score=None 表示缺输入已跳过）
    skipped = result.score is None

    return {
        "metric": req.metric,
        "score": result.score,
        "passed": passed and not skipped,
        "threshold": effective_threshold,
        "reason": result.reason,
        "duration_ms": result.duration_ms,
        "skipped": skipped,
        "eval_method": "llm",
    }


# ==================== 自定义指标 CRUD ====================

class CustomMetricCreateRequest(BaseModel):
    """创建自定义指标请求"""
    name: str = Field(..., min_length=1, max_length=200)
    kind: str = Field(..., pattern="^(geval|dag|code)$")
    category: Optional[str] = None    # 自定义指标类别（指标集合），空则归入「自定义指标」
    criteria: Optional[str] = None      # geval: 自然语言评判标准
    dag: Optional[dict] = None           # dag: 决策树定义
    code: Optional[str] = None           # code: 用户 Python 代码
    multi_turn: bool = False
    params: Optional[list] = None        # geval: LLMTestCaseParams 名列表
    threshold: float = 0.5


class CustomMetricUpdateRequest(BaseModel):
    """更新自定义指标请求"""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    kind: Optional[str] = Field(None, pattern="^(geval|dag|code)$")
    category: Optional[str] = None
    criteria: Optional[str] = None
    dag: Optional[dict] = None
    code: Optional[str] = None
    multi_turn: Optional[bool] = None
    params: Optional[list] = None
    threshold: Optional[float] = None
    is_active: Optional[bool] = None


class CustomMetricTestRequest(BaseModel):
    """试运行自定义指标请求"""
    question: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1)
    context: Optional[str] = None
    ground_truth: Optional[str] = None


def _validate_custom_metric_fields(kind: str, criteria: Optional[str], dag: Optional[dict], code: Optional[str]):
    """按 kind 校验必填字段。"""
    if kind == "geval" and not (criteria and criteria.strip()):
        raise HTTPException(status_code=422, detail="kind=geval 时 criteria 不能为空")
    if kind == "dag" and not dag:
        raise HTTPException(status_code=422, detail="kind=dag 时 dag 不能为空")
    if kind == "code" and not (code and code.strip()):
        raise HTTPException(status_code=422, detail="kind=code 时 code 不能为空")


@app.get("/custom-metrics")
async def list_custom_metrics():
    """获取所有自定义指标列表"""
    async with async_session() as session:
        result = await session.execute(
            select(CustomMetric).order_by(CustomMetric.id)
        )
        items = []
        for cm in result.scalars():
            items.append({
                "id": cm.id,
                "name": cm.name,
                "kind": cm.kind,
                "category": cm.category,
                "criteria": cm.criteria,
                "dag": cm.dag,
                "code": cm.code,
                "multi_turn": cm.multi_turn,
                "params": cm.params,
                "threshold": cm.threshold,
                "is_active": cm.is_active,
                "created_at": cm.created_at.isoformat() if cm.created_at else None,
                "updated_at": cm.updated_at.isoformat() if cm.updated_at else None,
            })
        return {"items": items, "total": len(items)}


@app.get("/custom-metric-categories")
async def list_custom_metric_categories():
    """获取已使用的自定义指标类别（去重，供前端下拉），NULL 归入「自定义指标」"""
    async with async_session() as session:
        result = await session.execute(
            select(CustomMetric.category).distinct().where(CustomMetric.is_active == True)  # noqa: E712
        )
        cats = []
        seen_default = False
        for (cat,) in result:
            if cat:
                cats.append(cat)
            else:
                seen_default = True
        # 默认类别「自定义指标」始终提供，保证未分类指标可见
        if seen_default or not cats:
            cats.append("自定义指标")
        return {"categories": cats}


@app.post("/custom-metrics", status_code=201)
async def create_custom_metric(req: CustomMetricCreateRequest):
    """创建自定义指标"""
    _validate_custom_metric_fields(req.kind, req.criteria, req.dag, req.code)
    async with async_session() as session:
        cm = CustomMetric(
            name=req.name,
            kind=req.kind,
            category=(req.category.strip() if req.category and req.category.strip() else None),
            criteria=req.criteria,
            dag=req.dag,
            code=req.code,
            multi_turn=req.multi_turn,
            params=req.params,
            threshold=req.threshold,
            is_active=True,
        )
        session.add(cm)
        await session.commit()
        await session.refresh(cm)
        return {
            "id": cm.id,
            "name": cm.name,
            "kind": cm.kind,
            "category": cm.category,
            "threshold": cm.threshold,
            "is_active": cm.is_active,
            "message": "创建成功",
        }


@app.put("/custom-metrics/{cm_id}")
async def update_custom_metric(cm_id: int, req: CustomMetricUpdateRequest):
    """更新自定义指标"""
    async with async_session() as session:
        result = await session.execute(
            select(CustomMetric).where(CustomMetric.id == cm_id)
        )
        cm = result.scalar_one_or_none()
        if not cm:
            raise HTTPException(status_code=404, detail=f"自定义指标 {cm_id} 不存在")

        # 部分更新：仅更新非 None 字段
        kind = req.kind if req.kind is not None else cm.kind
        criteria = req.criteria if req.criteria is not None else cm.criteria
        dag = req.dag if req.dag is not None else cm.dag
        code = req.code if req.code is not None else cm.code
        _validate_custom_metric_fields(kind, criteria, dag, code)

        if req.name is not None:
            cm.name = req.name
        cm.kind = kind
        # category 允许清空：空串 → NULL（归入默认「自定义指标」）
        if req.category is not None:
            cm.category = req.category.strip() or None
        cm.criteria = criteria
        cm.dag = dag
        cm.code = code
        if req.multi_turn is not None:
            cm.multi_turn = req.multi_turn
        if req.params is not None:
            cm.params = req.params
        if req.threshold is not None:
            cm.threshold = req.threshold
        if req.is_active is not None:
            cm.is_active = req.is_active

        await session.commit()
        return {"id": cm.id, "message": "更新成功"}


@app.delete("/custom-metrics/{cm_id}")
async def delete_custom_metric(cm_id: int):
    """删除自定义指标（软删除：is_active=False）"""
    async with async_session() as session:
        result = await session.execute(
            select(CustomMetric).where(CustomMetric.id == cm_id)
        )
        cm = result.scalar_one_or_none()
        if not cm:
            raise HTTPException(status_code=404, detail=f"自定义指标 {cm_id} 不存在")

        # 检查是否有评测任务引用
        ref_result = await session.execute(
            select(EvaluationTask).where(EvaluationTask.custom_metric_ids.isnot(None))
        )
        for task in ref_result.scalars():
            try:
                ids = json.loads(task.custom_metric_ids) if task.custom_metric_ids else []
                if cm_id in ids:
                    raise HTTPException(
                        status_code=409,
                        detail=f"自定义指标 {cm_id} 被评测任务 {task.id} 引用，无法删除。请先移除引用或设为停用。"
                    )
            except (json.JSONDecodeError, TypeError):
                continue

        cm.is_active = False
        await session.commit()
        return {"id": cm_id, "message": "已停用（软删除）"}


@app.post("/custom-metrics/{cm_id}/test")
async def test_custom_metric(cm_id: int, req: CustomMetricTestRequest):
    """试运行自定义指标：对单个 question+answer 打分验证"""
    async with async_session() as session:
        result = await session.execute(
            select(CustomMetric).where(CustomMetric.id == cm_id)
        )
        cm = result.scalar_one_or_none()
        if not cm:
            raise HTTPException(status_code=404, detail=f"自定义指标 {cm_id} 不存在")

        cm_def = {
            "key": f"custom_{cm.id}",
            "name": cm.name,
            "kind": cm.kind,
            "criteria": cm.criteria or "",
            "dag": cm.dag,
            "code": cm.code or "",
            "multi_turn": cm.multi_turn,
            "params": cm.params,
            "threshold": cm.threshold,
        }

        from services.evaluation_pipeline import _run_custom_metric
        eval_result = await _run_custom_metric(
            cm_def, req.question, req.answer, req.context, req.ground_truth,
            "", "",  # llm_model, llm_model_name（试运行用空，geval/dag 会降级报错）
        )

        if eval_result is None:
            return {"score": 0.0, "reason": "指标执行返回空结果", "duration_ms": 0}

        return {
            "score": eval_result.get("score", 0.0),
            "reason": eval_result.get("reason", ""),
            "duration_ms": eval_result.get("duration_ms", 0),
        }


def _has_scenario_step_columns(content: bytes, filename: str) -> bool:
    """检测文件是否含场景子步骤列（Step_Question 等）。

    CSV：读表头检测；XLSX：用 parse_scenario_xlsx 尝试解析；
    JSON：不含 Step 列。
    """
    from services.dataset_io_service import is_scenario_csv

    lower = (filename or "").lower()
    if lower.endswith(".json"):
        return False
    if lower.endswith(".xlsx") or content[:2] == b"PK":
        # XLSX：尝试用场景解析器解析，成功则认为含 Step 列
        from services.dataset_io_service import parse_scenario_xlsx
        try:
            parse_scenario_xlsx(content)
            return True
        except Exception:
            return False
    if lower.endswith(".csv"):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = content.decode("gbk")
            except UnicodeDecodeError:
                return False
        lines = text.splitlines()
        if lines and lines[0].startswith("说明"):
            lines = lines[1:]
        if not lines:
            return False
        headers = lines[0].split(",")
        return is_scenario_csv([h.strip() for h in headers])
    return False


@app.post("/upload", response_model=DatasetUploadResponse)
async def upload_dataset(
    file: UploadFile = File(...),
    name: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session)
):
    """Upload and validate a JSON or CSV dataset.

    统一入口：自动检测文件是否含场景子步骤列（Step_Question 等）。
    含则走场景导入（建 dataset + 子步骤模板）；不含走普通导入 + 质量预校验。
    """
    from services.dataset_io_service import (
        DatasetUploadFormatError,
        default_dataset_name,
        detect_upload_format,
        ensure_unique_dataset_name,
        parse_upload_content,
        validate_dataset_items_with_format,
        validate_dataset_quality,
    )

    log("INFO", "API", "数据集上传开始", filename=file.filename)
    content = await file.read()
    filename = file.filename or "upload.json"

    # 检测是否含场景子步骤列 → 自动分流到场景导入
    if _has_scenario_step_columns(content, filename):
        result = await _do_scenario_import(content, filename, name, session)
        # 场景导入成功，返回时补 quality_warnings（空，已有子步骤无需提炼）
        return DatasetUploadResponse(
            dataset_id=result["dataset_id"],
            item_count=result["item_count"],
            quality_warnings=[],
        )

    try:
        data = parse_upload_content(content, filename)
    except DatasetUploadFormatError as exc:
        log("ERROR", "API", "数据集解析失败", filename=filename, error=str(exc))
        detail = str(exc)
        if "\n" in detail:
            raise HTTPException(400, {"error": "Parse failed", "details": detail.split("\n")})
        raise HTTPException(400, detail)

    errors = validate_dataset_items_with_format(data, filename)
    if errors:
        log("ERROR", "API", "数据集验证失败", filename=filename, errors=errors)
        raise HTTPException(400, {"error": "Validation failed", "details": errors})

    # 质量预校验（不阻断，仅返回 warnings 供前端提示）
    is_csv = detect_upload_format(filename) == "csv"
    quality_warnings = validate_dataset_quality(
        data, row_label="行" if is_csv else "Item"
    )
    if quality_warnings:
        log(
            "INFO",
            "API",
            "数据集质量预校验警告",
            filename=filename,
            warning_count=len(quality_warnings),
        )

    dataset_id = f"ds_{uuid.uuid4().hex}"
    final_name = default_dataset_name(name, filename, dataset_id)
    final_name = await ensure_unique_dataset_name(session, final_name)
    dataset = Dataset(
        id=dataset_id,
        name=final_name,
        data=json.dumps(data, ensure_ascii=False),
        item_count=len(data),
    )
    session.add(dataset)
    await session.commit()

    log(
        "INFO",
        "API",
        "数据集上传完成",
        dataset_id=dataset_id,
        name=final_name,
        item_count=len(data),
    )
    return DatasetUploadResponse(
        dataset_id=dataset_id,
        item_count=len(data),
        quality_warnings=quality_warnings,
    )



async def _do_scenario_import(
    content: bytes,
    filename: str,
    name: Optional[str],
    session: AsyncSession,
) -> Dict[str, Any]:
    """场景导入核心逻辑：解析 CSV/XLSX → 建 dataset + 场景子步骤模板。

    被 /upload（检测到 Step 列时）和 /datasets/import/with-scenario 共用。
    """
    from services.dataset_io_service import (
        DatasetUploadFormatError,
        default_dataset_name,
        ensure_unique_dataset_name,
        is_scenario_csv,
        parse_scenario_csv,
        parse_scenario_xlsx,
    )
    from services.dataset_item_scenario_service import upsert_item_template

    log("INFO", "API", "场景导入开始", filename=filename)
    is_xlsx = filename.lower().endswith(".xlsx") or content[:2] == b"PK"

    if is_xlsx:
        try:
            parsed = parse_scenario_xlsx(content)
        except DatasetUploadFormatError as exc:
            log("ERROR", "API", "XLSX解析失败", filename=filename, error=str(exc))
            raise HTTPException(400, str(exc))
    else:
        # CSV：检测是否为场景 CSV
        try:
            import io as _io
            import csv as _csv
            text = content.decode("utf-8-sig")
            lines = text.splitlines()
            if lines and lines[0].startswith("说明"):
                lines = lines[1:]
            clean_text = "\n".join(lines)
            reader = _csv.DictReader(_io.StringIO(clean_text))
            if not reader.fieldnames or not is_scenario_csv(list(reader.fieldnames)):
                raise HTTPException(400, "非场景CSV格式，请使用普通 JSON/CSV 导入（表头需包含 Step_Question 列）")
        except UnicodeDecodeError:
            raise HTTPException(400, "CSV 须为 UTF-8 编码")
        except DatasetUploadFormatError as exc:
            raise HTTPException(400, str(exc))

        try:
            parsed = parse_scenario_csv(content)
        except DatasetUploadFormatError as exc:
            log("ERROR", "API", "场景CSV解析失败", filename=filename, error=str(exc))
            detail = str(exc)
            if "\n" in detail:
                raise HTTPException(400, {"error": "Parse failed", "details": detail.split("\n")})
            raise HTTPException(400, detail)

    items = parsed["items"]
    scenario_steps = parsed["scenario_steps"]

    dataset_items: List[Dict[str, Any]] = []
    for i, item in enumerate(items):
        ditem_id = f"ditem_{i + 1}"
        dataset_items.append({
            "id": ditem_id,
            "dataset_item_id": ditem_id,
            "question": item["question"],
            "prompt": item.get("prompt", ""),
            "ground_truth": item.get("ground_truth", ""),
            "model_answer": item.get("model_answer", ""),
            "judging_mode": item.get("judging_mode", ""),
        })

    dataset_id = f"ds_{uuid.uuid4().hex}"
    final_name = default_dataset_name(name, filename, dataset_id)
    final_name = await ensure_unique_dataset_name(session, final_name)
    dataset = Dataset(
        id=dataset_id,
        name=final_name,
        data=json.dumps(dataset_items, ensure_ascii=False),
        item_count=len(dataset_items),
    )
    session.add(dataset)
    await session.flush()

    created_templates = 0
    created_steps = 0

    for item_idx, ditem in enumerate(dataset_items):
        ditem_id = ditem["id"]
        item_steps_raw = [
            s for s in scenario_steps
            if s["question_index"] == item_idx
        ]
        if not item_steps_raw:
            continue

        template_steps: List[Dict[str, Any]] = []

        weights: List[float] = []
        has_custom_weight = False
        for s in item_steps_raw:
            w_str = s.get("step_weight", "")
            if w_str:
                try:
                    weights.append(float(w_str))
                    has_custom_weight = True
                except (ValueError, TypeError):
                    weights.append(0.0)
            else:
                weights.append(0.0)

        if has_custom_weight:
            total_w = sum(weights)
            if total_w > 0:
                weights = [round(w / total_w, 4) for w in weights]
            else:
                weights = [round(1.0 / len(weights), 4)] * len(weights)
        else:
            weights = [round(1.0 / len(item_steps_raw), 4)] * len(item_steps_raw)

        for step_idx, s in enumerate(item_steps_raw):
            jm = s.get("step_judging_mode", "Ground_Truth评测")
            # 兼容新旧名称：旧名"提示词评测"，新名"基于Prompt评测"
            mode = "llm_aggregate" if ("Prompt" in jm or "提示词" in jm) else "keyword"

            gt_text = s.get("step_ground_truth", "")
            keywords = [k.strip() for k in gt_text.replace(",", "，").split("，") if k.strip()] if gt_text else []

            step_name = s.get("step_question", f"步骤{step_idx + 1}")
            step_key = f"step_{step_idx + 1}"

            eval_config: Dict[str, Any] = {"mode": mode}
            if mode == "llm_aggregate" and s.get("step_prompt"):
                eval_config["aggregate_prompt"] = s["step_prompt"]

            gt_expected: Dict[str, Any] = {"keywords": keywords, "text": gt_text} if keywords or gt_text else {"keywords": []}
            if s.get("step_model_answer"):
                gt_expected["model_answer"] = s["step_model_answer"]

            template_steps.append({
                "step_key": step_key,
                "name": step_name,
                "step_order": step_idx + 1,
                "weight": weights[step_idx],
                "required": True,
                "enabled": True,
                "enforce_order": False,
                "gt_expected": gt_expected,
                "gt_criteria": keywords,
                "match_rules": {
                    "eval_config": eval_config,
                },
            })

        if template_steps:
            await upsert_item_template(
                session,
                dataset_id=dataset_id,
                dataset_item_id=ditem_id,
                name=ditem["question"],
                steps=template_steps,
                enabled=True,
            )
            created_templates += 1
            created_steps += len(template_steps)

    await session.commit()

    log(
        "INFO",
        "API",
        "场景导入完成",
        dataset_id=dataset_id,
        name=final_name,
        item_count=len(dataset_items),
        template_count=created_templates,
        step_count=created_steps,
    )

    return {
        "dataset_id": dataset_id,
        "name": final_name,
        "item_count": len(dataset_items),
        "template_count": created_templates,
        "step_count": created_steps,
    }


@app.post("/datasets/import/with-scenario")
async def import_dataset_with_scenario(
    file: UploadFile = File(...),
    name: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session)
):
    """一键导入：从扩展 CSV/XLSX（含 Step_Question 等步骤列）创建评测集 + 自动配置场景子步骤模板。"""
    content = await file.read()
    filename = file.filename or "upload.csv"
    return await _do_scenario_import(content, filename, name, session)



@app.post("/evaluate", response_model=EvaluateResponse, status_code=202)
async def start_evaluation(
    request: EvaluateRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session)
):
    """Start a new evaluation task.

    核心逻辑抽至 services.eval_task_service.create_and_start_eval_task，
    供 AI 对话配置的 /chat/sessions/{id}/apply 复用，避免二次 HTTP 回环。
    """
    from services.eval_task_service import create_and_start_eval_task
    return await create_and_start_eval_task(request, session, background_tasks)


@app.get("/result/{task_uuid}")
async def get_result(
    task_uuid: str,
    partial: bool = Query(False, description="进行中任务返回已落库部分结果"),
    session: AsyncSession = Depends(get_session)
):
    """Get evaluation results (polling endpoint)."""
    from services.partial_result_service import build_partial_result_payload

    result = await session.execute(
        select(EvaluationTask).where(EvaluationTask.id == task_uuid)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")

    if task.status in [TaskStatus.PENDING, TaskStatus.PROCESSING, TaskStatus.PAUSED]:
        if partial:
            return await build_partial_result_payload(session, task)
        return {
            "status": task.status,
            "progress": f"{task.progress_current}/{task.progress_total}",
            "is_paused": task.paused_at is not None
        }

    if task.status == TaskStatus.FAILED:
        return {"status": "failed", "error": task.error}

    from services.evaluation_mode import (
        get_evaluation_mode,
        get_task_capabilities,
        get_task_dimension_flags,
        is_offline_task,
    )
    from services.quality_score_service import RUNTIME_METRICS

    evaluation_mode = get_evaluation_mode(task)
    capabilities = get_task_capabilities(task)
    offline = is_offline_task(task)

    # For completed tasks, ensure progress shows 100%
    if task.status == TaskStatus.COMPLETED:
        # Fix progress if it's not at total
        if task.progress_current != task.progress_total and task.progress_total > 0:
            await session.execute(
                update(EvaluationTask)
                .where(EvaluationTask.id == task_uuid)
                .values(progress_current=task.progress_total)
            )
            await session.commit()
            # Refresh the task object to get the updated progress
            await session.refresh(task)

    results = await session.execute(
        select(EvaluationResult).where(EvaluationResult.task_id == task_uuid)
    )
    results = results.scalars().all()

    # 获取数据集问题内容
    dataset_result = await session.execute(
        select(Dataset).where(Dataset.id == task.dataset_id)
    )
    dataset = dataset_result.scalar_one_or_none()
    question_map = {}
    if dataset and dataset.data:
        try:
            dataset_items = json.loads(dataset.data)
            for idx, item in enumerate(dataset_items):
                question = item.get("question") or item.get("q") or item.get("input", "")
                if question:
                    question_map[idx] = question
        except:
            pass

    radar_data = {}
    runtime_summary: dict = {}
    runs_for_task: list = []
    inference_model_summary = None
    execution_mode_summary = None
    if not offline:
        runs_for_task = (
            await session.execute(select(AgentRun).where(AgentRun.task_id == task_uuid))
        ).scalars().all()
    token_breakdown_by_run = await _load_runtime_token_breakdowns(session, runs_for_task)
    runs_by_id_result = {ar.id: ar for ar in runs_for_task}
    trace_latency_by_run_result = await batch_load_trace_latency_ms(
        session, [ar.id for ar in runs_for_task]
    )
    tokens_by_q: dict[int, int] = {}
    tokens_llm_by_q: dict[int, int] = {}
    tokens_tool_by_q: dict[int, int] = {}
    if not offline:
        for ar in runs_for_task:
            if ar.question_id is not None and ar.tokens_total is not None:
                tokens_by_q[ar.question_id] = ar.tokens_total
            if ar.question_id is not None:
                llm_tokens, tool_tokens = token_breakdown_by_run.get(
                    ar.id,
                    _runtime_token_breakdown(ar),
                )
                if llm_tokens is not None:
                    tokens_llm_by_q[ar.question_id] = llm_tokens
                if tool_tokens is not None:
                    tokens_tool_by_q[ar.question_id] = tool_tokens
        inference_model_summary = summarize_inference_models_from_runs(runs_for_task)
        execution_mode_summary = summarize_execution_modes_from_runs(runs_for_task)

        lat_rows = [x for x in results if x.metric_name == "inference_latency"]
        lat_ms = []
        for row in lat_rows:
            run_row = runs_by_id_result.get(row.agent_run_id or "")
            resolved = None
            if run_row:
                resolved = resolve_agent_e2e_duration_ms(
                    run_row,
                    trace_latency_ms=trace_latency_by_run_result.get(run_row.id),
                    stored_inference_duration_ms=row.inference_duration_ms,
                )
            elif row.inference_duration_ms is not None:
                resolved = int(row.inference_duration_ms)
            if resolved is not None:
                lat_ms.append(resolved)
        if lat_ms:
            norm = [
                (x.avg_score if x.avg_score is not None else x.score or 0) for x in lat_rows
            ]
            passed_count = sum(1 for x in lat_rows if x.passed)
            runtime_summary["inference_latency"] = {
                "avg_raw": round(sum(lat_ms) / len(lat_ms), 1),
                "unit": "ms",
                "normalized_avg": round(sum(norm) / len(norm), 3) if norm else 0,
                "threshold": lat_rows[0].threshold if lat_rows else None,
                "pass_rate": round(passed_count / len(lat_rows) * 100, 1) if lat_rows else 0,
                "runtime_target_source": _runtime_target_source(task, "inference_latency"),
            }
        tok_rows = [x for x in results if x.metric_name == "tokens_total"]
        # avg_raw 与 performance-report 的 avg_tokens_per_question 同口径：
        # 所有 run 的 tokens_total 求和 / 唯一题数。
        # 旧实现用 tokens_by_q（遍历覆盖只留每题最后一轮），多轮场景下 avg_raw 偏低、与详情页不一致。
        tok_run_vals = [
            ar.tokens_total
            for ar in runs_for_task
            if ar.tokens_total is not None
        ]
        unique_tok_q = {
            ar.question_id
            for ar in runs_for_task
            if ar.question_id is not None and ar.tokens_total is not None
        }
        if tok_run_vals and unique_tok_q:
            norm_t = [
                (x.avg_score if x.avg_score is not None else x.score or 0) for x in tok_rows
            ]
            passed_count = sum(1 for x in tok_rows if x.passed)
            runtime_summary["tokens_total"] = {
                "avg_raw": round(sum(tok_run_vals) / len(unique_tok_q), 1),
                "unit": "tokens",
                "normalized_avg": round(sum(norm_t) / len(norm_t), 3) if norm_t else 0,
                "threshold": tok_rows[0].threshold if tok_rows else None,
                "pass_rate": round(passed_count / len(tok_rows) * 100, 1) if tok_rows else 0,
                "runtime_target_source": _runtime_target_source(task, "tokens_total"),
            }

    for r in results:
        if r.metric_name in RUNTIME_METRICS or (r.eval_method or "") == "runtime":
            continue
        if r.model not in radar_data:
            radar_data[r.model] = {}
        if r.metric_name not in radar_data[r.model]:
            radar_data[r.model][r.metric_name] = []
        if r.score is not None:
            radar_data[r.model][r.metric_name].append(r.score)

    for model in radar_data:
        for metric in radar_data[model]:
            scores = radar_data[model][metric]
            if scores:
                radar_data[model][metric] = round(sum(scores) / len(scores), 3)
            else:
                radar_data[model][metric] = 0

    # 按 question_id 去重题目文本（前端表格仅用 question_id 编号，文本外键化到 questions 字典）
    questions_map: dict[int, str] = {}
    for r in results:
        qid = r.question_id
        if qid is not None and qid not in questions_map:
            questions_map[qid] = r.question or question_map.get(qid, "")

    details = []
    for r in results:
        run_row = runs_by_id_result.get(r.agent_run_id or "")
        resolved_latency = None
        if run_row:
            resolved_latency = resolve_agent_e2e_duration_ms(
                run_row,
                trace_latency_ms=trace_latency_by_run_result.get(run_row.id),
                stored_inference_duration_ms=r.inference_duration_ms,
            )
        elif r.inference_duration_ms is not None:
            resolved_latency = int(r.inference_duration_ms)
        details.append({
            "question_id": r.question_id,
            "model": r.model,
            "agent_run_id": r.agent_run_id,
            "run_index": run_row.run_index if run_row and run_row.run_index is not None else 0,
            "metrics": [{
                "name": r.metric_name,
                "score": r.score,
                "threshold": r.threshold,
                "passed": r.passed,
                "duration_ms": r.duration_ms,
                "cot": r.cot,
                "agent_name": r.agent_name,
                "avg_score": r.avg_score,
                "eval_model": r.eval_model,
                "eval_rounds": r.eval_rounds,
                "inference_duration_ms": resolved_latency,
                "eval_method": r.eval_method,
                "script_name": r.script_name,
            }]
        })

    runtime_details = []
    if not offline:
        latency_by_q: dict[int, int] = {}
        stored_latency_by_run: dict[str, int] = {}
        for row in results:
            if (
                row.metric_name == "inference_latency"
                and row.agent_run_id
                and row.inference_duration_ms is not None
                and row.inference_duration_ms > 0
            ):
                stored_latency_by_run.setdefault(
                    row.agent_run_id, int(row.inference_duration_ms)
                )
        trace_status_by_q: dict[int, Optional[str]] = {}
        for ar in runs_for_task:
            if ar.question_id is None:
                continue
            trace_status_by_q.setdefault(ar.question_id, ar.trace_sync_status)
            resolved = resolve_agent_e2e_duration_ms(
                ar,
                trace_latency_ms=trace_latency_by_run_result.get(ar.id),
                stored_inference_duration_ms=stored_latency_by_run.get(ar.id),
            )
            if resolved is not None:
                latency_by_q[ar.question_id] = resolved

        runtime_question_ids = sorted(set(latency_by_q.keys()) | set(tokens_by_q.keys()))
        for qid in runtime_question_ids:
            runtime_details.append(
                {
                    "question_id": qid,
                    "inference_duration_ms": latency_by_q.get(qid),
                    "tokens_total": tokens_by_q.get(qid),
                    "tokens_llm": tokens_llm_by_q.get(qid),
                    "tokens_tool": tokens_tool_by_q.get(qid),
                    "trace_sync_status": trace_status_by_q.get(qid),
                }
            )
    
    quality_score = await compute_task_quality_score(session, task_uuid, results=results)
    dimension_verdicts = await build_task_dimension_verdicts(session, task_uuid, results=results)

    from config import get_model_display_name
    llm_model_key = task.llm_model or "glm"
    judge_model_name = get_model_display_name(llm_model_key)

    return {
        "status": "completed",
        "progress": f"{task.progress_current}/{task.progress_total}",
        "evaluation_mode": evaluation_mode,
        "capabilities": capabilities,
        "dimension_flags": get_task_dimension_flags(task),
        "dataset_id": task.dataset_id,
        "summary": {
            "radar_data": radar_data,
            "runtime_summary": runtime_summary,
            "inference_model_summary": inference_model_summary,
            "execution_mode_summary": execution_mode_summary,
            "quality_score": quality_score,
            "dimension_verdicts": dimension_verdicts,
            "text": task.summary,
            "judge_model_name": judge_model_name,
            "answer_source": "dataset" if offline else "agent",
        },
        "quality_score": quality_score,
        "dimension_verdicts": dimension_verdicts,
        "details": details,
        "questions": {str(k): v for k, v in questions_map.items()},
        "runtime_details": runtime_details,
    }


def _runtime_targets_from_task(task: EvaluationTask) -> dict:
    raw = getattr(task, "runtime_metric_targets", None)
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        return {}


def _runtime_target_source(task: EvaluationTask, metric_name: str) -> str:
    targets = _runtime_targets_from_task(task)
    return "task_custom" if metric_name in targets else "system_default"


async def _load_runtime_token_breakdowns(
    session: AsyncSession,
    runs: list[AgentRun],
) -> dict[str, tuple[Optional[int], Optional[int]]]:
    """优先读取 Trace 物化后的 runtime token 指标，快照仅作兜底。"""
    run_ids = [run.id for run in runs if run and run.id]
    if not run_ids:
        return {}

    rows = (
        await session.execute(
            select(RunMetric).where(
                RunMetric.agent_run_id.in_(run_ids),
                RunMetric.source == "runtime",
                RunMetric.metric_key.in_(
                    ["runtime.tokens_llm", "runtime.tokens_tool"]
                ),
            )
        )
    ).scalars().all()

    runtime_values: dict[str, dict[str, int]] = {}
    for row in rows:
        if row.value_num is None:
            continue
        try:
            value = int(row.value_num)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        runtime_values.setdefault(row.agent_run_id, {})[row.metric_key] = value

    out: dict[str, tuple[Optional[int], Optional[int]]] = {}
    for run in runs:
        snap_llm, snap_tool = _runtime_token_breakdown(run)
        vals = runtime_values.get(run.id, {})
        out[run.id] = (
            vals.get("runtime.tokens_llm", snap_llm),
            vals.get("runtime.tokens_tool", snap_tool),
        )
    return out


def _first_int_from_dict(data: dict, keys: list[str]) -> Optional[int]:
    for key in keys:
        try:
            value = data.get(key)
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _runtime_token_breakdown(run: Optional[AgentRun]) -> tuple[Optional[int], Optional[int]]:
    """尽力从 Run 快照中提取 LLM / 工具 Token；未上报时返回 None。"""
    if not run:
        return None, None
    candidates = []
    for snap in (run.output_snapshot, run.input_snapshot):
        if isinstance(snap, dict):
            candidates.append(snap)
            for key in ("usage", "metrics", "metadata", "output", "result"):
                nested = snap.get(key)
                if isinstance(nested, dict):
                    candidates.append(nested)

    llm_keys = [
        "tokens_llm",
        "llm_tokens",
        "llmTokens",
        "model_tokens",
        "modelTokens",
    ]
    tool_keys = [
        "tokens_tool",
        "tool_tokens",
        "toolTokens",
        "tools_tokens",
        "toolsTokens",
    ]
    prompt_keys = ["prompt_tokens", "promptTokens", "input_tokens", "inputTokens"]
    completion_keys = [
        "completion_tokens",
        "completionTokens",
        "output_tokens",
        "outputTokens",
    ]

    llm_tokens: Optional[int] = None
    tool_tokens: Optional[int] = None
    from services.inference_metrics import normalize_reported_submetric

    for item in candidates:
        if llm_tokens is None:
            llm_tokens = _first_int_from_dict(item, llm_keys)
        if tool_tokens is None:
            tool_tokens = normalize_reported_submetric(
                _first_int_from_dict(item, tool_keys)
            )
        if llm_tokens is None:
            prompt = _first_int_from_dict(item, prompt_keys)
            completion = _first_int_from_dict(item, completion_keys)
            if prompt is not None or completion is not None:
                llm_tokens = (prompt or 0) + (completion or 0)
    return llm_tokens, tool_tokens


@app.get("/tasks/{task_id}/detail")
async def get_task_detail(
    task_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Get detailed evaluation results with round-by-round information."""
    # Get task
    result = await session.execute(
        select(EvaluationTask).where(EvaluationTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(404, "Task not found")

    from services.evaluation_mode import (
        get_evaluation_mode,
        get_task_capabilities,
        get_task_dimension_flags,
        is_offline_task,
        is_online_task,
    )
    from services.task_detail_inference_runs import build_inference_runs_by_question
    from services.task_agent_summary import build_task_agent_summary

    agent_summary = await build_task_agent_summary(session, task)
    primary_agent_id = (
        (agent_summary.get("agent_ids") or [None])[0] or task.agent_id
    )
    if not primary_agent_id:
        agent_display_name = "离线评估"
    else:
        agent_result = await session.execute(
            select(Agent).where(Agent.id == primary_agent_id)
        )
        agent = agent_result.scalar_one_or_none()
        agent_display_name = agent.name if agent else None
    
    # Get LLM model name for display
    from config import get_model_display_name
    llm_model_key = task.llm_model or "glm"
    llm_model_name = get_model_display_name(llm_model_key)
    
    # Get dataset info
    dataset_result = await session.execute(
        select(Dataset).where(Dataset.id == task.dataset_id)
    )
    dataset = dataset_result.scalar_one_or_none()
    dataset_name = dataset.name if dataset else None
    
    # Get all results for this task
    result = await session.execute(
        select(EvaluationResult).where(EvaluationResult.task_id == task_id)
    )
    results = result.scalars().all()

    # Batch-fetch all rounds for all results at once to avoid N+1 queries
    all_result_ids = [r.id for r in results]
    rounds_by_result: dict[str, list] = {}
    if all_result_ids:
        rounds_result = await session.execute(
            select(EvaluationRound)
            .where(EvaluationRound.result_id.in_(all_result_ids))
            .order_by(EvaluationRound.result_id, EvaluationRound.round_num)
        )
        for rd in rounds_result.scalars().all():
            rounds_by_result.setdefault(rd.result_id, []).append(rd)

    runs_result = await session.execute(
        select(AgentRun).where(AgentRun.task_id == task_id)
    )
    agent_runs = runs_result.scalars().all()
    # 使用与当前 session 相同的引擎创建并行查询 session（兼容测试的内存数据库）
    from sqlalchemy.ext.asyncio import async_sessionmaker as _asm
    _parallel_sf = _asm(session.bind, class_=AsyncSession, expire_on_commit=False)
    # 并行执行独立的聚合查询（各自使用独立 session，规避 AsyncSession 并发限制）
    async def _compute_fact_metrics():
        async with _parallel_sf() as s:
            return await aggregate_task_run_metrics(s, task_id)

    async def _compute_analytics_rows():
        async with _parallel_sf() as s:
            return await fetch_run_analytics_rows_for_task(s, task_id)

    fact_metrics, analytics_rows = await asyncio.gather(
        _compute_fact_metrics(),
        _compute_analytics_rows(),
    )
    runs_by_id: dict[str, AgentRun] = {ar.id: ar for ar in agent_runs}
    token_breakdown_by_run = await _load_runtime_token_breakdowns(session, agent_runs)
    trace_latency_by_run = await batch_load_trace_latency_ms(
        session, [ar.id for ar in agent_runs]
    )
    # 批量预取 agent.langfuse_project（仅 run.langfuse_project 为空时回退，避免 N+1）
    agent_ids_for_langfuse = {
        ar.agent_id for ar in agent_runs
        if ar.agent_id and not (ar.langfuse_project or "").strip()
    }
    agent_langfuse_project_map: dict[str, str] = {}
    if agent_ids_for_langfuse:
        _ag_res = await session.execute(
            select(Agent.id, Agent.langfuse_project).where(Agent.id.in_(agent_ids_for_langfuse))
        )
        for _aid, _lp in _ag_res.all():
            if _lp:
                agent_langfuse_project_map[_aid] = str(_lp).strip()

    # 预解析所有 run 涉及的 langfuse project_key → 真实 cxxx project_id
    # resolve_langfuse_project_id 首次调用会请求 Langfuse API 取真实 ID（cxxx 格式），
    # 之后永久缓存（进程级）。配置中的 langfuse_project_id 可能是人工填写的名称
    # （如 "197_project"），非 Langfuse 内部 ID，直接用于 URL 深链会打不开 trace。
    from services.langfuse_link_service import (
        resolve_langfuse_host,
        build_trace_urls_sync,
        resolve_langfuse_project_id,
    )
    from config import LANGFUSE_WRITE_PROJECT

    _all_project_keys: set = set()
    for ar in agent_runs:
        _pk = (ar.langfuse_project or "").strip() or None
        if not _pk:
            _pk = agent_langfuse_project_map.get(ar.agent_id)
        if not _pk:
            _pk = (LANGFUSE_WRITE_PROJECT or "").strip() or None
        _all_project_keys.add(_pk)
    _project_key_to_id: dict = {}
    for _pk in _all_project_keys:
        _project_key_to_id[_pk] = await resolve_langfuse_project_id(_pk)

    _run_link_cache: dict[str, dict] = {}
    def _build_run_links(run: AgentRun) -> dict:
        if run.id in _run_link_cache:
            return _run_link_cache[run.id]
        project_key = (run.langfuse_project or "").strip() or None
        if not project_key:
            project_key = (
                agent_langfuse_project_map.get(run.agent_id)
                or (LANGFUSE_WRITE_PROJECT or "").strip() or None
            )
        host = resolve_langfuse_host(project_key)
        langfuse_project_id = _project_key_to_id.get(project_key)
        trace_id = (run.trace_id or run.root_trace_id or "").strip()
        links = build_trace_urls_sync(
            trace_id=trace_id,
            langfuse_project_id=langfuse_project_id,
            host=host,
        )
        _run_link_cache[run.id] = links
        return links

    sync_summary = summarize_trace_sync_counts(agent_runs)
    sync_failure_categories = (
        await summarize_trace_sync_failure_categories(session, task_id)
        if is_online_task(task)
        else {}
    )
    inference_model_summary = summarize_inference_models_from_runs(agent_runs)
    execution_mode_summary = summarize_execution_modes_from_runs(agent_runs)
    fact_materialization_status = compute_fact_materialization_status(
        sync_summary,
        agent_run_count=len(agent_runs),
    )
    
    # 按 question_id 去重题目文本（同题在多指标下共享，避免 M 倍膨胀）
    questions_map: dict = {}
    # 按 agent_run_id 去重 agent_answer（同 run 在多指标下共享）
    agent_answers_map: dict = {}
    for r in results:
        qid = r.question_id
        if qid is not None and qid not in questions_map:
            questions_map[qid] = {"question": r.question, "ground_truth": r.ground_truth}
        if r.agent_run_id and r.agent_answer and r.agent_run_id not in agent_answers_map:
            agent_answers_map[r.agent_run_id] = r.agent_answer

    # Group results by metric
    metrics_data = {}
    for r in results:
        metric_name = r.metric_name
        if metric_name not in metrics_data:
            metrics_data[metric_name] = {
                "metric_name": metric_name,
                "threshold": r.threshold,
                "eval_method": getattr(r, 'eval_method', None) or ("script" if hasattr(r, 'script_name') and r.script_name else "llm"),
                "script_name": getattr(r, 'script_name', None),
                "results": []
            }
        
        # Get rounds for this result (from pre-fetched batch)
        rounds = rounds_by_result.get(r.id, [])

        run_row = (
            runs_by_id.get(r.agent_run_id) if r.agent_run_id else None
        )
        
        eval_method_row = getattr(r, "eval_method", None) or (
            "script" if getattr(r, "script_name", None) else "llm"
        )
        runtime_value = None
        resolved_latency = None
        if run_row:
            resolved_latency = resolve_agent_e2e_duration_ms(
                run_row,
                trace_latency_ms=trace_latency_by_run.get(run_row.id),
                stored_inference_duration_ms=r.inference_duration_ms,
            )
        elif r.inference_duration_ms is not None:
            resolved_latency = int(r.inference_duration_ms)
        if eval_method_row == "runtime":
            if metric_name == "inference_latency":
                runtime_value = resolved_latency
            elif metric_name == "tokens_total":
                runtime_value = run_row.tokens_total if run_row else None
        llm_tokens, tool_tokens = (
            token_breakdown_by_run.get(run_row.id, _runtime_token_breakdown(run_row))
            if run_row
            else (None, None)
        )

        _run_links = _build_run_links(run_row) if run_row else {}
        metrics_data[metric_name]["results"].append({
            "result_id": r.id,
            "question_id": r.question_id,
            "avg_score": r.avg_score,
            "passed": r.passed,
            "inference_duration_ms": resolved_latency,
            "agent_run_id": r.agent_run_id or (run_row.id if run_row else None),
            "run_index": run_row.run_index if run_row and run_row.run_index is not None else 0,
            "trace_sync_status": run_row.trace_sync_status if run_row else None,
            "trace_id": run_row.trace_id if run_row else None,
            "langfuse_host": _run_links.get("langfuse_host"),
            "langfuse_trace_url": _run_links.get("trace_url"),
            "langfuse_project_id": _run_links.get("langfuse_project_id"),
            "tokens_total": run_row.tokens_total if run_row else None,
            "tokens_llm": llm_tokens,
            "tokens_tool": tool_tokens,
            "runtime_value": runtime_value,
            "eval_method": eval_method_row,
            "rounds": [
                {
                    "round_num": round.round_num,
                    "score": round.score,
                    "reason": round.reason,
                    "duration_ms": round.duration_ms
                }
                for round in rounds
            ]
        })
    
    # Calculate metric summaries
    metrics_summary = []
    for metric_name, metric_data in metrics_data.items():
        all_results = metric_data["results"]
        # Phase 5: 区分「已跳过」(score=None) 和「已评分」(score=float)
        # 跳过项不参与 avg_score 和 pass_rate 统计(分母排除)
        scored_results = [r for r in all_results if r["avg_score"] is not None]
        skipped_count = len(all_results) - len(scored_results)
        scores = [r["avg_score"] for r in scored_results]
        passed_count = sum(1 for r in scored_results if r["passed"])
        scored_count = len(scored_results)
        total_count = len(all_results)

        eval_method = metric_data.get("eval_method", "llm")
        summary_entry = {
            "metric_name": metric_name,
            "avg_score": round(sum(scores) / len(scores), 3) if scores else None,
            "pass_rate": round(passed_count / scored_count * 100, 1) if scored_count > 0 else None,
            "passed_count": passed_count,
            "total_count": total_count,
            "skipped_count": skipped_count,
            "scored_count": scored_count,
            "threshold": metric_data["threshold"],
            "eval_method": eval_method,
            "script_name": metric_data.get("script_name"),
        }
        if eval_method == "runtime":
            summary_entry["runtime_target_source"] = _runtime_target_source(task, metric_name)
            raw_values = [
                r.get("runtime_value")
                for r in metric_data["results"]
                if r.get("runtime_value") is not None
            ]
            if metric_name == "inference_latency" and raw_values:
                summary_entry["runtime_avg_value"] = round(
                    sum(raw_values) / len(raw_values), 1
                )
                summary_entry["runtime_unit"] = "ms"
            elif metric_name == "tokens_total" and raw_values:
                summary_entry["runtime_avg_value"] = round(
                    sum(raw_values) / len(raw_values), 1
                )
                summary_entry["runtime_unit"] = "tokens"
            else:
                summary_entry["runtime_avg_value"] = None
                summary_entry["runtime_unit"] = None
        metrics_summary.append(summary_entry)
    
    # 并行执行独立的重量级查询（各自使用独立 session，规避 AsyncSession 并发限制）
    async def _compute_checkpoint_report():
        if is_offline_task(task):
            return None
        async with _parallel_sf() as s:
            return await build_task_checkpoint_report(s, task_id)

    async def _compute_quality_score():
        async with _parallel_sf() as s:
            return await compute_task_quality_score(s, task_id, results=results)

    checkpoint_report, quality_score = await asyncio.gather(
        _compute_checkpoint_report(),
        _compute_quality_score(),
    )
    # 传入已计算的 quality_score 和 task，避免 build_task_dimension_verdicts 内部重复计算
    # results 同样透传，避免子函数重复查询 EvaluationResult
    dimension_verdicts = await build_task_dimension_verdicts(
        session, task_id, task=task, quality_score=quality_score, results=results,
    )

    return {
        "task": {
            "id": task.id,
            "dataset_id": task.dataset_id,
            "dataset_name": dataset_name,
            "agent_id": task.agent_id,
            "evaluation_mode": get_evaluation_mode(task),
            "capabilities": get_task_capabilities(task),
            "dimension_flags": get_task_dimension_flags(task),
            "agent_name": agent_display_name,
            "agent_count": agent_summary.get("agent_count", 0),
            "agent_versions": agent_summary.get("agent_versions", []),
            "agent_versions_label": agent_summary.get("agent_versions_label"),
            "inference_model_summary": inference_model_summary,
            "execution_mode_summary": execution_mode_summary,
            "llm_model_name": llm_model_name,  # LLM模型显示名称
            "llm_model": task.llm_model,
            "run_count": task.run_count,
            "inference_rounds": max(1, int(getattr(task, "inference_rounds", None) or 1)),
            "eval_mode": (
                "stability"
                if max(1, int(getattr(task, "inference_rounds", None) or 1)) > 1
                else "standard"
            ),
            "created_at": format_datetime(task.created_at),
            "completed_at": format_datetime(task.completed_at),
            "status": task.status,
            "langfuse_source": getattr(task, "langfuse_source", None),
            "process_eval_completed": getattr(task, "process_eval_completed", False),
            "auto_scenario_score": getattr(task, "auto_scenario_score", True) is not False,
            "enable_process_consistency": getattr(task, "enable_process_consistency", True) is not False,
            "enable_stability": getattr(task, "enable_stability", True) is not False,
        },
        "metrics_summary": metrics_summary,
        "metrics_data": metrics_data,
        "questions": {str(k): v for k, v in questions_map.items()},
        "agent_answers": agent_answers_map,
        "sync_summary": sync_summary if is_online_task(task) else None,
        "sync_failure_categories": sync_failure_categories if is_online_task(task) else None,
        "fact_materialization_status": fact_materialization_status if is_online_task(task) else None,
        "agent_run_count": len(agent_runs) if is_online_task(task) else 0,
        "fact_metrics": fact_metrics if is_online_task(task) else [],
        "analytics_rows": analytics_rows if is_online_task(task) else [],
        "quality_score": quality_score,
        "dimension_verdicts": dimension_verdicts,
        "checkpoint_report": checkpoint_report,
        "inference_runs_by_question": (
            build_inference_runs_by_question(
                agent_runs,
                results,
                token_breakdown_fn=lambda run: token_breakdown_by_run.get(
                    run.id,
                    _runtime_token_breakdown(run),
                ),
                trace_latency_by_run=trace_latency_by_run,
            )
            if is_online_task(task)
            else {}
        ),
    }


@app.patch("/tasks/{task_id}/runtime-targets")
async def update_task_runtime_targets(
    task_id: str,
    request: RuntimeMetricTargetsUpdate,
    session: AsyncSession = Depends(get_session),
):
    """更新本任务运行时目标，并仅重算 runtime 指标效率分。"""
    task = await session.get(EvaluationTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    updates: dict[str, float] = {}
    if request.inference_latency_ms is not None:
        updates["inference_latency"] = float(request.inference_latency_ms)
    if request.tokens_total is not None:
        updates["tokens_total"] = float(request.tokens_total)
    if not updates:
        raise HTTPException(400, "请至少提供一个运行时目标")

    targets = _runtime_targets_from_task(task)
    targets.update(updates)
    task.runtime_metric_targets = json.dumps(targets, ensure_ascii=False)

    rows = (
        await session.execute(
            select(EvaluationResult).where(
                EvaluationResult.task_id == task_id,
                EvaluationResult.metric_name.in_(list(updates.keys())),
            )
        )
    ).scalars().all()
    run_ids = [r.agent_run_id for r in rows if r.agent_run_id]
    runs_by_id: dict[str, AgentRun] = {}
    if run_ids:
        runs = (
            await session.execute(select(AgentRun).where(AgentRun.id.in_(run_ids)))
        ).scalars().all()
        runs_by_id = {run.id: run for run in runs}

    from evaluators.runtime_metrics import build_runtime_metric_values, score_runtime_metric

    updated_count = 0
    for row in rows:
        threshold = updates.get(row.metric_name)
        if threshold is None:
            continue
        run = runs_by_id.get(row.agent_run_id or "")
        tokens_total = run.tokens_total if run else None
        latency_ms = None
        if row.metric_name == "inference_latency" and run:
            trace_lat = (await batch_load_trace_latency_ms(session, [run.id])).get(run.id)
            latency_ms = resolve_agent_e2e_duration_ms(
                run,
                trace_latency_ms=trace_lat,
                stored_inference_duration_ms=row.inference_duration_ms,
            )
        elif row.metric_name == "inference_latency":
            latency_ms = row.inference_duration_ms
        raw_value, detail = build_runtime_metric_values(
            row.metric_name,
            inference_duration_ms=latency_ms,
            tokens_total=tokens_total,
        )
        normalized, passed, _label = score_runtime_metric(
            row.metric_name, raw_value, threshold
        )
        row.threshold = threshold
        row.score = normalized
        row.avg_score = normalized
        row.passed = passed
        row.cot = f"[运行时指标] {detail}；本任务目标阈值 {threshold:g}"
        await sync_evaluation_result_to_run_metrics(session, row)
        updated_count += 1

    await session.commit()
    return {
        "task_id": task_id,
        "runtime_metric_targets": targets,
        "updated_count": updated_count,
        "message": "已更新本任务运行时目标，仅重算运行效率指标，不影响质量评测结果。",
    }


@app.get("/tasks/{task_id}/experiment-report", deprecated=True)
async def get_task_experiment_report(
    task_id: str,
    session: AsyncSession = Depends(get_session),
):
    """[已废弃] 任务内版本实验；请使用评估历史多选任务 → POST /api/scenario-eval/from-tasks。"""
    task = await session.get(EvaluationTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return await build_task_experiment_report(session, task_id)


@app.get("/tasks/{task_id}/performance-report")
async def get_task_performance_report(
    task_id: str,
    session: AsyncSession = Depends(get_session),
):
    """任务级运行效能报告（懒加载；读 run_metrics + run_span_nodes）。"""
    payload = await build_task_performance_report(session, task_id)
    if payload is None:
        raise HTTPException(404, "Task not found")
    return payload


@app.get("/tasks/{task_id}/process-consistency")
async def get_process_consistency(
    task_id: str,
    session: AsyncSession = Depends(get_session),
):
    """过程一致性总览：按题聚合必达过程点 + 推理链条稳定性。"""
    return await build_process_consistency(session, task_id)


@app.get("/tasks/{task_id}/span-tree-batch")
async def get_span_tree_batch(
    task_id: str,
    session: AsyncSession = Depends(get_session),
):
    """批量获取任务下所有 run 的轻量 span-tree（仅结构字段，不含 input/output）。"""
    from services.span_tree_service import build_span_tree_batch_for_task
    return await build_span_tree_batch_for_task(session, task_id)


@app.get("/tasks/{task_id}/dimension-verdicts")
async def get_task_dimension_verdicts(
    task_id: str,
    session: AsyncSession = Depends(get_session),
):
    """任务级评测四维结论（质量 / 过程一致性 / 场景交付 / 运行稳定性）。"""
    payload = await build_task_dimension_verdicts(session, task_id)
    if payload is None:
        raise HTTPException(404, "Task not found")
    return payload


@app.get("/tasks/{task_id}/verdict-thresholds")
async def get_task_verdict_thresholds(
    task_id: str,
    session: AsyncSession = Depends(get_session),
):
    """获取任务四维结论阈值（系统默认 + 本任务覆盖）。"""
    from services.verdict_threshold_service import threshold_meta

    task = await session.get(EvaluationTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return {"task_id": task_id, **threshold_meta(task)}


@app.patch("/tasks/{task_id}/verdict-thresholds")
async def update_task_verdict_thresholds(
    task_id: str,
    request: VerdictThresholdsUpdate,
    session: AsyncSession = Depends(get_session),
):
    """更新本任务四维结论阈值，并即时重算结论（不重跑 Agent/Judge）。"""
    from services.verdict_threshold_service import (
        resolve_verdict_thresholds,
        serialize_thresholds_for_storage,
        threshold_meta,
        validate_threshold_patch,
    )

    task = await session.get(EvaluationTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    patch = request.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(400, "请至少提供一个阈值字段")

    try:
        validated = validate_threshold_patch(patch)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    from services.verdict_threshold_service import VerdictThresholds

    defaults = VerdictThresholds.defaults().to_dict()
    current = resolve_verdict_thresholds(task).to_dict()
    current.update(validated)
    custom_only = {
        k: v for k, v in current.items() if abs(float(v) - float(defaults[k])) > 1e-9
    }
    task.verdict_thresholds = (
        serialize_thresholds_for_storage(custom_only) if custom_only else None
    )
    await session.commit()

    dimension_verdicts = await build_task_dimension_verdicts(session, task_id, task=task)
    return {
        "task_id": task_id,
        **threshold_meta(task),
        "dimension_verdicts": dimension_verdicts,
        "message": "已更新结论规则，四维结论已重算（未重跑评测）。",
    }


@app.get("/tasks/{task_id}/run-step-compare")
async def get_run_step_compare(
    task_id: str,
    left_run_id: str,
    right_run_id: str,
    session: AsyncSession = Depends(get_session),
):
    """同 Agent 同题多轮 Step 对齐对比与差异提炼。"""
    task = await session.get(EvaluationTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    payload = await build_run_step_compare(
        session,
        left_run_id=left_run_id,
        right_run_id=right_run_id,
        task_id=task_id,
    )
    if payload is None:
        raise HTTPException(404, "Run not found")
    return payload


@app.get("/tasks/{task_id}/runtime-target-suggestions")
async def get_runtime_target_suggestions(
    task_id: str,
    session: AsyncSession = Depends(get_session),
):
    """M-02：基于历史 Run 的 P75/P90 运行时目标建议（不自动覆盖任务目标）。"""
    suggestion = await suggest_runtime_targets(session, task_id)
    if suggestion is None:
        raise HTTPException(404, "Task not found")
    return suggestion


@app.get("/stream/{task_uuid}")
async def stream_results(task_uuid: str):
    """SSE endpoint for real-time CoT and results streaming."""
    async def event_generator():
        queue = await get_task_queue(task_uuid)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=60.0)
                yield {
                    "event": event["event"],
                    "data": json.dumps(event["data"])
                }
                if event["event"] in ["task_complete", "task_error"]:
                    break
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": "{}"}
    
    return EventSourceResponse(event_generator())


@app.post("/tasks/{task_id}/pause")
async def pause_task(
    task_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Pause an evaluation task."""
    log("INFO", "API", "任务暂停请求", task_id=task_id)

    result = await session.execute(
        select(EvaluationTask).where(EvaluationTask.id == task_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        log("ERROR", "API", "任务不存在", task_id=task_id)
        raise HTTPException(404, "Task not found")

    if task.status not in [TaskStatus.PENDING, TaskStatus.PROCESSING]:
        log("ERROR", "API", "任务状态不允许暂停", task_id=task_id, status=task.status)
        raise HTTPException(400, "Task is not running or already completed")

    if task.paused_at is not None:
        log("ERROR", "API", "任务已暂停", task_id=task_id)
        raise HTTPException(400, "Task is already paused")

    await session.execute(
        update(EvaluationTask)
        .where(EvaluationTask.id == task_id)
        .values(
            paused_at=datetime.utcnow(),
            status=TaskStatus.PAUSED
        )
    )
    await session.commit()
    log("INFO", "API", "任务暂停成功", task_id=task_id)

    return {"message": "Task paused successfully", "task_id": task_id}


@app.post("/tasks/{task_id}/resume")
async def resume_task(
    task_id: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session)
):
    """Resume a paused evaluation task."""
    log("INFO", "API", "任务恢复请求", task_id=task_id)

    result = await session.execute(
        select(EvaluationTask).where(EvaluationTask.id == task_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        log("ERROR", "API", "任务不存在", task_id=task_id)
        raise HTTPException(404, "Task not found")

    if task.status != TaskStatus.PAUSED:
        log("ERROR", "API", "任务状态不允许恢复", task_id=task_id, status=task.status)
        raise HTTPException(400, "Task is not paused")

    # Get resume position
    resume_from = task.resume_from or 0

    # Get use_bm25 and use_doc_library from task
    use_bm25 = task.use_bm25 if task.use_bm25 is not None else False
    use_doc_library = task.use_doc_library if task.use_doc_library is not None else False

    # Update task status
    await session.execute(
        update(EvaluationTask)
        .where(EvaluationTask.id == task_id)
        .values(
            paused_at=None,
            status=TaskStatus.PROCESSING
        )
    )
    await session.commit()
    log("INFO", "API", "任务恢复成功", task_id=task_id, resume_from=resume_from)

    # Launch background task to resume evaluation
    background_tasks.add_task(
        resume_evaluation_task, task_id, resume_from, use_bm25, use_doc_library
    )

    return {"message": "Task resumed successfully", "task_id": task_id}


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Cancel an evaluation task."""
    log("INFO", "API", "任务取消请求", task_id=task_id)
    result = await session.execute(
        select(EvaluationTask).where(EvaluationTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    
    if not task:
        log("ERROR", "API", "任务不存在", task_id=task_id)
        raise HTTPException(404, "Task not found")
    
    if task.status == TaskStatus.COMPLETED:
        log("ERROR", "API", "任务已完成，无法取消", task_id=task_id)
        raise HTTPException(400, "Task is already completed")

    if task.status == TaskStatus.CANCELLED:
        log("WARN", "API", "任务已取消，重复请求", task_id=task_id)
        return {"message": "Task already cancelled", "task_id": task_id}

    await session.execute(
        update(EvaluationTask)
        .where(EvaluationTask.id == task_id)
        .values(
            status=TaskStatus.CANCELLED,
            completed_at=datetime.utcnow()
        )
    )
    await session.commit()
    log("INFO", "API", "任务取消成功", task_id=task_id)

    # 通知前端 SSE 监听端：任务已取消（pipeline 在下一个检查点会读到
    # status==CANCELLED 并退出循环，不再启动后续轮次/题目；当前正在进行的
    # 一次同步 HTTP 推理会跑完，这是 onto-agent 同步接口的固有限制）。
    try:
        from tasks import emit_event
        await emit_event(task_id, "task_cancelled", {"task_id": task_id})
    except Exception as e:
        log("WARN", "API", "发送 task_cancelled 事件失败", task_id=task_id, error=str(e))

    return {"message": "Task cancelled successfully", "task_id": task_id}


@app.get("/datasets")
async def list_datasets(
    session: AsyncSession = Depends(get_read_session)
):
    """List all uploaded datasets."""
    result = await session.execute(
        select(Dataset)
        .order_by(Dataset.created_at.desc())
    )
    datasets = result.scalars().all()
    
    return {
        "datasets": [
            {
                "id": d.id,
                "name": d.name,
                "item_count": d.item_count,
                "created_at": format_datetime(d.created_at)
            }
            for d in datasets
        ]
    }


@app.get("/datasets/template.csv")
async def download_dataset_csv_template():
    """CSV 导入模板（UTF-8 BOM）。"""
    from services.dataset_io_service import csv_template_bytes

    return Response(
        content=csv_template_bytes(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="dataset_template.csv"'},
    )


@app.get("/datasets/template-scenario.csv")
async def download_dataset_csv_scenario_template():
    """场景 CSV 导入模板（含过程点列 + Step_Weight，UTF-8 BOM）。"""
    from services.dataset_io_service import csv_scenario_template_bytes

    return Response(
        content=csv_scenario_template_bytes(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="dataset_template_scenario.csv"',
        },
    )


@app.get("/datasets/template-scenario.xlsx")
async def download_dataset_xlsx_scenario_template():
    """场景 XLSX 导入模板（含过程点列，带格式和合并单元格）。"""
    from services.dataset_io_service import xlsx_scenario_template_bytes

    return Response(
        content=xlsx_scenario_template_bytes(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="dataset_template_scenario.xlsx"',
        },
    )


@app.get("/datasets/{dataset_id}/export")
async def export_dataset(
    dataset_id: str,
    format: str = Query("json", pattern="^(json|csv|xlsx)$"),
    session: AsyncSession = Depends(get_session),
):
    """导出评测集为 JSON / CSV / XLSX（只读，不写库）。"""
    from services.dataset_io_service import (
        content_disposition_attachment,
        export_dataset_csv_bytes,
        export_dataset_json_bytes,
        export_dataset_xlsx_bytes,
    )

    result = await session.execute(select(Dataset).where(Dataset.id == dataset_id))
    dataset = result.scalar_one_or_none()
    if not dataset:
        raise HTTPException(404, f"Dataset {dataset_id} not found")

    items = json.loads(dataset.data or "[]")
    if not isinstance(items, list):
        raise HTTPException(500, "Dataset data is corrupted")

    # 还原子步骤：批量加载 enabled 子步骤并挂到对应 item（导出为导入的逆操作）
    from services.dataset_item_scenario_service import load_steps_for_dataset_items

    steps_by_item = await load_steps_for_dataset_items(session, dataset_id)
    if steps_by_item:
        for item in items:
            if not isinstance(item, dict):
                continue
            iid = str(item.get("id") or item.get("dataset_item_id") or "")
            if iid and iid in steps_by_item:
                item["steps"] = steps_by_item[iid]

    if format == "csv":
        body = export_dataset_csv_bytes(items)
        filename = f"{dataset.name or dataset_id}.csv"
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": content_disposition_attachment(filename)},
        )

    if format == "xlsx":
        body = export_dataset_xlsx_bytes(items)
        filename = f"{dataset.name or dataset_id}.xlsx"
        return Response(
            content=body,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": content_disposition_attachment(filename)},
        )

    body = export_dataset_json_bytes(items)
    filename = f"{dataset.name or dataset_id}.json"
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": content_disposition_attachment(filename)},
    )


@app.get("/datasets/{dataset_id}")
async def get_dataset(
    dataset_id: str,
    offset: int = 0,
    limit: int = None,
    session: AsyncSession = Depends(get_session)
):
    """Get detailed content of a specific dataset with optional pagination."""
    result = await session.execute(
        select(Dataset).where(Dataset.id == dataset_id)
    )
    dataset = result.scalar_one_or_none()

    if not dataset:
        raise HTTPException(404, f"Dataset {dataset_id} not found")

    # 解析完整数据
    full_data = json.loads(dataset.data)

    # 如果指定了分页参数，则只返回部分数据
    if limit is not None:
        paginated_data = full_data[offset:offset + limit]
        return {
            "id": dataset.id,
            "name": dataset.name,
            "item_count": dataset.item_count,
            "created_at": format_datetime(dataset.created_at),
            "data": paginated_data,
            "total": len(full_data),
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < len(full_data)
        }

    # 否则返回完整数据
    return {
        "id": dataset.id,
        "name": dataset.name,
        "item_count": dataset.item_count,
        "created_at": format_datetime(dataset.created_at),
        "data": full_data
    }


@app.put("/datasets/{dataset_id}")
async def update_dataset(
    dataset_id: str,
    request: UpdateDatasetRequest,
    session: AsyncSession = Depends(get_session)
):
    """Update a dataset's data."""
    result = await session.execute(
        select(Dataset).where(Dataset.id == dataset_id)
    )
    dataset = result.scalar_one_or_none()
    
    if not dataset:
        raise HTTPException(404, f"Dataset {dataset_id} not found")
    
    data = request.data
    
    if not isinstance(data, list):
        raise HTTPException(400, "data must be a JSON array")
    
    old_name = dataset.name
    old_item_count = dataset.item_count
    
    dataset.data = json.dumps(data)
    dataset.item_count = len(data)
    
    # Update name if provided
    if request.name is not None:
        dataset.name = request.name
    
    log("INFO", "API", "数据集更新开始", 
        dataset_id=dataset_id, 
        old_name=old_name, 
        new_name=request.name, 
        old_item_count=old_item_count, 
        new_item_count=len(data))
    
    await session.commit()
    
    log("INFO", "API", "数据集更新完成", 
        dataset_id=dataset_id, 
        name=dataset.name, 
        item_count=dataset.item_count)
    
    return {
        "id": dataset.id,
        "name": dataset.name,
        "item_count": dataset.item_count,
        "created_at": format_datetime(dataset.created_at),
        "message": "Dataset updated successfully"
    }


@app.post("/datasets/{dataset_id}/copy")
async def copy_dataset(
    dataset_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Create a copy of an existing dataset with a new ID."""
    result = await session.execute(
        select(Dataset).where(Dataset.id == dataset_id)
    )
    dataset = result.scalar_one_or_none()
    
    if not dataset:
        raise HTTPException(404, f"Dataset {dataset_id} not found")
    
    new_id = f"dataset_{uuid.uuid4().hex}"
    new_name = f"{dataset.name} (复制)"
    
    log("INFO", "API", "复制数据集开始", 
        source_dataset_id=dataset_id, 
        source_name=dataset.name, 
        new_dataset_id=new_id, 
        new_name=new_name, 
        item_count=dataset.item_count)
    
    # 深拷贝 data 并为每个 item 重新生成 id，避免 scenario_key 冲突
    from services.dataset_items import ensure_dataset_item_id
    items = json.loads(dataset.data) if isinstance(dataset.data, str) else (dataset.data or [])
    old_to_new_item_id: Dict[str, str] = {}
    new_items = []
    for item in items:
        new_item = dict(item)
        old_id = new_item.get("id") or new_item.get("dataset_item_id") or ""
        new_item.pop("id", None)
        new_item.pop("dataset_item_id", None)
        ensure_dataset_item_id(new_item)
        if old_id:
            old_to_new_item_id[old_id] = new_item["id"]
        new_items.append(new_item)
    new_data = json.dumps(new_items, ensure_ascii=False)
    
    new_dataset = Dataset(
        id=new_id,
        name=new_name,
        data=new_data,
        item_count=dataset.item_count
    )
    
    session.add(new_dataset)
    await session.flush()
    
    # 复制子步骤模板（含 LLM 评测 prompt 等配置）
    from database import ScenarioEvalTemplate, ScenarioEvalStep
    source_tpls = (
        await session.execute(
            select(ScenarioEvalTemplate).where(
                ScenarioEvalTemplate.dataset_id == dataset_id,
                ScenarioEvalTemplate.scenario_key.like("ditem_%"),
            )
        )
    ).scalars().all()
    
    copied_templates = 0
    copied_steps = 0
    for tpl in source_tpls:
        old_item_id = tpl.dataset_item_id or ""
        new_item_id = old_to_new_item_id.get(old_item_id, old_item_id)
        new_scenario_key = f"ditem_{new_item_id}"
        new_tpl_id = f"scen_{uuid.uuid4().hex[:12]}"
        new_tpl = ScenarioEvalTemplate(
            id=new_tpl_id,
            scenario_key=new_scenario_key,
            name=tpl.name,
            description=tpl.description,
            ground_truth_ref=tpl.ground_truth_ref,
            dataset_id=new_id,
            dataset_item_id=new_item_id,
            question_match=tpl.question_match,
            enabled=tpl.enabled,
        )
        session.add(new_tpl)
        await session.flush()
        
        source_steps = (
            await session.execute(
                select(ScenarioEvalStep).where(
                    ScenarioEvalStep.template_id == tpl.id
                ).order_by(ScenarioEvalStep.step_order)
            )
        ).scalars().all()
        for s in source_steps:
            session.add(ScenarioEvalStep(
                id=f"step_{new_tpl_id}_{s.step_key}",
                template_id=new_tpl_id,
                step_key=s.step_key,
                name=s.name,
                step_order=s.step_order,
                weight=s.weight,
                required=s.required,
                enabled=s.enabled,
                enforce_order=s.enforce_order,
                gt_criteria=s.gt_criteria,
                gt_expected=s.gt_expected,
                match_rules=s.match_rules,
            ))
            copied_steps += 1
        copied_templates += 1
    
    await session.commit()
    
    log("INFO", "API", "复制数据集完成", 
        source_dataset_id=dataset_id, 
        new_dataset_id=new_id, 
        new_name=new_name,
        copied_templates=copied_templates,
        copied_steps=copied_steps)
    
    return {
        "id": new_id,
        "name": new_dataset.name,
        "item_count": dataset.item_count,
        "created_at": format_datetime(new_dataset.created_at),
        "message": "Dataset copied successfully",
        "source_dataset_id": dataset_id,
        "copied_templates": copied_templates,
        "copied_steps": copied_steps,
    }


@app.get("/langfuse/projects")
async def list_langfuse_projects():
    """列出 onto-eval 中已配置的 Langfuse 项目。"""
    from services.langfuse_project_registry import get_registry

    registry = get_registry()
    return {"projects": registry.list_projects_public()}


class LangfuseProjectCreateRequest(BaseModel):
    id: str = Field(..., description="内部项目标识")
    name: str = Field(..., description="项目显示名称")
    host: Optional[str] = Field(None, description="Langfuse 服务器地址，留空使用默认")
    organization: Optional[str] = Field(None, description="Langfuse 组织名")
    langfuse_project_id: Optional[str] = Field(None, description="Langfuse 实际项目 ID")
    public_key: str = Field(..., description="Langfuse Public Key")
    secret_key: str = Field(..., description="Langfuse Secret Key")


class LangfuseProjectUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, description="项目显示名称")
    host: Optional[str] = Field(None, description="Langfuse 服务器地址，留空使用默认")
    organization: Optional[str] = Field(None, description="Langfuse 组织名")
    langfuse_project_id: Optional[str] = Field(None, description="Langfuse 实际项目 ID")
    public_key: Optional[str] = Field(None, description="Langfuse Public Key")
    secret_key: Optional[str] = Field(None, description="Langfuse Secret Key")


@app.post("/langfuse/projects")
async def create_langfuse_project(request: LangfuseProjectCreateRequest):
    """新增 Langfuse 项目配置并持久化到 langfuse.projects.json。"""
    from services.langfuse_project_registry import (
        LangfuseProjectConfig,
        get_registry,
        reset_registry_for_tests,
        save_langfuse_project_configs,
    )

    # 自动生成 id
    pid = (request.id or "").strip()
    if not pid:
        org = (request.organization or "default").strip()
        proj = (request.langfuse_project_id or "project").strip()
        pid = f"{org}-{proj}".lower()
        pid = "".join(c if c.isalnum() or c in "_-" else "-" for c in pid)
        pid = "-".join(filter(None, pid.split("-")))
    if not pid or "/" in pid:
        raise HTTPException(400, "项目 id 不能为空且不能包含 '/' 等特殊字符")

    registry = get_registry()
    existing = registry.all_configs()
    for cfg in existing:
        if cfg.id == pid:
            raise HTTPException(409, f"项目 id '{pid}' 已存在")
        # 同一 org+project 组合不允许重复
        if (cfg.organization or "").strip() == (request.organization or "").strip() and \
           (cfg.langfuse_project_id or "").strip() == (request.langfuse_project_id or "").strip():
            raise HTTPException(409, f"组织 '{request.organization}' 下项目 '{request.langfuse_project_id}' 已存在")

    new_cfg = LangfuseProjectConfig(
        id=pid,
        name=request.name,
        public_key=request.public_key,
        secret_key=request.secret_key,
        host=request.host,
        organization=request.organization,
        langfuse_project_id=request.langfuse_project_id,
    )
    updated = existing + [new_cfg]
    try:
        save_langfuse_project_configs(updated)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))
    reset_registry_for_tests()

    log("INFO", "API", "Langfuse 项目已创建", id=pid, name=request.name)
    return {"id": pid, "name": request.name}


@app.put("/langfuse/projects/{project_id}")
async def update_langfuse_project(project_id: str, request: LangfuseProjectUpdateRequest):
    """更新 Langfuse 项目配置并持久化。"""
    from services.langfuse_project_registry import (
        LangfuseProjectConfig,
        get_registry,
        reset_registry_for_tests,
        save_langfuse_project_configs,
    )

    registry = get_registry()
    existing = registry.all_configs()
    found = False
    updated: list = []
    for cfg in existing:
        if cfg.id == project_id:
            found = True
            updated.append(LangfuseProjectConfig(
                id=cfg.id,
                name=request.name if request.name is not None else cfg.name,
                public_key=request.public_key if request.public_key is not None else cfg.public_key,
                secret_key=request.secret_key if request.secret_key is not None else cfg.secret_key,
                host=request.host if request.host is not None else cfg.host,
                organization=request.organization if request.organization is not None else cfg.organization,
                langfuse_project_id=request.langfuse_project_id if request.langfuse_project_id is not None else cfg.langfuse_project_id,
            ))
        else:
            updated.append(cfg)

    if not found:
        raise HTTPException(404, f"项目 '{project_id}' 不存在")

    try:
        save_langfuse_project_configs(updated)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))
    reset_registry_for_tests()

    log("INFO", "API", "Langfuse 项目已更新", id=project_id)
    return {"id": project_id}


@app.delete("/langfuse/projects/{project_id}")
async def delete_langfuse_project(project_id: str):
    """删除 Langfuse 项目配置并持久化。"""
    from services.langfuse_project_registry import (
        get_registry,
        reset_registry_for_tests,
        save_langfuse_project_configs,
    )

    registry = get_registry()
    existing = registry.all_configs()
    updated = [cfg for cfg in existing if cfg.id != project_id]

    if len(updated) == len(existing):
        raise HTTPException(404, f"项目 '{project_id}' 不存在")

    try:
        save_langfuse_project_configs(updated)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))
    reset_registry_for_tests()

    log("INFO", "API", "Langfuse 项目已删除", id=project_id)
    return {"id": project_id}


@app.get("/langfuse/projects/config-file-path")
async def get_langfuse_config_file_path():
    """获取 langfuse.projects.json 文件路径（用于诊断）。"""
    from services.langfuse_project_registry import _resolve_projects_file_path

    path = _resolve_projects_file_path()
    return {"path": path, "exists": os.path.isfile(path) if path else False}


def _extract_langfuse_question(items: list) -> str:
    for ri in reversed(items):
        inp = ri.get("input") or {}
        q = (
            inp.get("user_query")
            or inp.get("question")
            or inp.get("query")
            or inp.get("resource")
            or ""
        )
        if isinstance(q, str) and q.strip() and not q.strip().startswith("查询") and not q.strip().startswith("让我") and not q.strip().startswith("找到了"):
            return q.strip()
    for ri in items:
        inp = ri.get("input") or {}
        q = (
            inp.get("user_query")
            or inp.get("question")
            or inp.get("query")
            or inp.get("resource")
            or ""
        )
        if isinstance(q, str) and q.strip():
            return q.strip()
    return ""


def _map_langfuse_dataset_to_eval(raw_items: list, source_dataset: str = "") -> list:
    """将 Langfuse 数据集展开为 item 列表（用于预览展示），每个原始 item 一行。

    合并时（merge）会按 _langfuse_source 折叠回 1 条 eval question。
    """
    if not raw_items:
        return [{
            "question": "",
            "ground_truth": "",
            "model_answer": "",
            "context": "",
            "scenario_key": "",
            "_langfuse_source": source_dataset,
            "_mapping_warnings": ["数据集无有效条目"],
            "_steps": [],
        }]

    # 全局 question（从最后一个非工具调用的 item 提取）
    global_question = _extract_langfuse_question(raw_items)

    results = []
    for i, ri in enumerate(raw_items):
        exp = ri.get("expectedOutput") or {}
        inp = ri.get("input") or {}

        # 提取 output / expectedOutput
        answer = (
            exp.get("answer")
            or exp.get("ground_truth")
            or exp.get("expected_output")
            or ""
        )
        if isinstance(answer, dict):
            answer = json.dumps(answer, ensure_ascii=False)
        if not isinstance(answer, str):
            answer = str(answer) if answer is not None else ""

        # 当前 item 的 question（优先从 input 提取）
        item_question = (
            inp.get("user_query")
            or inp.get("question")
            or inp.get("query")
            or inp.get("resource")
            or ""
        )
        if not isinstance(item_question, str):
            item_question = str(item_question) if item_question is not None else ""
        if not item_question.strip():
            item_question = global_question

        # model_answer: 仅包含该 item 的 output，带步骤标记（支持场景子步骤提炼），不含 input
        model_answer = f"步骤{i + 1}:\n{answer.strip()}" if answer.strip() else ""

        step = {
            "order": i + 1,
            "input": item_question.strip(),   # 当前步骤的输入（用户提问）
            "desc": "",  # 不再拼入 input 内容
            "answer": answer.strip(),
        }

        warnings = []
        if not item_question.strip():
            warnings.append("question 为空，请检查 Langfuse 数据集 input 字段")
        if not answer.strip():
            warnings.append("expectedOutput 为空")

        item = {
            "question": item_question.strip(),
            "ground_truth": answer.strip(),     # 每个 item 的标准答案即其 own output
            "model_answer": model_answer,
            "context": "",
            "scenario_key": "",
            "_steps": [step] if answer.strip() else [],
        }
        if source_dataset:
            item["_langfuse_source"] = source_dataset
        item["_mapping_warnings"] = warnings
        results.append(item)

    return results


@app.get("/datasets/langfuse/list")
async def list_langfuse_datasets(
    project_id: Optional[str] = None,
):
    """列出指定 Langfuse 项目中的数据集名称列表。"""
    from services.langfuse_client import get_langfuse_client

    client = get_langfuse_client(project_id)
    try:
        resp = await client.list_datasets(limit=100)
    except Exception as e:
        log("ERROR", "API", "Langfuse 数据集列表获取失败", error=str(e))
        raise HTTPException(502, f"无法获取 Langfuse 数据集列表: {e}")

    datasets = resp.get("data") or []
    items = []
    for d in datasets:
        if not d:
            continue
        name = d.get("name")
        id_ = d.get("id")
        item_count = len(d.get("items") or [])
        if name:
            items.append({"name": name, "id": id_, "item_count": item_count, "can_import": True})
        elif id_:
            items.append({"name": id_, "id": id_, "item_count": item_count, "no_name": True, "can_import": False})
    return {"datasets": items, "count": len(items)}


@app.post("/datasets/import/langfuse")
async def import_dataset_from_langfuse(
    request: dict,
    session: AsyncSession = Depends(get_session),
):
    """从 Langfuse 导入数据集为 onto-eval 评测集。"""
    from services.langfuse_client import get_langfuse_client
    from services.dataset_io_service import default_dataset_name, ensure_unique_dataset_name

    dataset_name = str(request.get("dataset_name", "")).strip()
    project_id = str(request.get("project_id") or "").strip() or None
    limit = min(int(request.get("limit", 100)), 100)

    if not dataset_name:
        raise HTTPException(400, "dataset_name is required")

    client = get_langfuse_client(project_id)

    try:
        items_resp = await client.get_dataset_items(dataset_name, limit=limit)
    except Exception as e:
        from services.langfuse_errors import LangfuseApiError
        log("ERROR", "API", "Langfuse 数据集读取失败", dataset_name=dataset_name, error=str(e))
        if isinstance(e, LangfuseApiError):
            raise HTTPException(e.http_status, f"无法读取 Langfuse 数据集: {e.message}")
        raise HTTPException(502, f"无法读取 Langfuse 数据集: {e}")

    raw_items = items_resp.get("data") or []
    mapped = _map_langfuse_dataset_to_eval(raw_items, source_dataset=dataset_name)
    mapped.pop("_mapping_warnings", None)
    onto_items = [mapped]

    if not onto_items:
        raise HTTPException(400, f"Langfuse 数据集 '{dataset_name}' 无有效条目")

    dataset_id = f"ds_{uuid.uuid4().hex}"
    final_name = default_dataset_name(None, f"langfuse_{dataset_name}.json", dataset_id)
    final_name = await ensure_unique_dataset_name(session, final_name)

    dataset = Dataset(
        id=dataset_id,
        name=final_name,
        data=json.dumps(onto_items, ensure_ascii=False),
        item_count=len(onto_items),
    )
    session.add(dataset)
    await session.commit()

    log("INFO", "API", "Langfuse 数据集导入完成",
        dataset_id=dataset_id, name=final_name, item_count=len(onto_items),
        source=dataset_name)

    return {
        "dataset_id": dataset_id,
        "name": final_name,
        "item_count": len(onto_items),
        "source": dataset_name,
    }


@app.post("/datasets/langfuse/preview")
async def preview_langfuse_datasets(
    request: dict,
):
    """预览多个 Langfuse 数据集映射为 eval 评测项的结果。"""
    from services.langfuse_client import get_langfuse_client

    project_id = str(request.get("project_id") or "").strip() or None
    dataset_names = request.get("dataset_names") or []

    if not dataset_names or not isinstance(dataset_names, list):
        raise HTTPException(400, "dataset_names is required and must be a list")

    dataset_names = [str(n).strip() for n in dataset_names if str(n).strip()]
    if not dataset_names:
        raise HTTPException(400, "dataset_names cannot be empty")

    client = get_langfuse_client(project_id)
    all_items = []
    global_warnings = []

    for ds_name in dataset_names:
        try:
            items_resp = await client.get_dataset_items(ds_name, limit=100)
        except Exception as e:
            from services.langfuse_errors import LangfuseApiError
            log("ERROR", "API", "Langfuse 数据集预览读取失败", dataset_name=ds_name, error=str(e))
            if isinstance(e, LangfuseApiError):
                raise HTTPException(e.http_status, f"无法读取 Langfuse 数据集 '{ds_name}': {e.message}")
            raise HTTPException(502, f"无法读取 Langfuse 数据集 '{ds_name}': {e}")

        raw_items = items_resp.get("data") or []
        if not raw_items:
            global_warnings.append(f"数据集 '{ds_name}' 无有效条目，已跳过")
            continue

        mapped_list = _map_langfuse_dataset_to_eval(raw_items, source_dataset=ds_name)
        all_items.extend(mapped_list)

    empty_question_count = sum(1 for it in all_items if not it.get("question", "").strip())
    empty_gt_count = sum(1 for it in all_items if not it.get("ground_truth", "").strip())
    empty_ma_count = sum(1 for it in all_items if not it.get("model_answer", "").strip())
    if empty_question_count:
        global_warnings.append(f"{empty_question_count} 条数据的 question 为空")
    if empty_gt_count:
        global_warnings.append(f"{empty_gt_count} 条数据的 ground_truth 为空")
    if empty_ma_count:
        global_warnings.append(f"{empty_ma_count} 条数据的 model_answer 为空，将无法提炼场景子步骤")

    return {
        "items": all_items,
        "total": len(all_items),
        "warnings": global_warnings,
    }


@app.post("/datasets/langfuse/merge")
async def merge_langfuse_datasets(
    request: dict,
    session: AsyncSession = Depends(get_session),
):
    """将预览后的 Langfuse 数据集合并为 eval 评测集。

    按 _langfuse_source 分组：每个 Langfuse dataset（含 N 个 items）折叠为 1 条 eval question，
    model_answer 包含带步骤标记的 input + output，以支撑场景子步骤提炼。
    """
    from services.dataset_io_service import default_dataset_name, ensure_unique_dataset_name

    name = str(request.get("name") or "").strip()
    items = request.get("items") or []

    if name:
        existing = (await session.execute(
            select(Dataset).where(Dataset.name == name)
        )).scalar_one_or_none()
        if existing:
            raise HTTPException(409, f"评测集名称 '{name}' 已存在，请修改名称后重试")

    if not items or not isinstance(items, list):
        raise HTTPException(400, "items is required and must be a non-empty list")

    if not name:
        name = None

    # ====== 按 _langfuse_source 分组，每个 Langfuse dataset 折叠为 1 条 eval question ======
    groups: dict[str, list] = {}
    for it in items:
        src = str(it.get("_langfuse_source") or "").strip() or "_unknown"
        groups.setdefault(src, []).append(it)

    clean_items = []
    for src, group_items in groups.items():
        # 收集该数据集下所有 items 的步骤（保持 order 排序）
        all_steps: list[dict] = []
        for it in group_items:
            steps = it.get("_steps")
            if steps and isinstance(steps, list):
                all_steps.extend(steps)

        # 按 order 排序
        all_steps.sort(key=lambda s: s.get("order") or 0)

        # 构建 model_answer：包含每个步骤的 input + output，带步骤标记
        # 格式：步骤1： input1\n输出：output1\n\n步骤2： input2\n输出：output2
        step_parts = []
        for s in all_steps:
            inp = str(s.get("input") or "").strip()
            answer = str(s.get("answer") or "").strip()
            if not answer:
                continue
            order = s.get("order") or 0
            # 每行内容：输出标注；输入合并到标题行
            block_lines = []
            block_lines.append(f"输出：{answer}")
            block = "\n".join(block_lines)
            if order:
                if inp:
                    step_parts.append(f"步骤{order}： {inp}\n{block}")
                else:
                    step_parts.append(f"步骤{order}：\n{block}")
            else:
                step_parts.append(block)
        model_answer = "\n\n".join(step_parts)

        # 取第一个有实质内容的 item 的 question 作为整体 question
        question = ""
        for it in group_items:
            q = str(it.get("question") or "").strip()
            if q:
                question = q
                break

        # ground_truth 取所有步骤的 output 拼接（或最后一步的输出）
        ground_truth_parts = [str(s.get("answer") or "").strip() for s in all_steps if str(s.get("answer") or "").strip()]
        ground_truth = "\n\n".join(ground_truth_parts) if ground_truth_parts else ""

        clean = {
            "question": question,
            "ground_truth": ground_truth,
            "model_answer": model_answer,
            "context": "",
            "scenario_key": "",
            "_langfuse_source": src if src != "_unknown" else "",
        }
        clean_items.append(clean)

    if not clean_items:
        raise HTTPException(400, "无有效评测条目")

    # 从来源数据集名称构造默认评测集名称，避免统一用 "langfuse_merged" 导致重复
    sources = list(dict.fromkeys(
        it.get("_langfuse_source", "").strip()
        for it in clean_items
        if it.get("_langfuse_source", "").strip()
    ))
    default_filename = "langfuse_" + "_".join(sources) if sources else "langfuse_merged"

    dataset_id = f"ds_{uuid.uuid4().hex}"
    final_name = default_dataset_name(name, default_filename, dataset_id)
    if name is None:
        final_name = await ensure_unique_dataset_name(session, final_name)

    dataset = Dataset(
        id=dataset_id,
        name=final_name,
        data=json.dumps(clean_items, ensure_ascii=False),
        item_count=len(clean_items),
    )
    session.add(dataset)
    await session.commit()
    log("INFO", "API", "Langfuse 数据集合并导入完成",
        dataset_id=dataset_id, name=final_name, item_count=len(clean_items),
        sources=sources)

    return {
        "dataset_id": dataset_id,
        "name": final_name,
        "item_count": len(clean_items),
        "sources": sources,
    }


@app.delete("/datasets/batch")
async def delete_datasets_batch(
    request: dict,
    session: AsyncSession = Depends(get_session)
):
    """Delete multiple datasets."""
    dataset_ids = request.get("dataset_ids", [])
    
    log("INFO", "API", "批量删除数据集开始", dataset_ids=dataset_ids)
    
    if not isinstance(dataset_ids, list):
        raise HTTPException(400, "dataset_ids must be a list")
    
    if not dataset_ids:
        raise HTTPException(400, "dataset_ids cannot be empty")
    
    result = await session.execute(
        select(Dataset).where(Dataset.id.in_(dataset_ids))
    )
    datasets = result.scalars().all()
    
    dataset_names = [ds.name for ds in datasets]
    
    for dataset in datasets:
        await session.delete(dataset)
    
    await session.commit()
    
    log("INFO", "API", "批量删除数据集完成", 
        deleted_count=len(datasets), 
        dataset_ids=dataset_ids, 
        dataset_names=dataset_names)
    
    return {
        "message": f"Successfully deleted {len(datasets)} datasets"
    }


@app.delete("/datasets/{dataset_id}")
async def delete_dataset(
    dataset_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Delete a dataset."""
    result = await session.execute(
        select(Dataset).where(Dataset.id == dataset_id)
    )
    dataset = result.scalar_one_or_none()
    
    if not dataset:
        raise HTTPException(404, f"Dataset {dataset_id} not found")
    
    log("INFO", "API", "删除数据集开始", 
        dataset_id=dataset_id, 
        dataset_name=dataset.name, 
        item_count=dataset.item_count)
    
    await session.delete(dataset)
    await session.commit()
    
    log("INFO", "API", "删除数据集完成", 
        dataset_id=dataset_id, 
        dataset_name=dataset.name)
    
    return {"message": f"Dataset {dataset_id} deleted successfully"}


def _normalize_langfuse_project(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    p = str(raw).strip()
    return p or None


def _validate_langfuse_project_id(project_id: Optional[str]) -> None:
    if not project_id:
        return
    from services.langfuse_project_registry import ProjectNotFoundError, get_registry

    try:
        get_registry().get_config(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(400, str(e)) from e


async def _resolve_langfuse_project_for_agent(
    *,
    explicit: Optional[str],
    inference_config: Optional[Any],
    parent_langfuse: Optional[str] = None,
) -> Optional[str]:
    """
    解析 Agent 绑定的 Langfuse 项目：
    1. 显式指定（非空）优先
    2. 从 onto-agent observability 按 public_key 自动匹配
    3. 继承父版本
    未配置 Langfuse 时返回 None，不阻断注册。
    """
    explicit_norm = _normalize_langfuse_project(explicit)
    if explicit_norm:
        # onto-agent 上报过来的"幻影"项目名（历史上是 df-agent-project）按
        # registry 的别名规则映射到真实项目，避免新 agent 落进不存在的项目。
        try:
            from services.langfuse_project_registry import get_registry
            return get_registry().resolve_project_id(explicit_norm)
        except Exception:
            _validate_langfuse_project_id(explicit_norm)
            return explicit_norm

    if inference_config is not None:
        cfg = (
            inference_config.model_dump()
            if hasattr(inference_config, "model_dump")
            else inference_config
        )
        if isinstance(cfg, dict) and (cfg.get("url") or "").strip():
            from services.agent_probe_service import resolve_langfuse_project_from_inference_config

            resolved = await resolve_langfuse_project_from_inference_config(cfg)
            matched = (resolved.get("langfuse_project") or "").strip() or None
            if matched:
                return matched

    parent_norm = _normalize_langfuse_project(parent_langfuse)
    if parent_norm:
        return parent_norm

    return None


def _derive_agent_kind(inference_config: Any) -> str:
    """endpoint 含 /agentic → single（onto-agent），否则 → workflow。"""
    endpoint = (getattr(inference_config, "endpoint", "") or "").lower()
    url = (getattr(inference_config, "url", "") or "").lower()
    if "/agentic" in endpoint or "/agentic" in url:
        return "single"
    return "workflow"


def agent_to_response(
    agent: Agent,
    *,
    checkpoint_seed: Optional[Dict[str, Any]] = None,
) -> AgentResponse:
    from schemas import CheckpointSeedSummary

    seed = None
    if checkpoint_seed is not None:
        seed = CheckpointSeedSummary(**checkpoint_seed)
    return AgentResponse(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        inference_config=json.loads(agent.inference_config) if agent.inference_config else None,
        logical_agent_id=agent.logical_agent_id,
        version=agent.version,
        version_tag=agent.version_tag,
        langfuse_project=agent.langfuse_project,
        created_at=format_datetime(agent.created_at),
        is_active=agent.is_active,
        agent_kind=agent.agent_kind or "single",
        checkpoint_seed=seed,
    )


async def _ensure_agent_version_unique(
    session: AsyncSession,
    logical_agent_id: Optional[str],
    version: Optional[str],
    exclude_agent_id: Optional[str] = None,
) -> None:
    """同一 logical_agent_id 下 version 不可重复（均非空时）。"""
    lid = (logical_agent_id or "").strip()
    ver = (version or "").strip()
    if not lid or not ver:
        return
    result = await session.execute(
        select(Agent).where(
            Agent.logical_agent_id == lid,
            Agent.version == ver,
        )
    )
    for row in result.scalars().all():
        if exclude_agent_id and row.id == exclude_agent_id:
            continue
        raise HTTPException(
            409,
            f"逻辑 Agent「{lid}」下已存在版本「{ver}」（记录 {row.id}）。请使用「注册新版本」新增记录，勿改旧记录版本号。",
        )


async def _ensure_inference_config_probe(
    inference_config: Optional[Any],
    skip_probe: bool,
) -> None:
    """注册/更新前 sync 健康预检（H-03）。"""
    if skip_probe or not inference_config:
        return
    cfg = inference_config.model_dump() if hasattr(inference_config, "model_dump") else inference_config
    if not (cfg.get("url") or "").strip():
        return
    from services.agent_probe_service import probe_agent_inference_config

    probe = await probe_agent_inference_config(cfg)
    if not probe.get("ok"):
        raise HTTPException(
            400,
            detail={
                "message": "Agent 推理配置预检未通过，请修正后重试或设置 skip_probe=true 跳过",
                "probe": probe,
            },
        )


@app.post("/agents", response_model=AgentResponse)
async def register_agent(
    request: AgentRegisterRequest,
    session: AsyncSession = Depends(get_session)
):
    """Register a new AI agent（每条记录对应一个可评测版本，保留历史请新增而非改版本号）。"""
    from services.agent_identity import (
        ensure_agent_name_unique_for_logical,
        resolve_logical_agent_id_for_register,
    )

    resolved_lid, parent_row = await resolve_logical_agent_id_for_register(
        session,
        logical_agent_id=request.logical_agent_id,
        parent_agent_id=request.parent_agent_id,
    )
    if parent_row:
        await session.flush()

    await ensure_agent_name_unique_for_logical(
        session,
        request.name,
        resolved_lid or None,
        parent_agent_id=request.parent_agent_id,
    )
    await _ensure_agent_version_unique(
        session, resolved_lid or None, request.version
    )
    await _ensure_inference_config_probe(request.inference_config, request.skip_probe)
    agent_id = f"agent_{uuid.uuid4().hex}"
    
    log("INFO", "API", "注册Agent开始", 
        agent_id=agent_id, 
        name=request.name, 
        description=request.description, 
        has_inference_config=request.inference_config is not None)
    
    inference_config_str = None
    if request.inference_config:
        inference_config_str = json.dumps(request.inference_config.model_dump())

    langfuse_project = await _resolve_langfuse_project_for_agent(
        explicit=request.langfuse_project,
        inference_config=request.inference_config,
        parent_langfuse=parent_row.langfuse_project if parent_row else None,
    )

    agent = Agent(
        id=agent_id,
        name=request.name,
        description=request.description,
        inference_config=inference_config_str,
        logical_agent_id=(resolved_lid or None),
        version=request.version,
        version_tag=request.version_tag,
        langfuse_project=langfuse_project,
        agent_kind=_derive_agent_kind(request.inference_config),
    )
    session.add(agent)
    await session.flush()

    from services.checkpoint_seed_service import seed_onto_agent_baseline_checkpoints

    checkpoint_seed = await seed_onto_agent_baseline_checkpoints(
        session,
        agent,
        enabled=request.seed_default_checkpoints,
    )
    await session.commit()
    
    log("INFO", "API", "注册Agent完成", 
        agent_id=agent_id, 
        name=agent.name, 
        description=agent.description,
        checkpoint_seed=checkpoint_seed)
    
    return agent_to_response(agent, checkpoint_seed=checkpoint_seed)


@app.get("/agents", response_model=List[AgentResponse])
async def list_agents(
    logical_agent_id: Optional[str] = Query(None, description="按逻辑 Agent ID 精确筛选"),
    session: AsyncSession = Depends(get_session),
):
    """List all registered agents（每条记录 = 一个版本实例）。"""
    q = select(Agent)
    if logical_agent_id:
        q = q.where(Agent.logical_agent_id == logical_agent_id.strip())
    q = q.order_by(Agent.logical_agent_id, Agent.version, Agent.created_at.desc())
    result = await session.execute(q)
    agents = result.scalars().all()

    return [agent_to_response(agent) for agent in agents]


@app.get("/agents/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Get agent details."""
    result = await session.execute(
        select(Agent).where(Agent.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    
    if not agent:
        raise HTTPException(404, f"Agent {agent_id} not found")
    
    return agent_to_response(agent)


@app.put("/agents/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: str,
    request: AgentUpdateRequest,
    session: AsyncSession = Depends(get_session)
):
    """Update an existing agent."""
    result = await session.execute(
        select(Agent).where(Agent.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    
    if not agent:
        raise HTTPException(404, f"Agent {agent_id} not found")
    
    old_name = agent.name
    old_description = agent.description
    old_is_active = agent.is_active
    old_has_config = agent.inference_config is not None
    
    log("INFO", "API", "更新Agent开始", 
        agent_id=agent_id, 
        old_name=old_name, 
        new_name=request.name, 
        old_description=old_description, 
        new_description=request.description,
        old_is_active=old_is_active,
        new_is_active=request.is_active,
        has_inference_config=request.inference_config is not None)
    
    if request.name is not None:
        from services.agent_identity import ensure_agent_name_unique_for_logical

        new_name = (request.name or "").strip()
        if not new_name:
            raise HTTPException(400, "Agent 名称不能为空")
        old_name = (agent.name or "").strip()
        if new_name != old_name:
            await ensure_agent_name_unique_for_logical(
                session, new_name, agent.logical_agent_id, exclude_agent_id=agent_id
            )
            lid = (agent.logical_agent_id or "").strip()
            if lid:
                siblings = await session.execute(
                    select(Agent).where(Agent.logical_agent_id == lid)
                )
                for row in siblings.scalars().all():
                    row.name = new_name
            else:
                agent.name = new_name
    if request.description is not None:
        agent.description = request.description
    if request.is_active is not None:
        agent.is_active = request.is_active
    if request.inference_config is not None:
        await _ensure_inference_config_probe(request.inference_config, request.skip_probe)
        agent.inference_config = json.dumps(request.inference_config.model_dump())
        agent.agent_kind = _derive_agent_kind(request.inference_config)
    if request.logical_agent_id is not None:
        new_lid = (request.logical_agent_id or "").strip()
        old_lid = (agent.logical_agent_id or "").strip()
        if new_lid != old_lid:
            raise HTTPException(
                400,
                "逻辑 Agent ID 创建后不可修改；请使用「注册新版本」添加其他版本。",
            )
    if request.version is not None:
        new_ver = (request.version or "").strip()
        old_ver = (agent.version or "").strip()
        if new_ver != old_ver:
            raise HTTPException(
                400,
                "版本号创建后不可修改；请使用「注册新版本」保留旧版并新增记录。",
            )
    if request.version_tag is not None:
        agent.version_tag = request.version_tag

    should_refresh_langfuse = (
        request.langfuse_project is not None or request.inference_config is not None
    )
    if should_refresh_langfuse:
        infer_cfg: Optional[Any] = request.inference_config
        if infer_cfg is None and agent.inference_config:
            infer_cfg = json.loads(agent.inference_config)
        langfuse_project = await _resolve_langfuse_project_for_agent(
            explicit=request.langfuse_project,
            inference_config=infer_cfg,
            parent_langfuse=None,
        )
        lid = (agent.logical_agent_id or "").strip()
        if lid:
            siblings = await session.execute(
                select(Agent).where(Agent.logical_agent_id == lid)
            )
            for row in siblings.scalars().all():
                row.langfuse_project = langfuse_project
        else:
            agent.langfuse_project = langfuse_project

    await session.commit()
    
    log("INFO", "API", "更新Agent完成", 
        agent_id=agent_id, 
        name=agent.name, 
        description=agent.description, 
        is_active=agent.is_active)
    
    return agent_to_response(agent)


@app.delete("/agents/{agent_id}")
async def delete_agent(
    agent_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Delete an agent."""
    result = await session.execute(
        select(Agent).where(Agent.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    
    if not agent:
        raise HTTPException(404, f"Agent {agent_id} not found")
    
    log("INFO", "API", "删除Agent开始", 
        agent_id=agent_id, 
        agent_name=agent.name, 
        agent_description=agent.description, 
        has_inference_config=agent.inference_config is not None)
    
    await session.delete(agent)
    await session.commit()
    
    log("INFO", "API", "删除Agent完成", 
        agent_id=agent_id, 
        agent_name=agent.name)
    
    return {"message": f"Agent {agent_id} deleted successfully"}


def format_datetime(dt):
    """格式化日期时间，添加UTC时区信息"""
    if dt is None:
        return None
    # 确保时间有时区信息（UTC）
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


@app.get("/tasks")
async def list_tasks(
    page: int = 1,
    limit: int = 20,
    task_id: Optional[str] = Query(None),
    dataset_name: Optional[str] = Query(None),
    agent_name: Optional[str] = Query(None),
    agent_version: Optional[str] = Query(None),
    inference_model: Optional[str] = Query(None),
    agent_workspace: Optional[str] = Query(None),
    eval_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    min_total_score: Optional[float] = Query(None),
    min_result_score: Optional[float] = Query(None),
    min_process_score: Optional[float] = Query(None),
    session: AsyncSession = Depends(get_session)
):
    """List historical tasks with pagination and server-side filtering."""
    # 构建 SQL 查询（仅 where 条件，不含 order_by/offset/limit）
    query = select(EvaluationTask)

    if task_id:
        query = query.where(EvaluationTask.id.ilike(f"%{task_id}%"))
    if status:
        query = query.where(EvaluationTask.status == status)
    if eval_type == "online":
        query = query.where(EvaluationTask.agent_id.isnot(None))
    elif eval_type == "offline":
        query = query.where(EvaluationTask.agent_id.is_(None))

    if dataset_name:
        query = query.where(
            exists().where(
                Dataset.id == EvaluationTask.dataset_id,
                Dataset.name.ilike(f"%{dataset_name}%"),
            )
        )

    if agent_name:
        query = query.where(
            exists().where(
                Agent.id == EvaluationTask.agent_id,
                Agent.name.ilike(f"%{agent_name}%"),
            )
        )

    if agent_version:
        query = query.where(
            exists().where(
                Agent.id == EvaluationTask.agent_id,
                or_(
                    Agent.version.ilike(f"%{agent_version}%"),
                    Agent.version_tag.ilike(f"%{agent_version}%"),
                ),
            )
        )

    if inference_model:
        query = query.where(
            exists().where(
                AgentRun.task_id == EvaluationTask.id,
                or_(
                    AgentRun.inference_model_name.ilike(f"%{inference_model}%"),
                    AgentRun.inference_model_provider.ilike(f"%{inference_model}%"),
                    AgentRun.inference_model_version.ilike(f"%{inference_model}%"),
                ),
            )
        )

    if agent_workspace:
        query = query.where(
            exists().where(
                Agent.id == EvaluationTask.agent_id,
                Agent.inference_config.ilike(f"%{agent_workspace}%"),
            )
        )

    has_score_filter = any([min_total_score is not None, min_result_score is not None, min_process_score is not None])

    from services.task_list_service import serialize_tasks_for_list

    if has_score_filter:
        # 分数过滤：SQL 过滤后拉取全部，序列化后 Python 过滤再分页
        result = await session.execute(query)
        all_tasks = result.scalars().all()

        enriched = await serialize_tasks_for_list(session, list(all_tasks))
        for item, t in zip(enriched, all_tasks):
            item["created_at"] = format_datetime(t.created_at)
            item["completed_at"] = format_datetime(t.completed_at)
            item["is_paused"] = t.paused_at is not None

        filtered = []
        for item in enriched:
            total_s = item.get("total_score") or item.get("overall_avg_score")
            result_s = item.get("result_score") or item.get("overall_avg_score")
            process_s = item.get("process_score")

            if min_total_score is not None and (total_s is None or total_s < min_total_score):
                continue
            if min_result_score is not None and (result_s is None or result_s < min_result_score):
                continue
            if min_process_score is not None and (process_s is None or process_s < min_process_score):
                continue
            filtered.append(item)

        total = len(filtered)
        offset = (page - 1) * limit
        page_items = filtered[offset:offset + limit]

        return {
            "tasks": page_items,
            "page": page,
            "limit": limit,
            "total": total,
        }
    else:
        # 无分数过滤：正常 SQL 分页
        count_query = select(func.count()).select_from(query.subquery())
        total = (await session.execute(count_query)).scalar() or 0

        offset = (page - 1) * limit
        result = await session.execute(
            query.order_by(EvaluationTask.created_at.desc(), EvaluationTask.id.desc())
            .offset(offset)
            .limit(limit)
        )
        tasks = result.scalars().all()

        enriched = await serialize_tasks_for_list(session, list(tasks))
        for item, t in zip(enriched, tasks):
            item["created_at"] = format_datetime(t.created_at)
            item["completed_at"] = format_datetime(t.completed_at)
            item["is_paused"] = t.paused_at is not None

        return {
            "tasks": enriched,
            "page": page,
            "limit": limit,
            "total": total,
        }


@app.get("/dataset/{dataset_id}/history")
async def get_dataset_history(
    dataset_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Get historical evaluation results for a specific dataset."""
    result = await session.execute(
        select(EvaluationTask)
        .where(EvaluationTask.dataset_id == dataset_id)
        .where(EvaluationTask.status == "completed")
        .order_by(EvaluationTask.created_at.desc(), EvaluationTask.id.desc())
    )
    tasks = result.scalars().all()
    
    if not tasks:
        return {"history": []}
    
    history = []
    for task in tasks:
        run_rows = (
            await session.execute(select(AgentRun).where(AgentRun.task_id == task.id))
        ).scalars().all()
        results = await session.execute(
            select(EvaluationResult)
            .where(EvaluationResult.task_id == task.id)
        )
        results = results.scalars().all()
        
        metrics = {}
        for r in results:
            if r.metric_name not in metrics:
                metrics[r.metric_name] = []
            metrics[r.metric_name].append(r.score)
        
        avg_metrics = {}
        for metric, scores in metrics.items():
            valid_scores = [s for s in scores if s is not None]
            avg_metrics[metric] = round(sum(valid_scores) / len(valid_scores), 3) if valid_scores else 0
        
        history.append({
            "task_id": task.id,
            "created_at": format_datetime(task.created_at),
            "metrics": avg_metrics,
            "model": json.loads(task.models)[0] if task.models else "default",
            "inference_model_summary": summarize_inference_models_from_runs(run_rows),
            "execution_mode_summary": summarize_execution_modes_from_runs(run_rows),
        })
    
    return {"history": history}


class ExportTasksRequest(BaseModel):
    """批量导出评测报告请求。"""
    task_ids: List[str] = Field(..., min_length=1, description="要导出的任务ID列表")


@app.post("/tasks/export")
async def export_tasks_report(
    request: ExportTasksRequest,
    session: AsyncSession = Depends(get_session),
):
    """批量导出评测结果报告为 xlsx。"""
    from services.export_report_service import build_export_report

    body = await build_export_report(session, request.task_ids)
    if body is None:
        raise HTTPException(404, "未找到可导出的任务")

    from services.dataset_io_service import content_disposition_attachment

    filename = "评测报告.xlsx"
    return Response(
        content=body,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition_attachment(filename)},
    )


@app.delete("/tasks/batch")
async def delete_tasks_batch(
    request: dict,
    session: AsyncSession = Depends(get_session)
):
    """Delete multiple evaluation tasks and their associated results."""
    try:
        task_ids = request.get("task_ids", [])
        if not isinstance(task_ids, list):
            raise HTTPException(400, "task_ids must be a list")
        
        await session.execute(
            EvaluationResult.__table__.delete().where(EvaluationResult.task_id.in_(task_ids))
        )
        
        result = await session.execute(
            EvaluationTask.__table__.delete().where(EvaluationTask.id.in_(task_ids))
        )
        
        await session.commit()
        return {"message": f"Successfully deleted {result.rowcount} tasks"}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        raise HTTPException(500, f"Failed to delete tasks: {str(e)}")


@app.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Delete a single evaluation task and its associated results."""
    try:
        task_check = await session.execute(
            select(EvaluationTask).where(EvaluationTask.id == task_id)
        )
        task = task_check.scalar_one_or_none()
        
        if not task:
            await session.rollback()
            raise HTTPException(404, f"Task {task_id} not found")
        
        await session.execute(
            EvaluationResult.__table__.delete().where(EvaluationResult.task_id == task_id)
        )
        
        await session.execute(
            EvaluationTask.__table__.delete().where(EvaluationTask.id == task_id)
        )
        
        await session.commit()
        return {"message": f"Task {task_id} deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        raise HTTPException(500, f"Failed to delete task: {str(e)}")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/llm-call-stats")
async def llm_call_stats(reset: bool = False):
    """返回 LLM 调用耗时统计摘要（含 P50/P95/P99），可选 reset=true 清零。"""
    from evaluators.llm_client import get_call_stats_summary, reset_call_stats
    summary = get_call_stats_summary()
    if reset:
        reset_call_stats()
    return {"stats": summary}


# ============ LLM 模型配置 API ============

def mask_api_key(api_key: str) -> str:
    """脱敏 API Key，只显示前4位和后4位"""
    if not api_key or len(api_key) <= 8:
        return "***" if api_key else ""
    return api_key[:4] + "*" * (len(api_key) - 8) + api_key[-4:]


async def refresh_llm_models_cache(session: AsyncSession):
    """刷新内存中的 LLM 模型配置缓存"""
    from config import update_llm_models_cache
    result = await session.execute(select(LLMModel))
    models = result.scalars().all()
    cache = {}
    for m in models:
        cache[m.id] = {
            "name": m.name,
            "provider": m.provider,
            "base_url": m.base_url,
            "model": m.model,
            "api_key": m.api_key or "",
            "is_default": m.is_default,
            "max_tokens": m.max_tokens,
            "temperature": m.temperature,
            "timeout": m.timeout
        }
    update_llm_models_cache(cache)


@app.get("/llm-models", response_model=List[LLMModelResponse])
async def list_llm_models(
    session: AsyncSession = Depends(get_session)
):
    """获取所有LLM模型配置"""
    result = await session.execute(select(LLMModel).order_by(LLMModel.created_at.desc()))
    models = result.scalars().all()
    return [
        LLMModelResponse(
            id=m.id,
            name=m.name,
            provider=m.provider,
            base_url=m.base_url,
            model=m.model,
            api_key=mask_api_key(m.api_key) if m.api_key else None,
            is_default=m.is_default,
            max_tokens=m.max_tokens,
            temperature=m.temperature,
            timeout=m.timeout,
            created_at=format_datetime(m.created_at),
            updated_at=format_datetime(m.updated_at)
        )
        for m in models
    ]


@app.post("/llm-models", response_model=LLMModelResponse)
async def create_llm_model(
    request: LLMModelCreateRequest,
    session: AsyncSession = Depends(get_session)
):
    """创建LLM模型配置"""
    # 检查 ID 是否重复
    existing = await session.execute(select(LLMModel).where(LLMModel.id == request.id))
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"模型标识 '{request.id}' 已存在")
    
    # 如果设为默认，取消其他默认
    if request.is_default:
        await session.execute(
            update(LLMModel).values(is_default=False)
        )
    
    model = LLMModel(
        id=request.id,
        name=request.name,
        provider=request.provider,
        base_url=request.base_url,
        model=request.model,
        api_key=request.api_key,
        is_default=request.is_default,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        timeout=request.timeout
    )
    session.add(model)
    await session.commit()
    
    # 刷新缓存
    await refresh_llm_models_cache(session)
    
    log("INFO", "API", "创建LLM模型配置", model_id=request.id, name=request.name, provider=request.provider)
    
    return LLMModelResponse(
        id=model.id,
        name=model.name,
        provider=model.provider,
        base_url=model.base_url,
        model=model.model,
        api_key=mask_api_key(model.api_key) if model.api_key else None,
        is_default=model.is_default,
        max_tokens=model.max_tokens,
        temperature=model.temperature,
        timeout=model.timeout,
        created_at=format_datetime(model.created_at),
        updated_at=format_datetime(model.updated_at)
    )


@app.put("/llm-models/{model_id}", response_model=LLMModelResponse)
async def update_llm_model(
    model_id: str,
    request: LLMModelUpdateRequest,
    session: AsyncSession = Depends(get_session)
):
    """更新LLM模型配置"""
    result = await session.execute(select(LLMModel).where(LLMModel.id == model_id))
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(404, f"模型 '{model_id}' 不存在")
    
    if request.name is not None:
        model.name = request.name
    if request.provider is not None:
        model.provider = request.provider
    if request.base_url is not None:
        model.base_url = request.base_url
    if request.model is not None:
        model.model = request.model
    if request.api_key is not None:
        model.api_key = request.api_key
    if request.is_default is not None:
        if request.is_default:
            # 取消其他默认
            await session.execute(
                update(LLMModel).values(is_default=False)
            )
        model.is_default = request.is_default
    if request.max_tokens is not None:
        model.max_tokens = request.max_tokens
    if request.temperature is not None:
        model.temperature = request.temperature
    if request.timeout is not None:
        model.timeout = request.timeout
    
    model.updated_at = datetime.utcnow()
    await session.commit()
    
    # 刷新缓存
    await refresh_llm_models_cache(session)
    
    log("INFO", "API", "更新LLM模型配置", model_id=model_id, name=model.name)
    
    return LLMModelResponse(
        id=model.id,
        name=model.name,
        provider=model.provider,
        base_url=model.base_url,
        model=model.model,
        api_key=mask_api_key(model.api_key) if model.api_key else None,
        is_default=model.is_default,
        max_tokens=model.max_tokens,
        temperature=model.temperature,
        timeout=model.timeout,
        created_at=format_datetime(model.created_at),
        updated_at=format_datetime(model.updated_at)
    )


@app.delete("/llm-models/{model_id}")
async def delete_llm_model(
    model_id: str,
    session: AsyncSession = Depends(get_session)
):
    """删除LLM模型配置"""
    result = await session.execute(select(LLMModel).where(LLMModel.id == model_id))
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(404, f"模型 '{model_id}' 不存在")
    
    await session.delete(model)
    await session.commit()
    
    # 刷新缓存
    await refresh_llm_models_cache(session)
    
    log("INFO", "API", "删除LLM模型配置", model_id=model_id, name=model.name)
    
    return {"message": f"LLM模型 '{model_id}' 删除成功"}


@app.post("/llm-models/{model_id}/test")
async def test_llm_model(
    model_id: str,
    session: AsyncSession = Depends(get_session)
):
    """测试LLM模型连接"""
    result = await session.execute(select(LLMModel).where(LLMModel.id == model_id))
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(404, f"模型 '{model_id}' 不存在")
    
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=model.api_key or "",
            base_url=model.base_url
        )
        response = await client.chat.completions.create(
            model=model.model,
            messages=[{"role": "user", "content": "你好"}],
            max_tokens=10,
            timeout=30.0
        )
        answer = response.choices[0].message.content if response.choices else ""
        return {"success": True, "message": f"连接成功，模型返回: {answer[:50]}"}
    except Exception as e:
        return {"success": False, "message": f"连接失败: {str(e)}"}


@app.get("/llm-models/{model_id}/api-key")
async def get_llm_model_api_key(
    model_id: str,
    session: AsyncSession = Depends(get_session)
):
    """获取LLM模型的明文API Key（用于前端展示）"""
    result = await session.execute(select(LLMModel).where(LLMModel.id == model_id))
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(404, f"模型 '{model_id}' 不存在")
    return {"api_key": model.api_key or ""}


def _onto_agent_request_headers(api_token: str) -> dict:
    headers = {"Content-Type": "application/json"}
    token = (api_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


@app.get("/onto-agent/workspaces")
async def get_onto_agent_workspaces(
    base_url: str,
    api_token: str = Query(default=""),
    auth_type: str = Query(default=""),
    username: str = Query(default=""),
    password: str = Query(default=""),
):
    """
    代理接口：获取 onto-agent 的工作空间列表

    Query 参数:
        - base_url: onto-agent 服务地址，如 http://localhost:5055
        - api_token: onto-agent API Token
        - auth_type: 鉴权类型（bearer / api_key / basic_auth / none）
        - username: basic_auth 用户名（用于 token 过期时自动刷新）
        - password: basic_auth 密码（用于 token 过期时自动刷新）

    返回:
        {
            "workspaces": [
                {"id": "workspace-1", "name": "工作空间1", "is_default": true},
                ...
            ],
            "default_workspace_id": "workspace-1"
        }
    """
    import aiohttp
    from utils.agent_url import resolve_agent_base_url
    from services.inference_service import _is_token_expired, _refresh_token

    resolved_base_url = resolve_agent_base_url(base_url)

    if not (api_token or "").strip():
        raise HTTPException(
            401,
            "Onto Agent 需要 API Token：请先开启鉴权，填写账号密码并点击「登录」",
        )

    # basic_auth 场景：token 过期时尝试自动刷新
    effective_token = api_token.strip()
    if auth_type == "basic_auth" and _is_token_expired(effective_token):
        if username and password:
            log("INFO", "ONTO_AGENT", "Token 已过期，尝试自动刷新", base_url=resolved_base_url)
            new_token = await _refresh_token(resolved_base_url, username, password)
            if new_token:
                effective_token = new_token
                log("INFO", "ONTO_AGENT", "Token 自动刷新成功")
            else:
                log("WARNING", "ONTO_AGENT", "Token 自动刷新失败，将使用原 Token 重试")
        else:
            log("WARNING", "ONTO_AGENT", "Token 已过期且未提供用户名密码，无法自动刷新")

    url = f"{resolved_base_url.rstrip('/')}/agentic/workspaces"
    headers = _onto_agent_request_headers(effective_token)

    async def _fetch_workspaces(tok: str) -> aiohttp.ClientResponse:
        hdrs = _onto_agent_request_headers(tok)
        return await session.get(url, headers=hdrs, timeout=aiohttp.ClientTimeout(total=10))

    try:
        async with aiohttp.ClientSession() as session:
            resp = await _fetch_workspaces(effective_token)

            # 401 重试：basic_auth 场景下刷新 token 后重试一次
            if resp.status == 401 and auth_type == "basic_auth" and username and password:
                log("WARNING", "ONTO_AGENT", "收到 401，尝试刷新 Token 后重试")
                resp.close()
                new_token = await _refresh_token(resolved_base_url, username, password)
                if new_token:
                    effective_token = new_token
                    log("INFO", "ONTO_AGENT", "Token 刷新成功，重试请求")
                    resp = await _fetch_workspaces(effective_token)

            if resp.status == 200:
                data = await resp.json()
                workspaces = []
                for ws in data.get("workspaces", []):
                    workspaces.append({
                        "id": ws.get("id"),
                        "name": ws.get("name"),
                        "description": ws.get("description"),
                        "is_default": ws.get("is_default", False)
                    })
                return {
                    "workspaces": workspaces,
                    "default_workspace_id": data.get("default_workspace_id"),
                    "total": data.get("total", len(workspaces))
                }
            elif resp.status == 401:
                raise HTTPException(401, "Onto Agent 认证失败，请检查 API Token 是否过期，可重新登录")
            elif resp.status == 403:
                raise HTTPException(403, "Onto Agent 权限不足")
            else:
                error_text = await resp.text()
                raise HTTPException(resp.status, f"Onto Agent 返回错误: {error_text}")
    except HTTPException:
        raise
    except aiohttp.ClientError as e:
        log(
            "ERROR",
            "ONTO_AGENT",
            "连接 onto-agent 失败",
            error=str(e),
            base_url=base_url,
            resolved_base_url=resolved_base_url,
        )
        raise HTTPException(
            502,
            f"无法连接到 onto-agent（{resolved_base_url}）: {str(e)}",
        )
    except Exception as e:
        log("ERROR", "ONTO_AGENT", "获取工作空间失败", error=str(e), base_url=base_url)
        raise HTTPException(500, f"获取工作空间失败: {str(e)}")


@app.post("/onto-agent/probe-sync", response_model=AgentProbeResponse)
async def probe_onto_agent_sync(request: AgentProbeRequest):
    """
    Agent 注册前 sync 健康预检：URL 解析 → 鉴权 → workspace → sync ping。
    """
    from services.agent_probe_service import probe_agent_inference_config

    result = await probe_agent_inference_config(request.inference_config.model_dump())
    return AgentProbeResponse(**result)


@app.post("/onto-agent/test-connection")
async def test_onto_agent_connection(
    request: dict
):
    """
    测试 onto-agent 连接
    
    请求体:
        {
            "base_url": "http://localhost:5055",
            "api_token": "your-token"
        }
    
    返回:
        {"success": true, "message": "连接成功"}
        或
        {"success": false, "message": "连接失败: ..."}
    """
    import aiohttp
    
    base_url = request.get("base_url", "").rstrip("/")
    api_token = request.get("api_token", "")
    
    if not base_url:
        return {"success": False, "message": "base_url 不能为空"}
    
    url = f"{base_url}/agentic/workspaces"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return {"success": True, "message": "连接成功"}
                elif resp.status == 401:
                    return {"success": False, "message": "认证失败，请检查 API Token"}
                else:
                    return {"success": False, "message": f"连接失败，状态码: {resp.status}"}
    except Exception as e:
        return {"success": False, "message": f"连接失败: {str(e)}"}


# ============ 本体包管理 API ============

import zipfile
import io
import re as _re
from fastapi import UploadFile, File, Form


def _get_ontology_base_dir() -> str:
    """获取本体包存储根目录"""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "md")


def _sanitize_dir_name(name: str) -> str:
    """将名称转换为安全的目录名"""
    name = name.strip()
    name = _re.sub(r'[^\w\u4e00-\u9fff\-]', '_', name)
    return name[:100] if name else "unnamed"


@app.get("/ontology-packages")
async def list_ontology_packages():
    """列出所有本体包"""
    base_dir = _get_ontology_base_dir()
    packages = []
    if os.path.exists(base_dir):
        for name in os.listdir(base_dir):
            pkg_dir = os.path.join(base_dir, name)
            if os.path.isdir(pkg_dir):
                md_files = []
                for root, dirs, files in os.walk(pkg_dir):
                    for f in files:
                        if f.endswith('.md'):
                            md_files.append(f)
                stat = os.stat(pkg_dir)
                packages.append({
                    "name": name,
                    "file_count": len(md_files),
                    "created_at": datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M:%S"),
                    "size_bytes": sum(
                        os.path.getsize(os.path.join(r, f))
                        for r, _, fs in os.walk(pkg_dir) for f in fs
                    ),
                })
    packages.sort(key=lambda x: x["created_at"], reverse=True)
    return packages


@app.post("/ontology-packages/upload")
async def upload_ontology_package(
    name: str = Form(..., description="本体包名称（用作目录名）"),
    file: UploadFile = File(..., description="ZIP 格式的本体包"),
):
    """上传 ZIP 格式的本体包，解压到 /app/md/{name}/ 目录"""
    if not file.filename or not file.filename.lower().endswith('.zip'):
        raise HTTPException(400, "仅支持 ZIP 格式文件")

    content = await file.read()
    if not content:
        raise HTTPException(400, "上传文件为空")

    base_dir = _get_ontology_base_dir()
    safe_name = _sanitize_dir_name(name)
    target_dir = os.path.join(base_dir, safe_name)

    if os.path.exists(target_dir):
        raise HTTPException(409, f"本体包 '{safe_name}' 已存在，请先删除或更换名称")

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            os.makedirs(target_dir, exist_ok=True)
            md_count = 0
            for member in zf.namelist():
                # 防止路径穿越攻击
                member_path = os.path.normpath(member)
                if member_path.startswith('..') or os.path.isabs(member_path):
                    continue
                target_path = os.path.join(target_dir, member_path)
                if member.endswith('/'):
                    os.makedirs(target_path, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    zf.extract(member, target_dir)
                    if member.lower().endswith('.md'):
                        md_count += 1
    except zipfile.BadZipFile:
        raise HTTPException(400, "无效的 ZIP 文件")
    except Exception as e:
        # 失败时清理已创建的目录
        import shutil as _shutil
        if os.path.exists(target_dir):
            _shutil.rmtree(target_dir, ignore_errors=True)
        raise HTTPException(500, f"解压失败: {str(e)}")

    log("INFO", "API", "本体包上传成功", name=safe_name, file_count=md_count)
    return {"success": True, "name": safe_name, "file_count": md_count}


@app.delete("/ontology-packages/{name}")
async def delete_ontology_package(name: str):
    """删除本体包"""
    import shutil as _shutil
    base_dir = _get_ontology_base_dir()
    safe_name = _sanitize_dir_name(name)
    target_dir = os.path.join(base_dir, safe_name)

    if not os.path.exists(target_dir):
        raise HTTPException(404, f"本体包 '{safe_name}' 不存在")

    try:
        _shutil.rmtree(target_dir)
    except Exception as e:
        raise HTTPException(500, f"删除失败: {str(e)}")

    log("INFO", "API", "本体包删除成功", name=safe_name)
    return {"success": True, "name": safe_name}


# ============ 自定义评估脚本 API ============

class ScriptCreateRequest(BaseModel):
    """创建评估脚本请求"""
    name: str = Field(..., description="脚本名称")
    description: Optional[str] = Field(None, description="脚本描述")
    script_code: str = Field(..., description="脚本代码，必须定义 evaluate(question, answer, context=None, ground_truth=None) -> dict")


class ScriptUpdateRequest(BaseModel):
    """更新评估脚本请求"""
    name: Optional[str] = Field(None, description="脚本名称")
    description: Optional[str] = Field(None, description="脚本描述")
    script_code: Optional[str] = Field(None, description="脚本代码")


class ScriptResponse(BaseModel):
    """脚本响应"""
    id: str
    name: str
    description: Optional[str] = None
    script_code_preview: Optional[str] = None  # 代码前200字符预览
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@app.get("/eval-scripts", response_model=List[ScriptResponse])
async def list_eval_scripts(
    session: AsyncSession = Depends(get_session)
):
    """获取所有评估脚本列表"""
    result = await session.execute(select(EvaluationScript).order_by(EvaluationScript.created_at.desc()))
    scripts = result.scalars().all()
    return [
        ScriptResponse(
            id=s.id,
            name=s.name,
            description=s.description,
            script_code_preview=s.script_code[:200] + "..." if len(s.script_code) > 200 else s.script_code,
            created_at=format_datetime(s.created_at),
            updated_at=format_datetime(s.updated_at)
        )
        for s in scripts
    ]


@app.post("/eval-scripts", response_model=ScriptResponse)
async def create_eval_script(
    request: ScriptCreateRequest,
    session: AsyncSession = Depends(get_session)
):
    """创建评估脚本"""
    from evaluators.custom_script_metric import validate_script

    # 验证脚本代码
    validation = validate_script(request.script_code)
    if not validation["valid"]:
        raise HTTPException(400, f"脚本验证失败: {validation['error']}")

    script_id = f"script_{uuid.uuid4().hex}"

    script = EvaluationScript(
        id=script_id,
        name=request.name,
        description=request.description,
        script_code=request.script_code
    )
    session.add(script)
    await session.commit()

    log("INFO", "API", "创建评估脚本", script_id=script_id, name=request.name)

    return ScriptResponse(
        id=script.id,
        name=script.name,
        description=script.description,
        script_code_preview=script.script_code[:200] + "..." if len(script.script_code) > 200 else script.script_code,
        created_at=format_datetime(script.created_at),
        updated_at=format_datetime(script.updated_at)
    )


@app.get("/eval-scripts/{script_id}", response_model=ScriptResponse)
async def get_eval_script(
    script_id: str,
    session: AsyncSession = Depends(get_session)
):
    """获取评估脚本详情（包含完整代码）"""
    result = await session.execute(select(EvaluationScript).where(EvaluationScript.id == script_id))
    script = result.scalar_one_or_none()
    if not script:
        raise HTTPException(404, f"脚本 '{script_id}' 不存在")

    return ScriptResponse(
        id=script.id,
        name=script.name,
        description=script.description,
        script_code_preview=script.script_code,  # 详情返回完整代码
        created_at=format_datetime(script.created_at),
        updated_at=format_datetime(script.updated_at)
    )


@app.put("/eval-scripts/{script_id}", response_model=ScriptResponse)
async def update_eval_script(
    script_id: str,
    request: ScriptUpdateRequest,
    session: AsyncSession = Depends(get_session)
):
    """更新评估脚本"""
    result = await session.execute(select(EvaluationScript).where(EvaluationScript.id == script_id))
    script = result.scalar_one_or_none()
    if not script:
        raise HTTPException(404, f"脚本 '{script_id}' 不存在")

    if request.name is not None:
        script.name = request.name
    if request.description is not None:
        script.description = request.description
    if request.script_code is not None:
        # 验证新脚本代码
        from evaluators.custom_script_metric import validate_script
        validation = validate_script(request.script_code)
        if not validation["valid"]:
            raise HTTPException(400, f"脚本验证失败: {validation['error']}")
        script.script_code = request.script_code

    script.updated_at = datetime.utcnow()
    await session.commit()

    log("INFO", "API", "更新评估脚本", script_id=script_id, name=script.name)

    return ScriptResponse(
        id=script.id,
        name=script.name,
        description=script.description,
        script_code_preview=script.script_code[:200] + "..." if len(script.script_code) > 200 else script.script_code,
        created_at=format_datetime(script.created_at),
        updated_at=format_datetime(script.updated_at)
    )


@app.delete("/eval-scripts/{script_id}")
async def delete_eval_script(
    script_id: str,
    session: AsyncSession = Depends(get_session)
):
    """删除评估脚本"""
    result = await session.execute(select(EvaluationScript).where(EvaluationScript.id == script_id))
    script = result.scalar_one_or_none()
    if not script:
        raise HTTPException(404, f"脚本 '{script_id}' 不存在")

    await session.delete(script)
    await session.commit()

    log("INFO", "API", "删除评估脚本", script_id=script_id, name=script.name)
    return {"message": f"评估脚本 '{script_id}' 删除成功"}


@app.post("/eval-scripts/validate")
async def validate_eval_script(
    request: ScriptCreateRequest,
):
    """验证脚本代码是否合法（不保存）"""
    from evaluators.custom_script_metric import validate_script
    validation = validate_script(request.script_code)
    return validation


# ============ 联机帮助 API ============

def _get_project_root() -> str:
    """获取项目根目录"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@app.get("/guide")
async def get_guide_content():
    """返回使用指南 Markdown 文本"""
    from starlette.responses import PlainTextResponse
    md_path = os.path.join(_get_project_root(), "本体评测平台使用指南.md")
    if not os.path.exists(md_path):
        raise HTTPException(404, "使用指南文件不存在")
    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8")


# ── AI 对话式配置（阶段 1）──
# 会话生命周期 + 意图解析 + 草稿编辑 + apply 启动评测。
# 核心逻辑在 chat/ 模块，apply 复用 services.eval_task_service.create_and_start_eval_task。


def _session_to_out(s: ChatSession, with_messages: bool = False) -> ChatSessionOut:
    """ChatSession ORM → 输出模型（含 draft_request/missing_fields JSON 解析）。"""
    import json as _json
    draft = None
    if s.draft_request:
        try:
            draft = _json.loads(s.draft_request)
        except (ValueError, TypeError):
            draft = None
    missing = None
    if s.missing_fields:
        try:
            missing = _json.loads(s.missing_fields)
        except (ValueError, TypeError):
            missing = None
    return ChatSessionOut(
        id=s.id,
        title=s.title,
        status=s.status,
        intent=s.intent,
        model_key=s.model_key,
        intent_text=s.intent_text,
        draft_request=draft,
        confidence=s.confidence,
        missing_fields=missing if isinstance(missing, list) else None,
        suggestions=[],
        error_message=s.error_message,
        created_at=s.created_at.isoformat() if s.created_at else None,
        updated_at=s.updated_at.isoformat() if s.updated_at else None,
    )


@app.post("/chat/sessions", response_model=ChatSessionOut, status_code=201)
async def chat_create_session(req: ChatSessionCreateRequest):
    """新建 AI 配置会话（状态 draft）。

    阶段 1：intent_text 必填（快速模式）。
    阶段 2：intent_text 可空（对话模式，首条消息走 POST /messages）。
    """
    from chat import session as chat_session_svc
    obj = await chat_session_svc.create_session(req.intent_text or "", req.model_key)
    return _session_to_out(obj)


@app.get("/chat/sessions")
async def chat_list_sessions(
    status: Optional[str] = Query(None),
    intent: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """会话分页列表（不含 draft_request 大字段）。"""
    from chat import session as chat_session_svc
    rows, total = await chat_session_svc.list_sessions(status, intent, page, page_size)
    items = [
        ChatSessionListItem(
            id=r.id, title=r.title, status=r.status, intent=r.intent,
            model_key=r.model_key, confidence=r.confidence,
            created_at=r.created_at.isoformat() if r.created_at else None,
            updated_at=r.updated_at.isoformat() if r.updated_at else None,
        ) for r in rows
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/chat/sessions/{session_id}", response_model=ChatSessionOut)
async def chat_get_session(session_id: str):
    """会话详情（含 draft_request / intent_text）。"""
    from chat import session as chat_session_svc
    obj = await chat_session_svc.get_session(session_id)
    if not obj:
        raise HTTPException(404, f"会话 {session_id} 不存在")
    return _session_to_out(obj)


@app.post("/chat/sessions/{session_id}/interpret", response_model=ChatInterpretResponse)
async def chat_interpret(session_id: str):
    """阶段 1 核心：单轮 LLM 意图解析 → EvaluateRequest 草稿。"""
    from chat import session as chat_session_svc
    from chat import recommend as recommend_svc

    obj = await chat_session_svc.get_session(session_id)
    if not obj:
        raise HTTPException(404, f"会话 {session_id} 不存在")
    if not obj.intent_text or not obj.intent_text.strip():
        raise HTTPException(400, "会话缺少意图文本")

    try:
        result = await recommend_svc.interpret_intent(obj.intent_text, obj.model_key)
    except Exception as exc:
        log("ERROR", "API", "意图解析失败", session_id=session_id, error=str(exc))
        await chat_session_svc.mark_error(session_id, f"意图解析失败: {exc}")
        raise HTTPException(500, f"意图解析失败: {exc}") from exc

    # 落库草稿
    await chat_session_svc.set_interpretation(
        session_id,
        draft=result["draft"],
        confidence=result["confidence"],
        missing_fields=result["missing_fields"],
        suggestions=result["suggestions"],
        intent=result["intent"],
    )
    # 存 user / assistant 两条消息
    await chat_session_svc.append_message(session_id, "user", obj.intent_text)
    await chat_session_svc.append_message(
        session_id, "assistant",
        content=f"已解析为配置草稿（置信度 {result['confidence']:.0%}）",
        tool_result={"draft": result["draft"], "missing_fields": result["missing_fields"]},
    )

    return ChatInterpretResponse(
        session_id=session_id,
        status="ready",
        draft=result["draft"],
        confidence=result["confidence"],
        missing_fields=result["missing_fields"],
        suggestions=result["suggestions"],
        intent=result["intent"],
    )


@app.patch("/chat/sessions/{session_id}/draft", response_model=ChatSessionOut)
async def chat_patch_draft(session_id: str, req: ChatDraftPatchRequest):
    """用户在前端改了草稿字段后回写（合并到 draft_request）。"""
    from chat import session as chat_session_svc
    if not req.fields:
        raise HTTPException(400, "fields 不能为空")
    obj = await chat_session_svc.update_draft(session_id, req.fields)
    if not obj:
        raise HTTPException(404, f"会话 {session_id} 不存在")
    return _session_to_out(obj)


@app.post("/chat/sessions/{session_id}/apply", response_model=EvaluateResponse, status_code=202)
async def chat_apply(
    session_id: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """草稿 → 启动评测（复用 create_and_start_eval_task，免二次 HTTP）。"""
    import json as _json
    from chat import session as chat_session_svc
    from services.eval_task_service import create_and_start_eval_task

    obj = await chat_session_svc.get_session(session_id)
    if not obj:
        raise HTTPException(404, f"会话 {session_id} 不存在")
    if obj.status not in ("ready", "consumed"):
        raise HTTPException(400, f"会话状态 {obj.status}，需先 interpret 解析草稿")
    if not obj.draft_request:
        raise HTTPException(400, "草稿为空，无法启动")

    try:
        draft_dict = _json.loads(obj.draft_request)
    except (ValueError, TypeError) as exc:
        await chat_session_svc.mark_error(session_id, f"草稿 JSON 解析失败: {exc}")
        raise HTTPException(400, "草稿 JSON 解析失败") from exc

    # dataset_id 必填校验
    if not draft_dict.get("dataset_id"):
        raise HTTPException(400, "草稿缺少 dataset_id，无法启动评测")

    try:
        eval_req = EvaluateRequest(**draft_dict)
    except Exception as exc:
        await chat_session_svc.mark_error(session_id, f"草稿字段非法: {exc}")
        raise HTTPException(400, f"草稿字段非法: {exc}") from exc

    try:
        resp = await create_and_start_eval_task(eval_req, session, background_tasks)
    except HTTPException as exc:
        await chat_session_svc.mark_error(session_id, exc.detail)
        raise
    except Exception as exc:
        await chat_session_svc.mark_error(session_id, str(exc))
        raise HTTPException(500, f"启动评测失败: {exc}") from exc

    await chat_session_svc.mark_consumed(session_id, resp.task_uuid)
    return resp


@app.get("/chat/sessions/{session_id}/messages")
async def chat_list_messages(session_id: str):
    """列出会话消息（阶段 1 user/assistant；阶段 2 含 tool 角色）。"""
    import json as _json
    from chat import session as chat_session_svc
    msgs = await chat_session_svc.list_messages(session_id)
    out = []
    for m in msgs:
        out.append(ChatMessageOut(
            id=m.id, session_id=m.session_id, role=m.role, content=m.content,
            tool_name=m.tool_name,
            tool_args=_json.loads(m.tool_args) if m.tool_args else None,
            tool_result=_json.loads(m.tool_result) if m.tool_result else None,
            tokens=m.tokens,
            created_at=m.created_at.isoformat() if m.created_at else None,
        ))
    return {"items": out}


@app.post("/chat/sessions/{session_id}/messages", response_model=ChatMessageOut, status_code=201)
async def chat_post_message(session_id: str, req: ChatMessageCreateRequest):
    """阶段 2：追加用户消息 + 异步启动 agent loop。

    流程：
    1. 追加 user 消息到 ChatMessage 表
    2. asyncio.create_task 启动 run_agent_loop（不阻塞 HTTP 响应）
    3. 前端随即 GET /stream 消费 SSE 事件
    """
    from chat import session as chat_session_svc
    from chat.orchestrator import run_agent_loop

    obj = await chat_session_svc.get_session(session_id)
    if not obj:
        raise HTTPException(404, f"会话 {session_id} 不存在")
    if not req.content or not req.content.strip():
        raise HTTPException(400, "消息内容不能为空")

    # 首条消息且 intent_text 为空 → 用首条消息作为 intent_text（便于标题生成）
    if not obj.intent_text:
        await chat_session_svc.set_intent_text(session_id, req.content.strip())

    msg = await chat_session_svc.append_message(
        session_id, role="user", content=req.content,
    )

    # 异步启动 agent loop（不阻塞响应）
    asyncio.create_task(
        run_agent_loop(session_id, obj.model_key)
    )
    log("INFO", "API", "agent loop 已调度", session_id=session_id)

    return ChatMessageOut(
        id=msg.id, session_id=msg.session_id, role=msg.role, content=msg.content,
        tool_name=msg.tool_name,
        created_at=msg.created_at.isoformat() if msg.created_at else None,
    )


@app.get("/chat/sessions/{session_id}/stream")
async def chat_stream(session_id: str):
    """阶段 2 SSE：推送 agent loop 中间态。

    事件类型：chat_start / chat_tool_call / chat_tool_result / chat_message / chat_complete / chat_error / ping
    """
    from chat.orchestrator import get_chat_queue

    async def event_generator():
        queue = await get_chat_queue(session_id)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=60.0)
                yield {
                    "event": event["event"],
                    "data": json.dumps(event["data"], ensure_ascii=False),
                }
                if event["event"] in ["chat_complete", "chat_error"]:
                    break
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": "{}"}

    return EventSourceResponse(event_generator())


@app.delete("/chat/sessions/{session_id}", status_code=204)
async def chat_delete_session(session_id: str):
    """删会话 + 级联删 messages。"""
    from chat import session as chat_session_svc
    ok = await chat_session_svc.delete_session(session_id)
    if not ok:
        raise HTTPException(404, f"会话 {session_id} 不存在")
    return None


@app.post("/chat/sessions/{session_id}/confirm")
async def chat_confirm_action(session_id: str, session: AsyncSession = Depends(get_session)):
    """确认执行写操作工具。

    读 pending_confirm → 执行写操作 → clear → 恢复 agent loop (set_result on Future)。
    """
    from chat import session as chat_session_svc
    from chat.orchestrator import _confirm_futures
    from chat.confirm_executor import execute_confirmed_action

    obj = await chat_session_svc.get_session(session_id, session)
    if not obj:
        raise HTTPException(404, f"会话 {session_id} 不存在")

    pending = await chat_session_svc.get_pending_confirm(session_id, session)
    if not pending:
        raise HTTPException(400, "无待确认操作")

    try:
        result = await execute_confirmed_action(
            pending["tool_name"], pending.get("tool_args", {}), session, session_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        log("ERROR", "API", "确认执行写操作失败",
            session_id=session_id, tool=pending.get("tool_name"), error=str(exc))
        raise HTTPException(500, f"执行写操作失败: {exc}") from exc

    await chat_session_svc.clear_pending_confirm(session_id, session)

    # 恢复 agent loop
    future = _confirm_futures.get(session_id)
    if future and not future.done():
        future.set_result({"cancelled": False, "result": result})
    else:
        log("WARN", "API", "确认时 agent loop Future 不存在或已完成",
           session_id=session_id)

    return result


@app.post("/chat/sessions/{session_id}/cancel")
async def chat_cancel_action(session_id: str, session: AsyncSession = Depends(get_session)):
    """取消待确认的写操作。"""
    from chat import session as chat_session_svc
    from chat.orchestrator import _confirm_futures

    obj = await chat_session_svc.get_session(session_id, session)
    if not obj:
        raise HTTPException(404, f"会话 {session_id} 不存在")

    await chat_session_svc.clear_pending_confirm(session_id, session)

    future = _confirm_futures.get(session_id)
    if future and not future.done():
        future.set_result({"cancelled": True})
    else:
        log("WARN", "API", "取消时 agent loop Future 不存在或已完成",
           session_id=session_id)

    return {"cancelled": True}


@app.get("/chat/export-download/{file_token}")
async def chat_export_download(file_token: str):
    """下载 AI 助手 export_report 工具生成的 xlsx 临时文件。

    file_token 由 tools.export_report 返回，文件存在系统临时目录。
    下载后自动删除临时文件。
    """
    import os
    import re
    import tempfile

    # token 格式校验（防路径穿越）
    if not re.match(r"^chat_export_[a-f0-9]{12}$", file_token):
        raise HTTPException(400, "非法的 file_token")

    file_path = os.path.join(tempfile.gettempdir(), f"{file_token}.xlsx")
    if not os.path.exists(file_path):
        raise HTTPException(404, "报告文件不存在或已过期")

    with open(file_path, "rb") as f:
        body = f.read()

    # 下载后删除临时文件
    try:
        os.remove(file_path)
    except OSError:
        pass

    from services.dataset_io_service import content_disposition_attachment

    filename = "评测报告.xlsx"
    return Response(
        content=body,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition_attachment(filename)},
    )


# 挂载图片静态目录
_images_dir = os.path.join(_get_project_root(), "images")
if os.path.exists(_images_dir):
    from starlette.staticfiles import StaticFiles
    app.mount("/guide-images", StaticFiles(directory=_images_dir), name="guide-images")
