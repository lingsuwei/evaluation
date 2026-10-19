"""过程一致性总览读模型（P1）：按题聚合必达过程点 + 推理链条稳定性。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, CheckpointResult, EvaluationResult, ProcessCheckpointTemplate
from services.chain_stability_service import _chain_ok_for_run, build_chain_stability
from services.checkpoint_result_service import (
    _load_templates_by_id,
    _serialize_checkpoint_item,
    _sort_key_for_item,
    summarize_checkpoint_items,
)
from services.eval_context import get_question_id, get_task_id


async def _load_runs_for_task(session: AsyncSession, task_id: str) -> List[AgentRun]:
    tid = (task_id or "").strip()
    if not tid:
        return []
    result = await session.execute(
        select(AgentRun)
        .where(
            or_(
                AgentRun.task_id == tid,
                AgentRun.eval_context["task_id"].as_string() == tid,
            )
        )
        .order_by(AgentRun.question_id, AgentRun.started_at)
    )
    runs = list(result.scalars().all())
    if not runs:
        fallback = await session.execute(select(AgentRun).order_by(AgentRun.question_id))
        runs = [r for r in fallback.scalars().all() if get_task_id(r) == tid]
    if not runs:
        er_run_ids = (
            await session.execute(
                select(EvaluationResult.agent_run_id).where(
                    EvaluationResult.task_id == tid,
                    EvaluationResult.agent_run_id.isnot(None),
                )
            )
        ).scalars().all()
        unique_ids = sorted({rid for rid in er_run_ids if rid})
        if unique_ids:
            runs = list(
                (
                    await session.execute(
                        select(AgentRun)
                        .where(AgentRun.id.in_(unique_ids))
                        .order_by(AgentRun.question_id, AgentRun.started_at)
                    )
                ).scalars().all()
            )
    return runs


async def _load_checkpoint_results_for_runs(
    session: AsyncSession, run_ids: List[str]
) -> Dict[str, List[CheckpointResult]]:
    if not run_ids:
        return {}
    rows = (
        await session.execute(
            select(CheckpointResult).where(CheckpointResult.agent_run_id.in_(run_ids))
        )
    ).scalars().all()
    grouped: Dict[str, List[CheckpointResult]] = defaultdict(list)
    for r in rows:
        grouped[r.agent_run_id].append(r)
    return grouped


def _build_run_checkpoint_summary(
    run: AgentRun,
    cp_results: List[CheckpointResult],
    tpl_map: Dict[str, ProcessCheckpointTemplate],
) -> Dict[str, Any]:
    items = [_serialize_checkpoint_item(r, tpl_map.get(r.checkpoint_id)) for r in cp_results]
    items.sort(key=_sort_key_for_item)
    breakdown = summarize_checkpoint_items(items)
    required_items = [i for i in items if i.get("required")]
    # items 为空（Run 存在但未产出 CheckpointResult）时视为未通过：
    # 典型场景是链路断联 / 任务被取消，过程点评估未执行，不能计为"必达全通过"。
    # 此时 required_total 从 tpl_map 推导期望的必达点数量（而非 0），
    # 使 required_pass_rate 的分子/分母能真实反映断联影响，
    # 而非将断联轮次在分子分母同时过滤掉导致 pass_rate 虚高。
    if not items:
        required_all_passed = False
        expected_required = sum(1 for t in tpl_map.values() if bool(t.required))
        req_total = expected_required
        req_passed = 0
    else:
        required_all_passed = all(i.get("passed") for i in required_items) if required_items else True
        req_total = breakdown["required_total"]
        req_passed = breakdown["required_passed"]
    return {
        "agent_run_id": run.id,
        "run_index": run.run_index if run.run_index is not None else 0,
        "agent_name": run.agent_name,
        "required_total": req_total,
        "required_passed": req_passed,
        "required_all_passed": required_all_passed,
        "optional_total": breakdown["optional_total"],
        "optional_hit": breakdown["optional_hit"],
        "items": items,
    }


def _question_chain_fields(
    run_summaries: List[Dict[str, Any]],
    tpl_map: Dict[str, ProcessCheckpointTemplate],
    is_multi_run: bool,
) -> Dict[str, Any]:
    if not is_multi_run:
        return {
            "chain_status": "单轮",
            "chain_pass_rate": None,
            "rounds_all_ok": 0,
            "round_count": len(run_summaries),
            "all_rounds_ok": False,
        }

    ok_count = sum(
        1 for s in run_summaries if _chain_ok_for_run(s["items"], tpl_map)
    )
    round_count = len(run_summaries)
    all_ok = ok_count == round_count and round_count > 0
    return {
        "chain_status": "全轮通过" if all_ok else "有轮未过",
        "chain_pass_rate": round(ok_count / round_count, 4) if round_count else None,
        "rounds_all_ok": ok_count,
        "round_count": round_count,
        "all_rounds_ok": all_ok,
    }


def _empty_summary() -> Dict[str, Any]:
    return {
        "total_runs": 0,
        "multi_run_questions": 0,
        "single_run_questions": 0,
        "green": 0,
        "yellow": 0,
        "red": 0,
        "chain_pass_rate": None,
        "questions_all_rounds_ok": 0,
        "question_count": 0,
        "weakest_chain": None,
    }


_QUESTION_STATUS_SCORE = {"green": 1.0, "yellow": 0.75, "red": 0.0}


def classify_question_consistency_bucket(
    *,
    required_status: str,
    is_multi_run: bool,
    all_rounds_ok: bool,
) -> str:
    """与 summary 绿/黄/红计数及四维过程一致性结论同源的分桶规则。"""
    if required_status == "red":
        return "red"
    if required_status == "green" and (not is_multi_run or all_rounds_ok):
        return "green"
    return "yellow"


def consistency_bucket_from_question(q: Dict[str, Any]) -> str:
    return classify_question_consistency_bucket(
        required_status=str(q.get("required_status") or "red"),
        is_multi_run=bool(q.get("is_multi_run")),
        all_rounds_ok=bool(q.get("all_rounds_ok")),
    )


def empty_consistency_score_metrics() -> Dict[str, Any]:
    return {
        "process_score": None,
        "required_pass_rate": None,
        "required_passed": 0,
        "required_total": 0,
        "process_run_count": 0,
        "chain_pass_rate": None,
        "red": 0,
    }


def score_metrics_from_consistency_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """从过程一致性读模型提取质量总分用的过程分与必达过程点通过率。"""
    summary = payload.get("summary") or {}
    questions = payload.get("questions") or []
    total_runs = int(summary.get("total_runs") or 0)

    if total_runs == 0 and not questions:
        return empty_consistency_score_metrics()

    required_total = 0
    required_passed = 0
    for q in questions:
        for run in q.get("runs") or []:
            required_total += int(run.get("required_total") or 0)
            required_passed += int(run.get("required_passed") or 0)

    if required_total == 0:
        return {
            **empty_consistency_score_metrics(),
            "process_run_count": total_runs,
            "chain_pass_rate": summary.get("chain_pass_rate"),
            "red": summary.get("red", 0),
        }

    q_scores = [
        _QUESTION_STATUS_SCORE[consistency_bucket_from_question(q)] for q in questions
    ]
    process_score = sum(q_scores) / len(q_scores) if q_scores else None
    if process_score is None:
        process_score = required_passed / required_total

    return {
        "process_score": round(float(process_score), 6),
        "required_pass_rate": round(required_passed / required_total, 6),
        "required_passed": required_passed,
        "required_total": required_total,
        "process_run_count": total_runs,
        "chain_pass_rate": summary.get("chain_pass_rate"),
        "red": summary.get("red", 0),
    }


async def compute_consistency_score_metrics(
    session: AsyncSession, task_id: str
) -> Dict[str, Any]:
    payload = await build_process_consistency(session, task_id)
    return score_metrics_from_consistency_payload(payload)


async def build_process_consistency(
    session: AsyncSession, task_id: str
) -> Dict[str, Any]:
    runs = await _load_runs_for_task(session, task_id)
    if not runs:
        return {"questions": [], "summary": _empty_summary()}

    run_ids = [r.id for r in runs]
    cp_by_run = await _load_checkpoint_results_for_runs(session, run_ids)

    all_cp_ids: set[str] = set()
    for cps in cp_by_run.values():
        for cp in cps:
            all_cp_ids.add(cp.checkpoint_id)
    tpl_map = await _load_templates_by_id(session, list(all_cp_ids))

    runs_by_q: Dict[int, List[AgentRun]] = defaultdict(list)
    run_q_from_er: Dict[str, int] = {}
    er_q_rows = (
        await session.execute(
            select(EvaluationResult.question_id, EvaluationResult.agent_run_id).where(
                EvaluationResult.task_id == (task_id or "").strip(),
                EvaluationResult.agent_run_id.isnot(None),
            )
        )
    ).all()
    for qid, rid in er_q_rows:
        if rid and qid is not None and rid not in run_q_from_er:
            run_q_from_er[str(rid)] = int(qid)

    for r in runs:
        qid = get_question_id(r)
        if qid is None and r.id in run_q_from_er:
            qid = run_q_from_er[r.id]
        if qid is not None:
            runs_by_q[int(qid)].append(r)

    questions: List[Dict[str, Any]] = []
    green = yellow = red = 0
    multi_run_questions = 0
    single_run_questions = 0

    for qid in sorted(runs_by_q.keys()):
        q_runs = sorted(
            runs_by_q[qid],
            key=lambda r: (
                r.run_index if r.run_index is not None else 0,
                r.started_at.isoformat() if r.started_at else "",
                r.id,
            ),
        )

        run_summaries: List[Dict[str, Any]] = []
        for run in q_runs:
            cps = cp_by_run.get(run.id, [])
            run_summaries.append(_build_run_checkpoint_summary(run, cps, tpl_map))

        required_status = "green"
        all_required_passed = all(s["required_all_passed"] for s in run_summaries)
        if not all_required_passed:
            required_status = "red"
        else:
            optional_differ = False
            for s in run_summaries:
                if s["optional_total"] > 0 and s["optional_hit"] < s["optional_total"]:
                    optional_differ = True
                    break
            if optional_differ:
                required_status = "yellow"

        is_multi_run = len(q_runs) >= 2
        if is_multi_run:
            multi_run_questions += 1
        else:
            single_run_questions += 1

        chain_fields = _question_chain_fields(run_summaries, tpl_map, is_multi_run)

        bucket = classify_question_consistency_bucket(
            required_status=required_status,
            is_multi_run=is_multi_run,
            all_rounds_ok=bool(chain_fields["all_rounds_ok"]),
        )
        if bucket == "red":
            red += 1
        elif bucket == "green":
            green += 1
        else:
            yellow += 1

        questions.append({
            "question_id": qid,
            "run_count": len(q_runs),
            "is_multi_run": is_multi_run,
            "required_status": required_status,
            **chain_fields,
            "runs": run_summaries,
        })

    chain_stability = await build_chain_stability(
        session, task_id, runs, cp_by_run=cp_by_run, tpl_map=tpl_map
    )

    return {
        "questions": questions,
        "summary": {
            "total_runs": len(runs),
            "multi_run_questions": multi_run_questions,
            "single_run_questions": single_run_questions,
            "green": green,
            "yellow": yellow,
            "red": red,
            "chain_pass_rate": chain_stability.get("chain_pass_rate"),
            "questions_all_rounds_ok": chain_stability.get("questions_all_rounds_ok"),
            "question_count": chain_stability.get("question_count"),
            "weakest_chain": chain_stability.get("weakest_chain"),
        },
    }
