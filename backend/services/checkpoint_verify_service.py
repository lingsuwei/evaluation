"""过程点配置页：最近 Run 取样与批量 Trace 验证。"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import LANGFUSE_ENABLED
from database import Agent, AgentRun, ProcessCheckpointTemplate, RunSpanNode
from services.agent_identity import resolve_logical_agent_id_by_name
from services.checkpoint_match_rules import evaluate_checkpoint_on_trace, validate_match_rules
from services.agent_run_trace_list import load_observations_for_preview
from services.eval_context import get_question_id
from services.langfuse_errors import LangfuseApiError
from trace_routes import _client_for_project as _default_client_for_project


async def _resolve_preview_project(
    session: AsyncSession,
    template: ProcessCheckpointTemplate,
    body_project: Optional[str],
) -> Optional[str]:
    explicit = (body_project or "").strip()
    if explicit:
        return explicit
    lid = (template.logical_agent_id or "").strip()
    if not lid:
        return None
    q = select(Agent.langfuse_project).where(Agent.logical_agent_id == lid)
    for proj in (await session.execute(q)).scalars().all():
        pid = (proj or "").strip()
        if pid:
            return pid
    return None


def _question_preview(run: AgentRun) -> str:
    qnum = get_question_id(run)
    snap = run.input_snapshot if isinstance(run.input_snapshot, dict) else {}
    for key in ("question", "q", "input", "prompt"):
        val = snap.get(key)
        if val is not None and str(val).strip():
            text = str(val).strip()
            return text[:80] + ("…" if len(text) > 80 else "")
    if qnum is not None:
        return f"Q{int(qnum) + 1}"
    return "—"


async def _run_has_span_tree(session: AsyncSession, agent_run_id: str) -> bool:
    row = await session.execute(
        select(RunSpanNode.id)
        .where(RunSpanNode.agent_run_id == agent_run_id)
        .limit(1)
    )
    return row.scalar_one_or_none() is not None


async def _find_agent_run_by_trace_ref(
    session: AsyncSession,
    trace_id: str,
) -> Optional[AgentRun]:
    tid = (trace_id or "").strip()
    if not tid:
        return None
    result = await session.execute(
        select(AgentRun).where(
            or_(
                AgentRun.trace_id == tid,
                AgentRun.id == tid,
            )
        )
    )
    return result.scalars().first()


def _format_trace_not_ready_error(run: AgentRun) -> str:
    sync_st = (run.trace_sync_status or "").strip() or "unknown"
    labels = {
        "pending_sync": "待同步",
        "pending": "同步排队中",
        "failed": "同步失败",
    }
    label = labels.get(sync_st, sync_st)
    return (
        f"无法批量验证：该 Run 的 Trace 为「{label}」，Span 尚未物化到评测平台，无法读取 Agent 执行路径。\n\n"
        "建议操作：\n"
        "1. 点击「刷新 Run 列表」，等待下拉项由「待同步」变为「已物化」后再验证\n"
        "2. 若长时间仍为待同步，请在「系统监控 → Trace 探索」或 Langfuse 控制台确认 trace 是否入库，并检查 Trace 回填任务\n"
        "3. 若 trace 已在 Langfuse 可用，可点击「手动 trace_id」粘贴 Langfuse trace_id 后重试"
    )


async def _load_observations_for_batch(
    session: AsyncSession,
    *,
    trace_id: str,
    project: Optional[str],
    source: Optional[str],
    template: ProcessCheckpointTemplate,
    client_for_project=None,
) -> tuple[List[Dict[str, Any]], str, Optional[str]]:
    use_langfuse = (source or "").strip().lower() == "langfuse"
    observations: List[Dict[str, Any]] = []
    obs_source = "langfuse"
    matched_run = await _find_agent_run_by_trace_ref(session, trace_id)

    if not use_langfuse:
        fact_obs = await load_observations_for_preview(session, trace_id)
        if fact_obs:
            observations = fact_obs
            obs_source = "fact_layer"

    resolved_project = await _resolve_preview_project(session, template, project)

    if not observations:
        if not LANGFUSE_ENABLED:
            if matched_run and not await _run_has_span_tree(session, matched_run.id):
                raise ValueError(_format_trace_not_ready_error(matched_run))
            raise ValueError(
                "Langfuse 未配置且无物化 Span；请先完成一次在线评测并等待 Trace 同步，"
                "或传 source=langfuse 并填写项目 id"
            )
        try:
            lf_client = (client_for_project or _default_client_for_project)(resolved_project)
            trace = await lf_client.get_trace(trace_id)
        except LangfuseApiError as e:
            if matched_run and not await _run_has_span_tree(session, matched_run.id):
                raise ValueError(
                    f"{_format_trace_not_ready_error(matched_run)}\n\n"
                    f"（已尝试从 Langfuse 拉取 trace，失败原因：{e.message}）"
                ) from e
            raise ValueError(f"拉取 trace 失败: {e.message}") from e
        raw = trace.get("observations") or []
        observations = raw if isinstance(raw, list) else []
        obs_source = "langfuse"

    if not observations:
        if matched_run and not await _run_has_span_tree(session, matched_run.id):
            raise ValueError(
                f"{_format_trace_not_ready_error(matched_run)}\n\n"
                "（Trace 已拉取但未包含 observation，过程点无法匹配。）"
            )
        raise ValueError(
            "Trace 中无 observation，无法匹配过程点。"
            "请确认 Agent 已上报 Langfuse Span，或等待 Trace 同步完成后再试。"
        )

    return observations, obs_source, resolved_project


async def list_sample_runs_for_agent(
    session: AsyncSession,
    *,
    agent_name: str,
    limit: int = 10,
) -> Dict[str, Any]:
    """按 Agent 名称返回最近评测 Run（供过程点页选 Trace 验证）。"""
    name = agent_name.strip()
    lid = await resolve_logical_agent_id_by_name(session, name)

    q = (
        select(AgentRun)
        .where(
            or_(
                AgentRun.logical_agent_id == lid,
                AgentRun.agent_name == name,
            )
        )
        .order_by(AgentRun.started_at.desc())
        .limit(max(1, min(limit, 30)))
    )
    runs = list((await session.execute(q)).scalars().all())

    items: List[Dict[str, Any]] = []
    for run in runs:
        trace_id = (run.trace_id or run.id or "").strip()
        if not trace_id:
            continue
        items.append(
            {
                "agent_run_id": run.id,
                "trace_id": trace_id,
                "question_index": get_question_id(run),
                "question_preview": _question_preview(run),
                "started_at": run.started_at.isoformat() if run.started_at else "",
                "status": run.status or "",
                "trace_sync_status": run.trace_sync_status,
                "has_span_tree": await _run_has_span_tree(session, run.id),
            }
        )

    return {
        "agent_name": name,
        "logical_agent_id": lid,
        "count": len(items),
        "runs": items,
    }


async def _resolve_preview_project_by_agent_name(
    session: AsyncSession,
    agent_name: str,
    body_project: Optional[str],
) -> Optional[str]:
    explicit = (body_project or "").strip()
    if explicit:
        return explicit
    lid = await resolve_logical_agent_id_by_name(session, agent_name.strip())
    q = select(Agent.langfuse_project).where(Agent.logical_agent_id == lid)
    for proj in (await session.execute(q)).scalars().all():
        pid = (proj or "").strip()
        if pid:
            return pid
    return None


async def preview_checkpoint_on_trace(
    session: AsyncSession,
    *,
    trace_id: str,
    match_rules: Dict[str, Any],
    checkpoint_name: str,
    order: Optional[int],
    required: bool,
    project: Optional[str],
    source: Optional[str],
    template: Optional[ProcessCheckpointTemplate] = None,
    agent_name: Optional[str] = None,
    client_for_project=None,
) -> Dict[str, Any]:
    """单条过程点在 trace 上试跑（编辑/新增表单预览共用）。"""
    tid = trace_id.strip()
    if not tid:
        raise ValueError("trace_id 不能为空")

    rules = validate_match_rules(match_rules)
    anchor = template
    if anchor is None:
        name = (agent_name or "").strip()
        lid = ""
        if name:
            lid = await resolve_logical_agent_id_by_name(session, name)
        anchor = ProcessCheckpointTemplate(logical_agent_id=lid, name=checkpoint_name)

    observations, obs_source, resolved_project = await _load_observations_for_batch(
        session,
        trace_id=tid,
        project=project,
        source=source,
        template=anchor,
        client_for_project=client_for_project,
    )

    if not resolved_project and agent_name:
        resolved_project = await _resolve_preview_project_by_agent_name(session, agent_name, project)

    ev = evaluate_checkpoint_on_trace(
        checkpoint_name=checkpoint_name,
        order=order,
        required=required,
        match_rules=rules,
        observations=observations,
    )

    return {
        "trace_id": tid,
        "project": resolved_project,
        "observation_count": len(observations),
        "observation_source": obs_source,
        "checkpoint_name": checkpoint_name,
        "hit": bool(ev.get("hit")),
        "matched_observation_name": ev.get("matched_observation_name"),
        "results": [
            {
                "template_name": checkpoint_name,
                "skipped": False,
                **ev,
            }
        ],
    }


async def batch_preview_checkpoints_for_agent(
    session: AsyncSession,
    *,
    agent_name: str,
    trace_id: str,
    project: Optional[str] = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """对 Agent 全部过程点（含禁用）在一条 trace 上批量试跑匹配。"""
    name = agent_name.strip()
    tid = trace_id.strip()
    if not tid:
        raise ValueError("trace_id 不能为空")

    lid = await resolve_logical_agent_id_by_name(session, name)
    templates = list(
        (
            await session.execute(
                select(ProcessCheckpointTemplate)
                .where(ProcessCheckpointTemplate.logical_agent_id == lid)
                .order_by(
                    ProcessCheckpointTemplate.order,
                    ProcessCheckpointTemplate.name,
                )
            )
        )
        .scalars()
        .all()
    )
    if not templates:
        raise ValueError("该 Agent 尚无过程点，请先初始化参考模板")

    anchor = next((t for t in templates if t.enabled), templates[0])
    observations, obs_source, resolved_project = await _load_observations_for_batch(
        session,
        trace_id=tid,
        project=project,
        source=source,
        template=anchor,
    )

    results: List[Dict[str, Any]] = []
    enabled_total = 0
    hit_count = 0
    required_miss_count = 0

    for tpl in templates:
        enabled = bool(tpl.enabled)
        required = bool(tpl.required)
        if enabled:
            enabled_total += 1

        if (tpl.match_mode or "rule") != "rule":
            results.append(
                {
                    "template_id": tpl.id,
                    "template_name": tpl.name,
                    "enabled": enabled,
                    "required": required,
                    "order": tpl.order,
                    "hit": False,
                    "passed": not required,
                    "skipped": True,
                    "skip_reason": "暂仅支持 rule 模式",
                    "matched_observation_name": None,
                    "suggestion": None,
                }
            )
            continue

        try:
            rules = validate_match_rules(json.loads(tpl.match_rules or "{}"))
        except Exception as e:
            results.append(
                {
                    "template_id": tpl.id,
                    "template_name": tpl.name,
                    "enabled": enabled,
                    "required": required,
                    "order": tpl.order,
                    "hit": False,
                    "passed": False,
                    "skipped": True,
                    "skip_reason": str(e),
                    "matched_observation_name": None,
                    "suggestion": None,
                }
            )
            if enabled and required:
                required_miss_count += 1
            continue

        ev = evaluate_checkpoint_on_trace(
            checkpoint_name=tpl.name,
            order=tpl.order,
            required=required,
            match_rules=rules,
            observations=observations,
        )
        hit = bool(ev.get("hit"))
        if enabled and hit:
            hit_count += 1
        if enabled and required and not hit:
            required_miss_count += 1

        suggestion = None
        if enabled and not hit and not required:
            suggestion = "可选过程点未命中，若业务不需要可考虑禁用"
        elif enabled and not hit and required:
            suggestion = "必达过程点未命中，请检查 Agent 执行或规则"

        passed = True
        if enabled and required:
            passed = hit
        elif enabled and not required:
            passed = hit or not required

        results.append(
            {
                "template_id": tpl.id,
                "template_name": tpl.name,
                "enabled": enabled,
                "required": required,
                "order": tpl.order,
                "hit": hit,
                "passed": passed,
                "skipped": False,
                "skip_reason": None,
                "matched_observation_name": ev.get("matched_observation_name"),
                "suggestion": suggestion,
            }
        )

    return {
        "agent_name": name,
        "logical_agent_id": lid,
        "trace_id": tid,
        "project": resolved_project,
        "observation_count": len(observations),
        "observation_source": obs_source,
        "enabled_total": enabled_total,
        "hit_count": hit_count,
        "required_miss_count": required_miss_count,
        "all_required_hit": required_miss_count == 0 and enabled_total > 0,
        "results": results,
    }
