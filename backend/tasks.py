import asyncio
import json
from datetime import datetime
from typing import Dict, List, Optional
from sqlalchemy import select, update

from database import async_session, EvaluationTask, EvaluationResult, EvaluationRound, Dataset, Agent, EvaluationScript, LLMModel
from evaluators import get_metric
from evaluators.custom_script_metric import CustomScriptMetric
from evaluators.llm_client import generate_evaluation_summary
from schemas import TaskStatus
from config import METRICS_CONFIG, ENABLED_METRICS, update_llm_models_cache, set_llm_call_override, clear_llm_call_override
from logger import log


# ⚠️ WARNING: SSE 流存储（进程内内存字典）
# 此字典仅存在于当前进程内存中，不支持多 Worker / 多实例部署。
# 若使用 uvicorn --workers > 1 或 K8s 多副本，SSE 事件将无法跨进程/实例送达，
# 导致前端监控页面收不到实时进度更新。
# 生产环境如需多 Worker，请改用 Redis Pub/Sub 或数据库轮询替代方案。
task_streams: Dict[str, asyncio.Queue] = {}


async def get_task_queue(task_id: str) -> asyncio.Queue:
    """Get or create a queue for task streaming."""
    if task_id not in task_streams:
        task_streams[task_id] = asyncio.Queue()
    return task_streams[task_id]


async def emit_event(task_id: str, event_type: str, data: dict):
    """Emit an SSE event to the task's queue."""
    queue = await get_task_queue(task_id)
    await queue.put({"event": event_type, "data": data})


async def refresh_llm_models_cache_task():
    """在后台任务中刷新LLM模型缓存"""
    try:
        async with async_session() as session:
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
            log("INFO", "TASK", "后台任务LLM模型缓存刷新完成", models=list(cache.keys()))
    except Exception as e:
        log("ERROR", "TASK", "后台任务刷新LLM模型缓存失败", error=str(e))


async def check_task_paused(task_id: str) -> bool:
    """Check if task is paused or cancelled.

    取消（CANCELLED）与暂停（PAUSED）都应让 pipeline 终止后续循环。取消
    走 status 字段触发（不写 paused_at，避免前端误判为可恢复的"已暂停"），
    详见 evaluation_pipeline.persist_pause_checkpoint 对取消任务不覆盖
    status 的处理。
    """
    async with async_session() as session:
        result = await session.execute(
            select(EvaluationTask).where(EvaluationTask.id == task_id)
        )
        task = result.scalar_one_or_none()
        if not task:
            return False
        if task.status == TaskStatus.CANCELLED:
            log("DEBUG", "TASK", "任务已取消，pipeline 退出", task_id=task_id)
            return True
        if task.paused_at:
            log("DEBUG", "TASK", "任务已暂停", task_id=task_id, paused_at=task.paused_at)
            return True
        return False


async def check_task_cancelled(task_id: str) -> bool:
    """Check if task is cancelled (status == CANCELLED)."""
    async with async_session() as session:
        result = await session.execute(
            select(EvaluationTask).where(EvaluationTask.id == task_id)
        )
        task = result.scalar_one_or_none()
        return bool(task and task.status == TaskStatus.CANCELLED)


