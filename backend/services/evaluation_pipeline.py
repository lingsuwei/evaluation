"""
评测任务 Pipeline 编排（P1）：在线推理 + AgentRun 落库 + 指标评估入口。
"""
from __future__ import annotations

import time

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    EVAL_LANGFUSE_WRAPPER_ENABLED,
    LANGFUSE_WRITE_PROJECT,
    METRICS_CONFIG,
    ONTO_REF_MAX_CHARS,
    ONTO_REF_TOP_K,
)
from database import (
    Agent,
    AgentRun,
    CustomMetric,
    Dataset,
    EvaluationResult,
    EvaluationRound,
    EvaluationScript,
    EvaluationTask,
    async_session,
)
from evaluators.custom_script_metric import CustomScriptMetric
from evaluators.deepeval_metrics.deepeval_suite import SPEC_BY_KEY
from evaluators.registry import DEEPEVAL_METRIC_KEYS, get_metric
from schemas import TaskStatus
from services.eval_metric_sync_service import sync_evaluation_result_to_run_metrics

# 每题每指标固定 Judge 1 次；输出稳定性由 inference_rounds（Agent 多轮推理）承担。
JUDGE_RUN_COUNT = 1

# === 并发控制信号量 ===
# 题目级并发：控制同时处理的题目数（Agent 支持 30 并发，5 题 × 3 轮 = 15，留 50% 余量）
_QUESTION_SEMAPHORE = asyncio.Semaphore(5)
# 推理级并发：控制同时进行的推理调用总数（题目并发 × 轮次并发），与 Agent 容量挂钩
_INFERENCE_SEMAPHORE = asyncio.Semaphore(15)
# 进度计数器锁：保护 progress / quality_progress 的自增操作
_progress_lock = asyncio.Lock()
from services.agent_run_service import (
    complete_agent_run,
    create_agent_run_started,
    get_agent_run_for_question,
)
from services.dataset_items import get_item_stable_ids, normalize_dataset_items
from services.fact_ingest_schedule import schedule_fact_ingest_after_run
from services.inference_service import InferenceService
from services.langfuse_tracing import TraceContext
from services.system_config_service import get_runtime_threshold
from logger import log


async def emit_task_phase_started(
    emit_event,
    task_id: str,
    *,
    question_count: int,
    metrics_count: int,
    progress_total: int,
    inference_rounds: int = 1,
    mode: str = "online",
    quality_total: int = 0,
    scenario_total: int = 0,
) -> None:
    await emit_event(
        task_id,
        "task_phase",
        {
            "phase": "started",
            "mode": mode,
            "question_count": question_count,
            "metrics_count": metrics_count,
            "progress_total": progress_total,
            "inference_rounds": inference_rounds,
            "quality_total": quality_total,
            "scenario_total": scenario_total,
        },
    )


async def emit_progress_tick(
    emit_event,
    task_id: str,
    *,
    current: int,
    total: int,
    phase: str,
    question_id: Optional[int] = None,
    metric: Optional[str] = None,
    run_index: Optional[int] = None,
    quality_current: Optional[int] = None,
    quality_total: Optional[int] = None,
    scenario_current: Optional[int] = None,
    scenario_total: Optional[int] = None,
) -> None:
    payload = {
        "current": current,
        "total": total,
        "phase": phase,
        "question_id": question_id,
        "metric": metric,
        "run_index": run_index,
    }
    if quality_current is not None:
        payload["quality_current"] = quality_current
    if quality_total is not None:
        payload["quality_total"] = quality_total
    if scenario_current is not None:
        payload["scenario_current"] = scenario_current
    if scenario_total is not None:
        payload["scenario_total"] = scenario_total
    await emit_event(task_id, "progress_tick", payload)


async def _run_auto_scenario_scoring(task_id: str, emit_event) -> None:
    """E-1：评测主流程结束后按配置自动场景子步骤打分。"""
    try:
        async with async_session() as session:
            from services.scenario_post_eval_service import run_post_eval_scenario_if_enabled

            rep = await run_post_eval_scenario_if_enabled(session, task_id, emit_event=emit_event)
            await session.commit()
            if rep and not rep.get("skipped"):
                await emit_event(task_id, "scenario_scored", rep)
    except Exception as e:
        log(
            "ERROR",
            "TASK",
            "场景子步骤自动打分失败",
            task_id=task_id,
            error=str(e),
        )
        await emit_event(task_id, "scenario_score_error", {"error": str(e)})


async def _run_offline_process_eval(
    task_id: str,
    emit_event,
    *,
    progress_base: int = 0,
    progress_total: int = 0,
    scenario_total: int = 0,
    quality_progress: int = 0,
    quality_total: int = 0,
) -> None:
    """E-1-offline：离线任务完成后从 Langfuse 拉历史 trace 跑过程评估。"""
    try:
        async with async_session() as session:
            from database import EvaluationTask

            task = await session.get(EvaluationTask, task_id)
            # 门控：过程一致性 或 场景子步骤 任一启用即需物化 langfuse AgentRun
            # enable_process_consistency=False 时仍要为 auto_scenario_score 物化，
            # 否则后续 _run_auto_scenario_scoring 无 AgentRun 可评 → 场景卡片显示灰横线
            enable_pc = bool(getattr(task, "enable_process_consistency", True)) if task else True
            enable_scn = bool(getattr(task, "auto_scenario_score", True)) if task else True
            if task and not enable_pc and not enable_scn:
                await emit_event(
                    task_id,
                    "process_eval_skipped",
                    {"reason": "enable_process_consistency and auto_scenario_score both disabled"},
                )
                return
            from services.offline_process_eval_service import run_offline_process_eval

            async def _progress_cb(question_id: int, done: bool) -> None:
                if not progress_total:
                    return
                current = progress_base + (question_id + (1 if done else 0))
                scenario_current = question_id + (1 if done else 0)
                await emit_event(task_id, "progress_tick", {
                    "current": min(current, progress_total),
                    "total": progress_total,
                    "phase": "process_eval",
                    "question_id": question_id,
                    "scenario_current": scenario_current,
                    "scenario_total": scenario_total,
                    "quality_current": quality_progress,
                    "quality_total": quality_total,
                })

            rep = await run_offline_process_eval(
                session, task_id, progress_cb=_progress_cb,
                enable_checkpoint=enable_pc,
            )
            await session.commit()
            if rep is None:
                return
            if rep.get("skipped"):
                await emit_event(task_id, "process_eval_skipped", rep)
            else:
                await emit_event(task_id, "process_eval_done", rep)
    except Exception as e:
        log(
            "ERROR",
            "TASK",
            "离线过程评估失败",
            task_id=task_id,
            error=str(e),
        )
        await emit_event(task_id, "process_eval_error", {"error": str(e)})


def compute_online_progress_total(
    question_count: int,
    metrics_to_run: List[str],
    inference_rounds: int = 1,
) -> int:
    """在线任务总步数：每题 K 次推理 + 每轮 runtime 指标 + 质量指标 Judge。

    所有 LLM 质量指标（correctness / faithfulness / answer_relevancy）
    对每轮推理答案都 Judge 1 次（多轮稳定性评估）。
    """
    from evaluators.runtime_metrics import is_runtime_metric

    if question_count <= 0 or not metrics_to_run:
        return 0
    runtime_n = sum(1 for m in metrics_to_run if is_runtime_metric(m))
    llm_metrics = [m for m in metrics_to_run if not is_runtime_metric(m)]
    # 所有 LLM 质量指标：每轮 Judge
    quality_judge = len(llm_metrics) * inference_rounds
    per_q = (
        inference_rounds
        + inference_rounds * runtime_n
        + quality_judge
    )
    return question_count * per_q


def compute_offline_progress_total(
    question_count: int,
    metrics_to_run: List[str],
    *,
    has_process_eval: bool = False,
) -> int:
    """离线任务总步数：每题每质量指标 1 次 Judge（不含 runtime）。

    has_process_eval=True 时，每题额外 +1 步（过程评估：trace 拉取 + 场景子步骤打分）。
    """
    from evaluators.runtime_metrics import is_runtime_metric

    quality_n = sum(1 for m in metrics_to_run if not is_runtime_metric(m))
    if question_count <= 0 or quality_n <= 0:
        return 0
    base = question_count * quality_n * JUDGE_RUN_COUNT
    if has_process_eval:
        base += question_count
    return base


async def _run_custom_metric(
    cm_def: dict,
    question: str,
    model_answer: str,
    context: Any,
    ground_truth: str,
    llm_model: str,
    llm_model_name: str,
) -> Optional[Dict[str, Any]]:
    """执行自定义指标（geval/dag/code 三档），返回 eval_result dict。

    kind=code：确定性沙箱执行用户代码，无 LLM 调用
    kind=geval：自然语言评判标准，由 LLM 打分
    kind=dag：决策树，由 DeepEval DAGMetric 串行判定
    """
    import time
    kind = cm_def.get("kind", "geval")
    name = cm_def.get("name", "自定义指标")
    t0 = time.time()

    try:
        if kind == "code":
            return await asyncio.to_thread(
                _run_code_metric_sync, cm_def, question, model_answer, context, ground_truth
            )
        # geval / dag 需要 DeepEval + LLM，受全局信号量控制
        from evaluators.llm_client import _LLM_CALL_SEMAPHORE
        async with _LLM_CALL_SEMAPHORE:
            return await asyncio.to_thread(
                _run_deepeval_custom_sync, cm_def, question, model_answer,
                context, ground_truth, llm_model,
            )
    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        return {"score": 0.0, "reason": f"{name}评估失败：{e}", "duration_ms": duration_ms}


def _run_code_metric_sync(
    cm_def: dict,
    question: str,
    model_answer: str,
    context: Any,
    ground_truth: str,
) -> Dict[str, Any]:
    """code 档：受限沙箱执行用户代码（确定性，无 LLM）。"""
    import time
    from evaluators.deepeval_metrics.code_metric import run_code_metric

    t0 = time.time()
    tc = {
        "input": question,
        "actual_output": model_answer,
        "expected_output": ground_truth or "",
        "context": [context] if context else [],
        "retrieval_context": [context] if context else [],
    }
    score, reason = run_code_metric(cm_def.get("code", ""), tc)
    duration_ms = int((time.time() - t0) * 1000)
    return {"score": float(score), "reason": str(reason), "duration_ms": duration_ms}


def _run_deepeval_custom_sync(
    cm_def: dict,
    question: str,
    model_answer: str,
    context: Any,
    ground_truth: str,
    llm_model: str,
) -> Dict[str, Any]:
    """geval/dag 档：构造 DeepEval 指标 + LLM 打分。"""
    import time

    from config import get_llm_config
    from evaluators.deepeval_metrics.deepeval_model import build_deepeval_model

    t0 = time.time()

    # 获取 LLM 配置
    model_cfg = get_llm_config(llm_model) if llm_model else None
    model_name = model_cfg.get("model", "gpt-4o") if model_cfg else "gpt-4o"
    base_url = model_cfg.get("base_url") if model_cfg else None
    api_key = model_cfg.get("api_key") if model_cfg else None
    temperature = model_cfg.get("temperature", 0.0) if model_cfg else 0.0
    provider = model_cfg.get("provider", "openai") if model_cfg else "openai"

    deepeval_model = build_deepeval_model(
        model=model_name, base_url=base_url, api_key=api_key,
        temperature=temperature, provider=provider,
    )

    kind = cm_def.get("kind", "geval")

    # 构造 LLMTestCase
    from deepeval.test_case import LLMTestCase
    tc = LLMTestCase(
        input=question,
        actual_output=model_answer,
        expected_output=ground_truth or None,
        context=[context] if context else None,
        retrieval_context=[context] if context else None,
    )

    if kind == "geval":
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCaseParams

        params = cm_def.get("params") or ["ACTUAL_OUTPUT"]
        eval_params = []
        for p in params:
            try:
                eval_params.append(getattr(LLMTestCaseParams, p))
            except AttributeError:
                continue
        if not eval_params:
            eval_params = [LLMTestCaseParams.ACTUAL_OUTPUT]

        metric = GEval(
            name=cm_def.get("name", "custom"),
            criteria=cm_def.get("criteria", ""),
            evaluation_params=eval_params,
            model=deepeval_model,
            async_mode=False,
        )
    elif kind == "dag":
        from deepeval.metrics import DAGMetric
        from deepeval.metrics.dag import DeepAcyclicGraph

        dag_def = cm_def.get("dag")
        if not dag_def:
            duration_ms = int((time.time() - t0) * 1000)
            return {"score": 0.0, "reason": "DAG 定义为空", "duration_ms": duration_ms}
        metric = DAGMetric(
            name=cm_def.get("name", "custom"),
            dag=DeepAcyclicGraph.from_dict(dag_def),
            model=deepeval_model,
            async_mode=False,
        )
    else:
        duration_ms = int((time.time() - t0) * 1000)
        return {"score": 0.0, "reason": f"不支持的指标类型: {kind}", "duration_ms": duration_ms}

    metric.measure(tc)
    score = float(metric.score)
    reason = str(getattr(metric, "reason", ""))[:2000]
    duration_ms = int((time.time() - t0) * 1000)
    return {"score": score, "reason": reason, "duration_ms": duration_ms}


