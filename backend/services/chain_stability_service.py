"""关键链条稳定性（P1）：基于过程点 checkpoint_results 的多轮聚合。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentRun, CheckpointResult, ProcessCheckpointTemplate
from services.eval_context import get_question_id, get_task_id


def _question_id_for_run(run: AgentRun) -> Optional[int]:
    qid = get_question_id(run)
    if qid is not None:
        return int(qid)
    if run.question_id is not None:
        return int(run.question_id)
    return None


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


def _chain_ok_for_run(
    items: List[Dict[str, Any]],
    templates: Dict[str, ProcessCheckpointTemplate],
) -> bool:
    """单 Run：必做过程点全部 passed；启用顺序依赖的须 order_ok 不为 False。

    items 为空（Run 存在但未产出 CheckpointResult）时返回 False——无过程点
    证据不能视为链条全通过。典型场景：任务被取消，Run 已创建但过程点评估未执行。
    """
    if not items:
        return False
    for item in items:
        cid = item.get("checkpoint_id") or ""
        tpl = templates.get(cid)
        if not tpl or not bool(tpl.required):
            continue
        if not item.get("passed"):
            return False
        if bool(getattr(tpl, "enforce_order", False)):
            order_ok = item.get("order_ok")
            if order_ok is False:
                return False
    return True


async def build_chain_stability(
    session: AsyncSession,
    task_id: str,
    runs: Optional[List[AgentRun]] = None,
    *,
    cp_by_run: Optional[Dict[str, List[CheckpointResult]]] = None,
    tpl_map: Optional[Dict[str, ProcessCheckpointTemplate]] = None,
) -> Dict[str, Any]:
    """
    任务级关键链条稳定性：
    - chain_pass_rate：题目在 K 轮内链条全对的占比
    - order_sensitive_checkpoints：各过程点 K 轮 pass / order_ok 统计

    可选参数 cp_by_run / tpl_map 允许调用方传入预加载数据，避免重复 DB 查询。
    """
    tid = (task_id or "").strip()
    if runs is None:
        from services.task_performance_report_service import _load_task_runs

        runs = await _load_task_runs(session, tid)

    if not runs:
        return {
            "chain_pass_rate": None,
            "questions_all_rounds_ok": 0,
            "question_count": 0,
            "max_rounds_per_question": 0,
            "weakest_chain": None,
            "order_sensitive_checkpoints": [],
            "structural_only_stable": None,
        }

    by_question: Dict[int, List[AgentRun]] = defaultdict(list)
    for run in runs:
        qid = _question_id_for_run(run)
        if qid is not None:
            by_question[qid].append(run)

    max_k = max((len(v) for v in by_question.values()), default=1)

    # 使用预加载数据或批量查询（消除 N+1）
    if cp_by_run is not None:
        all_cp_results = [cr for cps in cp_by_run.values() for cr in cps]
    else:
        run_ids = [r.id for r in runs]
        all_cp_results = (
            await session.execute(
                select(CheckpointResult).where(CheckpointResult.agent_run_id.in_(run_ids))
            )
        ).scalars().all()
        cp_by_run = defaultdict(list)
        for cr in all_cp_results:
            cp_by_run[cr.agent_run_id].append(cr)

    if tpl_map is not None:
        templates = tpl_map
    else:
        cp_ids: set[str] = {cr.checkpoint_id for cr in all_cp_results}
        templates = await _load_templates_by_id(session, list(cp_ids))

    run_items: Dict[str, List[Dict[str, Any]]] = {}
    for run in runs:
        results = cp_by_run.get(run.id, [])
        items: List[Dict[str, Any]] = []
        for r in results:
            tpl = templates.get(r.checkpoint_id)
            evidence = r.evidence if isinstance(r.evidence, dict) else {}
            items.append(
                {
                    "checkpoint_id": r.checkpoint_id,
                    "name": tpl.name if tpl else r.checkpoint_id,
                    "passed": bool(r.passed),
                    "hit": evidence.get("hit"),
                    "order_ok": evidence.get("order_ok"),
                    "required": bool(tpl.required) if tpl else False,
                    "enforce_order": bool(getattr(tpl, "enforce_order", False)) if tpl else False,
                    "order": tpl.order if tpl else None,
                }
            )
        run_items[run.id] = items

    questions_all_ok = 0
    weakest: Optional[Tuple[float, int, str]] = None

    for qid, qruns in by_question.items():
        all_ok = all(_chain_ok_for_run(run_items.get(r.id, []), templates) for r in qruns)
        if all_ok:
            questions_all_ok += 1

    q_count = len(by_question) or 1
    chain_pass_rate = round(questions_all_ok / q_count, 4) if by_question else None

    cp_stats: Dict[str, Dict[str, Any]] = {}
    for run in runs:
        for item in run_items.get(run.id, []):
            cid = item["checkpoint_id"]
            if cid not in cp_stats:
                cp_stats[cid] = {
                    "checkpoint_id": cid,
                    "name": item["name"],
                    "required": item["required"],
                    "enforce_order": item["enforce_order"],
                    "order": item["order"],
                    "pass_count": 0,
                    "order_ok_count": 0,
                    "round_total": 0,
                }
            bucket = cp_stats[cid]
            bucket["round_total"] += 1
            if item["passed"]:
                bucket["pass_count"] += 1
            if item.get("order_ok") is True:
                bucket["order_ok_count"] += 1
            elif item.get("enforce_order") and item.get("order_ok") is False:
                pass
            elif item.get("enforce_order") and item.get("passed"):
                bucket["order_ok_count"] += 1

    order_sensitive: List[Dict[str, Any]] = []
    for cid, st in cp_stats.items():
        total = st["round_total"] or 1
        pass_rate = f"{st['pass_count']}/{total}"
        entry = {
            **st,
            "pass_rate": pass_rate,
            "order_ok_rate": (
                f"{st['order_ok_count']}/{total}" if st["enforce_order"] else None
            ),
        }
        order_sensitive.append(entry)
        if st["required"]:
            rate = st["pass_count"] / total
            if weakest is None or rate < weakest[0]:
                weakest = (rate, st["pass_count"], st["name"])

    order_sensitive.sort(
        key=lambda x: (
            not x.get("required"),
            x.get("order") if x.get("order") is not None else 10_000,
            x.get("name") or "",
        )
    )

    weakest_chain = None
    if weakest and weakest[0] < 1.0:
        weakest_chain = {
            "name": weakest[2],
            "pass_rate": f"{weakest[1]}/{max_k}",
        }

    return {
        "chain_pass_rate": chain_pass_rate,
        "questions_all_rounds_ok": questions_all_ok,
        "question_count": len(by_question),
        "max_rounds_per_question": max_k,
        "weakest_chain": weakest_chain,
        "order_sensitive_checkpoints": order_sensitive,
        "structural_only_stable": None,
    }


async def build_question_chain_stability(
    session: AsyncSession,
    runs_for_question: List[AgentRun],
) -> Dict[str, Any]:
    """单题多轮链条统计（供 scenario-matrix cell）。"""
    if not runs_for_question:
        return {"chain_pass_rate": None, "rounds_all_ok": 0, "round_count": 0}

    # 批量查询所有 runs 的 CheckpointResult（消除 N+1）
    run_ids = [r.id for r in runs_for_question]
    all_cp_results = (
        await session.execute(
            select(CheckpointResult).where(CheckpointResult.agent_run_id.in_(run_ids))
        )
    ).scalars().all()
    cp_by_run: Dict[str, List[CheckpointResult]] = defaultdict(list)
    for cr in all_cp_results:
        cp_by_run[cr.agent_run_id].append(cr)

    cp_ids: set[str] = {cr.checkpoint_id for cr in all_cp_results}
    templates = await _load_templates_by_id(session, list(cp_ids))

    run_items: Dict[str, List[Dict[str, Any]]] = {}
    for run in runs_for_question:
        results = cp_by_run.get(run.id, [])
        items = []
        for r in results:
            evidence = r.evidence if isinstance(r.evidence, dict) else {}
            items.append(
                {
                    "checkpoint_id": r.checkpoint_id,
                    "passed": bool(r.passed),
                    "order_ok": evidence.get("order_ok"),
                }
            )
        run_items[run.id] = items
    k = len(runs_for_question)
    ok_count = sum(
        1
        for r in runs_for_question
        if _chain_ok_for_run(run_items.get(r.id, []), templates)
    )
    return {
        "chain_pass_rate": round(ok_count / k, 4) if k else None,
        "rounds_all_ok": ok_count,
        "round_count": k,
        "all_rounds_ok": ok_count == k and k > 0,
    }
