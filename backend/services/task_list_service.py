"""评估任务列表读模型（历史页 / 任务清单）。"""

from __future__ import annotations



from typing import Any, Dict, List, Optional



from sqlalchemy import func, select

from sqlalchemy.ext.asyncio import AsyncSession



from database import Agent, AgentRun, Dataset, EvaluationResult, EvaluationTask

from services.agent_workspace_display import resolve_workspace_display
from services.evaluation_mode import get_evaluation_mode, get_task_capabilities, get_task_dimension_flags
from services.evaluation_pipeline import parse_task_agent_ids
from services.inference_model_display import (
    summarize_execution_modes_from_runs,
    summarize_inference_models_from_runs,
)
from services.quality_score_service import compute_task_quality_scores
from services.task_agent_summary import build_task_agent_summary
from services.task_dimension_verdict_service import build_dimension_verdict_badges_for_tasks


# 历史列表仅在任务正常结束时展示质量/结果/过程分（进行中、暂停、取消等均为空）
_TASK_LIST_SCORE_STATUSES = frozenset({"completed"})


def _empty_list_scores() -> Dict[str, Any]:
    return {
        "quality_score": {},
        "total_score": None,
        "result_score": None,
        "process_score": None,
        "overall_avg_score": None,
    }


def _list_scores_for_task(
    task: EvaluationTask,
    quality_score: Dict[str, Any],
    overall_avg_score: Optional[float],
) -> Dict[str, Any]:
    if task.status not in _TASK_LIST_SCORE_STATUSES:
        return _empty_list_scores()
    return {
        "quality_score": quality_score,
        "total_score": quality_score.get("total_score"),
        "result_score": quality_score.get("result_score"),
        "process_score": quality_score.get("process_score"),
        "overall_avg_score": overall_avg_score,
    }



async def serialize_tasks_for_list(

    session: AsyncSession, tasks: List[EvaluationTask]

) -> List[Dict[str, Any]]:

    if not tasks:

        return []



    task_ids = [t.id for t in tasks]

    dataset_ids = {t.dataset_id for t in tasks if t.dataset_id}

    all_agent_ids: set[str] = set()

    for t in tasks:

        all_agent_ids.update(parse_task_agent_ids(t))



    datasets: Dict[str, str] = {}

    if dataset_ids:

        rows = await session.execute(select(Dataset).where(Dataset.id.in_(dataset_ids)))

        for ds in rows.scalars().all():

            datasets[ds.id] = ds.name



    agents: Dict[str, Agent] = {}

    if all_agent_ids:

        rows = await session.execute(select(Agent).where(Agent.id.in_(all_agent_ids)))

        for ag in rows.scalars().all():

            agents[ag.id] = ag



    avg_by_task: Dict[str, float] = {}

    score_rows = await session.execute(

        select(EvaluationResult.task_id, func.avg(EvaluationResult.avg_score))

        .where(EvaluationResult.task_id.in_(task_ids))

        .where(EvaluationResult.avg_score.isnot(None))

        .group_by(EvaluationResult.task_id)

    )

    for tid, avg_val in score_rows.all():

        if avg_val is not None:

            avg_by_task[str(tid)] = round(float(avg_val), 3)

    runs_by_task: Dict[str, List[AgentRun]] = {tid: [] for tid in task_ids}
    run_rows = await session.execute(
        select(AgentRun).where(AgentRun.task_id.in_(task_ids))
    )
    for run in run_rows.scalars().all():
        if run.task_id in runs_by_task:
            runs_by_task[run.task_id].append(run)



    # 历史列表是高频页面，不能因 onto-agent 工作空间接口不可达而阻塞加载。
    # 展示优先使用本地保存的 workspace_name；没有时由 resolve_workspace_display 回退到 workspace_id。
    workspace_name_map: Dict[str, str] = {}
    score_task_ids = [t.id for t in tasks if t.status in _TASK_LIST_SCORE_STATUSES]
    quality_scores = (
        await compute_task_quality_scores(session, score_task_ids)
        if score_task_ids
        else {}
    )

    items: List[Dict[str, Any]] = []

    # 四维评估结论徽章：复用已算好的 quality_scores，避免重复计算
    verdict_badges = await build_dimension_verdict_badges_for_tasks(
        session, tasks, quality_scores=quality_scores
    )

    for t in tasks:

        summary = await build_task_agent_summary(session, t)
        quality_score = quality_scores.get(t.id, {})
        list_scores = _list_scores_for_task(t, quality_score, avg_by_task.get(t.id))

        primary_id = (summary["agent_ids"][0] if summary["agent_ids"] else t.agent_id)

        agent = agents.get(primary_id) if primary_id else None

        if primary_id and agent:

            agent_name = agent.name

            agent_workspace_name = resolve_workspace_display(agent, workspace_name_map)

        elif t.agent_id:

            agent_name = None

            agent_workspace_name = None

        else:

            agent_name = "离线评估"

            agent_workspace_name = None



        dataset_name = datasets.get(t.dataset_id) if t.dataset_id else None

        if t.dataset_id and not dataset_name:

            dataset_name = "（评测集已删除）"



        items.append(

            {

                "id": t.id,

                "dataset_id": t.dataset_id,

                "dataset_name": dataset_name,

                "status": t.status,

                "agent_id": primary_id,

                "evaluation_mode": get_evaluation_mode(t),

                "capabilities": get_task_capabilities(t),

                "dimension_flags": get_task_dimension_flags(t),

                "agent_name": agent_name,

                "agent_workspace_name": agent_workspace_name,
                # 兼容旧前端字段名
                "agent_project_name": agent_workspace_name,

                "agent_count": summary["agent_count"],

                "agent_versions_label": summary["agent_versions_label"],

                "agent_versions": summary["agent_versions"],

                **list_scores,

                # 四维评估结论徽章（仅已完成/已取消/失败任务有值）
                "dimension_verdict_badges": verdict_badges.get(t.id),

                "inference_model_summary": summarize_inference_models_from_runs(
                    runs_by_task.get(t.id, [])
                ),
                "execution_mode_summary": summarize_execution_modes_from_runs(
                    runs_by_task.get(t.id, [])
                ),
                # 评估使用的 LLM 模型（判官模型），与被测推理模型区分
                "llm_model": t.llm_model,
                # 实际 Agent 运行次数（AgentRun 记录数）
                "agent_run_count": len(runs_by_task.get(t.id, [])),

            }

        )

    return items