async def _judge_quality_metric_once(
    *,
    task_id: str,
    question_id: int,
    model_name: str,
    metric_name: str,
    threshold: float,
    question: str,
    model_answer: str,
    context: Any,
    ground_truth: str,
    llm_model: str,
    use_bm25: bool,
    use_doc_library: bool,
    use_script_eval: bool,
    custom_script_metric: Any,
    evaluate_single: Any,
    evaluate_with_script: Any,
    ontology_package: str = None,
    judging_mode: str = "",
    eval_prompt: str = "",
) -> Optional[Dict[str, Any]]:
    """对单题单指标执行一次 Judge 或脚本评估。"""
    # 忠诚度降级检测：无本体包且无参考内容时触发兜底逻辑
    if (
        metric_name == "faithfulness"
        and not ontology_package
        and not context
        and not ground_truth
    ):
        if use_bm25 and use_doc_library:
            log("WARN", "EVALUATOR",
                "忠诚度评估降级：无本体包且知识库为空，将采用中性降级评分（0.5分）",
                task_id=task_id, question_id=question_id, metric=metric_name,
                fallback="bm25_library_empty")
        elif use_bm25:
            log("WARN", "EVALUATOR",
                "忠诚度评估降级：无参考内容或标准答案，将采用中性降级评分（0.5分）",
                task_id=task_id, question_id=question_id, metric=metric_name,
                fallback="bm25_no_reference")
        else:
            log("WARN", "EVALUATOR",
                "忠诚度评估降级：无参考内容，将切换为自包含事实一致性评估（基于模型自身知识）",
                task_id=task_id, question_id=question_id, metric=metric_name,
                fallback="llm_no_reference")
    if use_script_eval and custom_script_metric:
        return await evaluate_with_script(
            task_id,
            question_id,
            model_name,
            custom_script_metric,
            threshold,
            question,
            model_answer,
            context,
            ground_truth,
        )
    return await evaluate_single(
        task_id,
        question_id,
        model_name,
        metric_name,
        threshold,
        question,
        model_answer,
        context,
        ground_truth,
        llm_model=llm_model,
        use_bm25=use_bm25,
        use_doc_library=use_doc_library,
        ontology_package=ontology_package,
        judging_mode=judging_mode,
        eval_prompt=eval_prompt,
    )


async def _persist_quality_evaluation_result(
    session: AsyncSession,
    *,
    task_id: str,
    question_id: int,
    metric_name: str,
    threshold: float,
    eval_result: Optional[Dict[str, Any]],
    use_script_eval: bool,
    script_id: Optional[str],
    scripts_cache: Dict[str, Any],
    question: str,
    ground_truth: str,
    model_answer: str,
    agent_name: str,
    llm_model: str,
    llm_model_name: str,
    inference_duration_ms: Optional[int] = None,
    agent_run_id: Optional[str] = None,
    eval_method_override: Optional[str] = None,
) -> None:
    """落库 EvaluationResult 与单条 EvaluationRound（Judge 固定 1 次）。"""
    if eval_method_override:
        eval_method = eval_method_override
    else:
        eval_method = "script" if use_script_eval else "llm"
    script_name = (
        scripts_cache[script_id].name if use_script_eval and script_id else None
    )
    if eval_result:
        raw_score = eval_result.get("score")
        reason = eval_result.get("reason") or ""
        duration_ms = int(eval_result.get("duration_ms") or 0)
        if raw_score is None:
            # score=None 表示「已跳过」(DeepEval 指标缺输入或缺必填参数),不参与达标统计
            score = None
            passed = False
            combined_reason = f"[已跳过] {reason}" if reason else "[已跳过]"
        else:
            score = float(raw_score)
            passed = score >= threshold
            eval_method_tag = "🐍 Python脚本评估" if use_script_eval else "LLM评估"
            combined_reason = f"[{eval_method_tag}] 分数: {score:.3f}"
    else:
        score = 0.0
        reason = ""
        duration_ms = 0
        passed = False
        combined_reason = "[评估失败] 无法获取有效评分"

    row = EvaluationResult(
        task_id=task_id,
        question_id=question_id,
        model=llm_model_name,
        metric_name=metric_name,
        score=round(score, 3) if score is not None else None,
        threshold=threshold,
        passed=passed,
        duration_ms=0,
        cot=combined_reason,
        question=question,
        ground_truth=ground_truth,
        agent_name=agent_name,
        eval_method=eval_method,
        script_name=script_name,
        agent_answer=model_answer,
        avg_score=round(score, 3) if score is not None else None,
        eval_model=llm_model,
        eval_rounds=JUDGE_RUN_COUNT if eval_result else 0,
        inference_duration_ms=inference_duration_ms,
        agent_run_id=agent_run_id,
    )
    session.add(row)
    await session.flush()
    if eval_result:
        session.add(
            EvaluationRound(
                result_id=row.id,
                round_num=1,
                score=score,
                reason=reason,
                duration_ms=duration_ms,
            )
        )
    await sync_evaluation_result_to_run_metrics(session, row)


async def persist_pause_checkpoint(
    session: AsyncSession,
    task_id: str,
    question_id: int,
) -> None:
    """记录暂停/取消退出时的断点。

    - 暂停：写 resume_from + paused_at + status=PAUSED，供后续 resume 续跑。
    - 取消：保持 cancel API 写入的 status=CANCELLED，不覆盖为 PAUSED，也不
      写 paused_at（避免前端误判为可恢复的"已暂停"）。仅写 resume_from 留痕。
    """
    result = await session.execute(
        select(EvaluationTask).where(EvaluationTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if task and task.status == TaskStatus.CANCELLED:
        await session.execute(
            update(EvaluationTask)
            .where(EvaluationTask.id == task_id)
            .values(resume_from=question_id)
        )
        return
    await session.execute(
        update(EvaluationTask)
        .where(EvaluationTask.id == task_id)
        .values(
            resume_from=question_id,
            paused_at=datetime.utcnow(),
            status=TaskStatus.PAUSED,
        )
    )


def parse_task_agent_ids(task: EvaluationTask) -> List[str]:
    """从任务行解析 Agent ID 列表（兼容历史多版本 JSON；新任务仅 agent_id）。"""
    raw = getattr(task, "agent_ids", None)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                ids = [str(x).strip() for x in parsed if str(x).strip()]
                if ids:
                    return ids
        except json.JSONDecodeError:
            pass
    if task.agent_id:
        return [str(task.agent_id).strip()]
    return []


async def resolve_task_agent_name(session: AsyncSession, task_id: str) -> str:
    """取任务关联的被测 Agent 展示名（用于评测摘要 prompt 的"被测 Agent"字段）。

    多 Agent 取首个；离线无 Agent 时返回"离线评估"。
    """
    task = await session.get(EvaluationTask, task_id)
    if not task:
        return "未知对象"
    ids = parse_task_agent_ids(task)
    if not ids:
        return "离线评估"
    agent = await session.get(Agent, ids[0])
    return agent.name if agent else ids[0][:8]


def _build_fail_examples(results_list: list) -> list:
    """从评测结果列表构造 top-N fail 题目摘要，供 LLM summary prompt 引用。

    每题取最差指标的代表行（含 eval_reason），按得分升序，上限 10 条。
    """
    from collections import defaultdict

    if not results_list:
        return []
    fail_rows = [r for r in results_list if not r.get("passed")]
    if not fail_rows:
        return []
    worst_by_q: dict = {}
    for r in fail_rows:
        q = r.get("question_id")
        if q is None:
            continue
        cur = worst_by_q.get(q)
        score = r.get("score")
        if cur is None or (score is not None and (cur.get("score") is None or score < cur["score"])):
            worst_by_q[q] = r
    examples = sorted(
        worst_by_q.values(),
        key=lambda x: x.get("score") if x.get("score") is not None else float("inf"),
    )
    return examples[:10]


def extract_question_fields(item: Dict[str, Any]) -> Tuple[str, Any, str, str, str]:
    """提取数据项中的问题、上下文、标准答案、评测模式、提示词。

    返回: (question, context, ground_truth, judging_mode, eval_prompt)
    - judging_mode: 空值或"Ground_Truth评测"表示Ground_Truth评测；"基于Prompt评测"表示基于Prompt评测
    - eval_prompt: 数据项中的 prompt 字段，用于基于Prompt评测模式
    """
    question = (
        item.get("question")
        or item.get("input")
        or item.get("query")
        or ""
    )
    if not isinstance(question, str):
        question = str(question) if question is not None else ""
    question = question.strip()
    context = item.get("context")
    ground_truth = item.get("ground_truth") or item.get("expected_output", "")
    judging_mode = item.get("judging_mode") or ""
    eval_prompt = item.get("prompt") or ""
    return question, context, ground_truth, judging_mode, eval_prompt


def _char_bigrams(text: str) -> set:
    """字符 bigram 集合(无依赖中文检索:对中英文都有效,优于空格分词)。

    用于本体包检索时按问题打分,确定性地挑选相关文档,避免把整包塞给 judge。
    """
    s = "".join((text or "").split())  # 去除所有空白,得到连续字符序列
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _load_ontology_reference(
    ontology_package: Optional[str],
    question: Optional[str],
    top_k: Optional[int] = None,
    max_chars: Optional[int] = None,
) -> Optional[str]:
    """按问题从本体包检索 top-K 相关文档作为参照源(非全量,省 token + 保质量)。

    优先级:本体包(配置且检索到相关文档)→ 数据集 context(由调用处回退)。
    检索用字符 bigram 重叠度打分(中英文通用、无依赖),按 (分数降序, 文件名序) 确定排序,
    取 top_K 拼接并按 max_chars 截断。无本体包/无问题/检索无命中 → 返回 None
    (下游回退数据集 item 的 context)。复用 document_loader 全局单例(按 docs_dir 缓存 +
    文件名排序),使 faithfulness(LLM 模式)/幻觉等指标无需各自感知本体包,
    且同题跨轮检索结果确定(不引入评估不稳定)。
    top_k/max_chars 默认取 config.ONTO_REF_TOP_K / ONTO_REF_MAX_CHARS(可经环境变量调)。
    """
    top_k = top_k if top_k is not None else ONTO_REF_TOP_K
    max_chars = max_chars if max_chars is not None else ONTO_REF_MAX_CHARS
    if not ontology_package or not question:
        return None
    import os as _os
    from evaluators.document_loader import get_document_loader

    base_dir = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
        "md",
        ontology_package,
    )
    doc_loader = get_document_loader(docs_dir=base_dir)
    docs = doc_loader.get_all_documents()
    if not docs:
        return None
    q_grams = _char_bigrams(question)
    if not q_grams:
        return None
    scored = []  # (score, idx, content):idx 为文档在已排序列表中的下标,作确定性 tie-break
    for idx, content in enumerate(docs):
        d_grams = _char_bigrams(content)
        overlap = len(q_grams & d_grams)
        if overlap > 0:
            score = overlap / len(q_grams)  # 归一化,避免长文档天然占优
            scored.append((score, idx, content))
    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], x[1]))
    out, total = [], 0
    for _score, _idx, content in scored[:top_k]:
        if total >= max_chars:
            break
        piece = content[: max(0, max_chars - total)]
        if piece:
            out.append(piece)
            total += len(piece)
    return "\n\n".join(out) if out else None


@dataclass
class OnlineInferenceResult:
    model_answer: str
    inference_duration_ms: Optional[int]
    agent_run_id: Optional[str]
    inference_error: Optional[str]
    reused: bool = False
    tokens_total: Optional[int] = None


