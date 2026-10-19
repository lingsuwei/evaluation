"""评估任务创建与启动服务。

从 main.py 的 POST /evaluate 路由抽取的核心逻辑，供路由和 AI 对话配置的
apply 复用，避免二次 HTTP 回环。

行为与原路由完全一致，不改变现有 POST /evaluate 的任何语义。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Optional

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_model_display_name, get_model_pool, _db_llm_models_cache
from database import EvaluationTask, Dataset, Agent
from logger import log
from schemas import EvaluateRequest, EvaluateResponse, TaskStatus
from services.evaluation_mode import (
    EVALUATION_MODE_OFFLINE,
    EVALUATION_MODE_ONLINE,
    validate_offline_evaluate_request,
)


async def create_and_start_eval_task(
    request: EvaluateRequest,
    session: AsyncSession,
    background_tasks: BackgroundTasks,
) -> EvaluateResponse:
    """创建评测任务并启动后台评估。

    与原 POST /evaluate 路由逻辑等价：
    1. 校验 dataset / agent / llm_model
    2. 离线模式校验指标与轮次
    3. 创建 EvaluationTask 行
    4. 启动后台评估任务
    """
    # 延迟导入避免循环依赖（refresh_llm_models_cache 定义在 main 中）
    from main import refresh_llm_models_cache
    from tasks import run_evaluation_task, run_online_evaluation_task
    from config import ENABLED_METRICS, METRICS_CONFIG

    log("INFO", "EVAL_SERVICE", "评估任务创建开始",
        dataset_id=request.dataset_id,
        model_name=request.model_name,
        agent_id=request.agent_id,
        online_mode=request.online_mode,
        llm_model=request.llm_model,
        selected_metrics=request.selected_metrics)

    # Validate dataset exists
    result = await session.execute(
        select(Dataset).where(Dataset.id == request.dataset_id)
    )
    dataset = result.scalar_one_or_none()
    if not dataset:
        log("ERROR", "EVAL_SERVICE", "数据集不存在", dataset_id=request.dataset_id)
        raise HTTPException(404, "Dataset not found")

    offline_metrics: Optional[list[str]] = None
    effective_inference_rounds = request.inference_rounds
    if not request.online_mode:
        try:
            offline_metrics, effective_inference_rounds = validate_offline_evaluate_request(
                selected_metrics=request.selected_metrics,
                metrics_config=METRICS_CONFIG,
                enabled_metrics=ENABLED_METRICS,
                inference_rounds=request.inference_rounds,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    online_agent_id: Optional[str] = None
    if request.online_mode:
        if not request.agent_id or not str(request.agent_id).strip():
            raise HTTPException(400, "在线评估需指定 agent_id")
        online_agent_id = str(request.agent_id).strip()
        agent_result = await session.execute(
            select(Agent).where(Agent.id == online_agent_id)
        )
        agent = agent_result.scalar_one_or_none()
        if not agent:
            log("ERROR", "EVAL_SERVICE", "Agent 不存在", agent_id=online_agent_id)
            raise HTTPException(404, f"Agent {online_agent_id} not found")
        if not agent.inference_config:
            raise HTTPException(400, f"Agent {online_agent_id} 未配置推理接口")

    # 校验评测 LLM 模型是否已配置（刷新缓存确保与数据库一致）
    await refresh_llm_models_cache(session)
    llm_model_key = (request.llm_model or "").strip()
    if llm_model_key.lower() == "auto":
        if not get_model_pool():
            raise HTTPException(400, "未配置任何可用的大模型，请先在「模型管理」中添加模型配置")
    else:
        cfg = _db_llm_models_cache.get(llm_model_key)
        if not cfg or not (cfg.get("api_key") or "").strip():
            raise HTTPException(
                400,
                f"评测模型 '{llm_model_key}' 未配置或缺少 API Key，请先在「模型管理」中配置",
            )

    # Determine model name
    if request.online_mode:
        first_agent = (
            await session.execute(select(Agent).where(Agent.id == online_agent_id))
        ).scalar_one_or_none()
        agent_name = first_agent.name if first_agent else "Unknown Agent"
    else:
        agent_name = "Offline Evaluation"

    model_name = get_model_display_name(llm_model_key)

    # Generate task ID
    task_id = f"task_{uuid.uuid4().hex}"

    # Create task
    task = EvaluationTask(
        id=task_id,
        dataset_id=request.dataset_id,
        models=json.dumps([model_name]),
        status=TaskStatus.PENDING,
        progress_current=0,
        progress_total=0,
        created_at=datetime.utcnow(),
        run_count=1,
        inference_rounds=effective_inference_rounds,
        llm_model=request.llm_model or "glm",
        agent_id=online_agent_id if request.online_mode else None,
        evaluation_mode=EVALUATION_MODE_ONLINE if request.online_mode else EVALUATION_MODE_OFFLINE,
        auto_scenario_score=request.auto_scenario_score if request.auto_scenario_score is not None else True,
        enable_process_consistency=request.enable_process_consistency if request.enable_process_consistency is not None else True,
        enable_stability=request.enable_stability if request.enable_stability is not None else True,
        parallel_inference=request.parallel_inference,
        use_bm25=request.use_bm25 if request.use_bm25 is not None else False,
        use_doc_library=request.use_doc_library if request.use_doc_library is not None else False,
        ontology_package=request.ontology_package,
        selected_metrics=json.dumps(offline_metrics if offline_metrics is not None else request.selected_metrics)
        if (offline_metrics is not None or request.selected_metrics)
        else None,
        selected_indices=json.dumps(request.selected_indices)
        if request.selected_indices
        else None,
        custom_scripts=json.dumps(request.custom_scripts)
        if request.custom_scripts
        else None,
        custom_metric_ids=json.dumps(request.custom_metric_ids)
        if request.custom_metric_ids
        else None,
    )
    # Pydantic 模型列（langfuse_source）需要在 __init__ 之后单独赋值
    if request.langfuse_source is not None:
        task.langfuse_source = request.langfuse_source.model_dump()

    session.add(task)
    await session.commit()

    log("INFO", "EVAL_SERVICE", "评估任务创建完成", task_id=task_id,
        model_name=model_name, inference_rounds=request.inference_rounds,
        llm_model=request.llm_model, selected_indices=request.selected_indices)

    # Launch background evaluation
    if request.online_mode:
        background_tasks.add_task(
            run_online_evaluation_task,
            task_id,
            request.dataset_id,
            online_agent_id,
            agent_name,
            request.inference_rounds,
            request.llm_model,
            request.selected_indices,
            request.selected_metrics,
            request.use_bm25,
            request.use_doc_library,
            request.custom_scripts,
            request.llm_max_tokens,
            request.llm_temperature,
            request.ontology_package,
            request.parallel_inference,
        )
    else:
        background_tasks.add_task(
            run_evaluation_task,
            task_id,
            request.dataset_id,
            agent_name,
            request.llm_model,
            request.selected_indices,
            request.selected_metrics,
            request.use_bm25,
            request.use_doc_library,
            request.custom_scripts,
            request.llm_max_tokens,
            request.llm_temperature,
            request.langfuse_source.model_dump() if request.langfuse_source else None,
            None,  # resume_from
            request.ontology_package,
        )

    return EvaluateResponse(task_uuid=task_id)
