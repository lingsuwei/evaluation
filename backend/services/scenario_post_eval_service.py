"""评测完成后自动场景子步骤打分（阶段 E-1）。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, EvaluationResult, EvaluationTask, ScenarioStepResult
from services.scenario_eval_service import (
    get_template_by_key,
    resolve_scenario_for_run,
    score_run_against_scenario,
)
from services.scenario_template_utils import template_with_active_steps_only
from logger import log


async def score_scenario_for_task(
    session: AsyncSession,
    task_id: str,
    *,
    only_success: bool = True,
    emit_event=None,
) -> Dict[str, Any]:
    """
    对任务下所有 Run 按解析到的场景模板打分并持久化 ScenarioStepResult / span_refs。
    支持通过 emit_event 回调推送子步骤级别的进度事件。
    """
    log("INFO", "SCENARIO", "开始场景子步骤打分", task_id=task_id, only_success=only_success)

    runs = (
        await session.execute(select(AgentRun).where(AgentRun.task_id == task_id))
    ).scalars().all()
    scored: List[Dict[str, Any]] = []
    skipped: List[str] = []
    total_runs = len(runs)
    completed_runs = 0

    # 过滤有效的runs
    valid_runs = [r for r in runs if (not only_success or (r.status or "").lower() == "success")]
    log("INFO", "SCENARIO", "获取到AgentRun列表", task_id=task_id, total_runs=total_runs, valid_runs=len(valid_runs))

    # === 批量预加载（复用 task_scenario_matrix_service 模式，消除 N+1 查询）===
    run_ids = [r.id for r in valid_runs]

    # 批量查 ScenarioStepResult（1 次 IN 查询替代每题 1 次）
    preloaded_steps_by_run: Dict[str, List[Any]] = {}
    if run_ids:
        _steps_rows = (
            await session.execute(
                select(ScenarioStepResult).where(
                    ScenarioStepResult.agent_run_id.in_(run_ids)
                )
            )
        ).scalars().all()
        for _sr in _steps_rows:
            preloaded_steps_by_run.setdefault(_sr.agent_run_id, []).append(_sr)

    # 批量查 EvaluationResult（1 次 IN 查询替代每题 1 次）
    preloaded_eval_by_run: Dict[str, Any] = {}
    if run_ids:
        _eval_rows = (
            await session.execute(
                select(EvaluationResult).where(
                    EvaluationResult.agent_run_id.in_(run_ids)
                )
            )
        ).scalars().all()
        for _er in _eval_rows:
            preloaded_eval_by_run.setdefault(_er.agent_run_id, _er)

    # 模板缓存：同 scenario_key 只查一次 DB
    template_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    async def _get_cached_template(sk: str) -> Optional[Dict[str, Any]]:
        if sk not in template_cache:
            template_cache[sk] = await get_template_by_key(session, sk)
        return template_cache[sk]

    # 计算总步骤数（用于初始化进度）
    total_steps = 0
    run_scenarios = []  # 存储每个run的场景信息

    for run in valid_runs:
        key = await resolve_scenario_for_run(session, run.id)
        if not key:
            log("INFO", "SCENARIO", "Run无法解析场景模板", task_id=task_id, agent_run_id=run.id, question_id=run.question_id)
            continue

        tpl_raw = await _get_cached_template(key)
        if tpl_raw:
            tpl_data = template_with_active_steps_only(tpl_raw)
            step_count = len(tpl_data.get("steps") or [])
            total_steps += step_count
            run_scenarios.append({"run": run, "key": key, "step_count": step_count})
            log("INFO", "SCENARIO", "Run解析场景成功", task_id=task_id, agent_run_id=run.id, question_id=run.question_id, scenario_key=key, steps=step_count)
        else:
            log("WARNING", "SCENARIO", "场景模板不存在", task_id=task_id, agent_run_id=run.id, scenario_key=key)

    log("INFO", "SCENARIO", "场景评估准备完成", task_id=task_id, total_runs_to_evaluate=len(run_scenarios), total_steps=total_steps)

    # 发送场景评估开始事件（包含步骤总数）
    if emit_event:
        await emit_event(task_id, "scenario_eval_start", {
            "total_runs": len(valid_runs),
            "total_steps": total_steps,
        })
        log("INFO", "SCENARIO", "已发送scenario_eval_start事件", task_id=task_id, total_runs=len(valid_runs), total_steps=total_steps)

    current_step = 0
    for idx, run_info in enumerate(run_scenarios):
        run = run_info["run"]
        key = run_info["key"]

        log("INFO", "SCENARIO", "开始评估Run", task_id=task_id, run_index=idx + 1, total=len(run_scenarios), agent_run_id=run.id, question_id=run.question_id, scenario_key=key)

        # 发送当前 run 开始评估事件
        if emit_event:
            await emit_event(task_id, "scenario_run_start", {
                "run_index": idx + 1,
                "total_runs": len(run_scenarios),
                "question_id": run.question_id,
                "scenario_key": key,
            })

        rep = await score_run_against_scenario(
            session, run.id, key, persist=True, _emit_step_event=emit_event,
            _task_id=task_id, _current_step_ref=[current_step], _total_steps=total_steps,
            run_obj=run,
            _preloaded_steps=preloaded_steps_by_run.get(run.id),
            _preloaded_eval_row=preloaded_eval_by_run.get(run.id),
            _preloaded_template=await _get_cached_template(key),
        )
        if rep:
            steps = rep.get("steps") or []
            current_step += len(steps)
            completed_runs += 1
            scored.append(
                {
                    "agent_run_id": run.id,
                    "question_id": run.question_id,
                    "scenario_key": rep["scenario_key"],
                    "composite_score": rep["composite_score"],
                    "steps_count": len(steps),
                    "steps": [
                        {
                            "step_key": s.get("step_key"),
                            "name": s.get("name"),
                            "score": s.get("score"),
                            "passed": s.get("passed"),
                            "evidence": s.get("evidence"),  # 添加evidence信息
                        }
                        for s in steps
                    ],
                }
            )
            log("INFO", "SCENARIO", "Run评估完成", task_id=task_id, agent_run_id=run.id, composite_score=rep["composite_score"], steps_count=len(steps))

            # 发送当前 run 完成事件
            if emit_event:
                await emit_event(task_id, "scenario_run_done", {
                    "run_index": idx + 1,
                    "total_runs": len(run_scenarios),
                    "question_id": run.question_id,
                    "composite_score": rep["composite_score"],
                    "steps_count": len(steps),
                })
        else:
            skipped.append(run.id)
            log("WARNING", "SCENARIO", "Run评估返回空结果", task_id=task_id, agent_run_id=run.id)

    # 记录被跳过的runs（无法解析场景的）
    for run in runs:
        if run.id not in [s["agent_run_id"] for s in scored] and run.id not in skipped:
            if only_success and (run.status or "").lower() != "success":
                skipped.append(run.id)
            elif run.id not in [rs["run"].id for rs in run_scenarios]:
                skipped.append(run.id)

    await session.flush()
    log(
        "INFO",
        "SCENARIO",
        "任务场景子步骤打分完成",
        task_id=task_id,
        scored_count=len(scored),
        skipped_count=len(skipped),
    )
    return {
        "task_id": task_id,
        "scored_count": len(scored),
        "skipped_count": len(skipped),
        "runs": scored,
    }


async def run_post_eval_scenario_if_enabled(
    session: AsyncSession,
    task_id: str,
    *,
    emit_event=None,
) -> Optional[Dict[str, Any]]:
    """读取任务开关，在 Pipeline 结束时触发场景打分（在线 / 离线均支持）。

    离线任务通过 Langfuse 历史回放产生的 stub AgentRun 同样会被场景打分。
    """
    task = await session.get(EvaluationTask, task_id)
    if not task:
        return None
    # 不再拦截离线任务：离线 Langfuse 回放模式也会产出 AgentRun，
    # 需要场景打分才能让 scenario_delivery 维度可用。
    enabled = getattr(task, "auto_scenario_score", True)
    if enabled is False:
        return {"task_id": task_id, "skipped": True, "reason": "auto_scenario_score disabled"}
    return await score_scenario_for_task(session, task_id, emit_event=emit_event)