async def load_completed_metrics(
    session: AsyncSession,
    task_id: str,
    question_id: int,
    agent_id: Optional[str] = None,
    agent_run_id: Optional[str] = None,
) -> set[str]:
    stmt = select(EvaluationResult.metric_name).where(
        EvaluationResult.task_id == task_id,
        EvaluationResult.question_id == question_id,
    )
    if agent_run_id:
        stmt = stmt.where(EvaluationResult.agent_run_id == agent_run_id)
    elif agent_id:
        run_subq = select(AgentRun.id).where(
            AgentRun.task_id == task_id,
            AgentRun.question_id == question_id,
            AgentRun.agent_id == agent_id,
        )
        stmt = stmt.where(EvaluationResult.agent_run_id.in_(run_subq))
    result = await session.execute(stmt)
    return set(result.scalars().all())


async def is_question_online_evaluation_complete(
    session: AsyncSession,
    *,
    task_id: str,
    question_id: int,
    agent_id: str,
    metrics_to_run: List[str],
    inference_rounds: int,
) -> bool:
    """同题 K 轮推理 + 各轮 runtime + correctness 各轮 Judge + 主 Run 其余 LLM 指标均已完成时可跳过。"""
    from evaluators.runtime_metrics import is_runtime_metric

    if not metrics_to_run:
        return True
    runtime_metrics = [m for m in metrics_to_run if is_runtime_metric(m)]
    llm_metrics = [m for m in metrics_to_run if not is_runtime_metric(m)]
    # correctness 对每轮推理答案都 Judge（多轮稳定性）；其余 LLM 指标仅代表轮 Judge
    per_round_metrics = runtime_metrics + [m for m in llm_metrics if m == "correctness"]
    primary_only_llm = [m for m in llm_metrics if m != "correctness"]
    rounds = max(1, inference_rounds)

    for run_index in range(rounds):
        run = await get_agent_run_for_question(
            session,
            task_id=task_id,
            question_id=question_id,
            run_index=run_index,
            agent_id=agent_id,
        )
        if not run or run.status not in ("success", "failed"):
            return False
        if per_round_metrics:
            done = await load_completed_metrics(
                session, task_id, question_id, agent_run_id=run.id
            )
            if not all(m in done for m in per_round_metrics):
                return False

    if primary_only_llm:
        primary = await get_agent_run_for_question(
            session,
            task_id=task_id,
            question_id=question_id,
            run_index=0,
            agent_id=agent_id,
        )
        if not primary:
            return False
        done = await load_completed_metrics(
            session, task_id, question_id, agent_run_id=primary.id
        )
        if not all(m in done for m in primary_only_llm):
            return False
    return True


async def load_question_answer_from_results(
    session: AsyncSession,
    task_id: str,
    question_id: int,
    agent_run_id: Optional[str] = None,
) -> Tuple[Optional[str], int]:
    stmt = select(EvaluationResult).where(
        EvaluationResult.task_id == task_id,
        EvaluationResult.question_id == question_id,
    )
    if agent_run_id:
        stmt = stmt.where(EvaluationResult.agent_run_id == agent_run_id)
    stmt = stmt.limit(1)
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if not row:
        return None, 0
    return row.agent_answer, int(row.inference_duration_ms or 0)


async def execute_online_inference_with_run(
    session: AsyncSession,
    *,
    task_id: str,
    dataset_id: str,
    question_id: int,
    run_index: int,
    question: str,
    ground_truth: str,
    agent: Agent,
    inference_config: Dict[str, Any],
    emit_inference_events: bool = True,
    dataset_item_id: Optional[str] = None,
    question_key: Optional[str] = None,
    scenario_key: Optional[str] = None,
    # Phase 4: 会话模拟器(多轮对话)
    scenario: Optional[str] = None,
    user_description: Optional[str] = None,
    expected_outcome: Optional[str] = None,
    eval_model_key: Optional[str] = None,
) -> OnlineInferenceResult:
    """Pipeline Step: 推理 + AgentRun 起止落库；恢复时复用已有 Run（DEF-P1-02）。

    Phase 4: 若 scenario 非空,用会话模拟器(评测模型扮演 user)生成多轮对话,
    turns 存 AgentRun.eval_context.conversation_turns,最后一个 assistant 回复作为 model_answer。
    """
    from tasks import emit_event

    existing_run = await get_agent_run_for_question(
        session,
        task_id=task_id,
        question_id=question_id,
        run_index=run_index,
        agent_id=agent.id,
    )
    if existing_run and existing_run.status in ("success", "failed"):
        cached_answer, cached_duration = await load_question_answer_from_results(
            session, task_id, question_id, agent_run_id=existing_run.id
        )
        if cached_answer is not None:
            err = existing_run.error_message if existing_run.status == "failed" else None
            cached_tokens = getattr(existing_run, "tokens_total", None)
            return OnlineInferenceResult(
                model_answer=cached_answer,
                inference_duration_ms=cached_duration,
                agent_run_id=existing_run.id,
                inference_error=err,
                reused=True,
                tokens_total=int(cached_tokens) if cached_tokens is not None else None,
            )
        agent_run = existing_run
    else:
        agent_run = None

    if emit_inference_events and agent_run is None:
        await emit_event(
            task_id,
            "inference_start",
            {
                "question_id": question_id,
                "run_index": run_index,
                "question": question[:100] + "..." if len(question) > 100 else question,
            },
        )

    if agent_run is None:
        agent_run = await create_agent_run_started(
            session,
            task_id=task_id,
            dataset_id=dataset_id,
            question_id=question_id,
            run_index=run_index,
            agent=agent,
            inference_config=inference_config,
            langfuse_project=(
                (getattr(agent, "langfuse_project", None) or "").strip()
                or (LANGFUSE_WRITE_PROJECT or "").strip()
                or None
            ),
            dataset_item_id=dataset_item_id,
            question_key=question_key,
            scenario_key=scenario_key,
            question=question,
            ground_truth=ground_truth,
        )
        await session.commit()
    elif agent_run.status in ("success", "failed"):
        agent_run.status = "running"
        await session.flush()

    inference_start = datetime.utcnow()
    heartbeat_task: Optional[asyncio.Task] = None
    if emit_inference_events:

        async def _inference_heartbeat() -> None:
            t0 = asyncio.get_event_loop().time()
            while True:
                await asyncio.sleep(30)
                elapsed_s = int(asyncio.get_event_loop().time() - t0)
                await emit_event(
                    task_id,
                    "inference_heartbeat",
                    {
                        "question_id": question_id,
                        "run_index": run_index,
                        "elapsed_s": elapsed_s,
                    },
                )

        heartbeat_task = asyncio.create_task(_inference_heartbeat())

    trace_ctx: Optional[TraceContext] = None
    if EVAL_LANGFUSE_WRAPPER_ENABLED:
        trace_ctx = TraceContext(
            task_id=task_id,
            question_id=question_id,
            run_index=run_index,
            agent_id=agent.id,
            logical_agent_id=agent.logical_agent_id,
            agent_version=agent.version,
            dataset_id=dataset_id,
            agent_name=agent.name,
        )
    try:
        # Phase 4: 若有 scenario,走会话模拟器(多轮)而非单轮 call_inference
        _lf_project = (
            (getattr(agent_run, "langfuse_project", None) or "").strip()
            or (getattr(agent, "langfuse_project", None) or "").strip()
            or (LANGFUSE_WRITE_PROJECT or "").strip()
            or None
        )
        if scenario and scenario.strip():
            from services.conversation_simulator_service import (
                simulate_conversation,
                turns_to_eval_context_key,
            )

            sim_turns = await simulate_conversation(
                scenario=scenario,
                agent_config=inference_config,
                eval_model_key=eval_model_key or "glm",
                user_description=user_description,
                expected_outcome=expected_outcome,
                max_turns=4,
                agent_run_id=agent_run.id,
                langfuse_project=_lf_project,
                logical_agent_id=(agent.logical_agent_id or "").strip() or None,
                agent_version=(agent.version or "").strip() or None,
                agent_id=agent.id,
            )
            # 存 turns 到 AgentRun.eval_context.conversation_turns
            if sim_turns:
                _eval_ctx = dict(agent_run.eval_context or {})
                _eval_ctx["conversation_turns"] = turns_to_eval_context_key(sim_turns)
                agent_run.eval_context = _eval_ctx
                await session.flush()
            # 最后一个 assistant 回复作为 model_answer
            last_assistant = ""
            for _t in reversed(sim_turns):
                if _t.get("role") == "assistant" and _t.get("content"):
                    last_assistant = _t["content"]
                    break
            inference_result = {
                "model_answer": last_assistant,
                "error": None if sim_turns else "会话模拟器未生成有效回合",
            }
        else:
            inference_result = await InferenceService.call_inference(
                question=question,
                config=inference_config,
                ground_truth=ground_truth,
                trace_context=trace_ctx,
                agent_run_id=agent_run.id,
                langfuse_project=_lf_project,
                logical_agent_id=(agent.logical_agent_id or "").strip() or None,
                agent_version=(agent.version or "").strip() or None,
                task_id=task_id,
                agent_id=agent.id,
            )
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
    inference_end = datetime.utcnow()
    inference_duration_ms: Optional[int] = None
    agent_duration_ms = inference_result.get("duration_ms")
    if agent_duration_ms is not None:
        try:
            inference_duration_ms = int(agent_duration_ms)
        except (TypeError, ValueError):
            pass
    tokens_total = inference_result.get("tokens_total")
    if tokens_total is not None:
        try:
            tokens_total = int(tokens_total)
        except (TypeError, ValueError):
            tokens_total = None
    tokens_llm = inference_result.get("tokens_llm")
    if tokens_llm is not None:
        try:
            tokens_llm = int(tokens_llm)
        except (TypeError, ValueError):
            tokens_llm = None
    tokens_tool = inference_result.get("tokens_tool")
    if tokens_tool is not None:
        try:
            tokens_tool = int(tokens_tool)
        except (TypeError, ValueError):
            tokens_tool = None
    from services.inference_metrics import normalize_reported_submetric

    tokens_tool = normalize_reported_submetric(tokens_tool)
    usage = inference_result.get("usage")
    if not isinstance(usage, dict):
        usage = None

    model_answer = inference_result.get("model_answer", "") or ""
    inference_error = inference_result.get("error")
    # C-04 / ADR-033：物化 enrich（含 tokens）仅由 Builder/Push 写入，Pipeline 不写
    status = "failed" if inference_error and not model_answer else "success"
    if inference_error and not model_answer:
        model_answer = f"[推理失败: {inference_error}]"

    from services.inference_model_display import (
        apply_inference_model_to_run,
        execution_mode_label,
        inference_model_label,
        merge_execution_mode_info,
        merge_inference_model_info,
        parse_execution_mode_from_config,
        parse_inference_model_from_config,
    )

    config_model = parse_inference_model_from_config(inference_config or {})
    response_model = inference_result.get("inference_model")
    if isinstance(response_model, dict) and inference_model_label(response_model):
        model_info = merge_inference_model_info(response_model, config_model)
        apply_inference_model_to_run(agent_run, model_info, overwrite=True)
    else:
        fallback = response_model if isinstance(response_model, dict) else {}
        model_info = merge_inference_model_info(config_model, fallback)
        apply_inference_model_to_run(agent_run, model_info)

    config_mode = parse_execution_mode_from_config(inference_config or {})
    response_mode = inference_result.get("execution_mode")
    if not isinstance(response_mode, dict):
        response_mode = {}
    if execution_mode_label(response_mode):
        execution_mode = merge_execution_mode_info(response_mode, config_mode)
    else:
        execution_mode = merge_execution_mode_info(config_mode, response_mode)

    await complete_agent_run(
        session,
        agent_run,
        status=status,
        ended_at=inference_end,
        trace_id=inference_result.get("trace_id"),
        error_message=str(inference_error) if inference_error else None,
        model_answer=model_answer,
        # COUP-C04：tokens_total 不再传给 complete_agent_run，
        # 由 Runtime Fact Builder 物化时唯一写入 run.tokens_total。
        # tokens_llm/tokens_tool 仍写入 output_snapshot 供 Builder 合并。
        tokens_llm=tokens_llm,
        tokens_tool=tokens_tool,
        usage=usage,
        execution_mode=execution_mode if execution_mode_label(execution_mode) else None,
        onto_agent_run_id=inference_result.get("onto_agent_run_id"),
        onto_agent_session_id=inference_result.get("onto_agent_session_id"),
    )
    if inference_duration_ms is None and agent_run.started_at and agent_run.ended_at:
        inference_duration_ms = max(
            0,
            int((agent_run.ended_at - agent_run.started_at).total_seconds() * 1000),
        )
    await schedule_fact_ingest_after_run(session, agent_run)
    await session.commit()

    if emit_inference_events:
        if inference_error:
            await emit_event(
                task_id,
                "inference_error",
                {"question_id": question_id, "error": inference_error},
            )
        else:
            await emit_event(
                task_id,
                "inference_complete",
                {
                    "question_id": question_id,
                    "answer": model_answer[:100] + "..." if len(model_answer) > 100 else model_answer,
                },
            )

    return OnlineInferenceResult(
        model_answer=model_answer,
        inference_duration_ms=inference_duration_ms,
        agent_run_id=agent_run.id,
        inference_error=str(inference_error) if inference_error else None,
        tokens_total=tokens_total,
    )