# MARKER: offline evaluation function starts here
async def run_evaluation_task(task_id: str, dataset_id: str, model_name: str = "default", llm_model: str = "glm", selected_indices: list = None, selected_metrics: list = None, use_bm25: bool = False, use_doc_library: bool = False, custom_scripts: dict = None, llm_max_tokens: int = None, llm_temperature: float = None, langfuse_source: dict = None, resume_from: int = 0, ontology_package: str = None):
    """Main evaluation task - runs all metrics for single model answer.

    Args:
        task_id: 任务ID
        dataset_id: 数据集ID
        model_name: 模型名称
        llm_model: 评估使用的LLM模型 (glm)
        selected_indices: 要评估的评测项索引列表，None表示评估所有项
        selected_metrics: 要使用的评估因子列表，None表示使用所有可用因子
        use_bm25: 是否使用BM25算法计算忠实度
        use_doc_library: 是否使用文档库作为BM25评估的参考(仅当use_bm25=True时有效)
        llm_max_tokens: 临时覆盖最大输出tokens
        llm_temperature: 临时覆盖温度参数
        langfuse_source: 离线评估时从 Langfuse 获取答案的配置
        ontology_package: 本体包名称(使用指定目录的md文档进行BM25评估)
    """
    # 设置评测任务级别的 LLM 调用参数覆盖
    set_llm_call_override(max_tokens=llm_max_tokens, temperature=llm_temperature)

    from services.evaluation_pipeline import run_offline_evaluation_pipeline

    try:
        await run_offline_evaluation_pipeline(
                        task_id=task_id,
                        dataset_id=dataset_id,
                        model_name=model_name,
            llm_model=llm_model,
            selected_indices=selected_indices,
            selected_metrics=selected_metrics,
            use_bm25=use_bm25,
            use_doc_library=use_doc_library,
            custom_scripts=custom_scripts,
            resume_from=0,
            langfuse_source=langfuse_source,
            ontology_package=ontology_package,
        )
    finally:
        # 清除覆盖参数
        clear_llm_call_override()


async def evaluate_single(
    task_id: str,
    question_id: int,
    model: str,
    metric_name: str,
    threshold: float,
    question: str,
    answer: str,
    context: str,
    ground_truth: str,
    run_idx: int = 0,
    total_runs: int = 1,
    llm_model: str = "glm",
    use_bm25: bool = False,
    use_doc_library: bool = False,
    ontology_package: str = None,
    judging_mode: str = "",
    eval_prompt: str = "",
):
    """Evaluate a single model answer with a single metric.

    Args:
        run_idx: 当前运行索引（从0开始）
        total_runs: 总运行次数
        llm_model: 评估使用的LLM模型 (glm)
        use_bm25: 是否使用BM25算法计算忠实度
        use_doc_library: 是否使用文档库作为BM25评估的参考(仅当use_bm25=True时有效)
        ontology_package: 本体包名称(使用指定目录的md文档进行BM25评估)
        judging_mode: 评测模式（Ground_Truth评测/基于Prompt评测），仅对 correctness 有效
        eval_prompt: 基于Prompt评测模式下使用的评测提示词

    Returns:
        dict with 'score', 'reason', 'passed' or None on error
    """
    try:
        metric = get_metric(
            metric_name,
            model_key=llm_model,
            use_bm25=use_bm25,
            use_doc_library=use_doc_library,
            ontology_package=ontology_package,
            judging_mode=judging_mode,
            eval_prompt=eval_prompt,
        )
        
        run_label = f"[第{run_idx + 1}/{total_runs}次]" if total_runs > 1 else ""
        
        await emit_event(task_id, "cot_update", {
            "question_id": question_id,
            "model": model,
            "metric": metric_name,
            "cot_chunk": f"{run_label} 正在进行 {metric_name} 指标评估..."
        })
        
        # Run evaluation
        result, passed = await metric.measure(
            question=question,
            answer=answer,
            context=context,
            ground_truth=ground_truth,
            threshold=threshold
        )
        
        await emit_event(task_id, "cot_update", {
            "question_id": question_id,
            "model": model,
            "metric": metric_name,
            "cot_chunk": f"{run_label} {result.reason}"
        })
        
        await emit_event(task_id, "metric_result", {
            "question_id": question_id,
            "model": model,
            "metric": metric_name,
            "score": result.score,
            "passed": passed,
            "run_idx": run_idx
        })
        
        return {
            'score': result.score,
            'reason': result.reason,
            'passed': passed,
            'duration_ms': result.duration_ms
        }
        
    except Exception as e:
        await emit_event(task_id, "metric_error", {
            "question_id": question_id,
            "model": model,
            "metric": metric_name,
            "error": str(e),
            "run_idx": run_idx
        })
        return None


