"""过程点合规读模型（M3 / 任务详情 Tab）。"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

CheckpointOutcome = Literal[
    "required_hit",
    "required_miss",
    "optional_hit",
    "optional_skip",
]

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, CheckpointResult, EvaluationResult, ProcessCheckpointTemplate
from services.eval_context import get_question_id, get_task_id


async def _resolve_run(session: AsyncSession, run_id: str) -> Optional[AgentRun]:
    rid = (run_id or "").strip()
    if not rid:
        return None
    run = await session.get(AgentRun, rid)
    if run:
        return run
    result = await session.execute(
        select(AgentRun).where(or_(AgentRun.trace_id == rid, AgentRun.id == rid))
    )
    return result.scalars().first()


async def _load_templates_by_id(
    session: AsyncSession, checkpoint_ids: List[str]
) -> Dict[str, ProcessCheckpointTemplate]:
    if not checkpoint_ids:
        return {}
    rows = (
        await session.execute(
            select(ProcessCheckpointTemplate).where(
                ProcessCheckpointTemplate.id.in_(checkpoint_ids)
            )
        )
    ).scalars().all()
    return {t.id: t for t in rows}


def compute_checkpoint_outcome(
    *,
    required: bool,
    hit: bool,
    passed: bool,
) -> CheckpointOutcome:
    """UI 四态：必达合规/缺失，可选已执行/未执行。"""
    if required:
        return "required_hit" if passed else "required_miss"
    return "optional_hit" if hit else "optional_skip"


def summarize_checkpoint_items(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按必达/可选拆分汇总指标。"""
    required_items = [i for i in items if i.get("required")]
    optional_items = [i for i in items if not i.get("required")]

    required_total = len(required_items)
    required_passed = sum(1 for i in required_items if i.get("passed"))
    optional_total = len(optional_items)
    optional_hit = sum(1 for i in optional_items if i.get("hit"))

    required_rate = (
        round(required_passed / required_total, 4) if required_total else None
    )
    optional_rate = round(optional_hit / optional_total, 4) if optional_total else None

    return {
        "required_total": required_total,
        "required_passed": required_passed,
        "required_compliance_rate": required_rate,
        "optional_total": optional_total,
        "optional_hit": optional_hit,
        "optional_hit_rate": optional_rate,
    }


def _sort_key_for_item(item: Dict[str, Any]) -> tuple:
    order = item.get("order")
    return (
        order if order is not None else 10_000,
        item.get("name") or "",
        item.get("checkpoint_id") or "",
    )


def _serialize_checkpoint_item(
    result: CheckpointResult,
    template: Optional[ProcessCheckpointTemplate],
) -> Dict[str, Any]:
    evidence = result.evidence if isinstance(result.evidence, dict) else {}
    tpl_order = template.order if template else None
    order_ok = evidence.get("order_ok")
    required = bool(template.required) if template else False
    hit = bool(evidence.get("hit"))
    passed = bool(result.passed)
    outcome = compute_checkpoint_outcome(required=required, hit=hit, passed=passed)
    return {
        "checkpoint_id": result.checkpoint_id,
        "name": template.name if template else result.checkpoint_id,
        "description": template.description if template else None,
        "order": tpl_order,
        "enforce_order": bool(getattr(template, "enforce_order", False)) if template else None,
        "required": required if template else None,
        "passed": passed,
        "hit": evidence.get("hit"),
        "order_ok": order_ok,
        "outcome": outcome,
        "evidence": evidence,
    }