async def persist_runtime_metric_result(
    session: AsyncSession,
    *,
    task_id: str,
    question_id: int,
    metric_name: str,
    threshold: float,
    inference_duration_ms: Optional[int] = None,
    tokens_total: Optional[int] = None,
    question: str,
    ground_truth: str,
    model_answer: str,
    agent_name: str,
    llm_model_name: str,
    llm_model: str,
    agent_run_id: Optional[str],
) -> None:
    from evaluators.runtime_metrics import (
        build_runtime_metric_values,
        score_runtime_metric,
    )

    raw_value, detail = build_runtime_metric_values(
        metric_name,
        inference_duration_ms=inference_duration_ms,
        tokens_total=tokens_total,
    )
    normalized, passed, _label = score_runtime_metric(metric_name, raw_value, threshold)
    reason = f"[运行时指标] {detail}"

    row = EvaluationResult(
        task_id=task_id,
        question_id=question_id,
        model=llm_model_name,
        metric_name=metric_name,
        score=normalized,
        threshold=threshold,
        passed=passed,
        duration_ms=0,
        cot=reason,
        question=question,
        ground_truth=ground_truth,
        agent_name=agent_name,
        eval_method="runtime",
        agent_answer=model_answer,
        avg_score=normalized,
        eval_model=llm_model,
        eval_rounds=1,
        inference_duration_ms=inference_duration_ms,
        agent_run_id=agent_run_id,
    )
    session.add(row)
    await session.flush()
    if inference_duration_ms is not None:
        await sync_evaluation_result_to_run_metrics(session, row)


def _parse_stored_json_list(raw: Optional[str]) -> Optional[list]:
    if not raw:
        return None
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else None
    except json.JSONDecodeError:
        return None