async def evaluate_with_script(
    task_id: str,
    question_id: int,
    model: str,
    script_metric: CustomScriptMetric,
    threshold: float,
    question: str,
    answer: str,
    context: str,
    ground_truth: str,
    run_idx: int = 0,
    total_runs: int = 1,
):
    """使用自定义脚本评估单个回答

    Args:
        task_id: 任务ID
        question_id: 问题ID
        model: 模型名称
        script_metric: 自定义脚本评估器实例
        threshold: 阈值
        question: 问题
        answer: 回答
        context: 上下文
        ground_truth: 标准答案
        run_idx: 运行索引
        total_runs: 总运行次数

    Returns:
        dict with 'score', 'reason', 'passed' or None on error
    """
    try:
        run_label = f"[第{run_idx + 1}/{total_runs}次]" if total_runs > 1 else ""

        await emit_event(task_id, "cot_update", {
            "question_id": question_id,
            "model": model,
            "metric": "correctness",  # 脚本评估的指标始终是correctness
            "cot_chunk": f"{run_label} 🐍 正在执行Python脚本 '{script_metric.script_name}' 进行结构化评估...",
            "script_eval": True,
            "script_name": script_metric.script_name
        })

        result, passed = await script_metric.measure(
            question=question,
            answer=answer,
            context=context,
            ground_truth=ground_truth,
            threshold=threshold
        )

        # 发送脚本评估详细结果（每个检查项）
        await emit_event(task_id, "cot_update", {
            "question_id": question_id,
            "model": model,
            "metric": "correctness",  # 脚本评估的指标始终是correctness
            "cot_chunk": f"{run_label} {result.reason}",
            "script_eval": True,
            "script_name": script_metric.script_name,
            "script_score": result.score
        })

        await emit_event(task_id, "metric_result", {
            "question_id": question_id,
            "model": model,
            "metric": "correctness",  # 脚本评估的指标始终是correctness
            "score": result.score,
            "passed": passed,
            "run_idx": run_idx
        })

        return {
            'score': result.score,
            'reason': result.reason,
            'passed': passed,
            'duration_ms': result.duration_ms
        }

    except Exception as e:
        await emit_event(task_id, "metric_error", {
            "question_id": question_id,
            "model": model,
            "metric": "correctness",  # 脚本评估的指标始终是correctness
            "error": str(e),
            "run_idx": run_idx
        })
        return None


async def run_online_evaluation_task(
    task_id: str,
    dataset_id: str,
    agent_id: str,
    model_name: str = "default",
    inference_rounds: int = 1,
    llm_model: str = "glm",
    selected_indices: list = None,
    selected_metrics: list = None,
    use_bm25: bool = False,
    use_doc_library: bool = False,
    custom_scripts: dict = None,
    llm_max_tokens: int = None,
    llm_temperature: float = None,
    ontology_package: str = None,
    parallel_inference: bool = True,
):
    """在线评估任务入口 — 委托 P1 Pipeline（推理 + AgentRun + 指标评估）。"""
    # 设置评测任务级别的 LLM 调用参数覆盖
    set_llm_call_override(max_tokens=llm_max_tokens, temperature=llm_temperature)

    from services.evaluation_pipeline import run_online_evaluation_pipeline

    try:
        await run_online_evaluation_pipeline(
            task_id=task_id,
            dataset_id=dataset_id,
            agent_id=agent_id,
            model_name=model_name,
            inference_rounds=inference_rounds,
            llm_model=llm_model,
            selected_indices=selected_indices,
            selected_metrics=selected_metrics,
            use_bm25=use_bm25,
            use_doc_library=use_doc_library,
            custom_scripts=custom_scripts,
            ontology_package=ontology_package,
            parallel_inference=parallel_inference,
        )
    finally:
        # 清除覆盖参数
        clear_llm_call_override()


async def resume_evaluation_task(task_id: str, resume_from: int = 0, use_bm25: bool = False, use_doc_library: bool = False):
    """恢复暂停任务 — 委托 H-04 统一 Pipeline（选项从 task 行读取）。"""
    from services.evaluation_pipeline import resume_evaluation_pipeline

    await resume_evaluation_pipeline(task_id)