def _summarize_run_checkpoint(
    run: AgentRun, items: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """纯函数：由 run 元数据 + 已排序 items 构造单 Run report（不查库）。"""
    total = len(items)
    passed = sum(1 for i in items if i["passed"])
    breakdown = summarize_checkpoint_items(items)
    required_rate = breakdown["required_compliance_rate"]
    return {
        "agent_run_id": run.id,
        "run_index": run.run_index if run.run_index is not None else 0,
        "task_id": get_task_id(run) or run.task_id,
        "question_id": get_question_id(run)
        if get_question_id(run) is not None
        else run.question_id,
        "agent_id": run.agent_id,
        "agent_name": run.agent_name,
        "logical_agent_id": run.logical_agent_id,
        "trace_id": run.trace_id,
        "trace_sync_status": run.trace_sync_status,
        "checkpoint_total": total,
        "checkpoint_passed": passed,
        "compliance_rate": required_rate,
        "required_total": breakdown["required_total"],
        "required_passed": breakdown["required_passed"],
        "required_compliance_rate": required_rate,
        "optional_total": breakdown["optional_total"],
        "optional_hit": breakdown["optional_hit"],
        "optional_hit_rate": breakdown["optional_hit_rate"],
        "items": items,
    }


async def build_run_checkpoint_report(
    session: AsyncSession, run_id: str
) -> Optional[Dict[str, Any]]:
    """单 Run 过程点明细（按模板 order 排序）。"""
    run = await _resolve_run(session, run_id)
    if not run:
        return None

    results = (
        await session.execute(
            select(CheckpointResult).where(CheckpointResult.agent_run_id == run.id)
        )
    ).scalars().all()
    tpl_map = await _load_templates_by_id(
        session, [r.checkpoint_id for r in results]
    )

    items = [
        _serialize_checkpoint_item(r, tpl_map.get(r.checkpoint_id)) for r in results
    ]
    items.sort(key=_sort_key_for_item)
    return _summarize_run_checkpoint(run, items)


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


async def build_task_checkpoint_report(
    session: AsyncSession, task_id: str
) -> Dict[str, Any]:
    """任务级过程点合规汇总 + 各 Run 明细（批量加载，避免 N+1）。"""
    runs = await _load_runs_for_task(session, task_id)
    if not runs:
        return {
            "task_id": task_id,
            "runs": [],
            "summary": {
                "run_count": 0,
                "runs_with_checkpoints": 0,
                "avg_compliance_rate": None,
                "avg_required_compliance_rate": None,
                "avg_optional_hit_rate": None,
            },
        }

    run_ids = [r.id for r in runs if r.id]
    # 批量查所有 CheckpointResult（替代 per-run N+1 查询）
    all_crs: List[CheckpointResult] = (
        (
            await session.execute(
                select(CheckpointResult).where(
                    CheckpointResult.agent_run_id.in_(run_ids)
                )
            )
        ).scalars().all()
        if run_ids
        else []
    )
    crs_by_run: Dict[str, List[CheckpointResult]] = {}
    for cr in all_crs:
        crs_by_run.setdefault(cr.agent_run_id, []).append(cr)

    # 批量查所有 templates（一次，替代 per-run 查询）
    all_tpl_ids = list({cr.checkpoint_id for cr in all_crs})
    tpl_map = await _load_templates_by_id(session, all_tpl_ids)

    # 内存构造每个 run 的 report（不查库）
    run_reports: List[Dict[str, Any]] = []
    required_rates: List[float] = []
    optional_hit_rates: List[float] = []
    for run in runs:
        crs = crs_by_run.get(run.id, [])
        items = [
            _serialize_checkpoint_item(cr, tpl_map.get(cr.checkpoint_id)) for cr in crs
        ]
        items.sort(key=_sort_key_for_item)
        report = _summarize_run_checkpoint(run, items)
        run_reports.append(report)
        req_rate = report.get("required_compliance_rate")
        if req_rate is not None:
            required_rates.append(float(req_rate))
        opt_rate = report.get("optional_hit_rate")
        if opt_rate is not None:
            optional_hit_rates.append(float(opt_rate))

    runs_with_cp = sum(1 for r in run_reports if (r.get("checkpoint_total") or 0) > 0)
    avg_required = (
        round(sum(required_rates) / len(required_rates), 4) if required_rates else None
    )
    avg_optional_hit = (
        round(sum(optional_hit_rates) / len(optional_hit_rates), 4)
        if optional_hit_rates
        else None
    )
    return {
        "task_id": task_id,
        "runs": run_reports,
        "summary": {
            "run_count": len(run_reports),
            "runs_with_checkpoints": runs_with_cp,
            "avg_compliance_rate": avg_required,
            "avg_required_compliance_rate": avg_required,
            "avg_optional_hit_rate": avg_optional_hit,
        },
    }