def _parse_stored_json_dict(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else None
    except json.JSONDecodeError:
        return None


@dataclass
class TaskResumeContext:
    """从 evaluation_tasks 行还原的恢复上下文（H-04 单读源）。"""

    task_id: str
    dataset_id: str
    model_name: str
    inference_rounds: int
    llm_model: str
    agent_id: Optional[str]
    agent_ids: List[str]
    selected_indices: Optional[list]
    selected_metrics: Optional[list]
    custom_scripts: Optional[dict]
    use_bm25: bool
    use_doc_library: bool
    resume_from: int = 0
    resume_agent_id: Optional[str] = None
    is_online: bool = False
    ontology_package: Optional[str] = None
    parallel_inference: bool = True


async def load_task_resume_context(
    session: AsyncSession,
    task_id: str,
) -> TaskResumeContext:
    """加载恢复任务所需的全部 persisted 选项（TD-P1-04 / H-04）。"""
    result = await session.execute(
        select(EvaluationTask).where(EvaluationTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise ValueError(f"Task {task_id} not found")

    try:
        model_names = json.loads(task.models or '["default"]')
    except json.JSONDecodeError:
        model_names = ["default"]
    model_name = model_names[0] if model_names else "default"
    agent_ids = parse_task_agent_ids(task)
    agent_id = agent_ids[0] if agent_ids else task.agent_id
    resume_from = task.resume_from if task.resume_from is not None else 0
    resume_agent_id = getattr(task, "resume_agent_id", None)

    _resolved_rounds = max(1, int(getattr(task, "inference_rounds", None) or 1))

    return TaskResumeContext(
        task_id=task_id,
        dataset_id=task.dataset_id,
        model_name=model_name,
        inference_rounds=_resolved_rounds,
        llm_model=task.llm_model or "glm",
        agent_id=agent_id,
        agent_ids=agent_ids,
        selected_indices=_parse_stored_json_list(task.selected_indices),
        selected_metrics=_parse_stored_json_list(task.selected_metrics),
        custom_scripts=_parse_stored_json_dict(task.custom_scripts),
        use_bm25=bool(task.use_bm25) if task.use_bm25 is not None else False,
        use_doc_library=bool(task.use_doc_library)
        if task.use_doc_library is not None
        else False,
        ontology_package=task.ontology_package if hasattr(task, 'ontology_package') else None,
        parallel_inference=bool(task.parallel_inference) if getattr(task, "parallel_inference", None) is not None else True,
        resume_from=max(0, int(resume_from)),
        resume_agent_id=(str(resume_agent_id).strip() if resume_agent_id else None),
        is_online=bool(agent_ids),
    )


async def resume_evaluation_pipeline(task_id: str) -> None:
    """
    恢复暂停任务的唯一 Pipeline 入口（H-04）。

    评估段与新建任务共用 run_online/offline_evaluation_pipeline；
    resume_from、指标、BM25 等一律从 task 行读取，避免 tasks.py 分叉维护。
    """
    async with async_session() as session:
        ctx = await load_task_resume_context(session, task_id)

    log(
        "INFO",
        "TASK",
        "恢复评估 Pipeline 启动",
        task_id=ctx.task_id,
        resume_from=ctx.resume_from,
        online=ctx.is_online,
    )

    pipeline_kwargs = dict(
        task_id=ctx.task_id,
        dataset_id=ctx.dataset_id,
        model_name=ctx.model_name,
        llm_model=ctx.llm_model,
        selected_indices=ctx.selected_indices,
        selected_metrics=ctx.selected_metrics,
        use_bm25=ctx.use_bm25,
        use_doc_library=ctx.use_doc_library,
        custom_scripts=ctx.custom_scripts,
        resume_from=ctx.resume_from,
        ontology_package=ctx.ontology_package,
    )
    online_kwargs = {**pipeline_kwargs, "inference_rounds": ctx.inference_rounds, "parallel_inference": ctx.parallel_inference}

    if ctx.is_online:
        aid = ctx.agent_id or (ctx.agent_ids[0] if ctx.agent_ids else None)
        if not aid:
            raise ValueError(f"Task {task_id} has no agent_id for online resume")
        await run_online_evaluation_pipeline(
            agent_id=aid,
            **online_kwargs,
        )
    else:
        await run_offline_evaluation_pipeline(**pipeline_kwargs)


async def run_online_evaluation_pipeline(
    task_id: str,
    dataset_id: str,
    agent_id: str,
    model_name: str = "default",
    inference_rounds: int = 1,
    llm_model: str = "glm",
    selected_indices: Optional[list] = None,
    selected_metrics: Optional[list] = None,
    use_bm25: bool = False,
    use_doc_library: bool = False,
    custom_scripts: Optional[dict] = None,
    resume_from: int = 0,
    ontology_package: str = None,
    parallel_inference: bool = True,
) -> None:
    """在线评测 Pipeline 主入口（单次评测单 Agent）。"""
    from config import ENABLED_METRICS, get_model_display_name
    from tasks import (
        check_task_paused,
        emit_event,
        evaluate_single,
        evaluate_with_script,
        refresh_llm_models_cache_task,
    )
    from evaluators.llm_client import generate_evaluation_summary

    custom_scripts = custom_scripts or {}
    try:
        llm_model_name = get_model_display_name(llm_model)
        await refresh_llm_models_cache_task()
        log(
            "INFO",
            "TASK",
            "在线评估 Pipeline 启动",
            task_id=task_id,
            dataset_id=dataset_id,
            agent_id=agent_id,
        )

        if not agent_id or not str(agent_id).strip():
            raise ValueError("在线评估需要指定 Agent")
        agent_id = str(agent_id).strip()
        inference_rounds = max(1, int(inference_rounds or 1))

        scripts_cache: Dict[str, EvaluationScript] = {}
        if custom_scripts:
            async with async_session() as session:
                script_ids = list(set(custom_scripts.values()))
                result = await session.execute(
                    select(EvaluationScript).where(EvaluationScript.id.in_(script_ids))
                )
                for s in result.scalars().all():
                    scripts_cache[s.id] = s

        async with async_session() as session:
            await session.execute(
                update(EvaluationTask)
                .where(EvaluationTask.id == task_id)
                .values(status=TaskStatus.PROCESSING)
            )
            await session.commit()

        async with async_session() as session:
            result = await session.execute(select(Dataset).where(Dataset.id == dataset_id))
            dataset = result.scalar_one_or_none()
            if not dataset:
                raise ValueError(f"Dataset {dataset_id} not found")
            items = normalize_dataset_items(json.loads(dataset.data))

        if selected_indices:
            items = [items[i] for i in selected_indices if i < len(items)]

        question_count = len(items)
        if resume_from > 0:
            items = items[resume_from:]
            log(
                "INFO",
                "TASK",
                "在线评估 Pipeline 恢复",
                task_id=task_id,
                resume_from=resume_from,
                remaining=len(items),
            )

        if selected_metrics:
            metrics_to_run = [
                m.strip() for m in selected_metrics
                if m.strip() in METRICS_CONFIG or m.strip() in DEEPEVAL_METRIC_KEYS
            ]
        else:
            metrics_to_run = [m.strip() for m in ENABLED_METRICS if m.strip() in METRICS_CONFIG]

        # 加载自定义指标定义（一次性，避免每题重复查询）
        # 注意：本函数签名只传入 task_id，需先按 id 查出 EvaluationTask 行才能读 custom_metric_ids。
        custom_metric_defs: list[dict] = []
        async with async_session() as cm_session:
            task_row = await cm_session.execute(
                select(EvaluationTask).where(EvaluationTask.id == task_id)
            )
            task = task_row.scalar_one_or_none()
            cm_ids_raw = task.custom_metric_ids if task else None
            cm_ids = json.loads(cm_ids_raw) if cm_ids_raw else []
            if cm_ids:
                cm_result = await cm_session.execute(
                    select(CustomMetric)
                    .where(CustomMetric.id.in_(cm_ids))
                    .where(CustomMetric.is_active == True)  # noqa: E712
                )
                for cm in cm_result.scalars():
                    key = f"custom_{cm.id}"
                    custom_metric_defs.append({
                        "key": key,
                        "name": cm.name,
                        "kind": cm.kind,
                        "criteria": cm.criteria or "",
                        "dag": cm.dag,
                        "code": cm.code or "",
                        "multi_turn": cm.multi_turn,
                        "params": cm.params,
                        "threshold": cm.threshold,
                    })
                    metrics_to_run.append(key)

        progress_total = compute_online_progress_total(
            question_count, metrics_to_run, inference_rounds
        )
        # 独立计算质量指标总步数（所有 LLM 质量指标每轮 Judge）
        from evaluators.runtime_metrics import is_runtime_metric as _is_rt_online
        _llm_q_metrics = [m for m in metrics_to_run if not _is_rt_online(m)]
        _per_q_quality = len(_llm_q_metrics) * inference_rounds
        quality_total = question_count * _per_q_quality

        async with async_session() as session:
            await session.execute(
                update(EvaluationTask)
                .where(EvaluationTask.id == task_id)
                .values(progress_total=progress_total)
            )
            await session.commit()

        await emit_task_phase_started(
            emit_event,
            task_id,
            question_count=question_count,
            metrics_count=len(metrics_to_run),
            progress_total=progress_total,
            inference_rounds=inference_rounds,
            mode="online",
            quality_total=quality_total,
            # 在线评估的场景交付在质量指标完成后由 _run_auto_scenario_scoring 触发，
            # 此时 scenario_eval_start 事件会推送真实的 total_steps（子步骤数）。
            # 此处设为 0 避免显示 "1/1（题目数）" 的错误进度。
            scenario_total=0,
        )

        async with async_session() as session:
            result = await session.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent or not agent.inference_config:
                raise ValueError(f"Agent {agent_id} not found or not configured")
            agent_name = agent.name
            inference_config = json.loads(agent.inference_config)

        async with async_session() as session:
            await session.execute(
                update(EvaluationTask)
                .where(EvaluationTask.id == task_id)
                .values(status=TaskStatus.PROCESSING, paused_at=None)
            )
            await session.commit()

        progress = 0
        quality_progress = 0  # 独立跟踪质量指标进度
        async with async_session() as session:
            row = await session.execute(
                select(EvaluationTask.progress_current).where(
                    EvaluationTask.id == task_id
                )
            )
            progress = row.scalar_one_or_none() or 0

        async def _process_one_question(offset, item):
            nonlocal progress, quality_progress
            async with _QUESTION_SEMAPHORE, async_session() as q_session:
                q_idx = resume_from + offset
                if await check_task_paused(task_id):
                    await persist_pause_checkpoint(q_session, task_id, q_idx)
                    await q_session.commit()
                    return None

                question, context, ground_truth, judging_mode, eval_prompt = extract_question_fields(item)
                # 管线层统一解析参照源:按问题从本体包检索 top-K(若命中)优先于数据集 context,省 token 且同题跨轮稳定
                _onto_ref = _load_ontology_reference(ontology_package, question)
                if _onto_ref:
                    context = _onto_ref
                ditem_id, q_key, _ = get_item_stable_ids(item, index=q_idx)
                from services.dataset_item_scenario_service import (
                    resolve_scenario_key_for_eval_item,
                )

                run_scenario_key = await resolve_scenario_key_for_eval_item(
                    q_session, dataset_id, q_idx, item
                )

                from evaluators.runtime_metrics import is_runtime_metric

                if await is_question_online_evaluation_complete(
                    q_session,
                    task_id=task_id,
                    question_id=q_idx,
                    agent_id=agent.id,
                    metrics_to_run=metrics_to_run,
                    inference_rounds=inference_rounds,
                ):
                    # 跳过已完成的问题但仍需推进进度，避免进度条停滞
                    skipped_steps = compute_online_progress_total(
                        1, metrics_to_run, inference_rounds
                    )
                    async with _progress_lock:
                        progress += skipped_steps
                        quality_progress += _per_q_quality
                        current_progress = progress
                    await q_session.execute(
                        update(EvaluationTask)
                        .where(EvaluationTask.id == task_id)
                        .values(progress_current=current_progress)
                    )
                    await q_session.commit()
                    return True

                completed_metrics = await load_completed_metrics(
                    q_session, task_id, q_idx, agent_id=agent.id
                )
                pending_metrics = [
                    m for m in metrics_to_run if m not in completed_metrics
                ]

                async def _run_one_inference_round(run_index):
                    """单轮推理 + runtime 指标（每轮独立 session，可跨轮并发）"""
                    nonlocal progress
                    async with async_session() as r_session:
                        if await check_task_paused(task_id):
                            await persist_pause_checkpoint(r_session, task_id, q_idx)
                            await r_session.commit()
                            return None

                        await emit_progress_tick(
                            emit_event,
                            task_id,
                            current=progress,
                            total=progress_total,
                            phase="inference",
                            question_id=q_idx,
                            run_index=run_index,
                        )

                        async with _INFERENCE_SEMAPHORE:
                            inf_result = await execute_online_inference_with_run(
                                r_session,
                                task_id=task_id,
                                dataset_id=dataset_id,
                                question_id=q_idx,
                                run_index=run_index,
                                question=question,
                                ground_truth=ground_truth,
                                agent=agent,
                                inference_config=inference_config,
                                dataset_item_id=ditem_id,
                                question_key=q_key,
                                scenario_key=run_scenario_key,
                                scenario=item.get("scenario") if isinstance(item, dict) else None,
                                user_description=item.get("user_description") if isinstance(item, dict) else None,
                                expected_outcome=item.get("expected_outcome") if isinstance(item, dict) else None,
                                eval_model_key=llm_model,
                            )

                        if not inf_result.reused:
                            async with _progress_lock:
                                progress += 1
                                current_progress = progress
                            await r_session.execute(
                                update(EvaluationTask)
                                .where(EvaluationTask.id == task_id)
                                .values(progress_current=current_progress)
                            )
                            await r_session.commit()
                            await emit_progress_tick(
                                emit_event,
                                task_id,
                                current=current_progress,
                                total=progress_total,
                                phase="inference_done",
                                question_id=q_idx,
                                run_index=run_index,
                            )

                        runtime_for_run = [
                            m for m in metrics_to_run if is_runtime_metric(m)
                        ]
                        run_done = await load_completed_metrics(
                            r_session,
                            task_id,
                            q_idx,
                            agent_run_id=inf_result.agent_run_id,
                        )
                        runtime_pending = [
                            m for m in runtime_for_run if m not in run_done
                        ]

                        for metric_name in runtime_pending:
                            if await check_task_paused(task_id):
                                await persist_pause_checkpoint(r_session, task_id, q_idx)
                                await r_session.commit()
                                return None
                            await emit_progress_tick(
                                emit_event,
                                task_id,
                                current=progress,
                                total=progress_total,
                                phase="runtime",
                                question_id=q_idx,
                                metric=metric_name,
                                run_index=run_index,
                            )
                            threshold = await get_runtime_threshold(r_session, metric_name)
                            if threshold is None:
                                threshold = METRICS_CONFIG.get(metric_name, 0)
                            tokens_for_score: Optional[int] = None
                            if metric_name == "tokens_total" and inf_result.agent_run_id:
                                agent_run_row = await r_session.get(
                                    AgentRun, inf_result.agent_run_id
                                )
                                if agent_run_row is not None:
                                    from services.inference_duration_service import (
                                        resolve_tokens_total_for_run,
                                    )

                                    tokens_for_score = await resolve_tokens_total_for_run(
                                        r_session, agent_run_row
                                    )
                            await persist_runtime_metric_result(
                                r_session,
                                task_id=task_id,
                                question_id=q_idx,
                                metric_name=metric_name,
                                threshold=threshold,
                                inference_duration_ms=inf_result.inference_duration_ms,
                                tokens_total=tokens_for_score,
                                question=question,
                                ground_truth=ground_truth,
                                model_answer=inf_result.model_answer,
                                agent_name=agent_name,
                                llm_model_name=llm_model_name,
                                llm_model=llm_model,
                                agent_run_id=inf_result.agent_run_id,
                            )
                            async with _progress_lock:
                                progress += 1
                                current_progress = progress
                            await r_session.execute(
                                update(EvaluationTask)
                                .where(EvaluationTask.id == task_id)
                                .values(progress_current=current_progress)
                            )
                            await r_session.commit()
                            await emit_progress_tick(
                                emit_event,
                                task_id,
                                current=current_progress,
                                total=progress_total,
                                phase="runtime_done",
                                question_id=q_idx,
                                metric=metric_name,
                            )

                        return inf_result

                # 推理轮次（并发默认；串行模式逐轮执行，适合小参数模型 Agent）
                if parallel_inference:
                    round_results = await asyncio.gather(*[
                        _run_one_inference_round(run_index)
                        for run_index in range(inference_rounds)
                    ], return_exceptions=True)
                else:
                    round_results = []
                    for run_index in range(inference_rounds):
                        try:
                            r = await _run_one_inference_round(run_index)
                            round_results.append(r)
                        except Exception as _e:
                            round_results.append(_e)
                            continue
                        if r is None:  # paused
                            break
                    # 对齐长度，保持与 gather 返回结构一致
                    while len(round_results) < inference_rounds:
                        round_results.append(None)

                inf_results: List[OnlineInferenceResult] = []
                primary_inf: Optional[OnlineInferenceResult] = None
                for run_index, r in enumerate(round_results):
                    if isinstance(r, Exception):
                        log("ERROR", "EVAL_PIPELINE",
                            f"题目 {q_idx} 第 {run_index} 轮推理异常: {r}",
                            question_id=q_idx, run_index=run_index, error=str(r))
                        continue
                    if r is None:
                        return None
                    inf_results.append(r)
                    if run_index == 0:
                        primary_inf = r

                if primary_inf is None:
                    return True

                # --- LLM 质量指标 Judge ---
                # 所有 LLM 质量指标（correctness / faithfulness / answer_relevancy）：
                # 对每轮推理答案都 Judge（多轮稳定性评估）。
                llm_metric_names = [
                    m for m in metrics_to_run if not is_runtime_metric(m)
                ]

                # Phase 3: trace 过程数据缓存(per agent_run_id,避免同一 run 多指标重复拉 trace)
                _trace_data_cache: Dict[str, Any] = {}

                async def _judge_one(
                    metric_name: str,
                    model_answer: str,
                    inference_duration_ms: Optional[int],
                    agent_run_id: Optional[str],
                    allow_script: bool,
                ) -> bool:
                    """单次质量 Judge（含暂停检查/进度/落库）。返回 False 表示已暂停需退出。"""
                    nonlocal progress, quality_progress
                    if await check_task_paused(task_id):
                        await persist_pause_checkpoint(q_session, task_id, q_idx)
                        await q_session.commit()
                        return False
                    await emit_event(
                        task_id,
                        "judge_start",
                        {"question_id": q_idx, "metric": metric_name},
                    )
                    await emit_progress_tick(
                        emit_event,
                        task_id,
                        current=progress,
                        total=progress_total,
                        phase="judge",
                        question_id=q_idx,
                        metric=metric_name,
                        quality_current=quality_progress,
                        quality_total=quality_total,
                    )

                    # === 分支1：DeepEval 指标 ===
                    if metric_name in DEEPEVAL_METRIC_KEYS:
                        _spec = SPEC_BY_KEY.get(metric_name)
                        threshold = _spec.gate_threshold if _spec else METRICS_CONFIG.get(metric_name, 0.5)
                        try:
                            metric = get_metric(metric_name)
                            # Phase 3: 从 Langfuse trace 提取过程数据,透传给适配器(缓存 per run)
                            # tool_calls/thoughts/retrieval_context/turns 缺失时适配器门控自动「已跳过」
                            process_data = _trace_data_cache.get(agent_run_id)
                            if process_data is None and agent_run_id:
                                from services.langfuse_trace_extract import (
                                    fetch_process_data_for_run,
                                )
                                process_data = await fetch_process_data_for_run(
                                    q_session, agent_run_id
                                )
                                _trace_data_cache[agent_run_id] = process_data
                            er = await metric.evaluate(
                                question=question,
                                answer=model_answer,
                                context=context,
                                ground_truth=ground_truth,
                                tool_calls=process_data.tool_calls if process_data else None,
                                thoughts=process_data.thoughts if process_data else None,
                                retrieval_context=process_data.retrieval_context if process_data else None,
                                turns=process_data.turns if process_data else None,
                                system_prompt=eval_prompt or None,
                            )
                            eval_result = {"score": er.score, "reason": er.reason, "duration_ms": er.duration_ms}
                        except Exception as e:
                            eval_result = {"score": 0.0, "reason": f"{metric_name}评估失败：{e}", "duration_ms": 0}

                        async with _progress_lock:
                            progress += 1
                            quality_progress += 1
                            current_progress = progress
                            current_quality = quality_progress
                        await _persist_quality_evaluation_result(
                            q_session,
                            task_id=task_id,
                            question_id=q_idx,
                            metric_name=metric_name,
                            threshold=threshold,
                            eval_result=eval_result,
                            use_script_eval=False,
                            script_id=None,
                            scripts_cache=scripts_cache,
                            question=question,
                            ground_truth=ground_truth,
                            model_answer=model_answer,
                            agent_name=agent_name,
                            llm_model=llm_model,
                            llm_model_name=llm_model_name,
                            inference_duration_ms=inference_duration_ms,
                            agent_run_id=agent_run_id,
                            eval_method_override="deepeval",
                        )
                        await q_session.execute(
                            update(EvaluationTask)
                            .where(EvaluationTask.id == task_id)
                            .values(progress_current=current_progress)
                        )
                        await q_session.commit()
                        await emit_progress_tick(
                            emit_event, task_id,
                            current=current_progress, total=progress_total,
                            phase="judge_done",
                            question_id=q_idx, metric=metric_name,
                            quality_current=current_quality, quality_total=quality_total,
                        )
                        return True

                    # === 分支2：自定义指标 ===
                    if metric_name.startswith("custom_"):
                        cm_def = next((c for c in custom_metric_defs if c["key"] == metric_name), None)
                        if cm_def:
                            threshold = cm_def.get("threshold", 0.5)
                            eval_result = await _run_custom_metric(
                                cm_def, question, model_answer, context, ground_truth,
                                llm_model, llm_model_name,
                            )
                            eval_tag = f"custom_{cm_def['kind']}"  # custom_geval / custom_dag / custom_code
                            async with _progress_lock:
                                progress += 1
                                quality_progress += 1
                                current_progress = progress
                                current_quality = quality_progress
                            await _persist_quality_evaluation_result(
                                q_session,
                                task_id=task_id,
                                question_id=q_idx,
                                metric_name=metric_name,
                                threshold=threshold,
                                eval_result=eval_result,
                                use_script_eval=False,
                                script_id=None,
                                scripts_cache=scripts_cache,
                                question=question,
                                ground_truth=ground_truth,
                                model_answer=model_answer,
                                agent_name=agent_name,
                                llm_model=llm_model,
                                llm_model_name=llm_model_name,
                                inference_duration_ms=inference_duration_ms,
                                agent_run_id=agent_run_id,
                                eval_method_override=eval_tag,
                            )
                            await q_session.execute(
                                update(EvaluationTask)
                                .where(EvaluationTask.id == task_id)
                                .values(progress_current=current_progress)
                            )
                            await q_session.commit()
                            await emit_progress_tick(
                                emit_event, task_id,
                                current=current_progress, total=progress_total,
                                phase="judge_done",
                                question_id=q_idx, metric=metric_name,
                                quality_current=current_quality, quality_total=quality_total,
                            )
                            return True

                    # === 分支3：原有指标（faithfulness/answer_relevancy/correctness + script）===
                    threshold = METRICS_CONFIG.get(metric_name, 0.8)
                    script_id = custom_scripts.get(str(q_idx)) if custom_scripts else None
                    use_script_eval = bool(
                        allow_script
                        and script_id
                        and script_id in scripts_cache
                        and metric_name == "correctness"
                    )
                    custom_script_metric = None
                    if use_script_eval:
                        script_obj = scripts_cache[script_id]
                        custom_script_metric = CustomScriptMetric(
                            script_code=script_obj.script_code,
                            script_name=script_obj.name,
                        )

                    eval_result = await _judge_quality_metric_once(
                        task_id=task_id,
                        question_id=q_idx,
                        model_name=model_name,
                        metric_name=metric_name,
                        threshold=threshold,
                        question=question,
                        model_answer=model_answer,
                        context=context,
                        ground_truth=ground_truth,
                        llm_model=llm_model,
                        use_bm25=use_bm25,
                        use_doc_library=use_doc_library,
                        use_script_eval=bool(use_script_eval and custom_script_metric),
                        custom_script_metric=custom_script_metric,
                        evaluate_single=evaluate_single,
                        evaluate_with_script=evaluate_with_script,
                        ontology_package=ontology_package,
                        judging_mode=judging_mode,
                        eval_prompt=eval_prompt,
                    )

                    async with _progress_lock:
                        progress += 1
                        quality_progress += 1
                        current_progress = progress
                        current_quality = quality_progress

                    await _persist_quality_evaluation_result(
                        q_session,
                        task_id=task_id,
                        question_id=q_idx,
                        metric_name=metric_name,
                        threshold=threshold,
                        eval_result=eval_result,
                        use_script_eval=bool(use_script_eval and custom_script_metric),
                        script_id=script_id if use_script_eval else None,
                        scripts_cache=scripts_cache,
                        question=question,
                        ground_truth=ground_truth,
                        model_answer=model_answer,
                        agent_name=agent_name,
                        llm_model=llm_model,
                        llm_model_name=llm_model_name,
                        inference_duration_ms=inference_duration_ms,
                        agent_run_id=agent_run_id,
                    )
                    # 进度与结果在同一个事务中持久化，避免崩溃时进度双计
                    await q_session.execute(
                        update(EvaluationTask)
                        .where(EvaluationTask.id == task_id)
                        .values(progress_current=current_progress)
                    )
                    await q_session.commit()
                    await emit_progress_tick(
                        emit_event,
                        task_id,
                        current=current_progress,
                        total=progress_total,
                        phase="judge_done",
                        question_id=q_idx,
                        metric=metric_name,
                        quality_current=current_quality,
                        quality_total=quality_total,
                    )
                    return True

                async def _evaluate_metric_only(
                    metric_name: str,
                    model_answer: str,
                    inference_duration_ms: Optional[int],
                    agent_run_id: Optional[str],
                    allow_script: bool,
                ) -> tuple:
                    """只做 LLM 评测（无 DB/无进度），返回
                    (eval_result, threshold, eval_method_override, script_id, use_script_eval)。
                    trace 数据需在调用前预取到 _trace_data_cache。
                    """
                    # === 分支1：DeepEval 指标 ===
                    if metric_name in DEEPEVAL_METRIC_KEYS:
                        _spec = SPEC_BY_KEY.get(metric_name)
                        threshold = _spec.gate_threshold if _spec else METRICS_CONFIG.get(metric_name, 0.5)
                        try:
                            metric = get_metric(metric_name)
                            process_data = _trace_data_cache.get(agent_run_id)
                            er = await metric.evaluate(
                                question=question,
                                answer=model_answer,
                                context=context,
                                ground_truth=ground_truth,
                                tool_calls=process_data.tool_calls if process_data else None,
                                thoughts=process_data.thoughts if process_data else None,
                                retrieval_context=process_data.retrieval_context if process_data else None,
                                turns=process_data.turns if process_data else None,
                                system_prompt=eval_prompt or None,
                            )
                            eval_result = {"score": er.score, "reason": er.reason, "duration_ms": er.duration_ms}
                        except Exception as e:
                            eval_result = {"score": 0.0, "reason": f"{metric_name}评估失败：{e}", "duration_ms": 0}
                        return eval_result, threshold, "deepeval", None, False

                    # === 分支2：自定义指标 ===
                    if metric_name.startswith("custom_"):
                        cm_def = next((c for c in custom_metric_defs if c["key"] == metric_name), None)
                        if cm_def:
                            threshold = cm_def.get("threshold", 0.5)
                            eval_result = await _run_custom_metric(
                                cm_def, question, model_answer, context, ground_truth,
                                llm_model, llm_model_name,
                            )
                            eval_tag = f"custom_{cm_def['kind']}"
                            return eval_result, threshold, eval_tag, None, False

                    # === 分支3：原有指标（faithfulness/answer_relevancy/correctness + script）===
                    threshold = METRICS_CONFIG.get(metric_name, 0.8)
                    script_id = custom_scripts.get(str(q_idx)) if custom_scripts else None
                    use_script_eval = bool(
                        allow_script
                        and script_id
                        and script_id in scripts_cache
                        and metric_name == "correctness"
                    )
                    custom_script_metric = None
                    if use_script_eval:
                        script_obj = scripts_cache[script_id]
                        custom_script_metric = CustomScriptMetric(
                            script_code=script_obj.script_code,
                            script_name=script_obj.name,
                        )

                    eval_result = await _judge_quality_metric_once(
                        task_id=task_id,
                        question_id=q_idx,
                        model_name=model_name,
                        metric_name=metric_name,
                        threshold=threshold,
                        question=question,
                        model_answer=model_answer,
                        context=context,
                        ground_truth=ground_truth,
                        llm_model=llm_model,
                        use_bm25=use_bm25,
                        use_doc_library=use_doc_library,
                        use_script_eval=bool(use_script_eval and custom_script_metric),
                        custom_script_metric=custom_script_metric,
                        evaluate_single=evaluate_single,
                        evaluate_with_script=evaluate_with_script,
                        ontology_package=ontology_package,
                        judging_mode=judging_mode,
                        eval_prompt=eval_prompt,
                    )
                    return eval_result, threshold, None, script_id if use_script_eval else None, bool(use_script_eval and custom_script_metric)

                # 所有 LLM 质量指标：对每轮推理答案都 Judge（多轮稳定性评估）
                # 跨轮×跨指标并发评测 + 串行落库：LLM 调用受 _LLM_CALL_SEMAPHORE(8) 控制

                # Step 1: 串行预取 trace + 收集待评测指标（需共享 q_session）
                judge_tasks: list = []  # [(inf_r, metric_name), ...]
                for inf_r in inf_results:
                    if not llm_metric_names:
                        break
                    run_done = await load_completed_metrics(
                        q_session,
                        task_id,
                        q_idx,
                        agent_run_id=inf_r.agent_run_id,
                    )
                    pending = [
                        m for m in llm_metric_names if m not in run_done
                    ]
                    if not pending:
                        continue
                    if inf_r.agent_run_id and inf_r.agent_run_id not in _trace_data_cache:
                        from services.langfuse_trace_extract import (
                            fetch_process_data_for_run,
                        )
                        _trace_data_cache[inf_r.agent_run_id] = await fetch_process_data_for_run(
                            q_session, inf_r.agent_run_id
                        )
                    for m in pending:
                        judge_tasks.append((inf_r, m))

                if not judge_tasks:
                    return True

                # 暂停检查（一次性）
                if await check_task_paused(task_id):
                    await persist_pause_checkpoint(q_session, task_id, q_idx)
                    await q_session.commit()
                    return None

                # Step 2: 跨轮×跨指标并发 LLM 评测
                eval_tasks = [
                    _evaluate_metric_only(
                        m, inf_r.model_answer,
                        inf_r.inference_duration_ms,
                        inf_r.agent_run_id,
                        allow_script=True,
                    )
                    for inf_r, m in judge_tasks
                ]
                eval_results = await asyncio.gather(*eval_tasks, return_exceptions=True)

                # Step 3: 串行落库（避免并发 session 竞态）
                for (inf_r, metric_name), result in zip(judge_tasks, eval_results):
                    if isinstance(result, Exception):
                        log("ERROR", "EVAL_PIPELINE",
                            f"指标 {metric_name} 并发评测异常",
                            metric_name=metric_name, error=str(result))
                        eval_result = {"score": 0.0, "reason": f"评测异常：{result}", "duration_ms": 0}
                        threshold = METRICS_CONFIG.get(metric_name, 0.5)
                        eval_method_override = "error"
                        script_id = None
                        use_script_eval = False
                    else:
                        eval_result, threshold, eval_method_override, script_id, use_script_eval = result

                    await emit_event(
                        task_id,
                        "judge_start",
                        {"question_id": q_idx, "metric": metric_name},
                    )
                    await emit_progress_tick(
                        emit_event,
                        task_id,
                        current=progress,
                        total=progress_total,
                        phase="judge",
                        question_id=q_idx,
                        metric=metric_name,
                        quality_current=quality_progress,
                        quality_total=quality_total,
                    )

                    async with _progress_lock:
                        progress += 1
                        quality_progress += 1
                        current_progress = progress
                        current_quality = quality_progress
                    _persist_t0 = time.monotonic()
                    await _persist_quality_evaluation_result(
                        q_session,
                        task_id=task_id,
                        question_id=q_idx,
                        metric_name=metric_name,
                        threshold=threshold,
                        eval_result=eval_result,
                        use_script_eval=use_script_eval,
                        script_id=script_id,
                        scripts_cache=scripts_cache,
                        question=question,
                        ground_truth=ground_truth,
                        model_answer=inf_r.model_answer,
                        agent_name=agent_name,
                        llm_model=llm_model,
                        llm_model_name=llm_model_name,
                        inference_duration_ms=inf_r.inference_duration_ms,
                        agent_run_id=inf_r.agent_run_id,
                        eval_method_override=eval_method_override,
                    )
                    await q_session.execute(
                        update(EvaluationTask)
                        .where(EvaluationTask.id == task_id)
                        .values(progress_current=current_progress)
                    )
                    await q_session.commit()
                    await emit_progress_tick(
                        emit_event, task_id,
                        current=current_progress, total=progress_total,
                        phase="judge_done",
                        question_id=q_idx, metric=metric_name,
                        quality_current=current_quality, quality_total=quality_total,
                    )
                    _persist_ms = (time.monotonic() - _persist_t0) * 1000
                    log("INFO", "EVAL_PIPELINE",
                        f"指标 {metric_name} 落库耗时 {_persist_ms:.1f}ms",
                        metric_name=metric_name,
                        question_id=q_idx,
                        persist_duration_ms=round(_persist_ms, 1))

                return True

        # Run all questions（并发默认；串行模式逐题执行，适合小参数模型 Agent）
        if parallel_inference:
            results = await asyncio.gather(*[
                _process_one_question(offset, item)
                for offset, item in enumerate(items)
            ], return_exceptions=True)
        else:
            results = []
            for offset, item in enumerate(items):
                try:
                    r = await _process_one_question(offset, item)
                    results.append(r)
                except Exception as _e:
                    results.append(_e)
                    continue
                if r is None:  # paused
                    break
            # 对齐长度，保持与 gather 返回结构一致
            while len(results) < len(items):
                results.append(None)

        # Check for pause or errors
        for offset, result in enumerate(results):
            if isinstance(result, Exception):
                log("ERROR", "EVAL_PIPELINE", f"题目 {resume_from + offset} 处理异常: {result}")

        # If any question returned None (paused), return early
        if any(result is None for result in results):
            return

        await _run_auto_scenario_scoring(task_id, emit_event)

        # 先生成评测总结并落库，再标记任务完成——避免 status=COMPLETED 已提交、
        # summary 仍在 LLM 生成中的窗口内前端拿到 summary.text=null，
        # 导致结果页雷达图单列居中（缺评测摘要区块），与再次打开时的左右布局不一致。
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(EvaluationResult).where(EvaluationResult.task_id == task_id)
                )
                results_list = [
                    {
                        "question_id": r.question_id,
                        "metric_name": r.metric_name,
                        "score": r.avg_score if r.avg_score is not None else r.score,
                        "passed": r.passed,
                        "eval_reason": r.eval_reason or "",
                    }
                    for r in result.scalars().all()
                ]
                from services.task_dimension_verdict_service import build_task_dimension_verdicts

                dimension_verdicts = await build_task_dimension_verdicts(session, task_id)
                agent_name = await resolve_task_agent_name(session, task_id)
                fail_examples = _build_fail_examples(results_list)
                summary = await generate_evaluation_summary(
                    task_id=task_id,
                    dataset_id=dataset_id,
                    model_name=model_name,
                    agent_name=agent_name,
                    results=results_list,
                    model_key=llm_model,
                    dimension_verdicts=dimension_verdicts,
                    fail_examples=fail_examples,
                )
                await session.execute(
                    update(EvaluationTask)
                    .where(EvaluationTask.id == task_id)
                    .values(summary=summary)
                )
                await session.commit()
                await emit_event(task_id, "summary_generated", {"summary": summary})
        except Exception as e:
            log("ERROR", "TASK", "评估总结生成失败", task_id=task_id, error=str(e))
            await emit_event(task_id, "summary_error", {"error": str(e)})

        async with async_session() as session:
            await session.execute(
                update(EvaluationTask)
                .where(EvaluationTask.id == task_id)
                .values(
                    status=TaskStatus.COMPLETED,
                    completed_at=datetime.utcnow(),
                    resume_from=0,
                    resume_agent_id=None,
                )
            )
            await session.commit()

        await emit_event(task_id, "task_complete", {"status": "completed"})
        log("INFO", "TASK", "在线评估 Pipeline 完成", task_id=task_id)

    except Exception as e:
        log("ERROR", "TASK", "在线评估 Pipeline 失败", task_id=task_id, error=str(e))
        async with async_session() as session:
            await session.execute(
                update(EvaluationTask)
                .where(EvaluationTask.id == task_id)
                .values(status=TaskStatus.FAILED, error=str(e))
            )
            await session.commit()
        await emit_event(task_id, "task_error", {"error": str(e)})
        raise


async def run_offline_evaluation_pipeline(
    task_id: str,
    dataset_id: str,
    model_name: str = "default",
    llm_model: str = "glm",
    selected_indices: Optional[list] = None,
    selected_metrics: Optional[list] = None,
    use_bm25: bool = False,
    use_doc_library: bool = False,
    custom_scripts: Optional[dict] = None,
    resume_from: int = 0,
    langfuse_source: Optional[dict] = None,
    ontology_package: str = None,
) -> None:
    """离线评测 Pipeline 主入口（与在线 Pipeline 对齐，支持 resume_from / 幂等恢复）。"""
    from config import ENABLED_METRICS, get_model_display_name
    from services.evaluation_mode import resolve_offline_metrics_to_run
    from tasks import (
        check_task_paused,
        emit_event,
        evaluate_single,
        evaluate_with_script,
        refresh_llm_models_cache_task,
    )
    from evaluators.llm_client import generate_evaluation_summary

    custom_scripts = custom_scripts or {}
    langfuse_answer_map: Optional[dict] = None
    # Phase 3: 离线 trace_id→question_id 映射 + 过程数据缓存(per trace_id)
    trace_id_map: Dict[int, str] = {}
    _offline_trace_cache: Dict[str, Any] = {}
    try:
        llm_model_name = get_model_display_name(llm_model)
        await refresh_llm_models_cache_task()
        log(
            "INFO",
            "TASK",
            "离线评估 Pipeline 启动",
            task_id=task_id,
            dataset_id=dataset_id,
            resume_from=resume_from,
            has_langfuse_source=bool(langfuse_source),
        )

        if langfuse_source and isinstance(langfuse_source, dict):
            from services.langfuse_answer_source import fetch_langfuse_answer_map

            # 当用户没指定 source_task_id 时，自动从该项目最新一批 trace 中
            # 提取最近一个 sessionId 形如 "<task_id>:<question>" 的 task_id，
            # 避免把整个项目所有 trace 都拉进来。
            source_task_id = langfuse_source.get("source_task_id")
            tags = langfuse_source.get("tags")
            session_id = langfuse_source.get("session_id")
            agent_name = langfuse_source.get("agent_name")

            if not source_task_id and not session_id and not agent_name:
                try:
                    from services.langfuse_answer_source import _resolve_latest_task_id
                    latest_tid = await _resolve_latest_task_id(
                        project=langfuse_source.get("project"),
                        tags=tags or "onto-eval",
                    )
                    if latest_tid:
                        log(
                            "INFO",
                            "TASK",
                            "未指定 source_task_id，自动选用最新 Langfuse 任务",
                            task_id=task_id,
                            latest_tid=latest_tid,
                        )
                        source_task_id = latest_tid
                except Exception as e:
                    log("WARN", "TASK", "自动解析最新 task_id 失败", error=str(e))

            # 时间窗 fallback 需要读 agent_runs；新开一个 session，
            # 与下文 transaction 隔离开，避免相互影响。
            from database import async_session as _answer_session_factory

            _trace_meta: List[Dict[str, Any]] = []
            try:
                async with _answer_session_factory() as answer_session:
                    langfuse_answer_map, _trace_meta = await fetch_langfuse_answer_map(
                        project=langfuse_source.get("project"),
                        source_task_id=source_task_id,
                        trace_ids=langfuse_source.get("trace_ids"),
                        tags=tags,
                        session_id=session_id,
                        agent_name=agent_name,
                        session=answer_session,
                    )
                log(
                    "INFO",
                    "TASK",
                    "Langfuse 答案源加载完成",
                    task_id=task_id,
                    answer_count=len(langfuse_answer_map),
                )
                # Phase 3: 从 _trace_meta 构建 question_id→trace_id 映射,供过程数据提取
                for _tm in (_trace_meta or []):
                    _tid = _tm.get("trace_id")
                    _qid = _tm.get("question_id")
                    if _tid and _qid is not None:
                        try:
                            trace_id_map[int(_qid)] = str(_tid)
                        except (TypeError, ValueError):
                            pass
            except Exception as e:
                log(
                    "WARN",
                    "TASK",
                    "Langfuse 答案源加载失败，将使用数据集中的 model_answer 继续评测",
                    task_id=task_id,
                    error=str(e),
                )
                langfuse_answer_map = {}

        scripts_cache: Dict[str, EvaluationScript] = {}
        if custom_scripts:
            async with async_session() as session:
                script_ids = list(set(custom_scripts.values()))
                result = await session.execute(
                    select(EvaluationScript).where(EvaluationScript.id.in_(script_ids))
                )
                for s in result.scalars().all():
                    scripts_cache[s.id] = s

        async with async_session() as session:
            await session.execute(
                update(EvaluationTask)
                .where(EvaluationTask.id == task_id)
                .values(status=TaskStatus.PROCESSING, paused_at=None)
            )
            await session.commit()

        async with async_session() as session:
            result = await session.execute(select(Dataset).where(Dataset.id == dataset_id))
            dataset = result.scalar_one_or_none()
            if not dataset:
                raise ValueError(f"Dataset {dataset_id} not found")
            items = normalize_dataset_items(json.loads(dataset.data))

        if selected_indices:
            items = [items[i] for i in selected_indices if i < len(items)]

        question_count = len(items)
        if resume_from > 0:
            items = items[resume_from:]
            log(
                "INFO",
                "TASK",
                "离线评估 Pipeline 恢复",
                task_id=task_id,
                resume_from=resume_from,
                remaining=len(items),
            )

        if selected_metrics:
            metrics_to_run = resolve_offline_metrics_to_run(
                selected_metrics, ENABLED_METRICS, METRICS_CONFIG
            )
        else:
            metrics_to_run = resolve_offline_metrics_to_run(
                None, ENABLED_METRICS, METRICS_CONFIG
            )

        # 追加 DeepEval 指标（resolve_offline_metrics_to_run 只保留 METRICS_CONFIG 中的指标）
        if selected_metrics:
            for m in selected_metrics:
                m = m.strip() if m else ""
                if m and m in DEEPEVAL_METRIC_KEYS and m not in metrics_to_run:
                    metrics_to_run.append(m)

        # 加载自定义指标定义（一次性，避免每题重复查询）
        # 注意：本函数签名只传入 task_id，需先按 id 查出 EvaluationTask 行才能读 custom_metric_ids。
        custom_metric_defs: list[dict] = []
        async with async_session() as cm_session:
            task_row = await cm_session.execute(
                select(EvaluationTask).where(EvaluationTask.id == task_id)
            )
            task = task_row.scalar_one_or_none()
            cm_ids_raw = task.custom_metric_ids if task else None
            cm_ids = json.loads(cm_ids_raw) if cm_ids_raw else []
            if cm_ids:
                cm_result = await cm_session.execute(
                    select(CustomMetric)
                    .where(CustomMetric.id.in_(cm_ids))
                    .where(CustomMetric.is_active == True)  # noqa: E712
                )
                for cm in cm_result.scalars():
                    key = f"custom_{cm.id}"
                    custom_metric_defs.append({
                        "key": key,
                        "name": cm.name,
                        "kind": cm.kind,
                        "criteria": cm.criteria or "",
                        "dag": cm.dag,
                        "code": cm.code or "",
                        "multi_turn": cm.multi_turn,
                        "params": cm.params,
                        "threshold": cm.threshold,
                    })
                    metrics_to_run.append(key)

        if not metrics_to_run:
            raise ValueError("离线评估至少需选择一个质量指标")

        progress_total = compute_offline_progress_total(
            question_count,
            metrics_to_run,
            has_process_eval=bool(langfuse_source),
        )
        # 独立计算质量和场景交付的总步数
        from evaluators.runtime_metrics import is_runtime_metric as _is_rt
        quality_total = question_count * sum(1 for m in metrics_to_run if not _is_rt(m)) * JUDGE_RUN_COUNT
        # 场景交付进度由 scenario_eval_start 事件推送真实子步骤数，
        # 此处不预设值（避免用 question_count 污染进度显示）
        scenario_total = 0

        async with async_session() as session:
            await session.execute(
                update(EvaluationTask)
                .where(EvaluationTask.id == task_id)
                .values(progress_total=progress_total)
            )
            await session.commit()

        await emit_task_phase_started(
            emit_event,
            task_id,
            question_count=question_count,
            metrics_count=len(metrics_to_run),
            progress_total=progress_total,
            inference_rounds=1,
            mode="offline",
            quality_total=quality_total,
            scenario_total=scenario_total,
        )

        progress = 0
        quality_progress = 0  # 独立跟踪质量指标进度
        async with async_session() as session:
            row = await session.execute(
                select(EvaluationTask.progress_current).where(
                    EvaluationTask.id == task_id
                )
            )
            progress = row.scalar_one_or_none() or 0

        async with async_session() as session:
            for offset, item in enumerate(items):
                q_idx = resume_from + offset
                if await check_task_paused(task_id):
                    await persist_pause_checkpoint(session, task_id, q_idx)
                    await session.commit()
                    return

                question, context, ground_truth, judging_mode, eval_prompt = extract_question_fields(item)
                # 管线层统一解析参照源:按问题从本体包检索 top-K(若命中)优先于数据集 context,省 token 且同题跨轮稳定
                _onto_ref = _load_ontology_reference(ontology_package, question)
                if _onto_ref:
                    context = _onto_ref
                model_answer = (
                    item.get("model_answer")
                    or item.get("answer")
                    or item.get("output")
                    or ""
                )
                # Mode_Answer 为空时从 Langfuse 拉取
                if (not model_answer or not model_answer.strip()) and langfuse_answer_map and q_idx in langfuse_answer_map:
                    lf_answer = langfuse_answer_map[q_idx]
                    if lf_answer and lf_answer.strip():
                        model_answer = lf_answer
                if not isinstance(model_answer, str):
                    model_answer = str(model_answer) if model_answer is not None else ""
                model_answer = model_answer.strip()

                completed_metrics = await load_completed_metrics(session, task_id, q_idx)
                if completed_metrics.issuperset(set(metrics_to_run)):
                    # 跳过已完成的问题但仍需推进进度，避免进度条停滞
                    skipped_steps = len(metrics_to_run)
                    progress += skipped_steps
                    quality_progress += sum(1 for m in metrics_to_run if not _is_rt(m)) * JUDGE_RUN_COUNT
                    await session.execute(
                        update(EvaluationTask)
                        .where(EvaluationTask.id == task_id)
                        .values(progress_current=progress)
                    )
                    await session.commit()
                    continue

                pending_metrics = [m for m in metrics_to_run if m not in completed_metrics]
                if not pending_metrics:
                    continue

                for metric_name in pending_metrics:
                    if await check_task_paused(task_id):
                        await persist_pause_checkpoint(session, task_id, q_idx)
                        await session.commit()
                        return

                    await emit_event(
                        task_id,
                        "judge_start",
                        {"question_id": q_idx, "metric": metric_name},
                    )
                    await emit_progress_tick(
                        emit_event,
                        task_id,
                        current=progress,
                        total=progress_total,
                        phase="judge",
                        question_id=q_idx,
                        metric=metric_name,
                        quality_current=quality_progress,
                        quality_total=quality_total,
                    )

                    # === 分支1：DeepEval 指标 ===
                    if metric_name in DEEPEVAL_METRIC_KEYS:
                        _spec = SPEC_BY_KEY.get(metric_name)
                        threshold = _spec.gate_threshold if _spec else METRICS_CONFIG.get(metric_name, 0.5)
                        try:
                            metric = get_metric(metric_name)
                            # Phase 3: 离线按 question_id 查 trace_id,拉 trace 提取过程数据(缓存 per trace_id)
                            _lf_trace_id = trace_id_map.get(q_idx)
                            process_data = _offline_trace_cache.get(_lf_trace_id) if _lf_trace_id else None
                            if process_data is None and _lf_trace_id:
                                from services.langfuse_trace_extract import (
                                    fetch_process_data_for_trace,
                                )
                                _lf_project = (langfuse_source or {}).get("project") if isinstance(langfuse_source, dict) else None
                                process_data = await fetch_process_data_for_trace(_lf_trace_id, _lf_project)
                                _offline_trace_cache[_lf_trace_id] = process_data
                            er = await metric.evaluate(
                                question=question,
                                answer=model_answer,
                                context=context,
                                ground_truth=ground_truth,
                                tool_calls=process_data.tool_calls if process_data else None,
                                thoughts=process_data.thoughts if process_data else None,
                                retrieval_context=process_data.retrieval_context if process_data else None,
                                turns=process_data.turns if process_data else None,
                                system_prompt=eval_prompt or None,
                            )
                            eval_result = {"score": er.score, "reason": er.reason, "duration_ms": er.duration_ms}
                        except Exception as e:
                            eval_result = {"score": 0.0, "reason": f"{metric_name}评估失败：{e}", "duration_ms": 0}

                        progress += 1
                        quality_progress += 1
                        await _persist_quality_evaluation_result(
                            session,
                            task_id=task_id,
                            question_id=q_idx,
                            metric_name=metric_name,
                            threshold=threshold,
                            eval_result=eval_result,
                            use_script_eval=False,
                            script_id=None,
                            scripts_cache=scripts_cache,
                            question=question,
                            ground_truth=ground_truth,
                            model_answer=model_answer,
                            agent_name=llm_model_name,
                            llm_model=llm_model,
                            llm_model_name=llm_model_name,
                            inference_duration_ms=None,
                            agent_run_id=None,
                            eval_method_override="deepeval",
                        )
                        await session.execute(
                            update(EvaluationTask)
                            .where(EvaluationTask.id == task_id)
                            .values(progress_current=progress)
                        )
                        await session.commit()
                        await emit_progress_tick(
                            emit_event, task_id,
                            current=progress, total=progress_total,
                            phase="judge_done",
                            question_id=q_idx, metric=metric_name,
                            quality_current=quality_progress, quality_total=quality_total,
                        )
                        continue

                    # === 分支2：自定义指标 ===
                    if metric_name.startswith("custom_"):
                        cm_def = next((c for c in custom_metric_defs if c["key"] == metric_name), None)
                        if cm_def:
                            threshold = cm_def.get("threshold", 0.5)
                            eval_result = await _run_custom_metric(
                                cm_def, question, model_answer, context, ground_truth,
                                llm_model, llm_model_name,
                            )
                            eval_tag = f"custom_{cm_def['kind']}"
                            progress += 1
                            quality_progress += 1
                            await _persist_quality_evaluation_result(
                                session,
                                task_id=task_id,
                                question_id=q_idx,
                                metric_name=metric_name,
                                threshold=threshold,
                                eval_result=eval_result,
                                use_script_eval=False,
                                script_id=None,
                                scripts_cache=scripts_cache,
                                question=question,
                                ground_truth=ground_truth,
                                model_answer=model_answer,
                                agent_name=llm_model_name,
                                llm_model=llm_model,
                                llm_model_name=llm_model_name,
                                inference_duration_ms=None,
                                agent_run_id=None,
                                eval_method_override=eval_tag,
                            )
                            await session.execute(
                                update(EvaluationTask)
                                .where(EvaluationTask.id == task_id)
                                .values(progress_current=progress)
                            )
                            await session.commit()
                            await emit_progress_tick(
                                emit_event, task_id,
                                current=progress, total=progress_total,
                                phase="judge_done",
                                question_id=q_idx, metric=metric_name,
                                quality_current=quality_progress, quality_total=quality_total,
                            )
                            continue

                    # === 分支3：原有指标（faithfulness/answer_relevancy/correctness + script）===
                    threshold = METRICS_CONFIG.get(metric_name, 0.8)
                    script_id = custom_scripts.get(str(q_idx)) if custom_scripts else None
                    use_script_eval = (
                        script_id
                        and script_id in scripts_cache
                        and metric_name == "correctness"
                    )
                    custom_script_metric = None
                    if use_script_eval:
                        script_obj = scripts_cache[script_id]
                        custom_script_metric = CustomScriptMetric(
                            script_code=script_obj.script_code,
                            script_name=script_obj.name,
                        )

                    eval_result = await _judge_quality_metric_once(
                        task_id=task_id,
                        question_id=q_idx,
                        model_name=model_name,
                        metric_name=metric_name,
                        threshold=threshold,
                        question=question,
                        model_answer=model_answer,
                        context=context,
                        ground_truth=ground_truth,
                        llm_model=llm_model,
                        use_bm25=use_bm25,
                        use_doc_library=use_doc_library,
                        use_script_eval=bool(use_script_eval and custom_script_metric),
                        custom_script_metric=custom_script_metric,
                        evaluate_single=evaluate_single,
                        evaluate_with_script=evaluate_with_script,
                        ontology_package=ontology_package,
                        judging_mode=judging_mode,
                        eval_prompt=eval_prompt,
                    )

                    progress += 1
                    quality_progress += 1

                    await _persist_quality_evaluation_result(
                        session,
                        task_id=task_id,
                        question_id=q_idx,
                        metric_name=metric_name,
                        threshold=threshold,
                        eval_result=eval_result,
                        use_script_eval=bool(use_script_eval and custom_script_metric),
                        script_id=script_id if use_script_eval else None,
                        scripts_cache=scripts_cache,
                        question=question,
                        ground_truth=ground_truth,
                        model_answer=model_answer,
                        agent_name=llm_model_name,
                        llm_model=llm_model,
                        llm_model_name=llm_model_name,
                        inference_duration_ms=None,
                        agent_run_id=None,
                    )
                    # 进度与结果在同一个事务中持久化，避免崩溃时进度双计
                    await session.execute(
                        update(EvaluationTask)
                        .where(EvaluationTask.id == task_id)
                        .values(progress_current=progress)
                    )
                    await session.commit()
                    await emit_progress_tick(
                        emit_event,
                        task_id,
                        current=progress,
                        total=progress_total,
                        phase="judge_done",
                        question_id=q_idx,
                        metric=metric_name,
                        quality_current=quality_progress,
                        quality_total=quality_total,
                    )

        try:
            async with async_session() as session:
                result = await session.execute(
                    select(EvaluationResult).where(EvaluationResult.task_id == task_id)
                )
                results_list = [
                    {
                        "question_id": r.question_id,
                        "metric_name": r.metric_name,
                        "score": r.avg_score if r.avg_score is not None else r.score,
                        "passed": r.passed,
                        "eval_reason": r.eval_reason or "",
                    }
                    for r in result.scalars().all()
                ]
                from services.task_dimension_verdict_service import build_task_dimension_verdicts

                dimension_verdicts = await build_task_dimension_verdicts(session, task_id)
                agent_name = await resolve_task_agent_name(session, task_id)
                fail_examples = _build_fail_examples(results_list)
                summary = await generate_evaluation_summary(
                    task_id=task_id,
                    dataset_id=dataset_id,
                    model_name=model_name,
                    agent_name=agent_name,
                    results=results_list,
                    model_key=llm_model,
                    dimension_verdicts=dimension_verdicts,
                    fail_examples=fail_examples,
                )
                await session.execute(
                    update(EvaluationTask)
                    .where(EvaluationTask.id == task_id)
                    .values(summary=summary)
                )
                await session.commit()
                await emit_event(task_id, "summary_generated", {"summary": summary})
        except Exception as e:
            log("ERROR", "TASK", "评估总结生成失败", task_id=task_id, error=str(e))
            await emit_event(task_id, "summary_error", {"error": str(e)})

        # 质量指标完成 → 执行过程评估（场景交付 trace 拉取，仅在离线模式）
        if langfuse_source:
            log(
                "INFO",
                "TASK",
                "质量指标评估完成，开始过程评估（场景交付）",
                task_id=task_id,
            )
            pe_base = progress_total - question_count if question_count else 0
            try:
                await _run_offline_process_eval(
                    task_id,
                    emit_event,
                    progress_base=pe_base,
                    progress_total=progress_total,
                    scenario_total=0,  # 进度由 scenario_eval_start 推送真实步骤数
                    quality_progress=quality_progress,
                    quality_total=quality_total,
                )
            except Exception as e:
                log("ERROR", "TASK", "过程评估（场景交付）失败", task_id=task_id, error=str(e))

        # 场景子步骤打分（在线/离线通用）
        await _run_auto_scenario_scoring(task_id, emit_event)

        # 质量指标和场景交付都完成后，标记任务完成
        async with async_session() as session:
            await session.execute(
                update(EvaluationTask)
                .where(EvaluationTask.id == task_id)
                .values(status=TaskStatus.COMPLETED, completed_at=datetime.utcnow())
            )
            await session.commit()

        await emit_event(task_id, "task_complete", {"status": "completed"})
        log("INFO", "TASK", "离线评估 Pipeline 完成", task_id=task_id)

    except Exception as e:
        log("ERROR", "TASK", "离线评估 Pipeline 失败", task_id=task_id, error=str(e))
        async with async_session() as session:
            await session.execute(
                update(EvaluationTask)
                .where(EvaluationTask.id == task_id)
                .values(status=TaskStatus.FAILED, error=str(e))
            )
            await session.commit()
        await emit_event(task_id, "task_error", {"error": str(e)})
        raise
