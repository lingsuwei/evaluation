"""进行中任务的部分结果聚合（EVAL-UX B1）。"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import Dataset, EvaluationResult, EvaluationTask
from services.evaluation_mode import get_evaluation_mode, get_task_capabilities, is_offline_task
from services.quality_score_service import RUNTIME_METRICS


def build_result_details_rows(
    results: List[EvaluationResult],
    question_map: Dict[int, str],
) -> List[dict]:
    details = []
    for r in results:
        details.append({
            "question_id": r.question_id,
            "model": r.model,
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
                "inference_duration_ms": r.inference_duration_ms,
            }],
        })
    return details


async def build_partial_result_payload(
    session: AsyncSession,
    task: EvaluationTask,
) -> Dict[str, Any]:
    """任务未完成时返回已落库明细与预览汇总。"""
    offline = is_offline_task(task)
    evaluation_mode = get_evaluation_mode(task)
    capabilities = get_task_capabilities(task)

    results = (
        await session.execute(
            select(EvaluationResult).where(EvaluationResult.task_id == task.id)
        )
    ).scalars().all()

    if offline:
        results = [
            r
            for r in results
            if r.metric_name not in RUNTIME_METRICS
            and (r.eval_method or "") != "runtime"
        ]

    dataset_result = await session.execute(
        select(Dataset).where(Dataset.id == task.dataset_id)
    )
    dataset = dataset_result.scalar_one_or_none()
    question_map: Dict[int, str] = {}
    if dataset and dataset.data:
        try:
            dataset_items = json.loads(dataset.data)
            for idx, item in enumerate(dataset_items):
                question = item.get("question") or item.get("q") or item.get("input", "")
                if question:
                    question_map[idx] = question
        except json.JSONDecodeError:
            pass

    completed_question_ids = sorted({r.question_id for r in results if r.question_id is not None})
    details = build_result_details_rows(results, question_map)
    # 题目文本按 question_id 去重（前端表格仅用 question_id 编号，文本外键化到 questions）
    questions_map: Dict[int, str] = {}
    for r in results:
        qid = r.question_id
        if qid is not None and qid not in questions_map:
            questions_map[qid] = r.question or question_map.get(qid, "")

    quality_rows = [r for r in results if r.metric_name not in RUNTIME_METRICS]
    preview: Optional[Dict[str, Any]] = None
    if quality_rows:
        # Phase 5: 区分「已跳过」(score=None) 和「已评分」(score=float)
        # 跳过项不参与 avg_score 和 pass_rate 统计(分母排除)
        scored_rows = [r for r in quality_rows if (r.avg_score if r.avg_score is not None else r.score) is not None]
        skipped_count = len(quality_rows) - len(scored_rows)
        scores = [
            float(r.avg_score if r.avg_score is not None else r.score)
            for r in scored_rows
        ]
        passed = sum(1 for r in scored_rows if r.passed)
        scored_count = len(scored_rows)
        preview = {
            "is_partial": True,
            "metric_result_count": len(quality_rows),
            "completed_question_count": len(completed_question_ids),
            "avg_score": round(sum(scores) / len(scores), 4) if scores else None,
            "pass_rate": round(passed / scored_count, 4) if scored_count > 0 else None,
            "skipped_count": skipped_count,
            "scored_count": scored_count,
        }

    from config import get_model_display_name

    llm_model_key = task.llm_model or "glm"
    judge_model_name = get_model_display_name(llm_model_key)

    return {
        "status": task.status,
        "progress": f"{task.progress_current}/{task.progress_total}",
        "is_paused": task.paused_at is not None,
        "partial": True,
        "evaluation_mode": evaluation_mode,
        "capabilities": capabilities,
        "completed_question_ids": completed_question_ids,
        "details": details,
        "questions": {str(k): v for k, v in questions_map.items()},
        "partial_quality_score": preview,
        "judge_model_name": judge_model_name,
        "answer_source": "dataset" if offline else "agent",
    }
