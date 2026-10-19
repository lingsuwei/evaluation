"""评测结果导出服务：生成多 sheet xlsx 评测报告。

Sheet 结构：
1. 评估总览 - KPI + 四维结论表 + 核心发现 + 配置项
2. 未通过题目汇总 - 失败题×轮次×指标明细（含运行效能指标）
3. 题目详情 - 题目×轮次×指标粒度全量明细（含运行效能指标）
4. 质量指标 - 维度结论
5. 过程一致性 - 维度结论
6. 场景子步骤交付 - 维度结论
7. 运行稳定性 - 维度结论
8. 关键发现与建议

多题多轮场景下，题目详情和未通过题目汇总按 (题目, 轮次) 展开，
展示轮次列。评估总览等统计页按题目聚合（取最差轮次）。

配色对齐 frontend/src/index.css CSS 变量。
得分/结论直接复用 quality_score 和 dimension_verdicts 读模型，不重新聚合。
"""
from __future__ import annotations

import io
import json
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import (
    Agent,
    AgentRun,
    CustomMetric,
    Dataset,
    EvaluationResult,
    EvaluationRound,
    EvaluationTask,
    LLMModel,
    RunMetric,
    RunSpanNode,
    ScenarioEvalStep,
    ScenarioStepResult,
)
from services.dimension_verdict_constants import METRIC_DISPLAY_NAMES
from services.evaluation_mode import get_evaluation_mode, is_offline_task
from services.evaluation_pipeline import parse_task_agent_ids
from services.inference_duration_service import (
    batch_load_trace_latency_ms,
    batch_resolve_agent_e2e_duration_ms,
    resolve_agent_e2e_duration_ms,
)
from services.inference_model_display import (
    execution_mode_info_from_run,
    execution_mode_label,
    inference_model_info_from_run,
    inference_model_label,
)
from services.quality_score_service import compute_task_quality_score
from services.run_call_stats_service import summarize_call_counts_from_nodes
from services.task_dimension_verdict_service import build_task_dimension_verdicts

# ─── 配色（对齐 frontend/src/index.css 的 CSS 变量） ───
C_PRIMARY = "6B5CE7"
C_PRIMARY_SOFT = "EEE9FF"
C_GREEN = "22A06B"
C_GREEN_SOFT = "E3F6EC"
C_AMBER = "E6A817"
C_AMBER_SOFT = "FDF3D8"
C_RED = "DC3545"
C_RED_SOFT = "FDE2E4"
C_TEXT3 = "9099B0"
C_TEXT2 = "5A607A"
C_TEXT1 = "2B2F3A"
C_BG_ZEBRA = "F7F8FC"
C_BORDER = "D0D5DD"
C_BORDER_SOFT = "EEF0F6"
C_WHITE = "FFFFFF"
C_GRAY_SOFT = "F0F2F5"

# 字体族：对齐前端 --edp-font-family，Excel 环境取中文字体名
FONT_FAMILY = "微软雅黑"

# UCD 字号刻度（与前端 --edp-font-* 对齐）
FONT_SIZE_XS = 10      # --edp-font-xs，极小标记
FONT_SIZE_SM = 11       # --edp-font-sm，表头/pill/标签
FONT_SIZE_BASE = 12     # --edp-font-base，辅助文字
FONT_SIZE_MD = 10       # 正文（数据行）
FONT_SIZE_LG = 12        # 子区域标题
FONT_SIZE_XL = 17        # --edp-font-xl，区域标题（section）
FONT_SIZE_2XL = 20       # --edp-font-2xl，KPI 数值
FONT_SIZE_3XL = 26       # --edp-font-3xl，大卡片数字

# 四维结论 status → 中文
_VERDICT_STATUS_CN = {
    "pass": "达标",
    "warn": "需关注",
    "fail": "未达标",
    "na": "不适用",
    "disabled": "未启用",
    "descriptive": "描述性",
}

# 运行稳定性 performance status → 中文
_PERF_STATUS_CN = {
    "within_target": "达标",
    "elevated": "需关注",
    "severe": "未达标",
    "na": "不适用",
    "disabled": "未启用",
}

# 质量指标固定排序
_PREFERRED_METRIC_ORDER = ["correctness", "answer_relevancy", "faithfulness"]


def _safe_float(value) -> Optional[float]:
    """安全转换为 float，失败返回 None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_score(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{float(value):.2f}"


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{float(value):.1f}%"


def _status_fill_font(status: str) -> Tuple[str, str]:
    """verdict status → (fill_color, font_color)。"""
    mapping = {
        "pass": (C_GREEN_SOFT, C_GREEN),
        "warn": (C_AMBER_SOFT, C_AMBER),
        "fail": (C_RED_SOFT, C_RED),
        "na": (C_GRAY_SOFT, C_TEXT3),
        "disabled": (C_GRAY_SOFT, C_TEXT3),
        "descriptive": (C_PRIMARY_SOFT, C_PRIMARY),
        "within_target": (C_GREEN_SOFT, C_GREEN),
        "elevated": (C_AMBER_SOFT, C_AMBER),
        "severe": (C_RED_SOFT, C_RED),
    }
    return mapping.get(status, (C_GRAY_SOFT, C_TEXT3))


def _status_label(status: str) -> str:
    if status in _PERF_STATUS_CN:
        return _PERF_STATUS_CN[status]
    return _VERDICT_STATUS_CN.get(status, status)


def _score_fill_font(score: Optional[float]) -> Tuple[str, str]:
    """分数 → (fill_color, font_color)。null→灰；<0.6→红；<0.8→琥珀；≥0.8→绿。"""
    if score is None:
        return C_GRAY_SOFT, C_TEXT3
    try:
        score_val = float(score)
    except (TypeError, ValueError):
        return C_GRAY_SOFT, C_TEXT3
    if score_val < 0.6:
        return C_RED_SOFT, C_RED
    if score_val < 0.8:
        return C_AMBER_SOFT, C_AMBER
    return C_GREEN_SOFT, C_GREEN


# ═══════════════════════════════════════════════════════════
# 数据收集层（复用现有读模型，不重新聚合）
# ═══════════════════════════════════════════════════════════

async def _load_run_metrics_map(
    session: AsyncSession, run_ids: List[str]
) -> Dict[str, Dict[str, Any]]:
    if not run_ids:
        return {}
    rows = (
        await session.execute(
            select(RunMetric).where(RunMetric.agent_run_id.in_(run_ids))
        )
    ).scalars().all()
    out: Dict[str, Dict[str, Any]] = defaultdict(dict)
    for m in rows:
        rid = m.agent_run_id
        key = m.metric_key
        if m.value_num is not None and not (isinstance(m.dims, dict) and m.dims):
            out[rid][key] = float(m.value_num)
    return dict(out)


def _metric_val(metrics: Dict[str, Any], key: str) -> Optional[float]:
    v = metrics.get(key)
    return float(v) if v is not None else None


def _runtime_token_breakdown(run: Optional[AgentRun]) -> Tuple[Optional[int], Optional[int]]:
    """从 Run 快照中提取 LLM / 工具 Token。"""
    if not run:
        return None, None
    candidates = []
    for snap in (run.output_snapshot, run.input_snapshot):
        if isinstance(snap, dict):
            candidates.append(snap)
            for key in ("usage", "metrics", "metadata", "output", "result"):
                nested = snap.get(key)
                if isinstance(nested, dict):
                    candidates.append(nested)
    llm_keys = ["tokens_llm", "llm_tokens", "llmTokens", "model_tokens", "modelTokens"]
    tool_keys = ["tokens_tool", "tool_tokens", "toolTokens", "tools_tokens"]
    llm = None
    tool = None
    for d in candidates:
        for k in llm_keys:
            try:
                v = d.get(k)
                if v is not None:
                    llm = int(v)
                    break
            except (TypeError, ValueError):
                continue
        if llm is not None:
            break
    for d in candidates:
        for k in tool_keys:
            try:
                v = d.get(k)
                if v is not None:
                    tool = int(v)
                    break
            except (TypeError, ValueError):
                continue
        if tool is not None:
            break
    return llm, tool


async def _load_runtime_token_breakdowns(
    session: AsyncSession, runs: List[AgentRun]
) -> Dict[str, Tuple[Optional[int], Optional[int]]]:
    """优先读取 Trace 物化后的 runtime token 指标，快照仅作兜底。"""
    run_ids = [run.id for run in runs if run and run.id]
    if not run_ids:
        return {}
    rows = (
        await session.execute(
            select(RunMetric).where(
                RunMetric.agent_run_id.in_(run_ids),
                RunMetric.source == "runtime",
                RunMetric.metric_key.in_(["runtime.tokens_llm", "runtime.tokens_tool"]),
            )
        )
    ).scalars().all()
    runtime_values: Dict[str, Dict[str, int]] = {}
    for row in rows:
        if row.value_num is None:
            continue
        try:
            value = int(row.value_num)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        runtime_values.setdefault(row.agent_run_id, {})[row.metric_key] = value
    out: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
    for run in runs:
        snap_llm, snap_tool = _runtime_token_breakdown(run)
        vals = runtime_values.get(run.id, {})
        out[run.id] = (
            vals.get("runtime.tokens_llm", snap_llm),
            vals.get("runtime.tokens_tool", snap_tool),
        )
    return out


async def _load_task_runs(session: AsyncSession, task_id: str) -> List[AgentRun]:
    tid = (task_id or "").strip()
    result = await session.execute(
        select(AgentRun)
        .where(
            (AgentRun.task_id == tid)
            | (AgentRun.eval_context["task_id"].as_string() == tid)
        )
        .order_by(AgentRun.question_id, AgentRun.run_index, AgentRun.started_at)
    )
    runs = list(result.scalars().all())
    if not runs:
        fallback = await session.execute(select(AgentRun))
        runs = [r for r in fallback.scalars().all() if _get_task_id(r) == tid]
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
                        .order_by(AgentRun.question_id, AgentRun.run_index, AgentRun.started_at)
                    )
                ).scalars().all()
            )
    return runs


def _get_task_id(run: AgentRun) -> Optional[str]:
    ctx = run.eval_context if isinstance(run.eval_context, dict) else {}
    return ctx.get("task_id") or run.task_id


def _get_question_id(run: AgentRun) -> Optional[int]:
    ctx = run.eval_context if isinstance(run.eval_context, dict) else {}
    qid = ctx.get("question_id")
    if qid is not None:
        try:
            return int(qid)
        except (TypeError, ValueError):
            pass
    return run.question_id


async def _load_rounds_by_result(
    session: AsyncSession, result_ids: List[int]
) -> Dict[int, List[Dict[str, Any]]]:
    """加载 EvaluationRound，按 result_id 分组。"""
    if not result_ids:
        return {}
    rows = (
        await session.execute(
            select(EvaluationRound)
            .where(EvaluationRound.result_id.in_(result_ids))
            .order_by(EvaluationRound.result_id, EvaluationRound.round_num)
        )
    ).scalars().all()
    out: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for rd in rows:
        out[rd.result_id].append({
            "round_num": rd.round_num,
            "score": rd.score,
            "reason": rd.reason,
            "duration_ms": rd.duration_ms,
        })
    return dict(out)


async def _load_scenario_step_fails(
    session: AsyncSession, runs: List[AgentRun]
) -> List[Dict[str, Any]]:
    """加载场景子步骤 fail 明细（passed=False 或 score < 0.6）。"""
    run_ids = [r.id for r in runs if r and r.id]
    if not run_ids:
        return []
    rows = (
        await session.execute(
            select(ScenarioStepResult, ScenarioEvalStep, AgentRun)
            .join(ScenarioEvalStep, ScenarioStepResult.step_id == ScenarioEvalStep.id)
            .join(AgentRun, ScenarioStepResult.agent_run_id == AgentRun.id)
            .where(ScenarioStepResult.agent_run_id.in_(run_ids))
            .order_by(AgentRun.question_id, ScenarioEvalStep.step_order)
        )
    ).all()
    fails: List[Dict[str, Any]] = []
    for result, step, run in rows:
        score = float(result.score or 0.0)
        if not result.passed or score < 0.6:
            qid = _get_question_id(run)
            evidence = result.evidence or {}
            reason = ""
            if isinstance(evidence, dict):
                reason = str(evidence.get("reason") or evidence.get("note") or "")
            fails.append({
                "qid": qid,
                "step_name": step.name,
                "step_key": step.step_key,
                "score": score,
                "required": bool(step.required),
                "reason": reason,
            })
    return fails


async def _load_stability_fails(
    session: AsyncSession,
    runs: List[AgentRun],
    latency_map: Dict[str, int],
    token_breakdown: Dict[str, Tuple[Optional[int], Optional[int]]],
    target_latency_ms: Optional[int],
    target_tokens: Optional[int],
) -> List[Dict[str, Any]]:
    """加载运行稳定性 fail 明细（延迟超标 / Token 超标）。"""
    fails: List[Dict[str, Any]] = []
    for run in runs:
        qid = _get_question_id(run)
        latency = latency_map.get(run.id)
        if latency is None:
            latency = resolve_agent_e2e_duration_ms(run)
        llm_tokens, _tool_tokens = token_breakdown.get(
            run.id, (None, None)
        )
        tokens_total = run.tokens_total or llm_tokens or 0
        run_label = f"轮次{run.run_index}" if run.run_index else "主轮"
        if target_latency_ms and latency and latency > target_latency_ms:
            fails.append({
                "qid": qid,
                "sub": "性能-延迟",
                "val": f"{latency}ms",
                "thr": f"≤{target_latency_ms}ms",
                "point": f"{run_label}推理耗时超标",
                "reason": f"推理耗时 {latency}ms 超过目标 {target_latency_ms}ms",
            })
        if target_tokens and tokens_total and tokens_total > target_tokens:
            fails.append({
                "qid": qid,
                "sub": "性能-Token",
                "val": str(tokens_total),
                "thr": f"≤{target_tokens}",
                "point": f"{run_label}Token 消耗超标",
                "reason": f"Token 总量 {tokens_total} 超过目标 {target_tokens}",
            })
    return fails


def _normalize_task_data(td: Dict[str, Any]) -> None:
    """原地规范化 task_data 中所有数值字段，防止 DB 返回字符串导致下游格式化/比较失败。"""
    # ── quality_score ──
    qs = td.get("quality_score") or {}
    for k in ("total_score", "result_score"):
        v = _safe_float(qs.get(k))
        if v is not None:
            qs[k] = v
    cs = qs.get("correctness_stability") or {}
    if cs:
        pr = _safe_float(cs.get("pass_rate"))
        if pr is not None:
            cs["pass_rate"] = pr
    mb = qs.get("metric_breakdown") or {}
    for mk, mv in list(mb.items()):
        fv = _safe_float(mv)
        if fv is not None:
            mb[mk] = fv

    # ── verdicts ──
    verdicts = td.get("verdicts") or {}
    for dim_key in ("quality", "process_consistency", "scenario_delivery", "runtime_stability"):
        dim = verdicts.get(dim_key) or {}
        sv = _safe_float(dim.get("score"))
        if sv is not None:
            dim["score"] = sv
        # quality.metrics.metric_pass_rates
        qm = dim.get("metrics") or {}
        mpr = qm.get("metric_pass_rates") or {}
        for _mn, ms in mpr.items():
            if isinstance(ms, dict):
                for fk in ("pass_rate", "avg_score"):
                    fv = _safe_float(ms.get(fk))
                    if fv is not None:
                        ms[fk] = fv
        # process_consistency / scenario_delivery metrics
        for fk in ("chain_pass_rate", "required_pass_rate"):
            fv = _safe_float(qm.get(fk))
            if fv is not None:
                qm[fk] = fv
        # runtime_stability.performance.metrics
        perf = dim.get("performance") or {}
        pm = perf.get("metrics") or {}
        for fk in ("latency_pass_rate", "tokens_pass_rate", "avg_latency_ms",
                    "target_latency_ms", "avg_tokens", "target_tokens"):
            fv = _safe_float(pm.get(fk))
            if fv is not None:
                pm[fk] = fv
        # runtime_stability.quality_stability.metrics
        qsm = dim.get("quality_stability") or {}
        qsmm = qsm.get("metrics") or {}
        for fk in ("pass_rate", "avg_std", "max_std", "min_score"):
            fv = _safe_float(qsmm.get(fk))
            if fv is not None:
                qsmm[fk] = fv
        # structural.metrics
        stm = (dim.get("structural") or {}).get("metrics") or {}
        for fk in ("multi_round_question_count", "drift_question_count", "sequence_jaccard_min"):
            fv = _safe_float(stm.get(fk))
            if fv is not None:
                stm[fk] = fv

    # ── questions (per-round) / questions_summary ──
    for q_list_key in ("questions", "questions_summary"):
        for q in td.get(q_list_key) or []:
            sv = _safe_float(q.get("score"))
            if sv is not None:
                q["score"] = sv
            elif q.get("score") is not None:
                q["score"] = None  # 非数值 → None
            ms = q.get("metric_scores") or {}
            for mk, mv in list(ms.items()):
                fv = _safe_float(mv)
                if fv is not None:
                    ms[mk] = fv
                else:
                    ms[mk] = None  # 非数值 → None
            # rounds
            for rd in q.get("rounds") or []:
                rv = _safe_float(rd.get("score"))
                if rv is not None:
                    rd["score"] = rv

    # ── scenario_fails ──
    for sf in td.get("scenario_fails") or []:
        fv = _safe_float(sf.get("score"))
        if fv is not None:
            sf["score"] = fv

    # ── metric_summary ──
    for ms in td.get("metric_summary") or []:
        fv = _safe_float(ms.get("avg_score"))
        if fv is not None:
            ms["avg_score"] = fv

    # ── multi_round_data ──
    for _qid, rounds in (td.get("multi_round_data") or {}).items():
        for rd in rounds:
            for fk in ("score", "duration_ms", "tokens_total"):
                fv = _safe_float(rd.get(fk))
                if fv is not None:
                    rd[fk] = fv

    # ── process_consistency_data.questions[*].runs[*].items[*] ──
    pc_data = td.get("process_consistency_data") or {}
    for q in pc_data.get("questions") or []:
        for run in q.get("runs") or []:
            for item in run.get("items") or []:
                pass  # passed/required are bool, no numeric to normalize

    # ── scenario_matrix.questions[*].cells[*].steps[*] ──
    sm = td.get("scenario_matrix") or {}
    for q in sm.get("questions") or []:
        for _mid, cell in (q.get("cells") or {}).items():
            if not isinstance(cell, dict):
                continue
            for step in cell.get("steps") or []:
                fv = _safe_float(step.get("score"))
                if fv is not None:
                    step["score"] = fv


async def _collect_task_data(
    session: AsyncSession, task_id: str
) -> Optional[Dict[str, Any]]:
    """收集单个任务的全部导出数据。复用现有读模型。"""
    task = await session.get(EvaluationTask, task_id)
    if not task:
        return None
    # 数据集
    dataset_name = None
    if task.dataset_id:
        ds = await session.get(Dataset, task.dataset_id)
        dataset_name = ds.name if ds else "（评测集已删除）"

    # Agent 信息
    agent_ids = parse_task_agent_ids(task)
    agent_name = None
    agent_version = None
    if agent_ids:
        agent = await session.get(Agent, agent_ids[0])
        agent_name = agent.name if agent else None
        agent_version = agent.version_tag or agent.version if agent else None
    elif task.agent_id:
        agent = await session.get(Agent, task.agent_id)
        agent_name = agent.name if agent else None
        agent_version = agent.version_tag or agent.version if agent else None
    else:
        agent_name = "离线评估"

    # 评估模型显示名 + temperature
    from config import get_model_display_name, get_llm_config

    eval_model_display = get_model_display_name(task.llm_model)
    llm_cfg = get_llm_config(task.llm_model)
    temperature = llm_cfg.get("temperature") if llm_cfg else None

    # AgentRun 列表
    runs = await _load_task_runs(session, task_id)
    run_ids = [r.id for r in runs]

    # 质量分（复用读模型）
    quality_score = await compute_task_quality_score(session, task_id)

    # 四维结论（复用读模型）
    verdicts = await build_task_dimension_verdicts(
        session, task_id, task=task, include_threshold_meta=False
    )

    # 过程一致性明细（用于过程点通过率汇总）
    from services.process_consistency_service import build_process_consistency
    process_consistency_data = await build_process_consistency(session, task_id)

    # 场景步骤矩阵（用于步骤通过率汇总）
    from services.task_scenario_matrix_service import build_task_scenario_matrix
    scenario_matrix = await build_task_scenario_matrix(session, task_id)

    # 运行时指标
    metrics_map = await _load_run_metrics_map(session, run_ids)
    token_breakdown = await _load_runtime_token_breakdowns(session, runs)
    trace_latency = await batch_load_trace_latency_ms(session, run_ids)
    latency_map = await batch_resolve_agent_e2e_duration_ms(
        session, runs, stored_by_run=None
    )

    # 质量指标结果（按 metric_name + agent_run_id 分组）
    er_rows = (
        await session.execute(
            select(EvaluationResult).where(EvaluationResult.task_id == task_id)
        )
    ).scalars().all()

    # 加载多轮评估详情
    result_ids = [r.id for r in er_rows if r.id]
    rounds_by_result = await _load_rounds_by_result(session, result_ids)

    # 按 (题目, 轮次) 聚合，支持多题多轮场景
    # 轮次来自 AgentRun.run_index；agent_run_id 为空时归入 "round 0"
    run_index_map: Dict[str, int] = {}
    for run in runs:
        run_index_map[run.id] = run.run_index or 0

    question_data: Dict[Tuple[int, str], Dict[str, Any]] = {}
    all_metric_names: set = set()
    for r in er_rows:
        mname = (r.metric_name or "").strip()
        if mname and mname not in ("inference_latency", "tokens_total"):
            all_metric_names.add(mname)
        qid = r.question_id
        if qid is None:
            continue
        qid_int = int(qid)
        run_id = r.agent_run_id or ""
        key = (qid_int, run_id)
        if key not in question_data:
            question_data[key] = {
                "original_qid": qid_int,
                "qid": qid_int,
                "question": r.question or "",
                "ground_truth": r.ground_truth or "",
                "agent_answer": r.agent_answer or "",
                "eval_reason": "",
                "score": r.avg_score if r.avg_score is not None else r.score,
                "passed": bool(r.passed) if r.passed is not None else False,
                "metric_scores": {},
                "metric_passed": {},
                "metric_reasons": {},
                "rounds": [],
                "agent_run_id": run_id or None,
                "run_index": run_index_map.get(run_id, 0) if run_id else 0,
            }
        qd = question_data[key]
        # 指标得分 / passed / reason
        if mname and mname not in ("inference_latency", "tokens_total"):
            score = r.avg_score if r.avg_score is not None else r.score
            if score is not None:
                qd["metric_scores"][mname] = float(score)
            qd["metric_passed"][mname] = bool(r.passed) if r.passed is not None else False
            # 指标级评语：优先从 EvaluationRound.reason（详细评语）获取，
            # 跳过的指标从 cot 字段获取（去掉 [已跳过] 前缀）
            # 注意：EvaluationResult.eval_reason 是遗留兼容字段，始终为 NULL
            if r.id and r.id in rounds_by_result:
                rounds = rounds_by_result[r.id]
                # 取最低分轮次的 reason（最能解释 fail 原因）
                reason_round = min(
                    (rd for rd in rounds if rd.get("reason")),
                    key=lambda rd: (rd.get("score") if rd.get("score") is not None else float("inf")),
                    default=None,
                )
                if reason_round and reason_round.get("reason"):
                    qd["metric_reasons"][mname] = reason_round["reason"]
            # 如果没有轮次评语，且 cot 包含跳过信息，提取跳过原因
            if mname not in qd["metric_reasons"] and r.cot:
                cot = r.cot.strip()
                if cot.startswith("[已跳过]"):
                    # 去掉前缀，保留跳过原因
                    skip_reason = cot.replace("[已跳过]", "", 1).strip()
                    if skip_reason:
                        qd["metric_reasons"][mname] = skip_reason
        # 多轮详情（EvaluationRound 级别的 judge rounds）
        if r.id and r.id in rounds_by_result:
            qd["rounds"] = rounds_by_result[r.id]
        # 取最差得分作为该 (题目, 轮次) 条目得分
        score_val = r.avg_score if r.avg_score is not None else r.score
        if score_val is not None:
            existing = qd.get("score")
            if existing is None or score_val < existing:
                qd["score"] = float(score_val)
                qd["passed"] = bool(r.passed)

    # 题目级评语回填：从 metric_reasons 中取最差指标（最低分）的评语
    for qd in question_data.values():
        if qd.get("eval_reason"):
            continue
        metric_reasons = qd.get("metric_reasons") or {}
        metric_scores = qd.get("metric_scores") or {}
        if metric_reasons and metric_scores:
            worst_metric = min(
                (m for m in metric_scores if m in metric_reasons),
                key=lambda m: metric_scores[m],
                default=None,
            )
            if worst_metric and metric_reasons.get(worst_metric):
                qd["eval_reason"] = metric_reasons[worst_metric]
                continue
        # 兜底：从 EvaluationRound.reason 获取（取最低分轮次）
        rounds = qd.get("rounds") or []
        if rounds:
            reason_round = min(
                (rd for rd in rounds if rd.get("reason")),
                key=lambda rd: (rd.get("score") if rd.get("score") is not None else float("inf")),
                default=None,
            )
            if reason_round:
                qd["eval_reason"] = reason_round.get("reason") or ""

    # 运行目标阈值（用于运行效能指标 passed 判定）
    runtime_targets = {}
    if task.runtime_metric_targets:
        try:
            runtime_targets = json.loads(task.runtime_metric_targets) if isinstance(task.runtime_metric_targets, str) else task.runtime_metric_targets
        except (json.JSONDecodeError, TypeError):
            pass
    target_latency = runtime_targets.get("latency_ms") or runtime_targets.get("inference_latency_ms")
    target_tokens = runtime_targets.get("tokens_total")

    # 合并运行效能指标（推理耗时 / Token 消耗）到各条目的 metric_scores
    for qd in question_data.values():
        aid = qd.get("agent_run_id")
        if not aid:
            continue
        run_metrics = metrics_map.get(aid, {})
        # 推理耗时 (ms)
        latency_val = run_metrics.get("runtime.inference_latency_ms")
        if latency_val is None:
            latency_val = latency_map.get(aid)
        if latency_val is not None:
            qd["metric_scores"]["runtime.inference_latency_ms"] = float(latency_val)
            qd["metric_passed"]["runtime.inference_latency_ms"] = (
                (target_latency is None or latency_val <= target_latency)
                if target_latency else True
            )
        # Token 消耗
        tokens_val = run_metrics.get("runtime.tokens_total")
        if tokens_val is None:
            # 兜底：从 token_breakdown 取 llm_tokens
            llm_tok, _tool_tok = token_breakdown.get(aid, (None, None))
            if llm_tok is not None:
                tokens_val = float(llm_tok)
        if tokens_val is not None:
            qd["metric_scores"]["runtime.tokens_total"] = float(tokens_val)
            qd["metric_passed"]["runtime.tokens_total"] = (
                (target_tokens is None or tokens_val <= target_tokens)
                if target_tokens else True
            )

    # 构建题目列表：
    # - questions: 按 (题目, 轮次) 展开，用于「题目详情」「未通过题目汇总」
    # - questions_summary: 按题目聚合（取最差轮次），用于「评估总览」等统计
    questions_per_round = sorted(
        question_data.values(),
        key=lambda x: (x["original_qid"], x["run_index"]),
    )

    questions_summary_map: Dict[int, Dict[str, Any]] = {}
    for qd in question_data.values():
        oqid = qd["original_qid"]
        if oqid not in questions_summary_map:
            questions_summary_map[oqid] = dict(qd)
        else:
            existing = questions_summary_map[oqid]
            score = qd.get("score")
            existing_score = existing.get("score")
            if score is not None and (existing_score is None or score < existing_score):
                existing["score"] = score
                existing["passed"] = qd["passed"]
                existing["eval_reason"] = qd.get("eval_reason", "")
                existing["agent_answer"] = qd.get("agent_answer", "")
            # 合并 metric_scores：取各指标最差得分
            for mname, mscore in qd.get("metric_scores", {}).items():
                if mscore is None:
                    continue
                cur = existing.get("metric_scores", {}).get(mname)
                if cur is None or mscore < cur:
                    existing.setdefault("metric_scores", {})[mname] = float(mscore)
                    existing.setdefault("metric_passed", {})[mname] = qd["metric_passed"].get(mname, False)
                    mr = qd.get("metric_reasons", {}).get(mname)
                    if mr:
                        existing.setdefault("metric_reasons", {})[mname] = mr

    questions_summary = sorted(questions_summary_map.values(), key=lambda x: x["original_qid"])

    # 场景子步骤 fail 明细
    scenario_fails = await _load_scenario_step_fails(session, runs)

    # 运行稳定性 fail 明细
    stability_fails = await _load_stability_fails(
        session, runs, latency_map, token_breakdown, target_latency, target_tokens
    )

    # 多轮推理对比数据：按 question_id → [{run_index, score, duration_ms, answer, tokens, latency}]
    multi_round_data: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for run in runs:
        qid = _get_question_id(run)
        if qid is None:
            continue
        qid_int = int(qid)
        model_info = inference_model_info_from_run(run)
        model_label = inference_model_label(model_info) or "—"
        llm_tokens, tool_tokens = token_breakdown.get(run.id, (None, None))
        latency = latency_map.get(run.id)
        if latency is None:
            latency = resolve_agent_e2e_duration_ms(run)
        # Agent 回答
        answer = ""
        if run.output_snapshot and isinstance(run.output_snapshot, dict):
            for key in ("model_answer", "answer", "final_answer", "output"):
                val = run.output_snapshot.get(key)
                if val is not None and str(val).strip():
                    answer = str(val)
                    break
        if not answer and run.error_message:
            answer = f"[推理失败: {run.error_message}]"
        # 得分（从 question_data 找，key = (qid_int, run_id)）
        qd_match = question_data.get((qid_int, run.id))
        score = qd_match.get("score") if qd_match else None
        multi_round_data[qid_int].append({
            "run_index": run.run_index or 0,
            "agent_run_id": run.id,
            "score": score,
            "duration_ms": latency,
            "answer": answer,
            "tokens_total": run.tokens_total,
            "inference_model": model_label,
        })

    # questions 已在上文构建（questions_per_round / questions_summary）

    # 题目ID 从 1 开始编号（DB question_id 可能 0-indexed）
    # 按 original_qid 唯一分配展示 ID
    unique_oqids = sorted({q["original_qid"] for q in questions_per_round})
    qid_lookup = {oqid: (idx + 1) for idx, oqid in enumerate(unique_oqids)}

    for q in questions_per_round:
        q["qid"] = qid_lookup[q["original_qid"]]
    for q in questions_summary:
        q["qid"] = qid_lookup[q["original_qid"]]
    for fr in scenario_fails:
        if fr.get("qid") in qid_lookup:
            fr["qid"] = qid_lookup[fr["qid"]]
    for fr in stability_fails:
        if fr.get("qid") in qid_lookup:
            fr["qid"] = qid_lookup[fr["qid"]]
    multi_round_data = {qid_lookup.get(k, k): v for k, v in multi_round_data.items()}

    # 质量指标汇总（从 metrics_summary 读模型取，或从 quality_score.metric_breakdown 取）
    metric_summary: List[Dict[str, Any]] = []
    metric_breakdown = quality_score.get("metric_breakdown") or {}

    # 构建指标名映射：内置指标 + 自定义指标，与 web 端 metricLabelMap 保持一致
    # 从 METRIC_SPECS 获取全量内置指标中文名（与界面显示一致）
    from evaluators.deepeval_metrics.deepeval_suite import METRIC_SPECS
    metric_name_map: Dict[str, str] = {spec.key: spec.label for spec in METRIC_SPECS}
    custom_ids = _parse_custom_metric_ids(task)
    if custom_ids:
        custom_rows = (
            await session.execute(
                select(CustomMetric).where(CustomMetric.id.in_(custom_ids))
            )
        ).scalars().all()
        for cm in custom_rows:
            metric_name_map[f"custom_{cm.id}"] = cm.name

    # 运行效能指标展示名
    metric_name_map.setdefault("runtime.inference_latency_ms", "推理耗时(ms)")
    metric_name_map.setdefault("runtime.tokens_total", "Token消耗")

    for mkey in _ordered_metric_names(all_metric_names):
        avg = metric_breakdown.get(mkey)
        metric_summary.append({
            "metric_name": mkey,
            "display_name": _metric_display_name(mkey, metric_name_map),
            "avg_score": avg,
        })

    result = {
        "task_id": task.id,
        "status": task.status,
        "agent_name": agent_name or "—",
        "agent_version": agent_version or "—",
        "eval_model": eval_model_display or "—",
        "dataset_name": dataset_name or "—",
        "temperature": temperature,
        "quality_score": quality_score,
        "verdicts": verdicts,
        "process_consistency_data": process_consistency_data,
        "scenario_matrix": scenario_matrix,
        "questions": questions_per_round,
        "questions_summary": questions_summary,
        "scenario_fails": scenario_fails,
        "stability_fails": stability_fails,
        "multi_round_data": dict(multi_round_data),
        "metric_summary": metric_summary,
        "all_metric_names": all_metric_names,
        "metric_name_map": metric_name_map,
        "created_at": task.created_at,
        "completed_at": task.completed_at,
        "evaluation_mode": get_evaluation_mode(task),
        "inference_rounds": task.inference_rounds or 1,
        "selected_metrics": _parse_selected_metrics(task),
        "dimension_flags": _get_dimension_flags(task),
        "target_latency": target_latency,
        "target_tokens": target_tokens,
        "summary": task.summary or "",
    }
    _normalize_task_data(result)
    return result


def _parse_selected_metrics(task: EvaluationTask) -> List[str]:
    """解析任务选定的评估指标。"""
    metrics = []
    if task.selected_metrics:
        try:
            raw = json.loads(task.selected_metrics) if isinstance(task.selected_metrics, str) else task.selected_metrics
            if isinstance(raw, list):
                metrics = [str(m) for m in raw if m]
        except (json.JSONDecodeError, TypeError):
            pass
    return metrics


def _get_dimension_flags(task: EvaluationTask) -> Dict[str, bool]:
    """获取四维开关。"""
    return {
        "quality": True,
        "process_consistency": bool(getattr(task, "enable_process_consistency", True)),
        "scenario": bool(getattr(task, "auto_scenario_score", True)),
        "stability": bool(getattr(task, "enable_stability", True)),
    }


def _ordered_metric_names(metric_names: set) -> List[str]:
    ordered = [m for m in _PREFERRED_METRIC_ORDER if m in metric_names]
    rest = sorted(m for m in metric_names if m not in _PREFERRED_METRIC_ORDER)
    return ordered + rest


def _metric_display_name(metric_key: str, name_map: Optional[Dict[str, str]] = None) -> str:
    if name_map and metric_key in name_map:
        return name_map[metric_key]
    return METRIC_DISPLAY_NAMES.get(metric_key, metric_key)


def _parse_custom_metric_ids(task: EvaluationTask) -> List[int]:
    """从任务配置中解析自定义指标 ID 列表。"""
    raw = getattr(task, "custom_metric_ids", None)
    if not raw:
        return []
    if isinstance(raw, list):
        return [int(x) for x in raw if x]
    try:
        import json as _json
        parsed = _json.loads(raw) if isinstance(raw, str) else []
        return [int(x) for x in parsed if x]
    except (TypeError, ValueError):
        return []


def _verdict_label(payload: Optional[Dict[str, Any]], key: str) -> str:
    if not payload:
        return "—"
    dim = payload.get(key) or {}
    status = dim.get("status", "na")
    if key == "runtime_stability" and status == "descriptive":
        perf = dim.get("performance") or {}
        perf_status = perf.get("status", "na")
        return _PERF_STATUS_CN.get(perf_status, perf_status)
    return _VERDICT_STATUS_CN.get(status, status)


def _format_dt(dt) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════
# Sheet 构建层
# ═══════════════════════════════════════════════════════════

def _init_styles():
    """初始化样式对象（openpyxl 需要延迟导入）。字体/字号/颜色对齐前端 UCD 设计 token。"""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    class _S:
        # 字体族统一为微软雅黑（与前端 --edp-font-family 中文 fallback 对齐）
        # 表头：白字 + 主色底，11px bold
        font_header = Font(name=FONT_FAMILY, bold=True, color=C_WHITE, size=FONT_SIZE_SM)
        # 大标题：主色字 17px bold（对齐 --edp-font-xl）
        font_title = Font(name=FONT_FAMILY, bold=True, color=C_PRIMARY, size=FONT_SIZE_XL)
        # 副标题：辅助灰 11px
        font_subtitle = Font(name=FONT_FAMILY, color=C_TEXT3, size=FONT_SIZE_SM)
        # section 标题：主色字 12px bold
        font_section = Font(name=FONT_FAMILY, bold=True, color=C_PRIMARY, size=FONT_SIZE_LG)
        # KPI 数值：主色 20px bold（对齐 --edp-font-2xl）
        font_kpi_val = Font(name=FONT_FAMILY, bold=True, color=C_PRIMARY, size=FONT_SIZE_2XL)
        # KPI 标签：辅助灰 11px
        font_kpi_lbl = Font(name=FONT_FAMILY, color=C_TEXT3, size=FONT_SIZE_SM)
        # 数据正文：10px
        font_data = Font(name=FONT_FAMILY, size=FONT_SIZE_MD)
        # 数据加粗：10px bold
        font_data_bold = Font(name=FONT_FAMILY, size=FONT_SIZE_MD, bold=True)
        # 主文字：10px 主色 #2B2F3A
        font_text = Font(name=FONT_FAMILY, size=FONT_SIZE_MD, color=C_TEXT1)
        # 脚注：辅助灰 10px（对齐 --edp-font-xs）
        font_footnote = Font(name=FONT_FAMILY, color=C_TEXT3, size=FONT_SIZE_XS)

        fill_header = PatternFill(start_color=C_PRIMARY, end_color=C_PRIMARY, fill_type="solid")
        fill_section_bg = PatternFill(start_color=C_PRIMARY_SOFT, end_color=C_PRIMARY_SOFT, fill_type="solid")
        fill_primary_soft = PatternFill(start_color=C_PRIMARY_SOFT, end_color=C_PRIMARY_SOFT, fill_type="solid")
        fill_zebra = PatternFill(start_color=C_BG_ZEBRA, end_color=C_BG_ZEBRA, fill_type="solid")
        fill_white = PatternFill(start_color=C_WHITE, end_color=C_WHITE, fill_type="solid")

        border = Border(
            left=Side(style="thin", color=C_BORDER),
            right=Side(style="thin", color=C_BORDER),
            top=Side(style="thin", color=C_BORDER),
            bottom=Side(style="thin", color=C_BORDER),
        )
        border_soft = Border(
            left=Side(style="thin", color=C_BORDER_SOFT),
            right=Side(style="thin", color=C_BORDER_SOFT),
            top=Side(style="thin", color=C_BORDER_SOFT),
            bottom=Side(style="thin", color=C_BORDER_SOFT),
        )

        align_center = Alignment(horizontal="center", vertical="center", wrap_text=True)
        align_left = Alignment(horizontal="left", vertical="center", wrap_text=True)
        align_left_top = Alignment(horizontal="left", vertical="top", wrap_text=True)
        # 表头对齐：左对齐垂直居中（对齐前端 .edp-table th text-align:left）
        align_header = Alignment(horizontal="left", vertical="center", wrap_text=True)

        def fill_for(self, color: str) -> Any:
            return PatternFill(start_color=color, end_color=color, fill_type="solid")

        def font_for(self, color: str, bold: bool = True, size: int = FONT_SIZE_MD) -> Font:
            return Font(name=FONT_FAMILY, size=size, bold=bold, color=color)

    return _S()


def _write_header_row(ws, row: int, headers: List[str], styles, col_offset: int = 1):
    """写表头行。表头左对齐（对齐前端 .edp-table th text-align:left）。"""
    for i, h in enumerate(headers):
        c = ws.cell(row=row, column=col_offset + i, value=h)
        c.font = styles.font_header
        c.fill = styles.fill_header
        c.alignment = styles.align_header
        c.border = styles.border


def _set_col_widths(ws, widths: Dict[str, float]):
    from openpyxl.utils import get_column_letter
    for col_letter, w in widths.items():
        ws.column_dimensions[col_letter].width = w


def _build_overview_sheet(wb, td: Dict[str, Any], styles):
    """Sheet 1: 评估总览。"""
    from openpyxl.utils import get_column_letter

    ws = wb.create_sheet("评估总览")
    _set_col_widths(ws, {"A": 22, "B": 14, "C": 18, "D": 12, "E": 55, "F": 12, "G": 14, "H": 12})

    qs = td["quality_score"]
    verdicts = td["verdicts"]
    questions = td["questions_summary"]
    total_q = len(questions)
    failed_q = [q for q in questions if not q["passed"]]
    zero_q = [q for q in questions if q["score"] is not None and q["score"] <= 0]

    # 计算所有指标的综合通过率
    q_dim = verdicts.get("quality") or {}
    q_metrics = q_dim.get("metrics") or {}
    metric_pass_rates = q_metrics.get("metric_pass_rates") or {}
    all_pass_rates = []
    for stats in metric_pass_rates.values():
        if isinstance(stats, dict):
            pr = stats.get("pass_rate")
            if pr is not None:
                all_pass_rates.append(pr)
    overall_pass_rate = sum(all_pass_rates) / len(all_pass_rates) if all_pass_rates else None

    # 正确性通过率（保留用于向后兼容）
    correctness_pass_rate = None
    correctness_stats = metric_pass_rates.get("correctness") or {}
    if isinstance(correctness_stats, dict):
        correctness_pass_rate = correctness_stats.get("pass_rate")
    # 兜底：从 quality_score.correctness_stability.pass_rate 取（0-100 百分比）
    if correctness_pass_rate is None:
        cs = qs.get("correctness_stability") or {}
        correctness_pass_rate = cs.get("pass_rate")

    # 标题
    ws.merge_cells("A1:H1")
    ws["A1"] = f"{td['dataset_name']} 评测报告"
    ws["A1"].font = styles.font_title
    ws["A1"].alignment = styles.align_left
    ws.row_dimensions[1].height = 36

    # 副标题
    ws.merge_cells("A2:H2")
    ws["A2"] = (f"Onto-Eval 本体评测平台 | Agent: {td['agent_name']} | "
                f"评测模型: {td['eval_model']} | 评测时间: {_format_dt(td.get('completed_at'))[:10]}")
    ws["A2"].font = styles.font_subtitle
    ws["A2"].alignment = styles.align_left
    ws.row_dimensions[2].height = 20

    # ── KPI 卡片区 (行4-6) ──
    total_score = qs.get("total_score")
    kpi_cards = [
        ("质量总分", f"{total_score:.2f}" if total_score is not None else "—",
         f"{total_score*100:.1f}%" if total_score is not None else "—",
         "pass" if q_dim.get("status") == "pass" else "warn" if q_dim.get("status") == "warn" else "fail"),
        ("综合通过率",
         f"{overall_pass_rate:.1f}%" if overall_pass_rate is not None else "—",
         f"{total_q - len(failed_q)}/{total_q}题" if total_q else "—",
         "pass" if (overall_pass_rate or 0) >= 80 else "warn" if (overall_pass_rate or 0) >= 60 else "fail"),
        ("未通过题数", str(len(failed_q)), f"{len(failed_q)}/{total_q}题",
         "fail" if failed_q else "pass"),
        ("0分题数", str(len(zero_q)), f"{len(zero_q)}/{total_q}题",
         "fail" if zero_q else "pass"),
    ]
    for i, (label, val, sub, status) in enumerate(kpi_cards):
        col_start = 1 + i * 2
        col_end = col_start + 1
        cls = get_column_letter(col_start)
        cle = get_column_letter(col_end)
        fill_color, font_color = _status_fill_font(status)

        ws.merge_cells(f"{cls}4:{cle}4")
        c = ws.cell(row=4, column=col_start, value=label)
        c.font = styles.font_kpi_lbl
        c.alignment = styles.align_center
        c.fill = styles.fill_primary_soft

        ws.merge_cells(f"{cls}5:{cle}5")
        c = ws.cell(row=5, column=col_start, value=val)
        c.font = styles.font_kpi_val
        c.alignment = styles.align_center
        c.fill = styles.fill_for(fill_color)

        ws.merge_cells(f"{cls}6:{cle}6")
        c = ws.cell(row=6, column=col_start, value=sub)
        c.font = styles.font_for(font_color)
        c.alignment = styles.align_center
        c.fill = styles.fill_for(fill_color)
    ws.row_dimensions[4].height = 22
    ws.row_dimensions[5].height = 36
    ws.row_dimensions[6].height = 20

    # ── 各维度评估结果 (行9-15) ──（行8 空行作为 section 间距）
    ws.merge_cells("A9:H9")
    ws["A9"] = "各维度评估结果"
    ws["A9"].font = styles.font_section
    ws["A9"].alignment = styles.align_left
    ws["A9"].fill = styles.fill_section_bg
    ws.row_dimensions[9].height = 26

    _write_header_row(ws, 10, ["维度", "状态", "得分/通过率", "达标线", "说明"], styles)
    ws.merge_cells("E10:H10")
    ws.row_dimensions[10].height = 28

    dim_rows = _build_dim_verdict_rows(verdicts, qs, correctness_pass_rate, total_q, len(failed_q))
    for r_idx, (dim, status, score, thr, desc) in enumerate(dim_rows, start=11):
        ws.cell(row=r_idx, column=1, value=dim).font = styles.font_data_bold
        ws.cell(row=r_idx, column=1).alignment = styles.align_left
        ws.cell(row=r_idx, column=1).border = styles.border

        fill_color, font_color = _status_fill_font(status)
        c = ws.cell(row=r_idx, column=2, value=_status_label(status))
        c.fill = styles.fill_for(fill_color)
        c.font = styles.font_for(font_color)
        c.alignment = styles.align_center
        c.border = styles.border

        for col_idx, val in enumerate([score, thr], start=3):
            c = ws.cell(row=r_idx, column=col_idx, value=val)
            c.font = styles.font_data
            c.alignment = styles.align_center
            c.border = styles.border

        ws.merge_cells(start_row=r_idx, start_column=5, end_row=r_idx, end_column=8)
        c = ws.cell(row=r_idx, column=5, value=desc)
        c.font = styles.font_data
        c.alignment = styles.align_left
        c.border = styles.border
        ws.row_dimensions[r_idx].height = 24

    # ── 核心发现 (行17-18) ──（行16 空行 section 间距）
    dim_end = 11 + len(dim_rows) - 1
    sec_finding_row = dim_end + 2  # 空行 + section 标题
    ws.merge_cells(start_row=sec_finding_row, start_column=1, end_row=sec_finding_row, end_column=8)
    c = ws.cell(row=sec_finding_row, column=1, value="核心发现")
    c.font = styles.font_section
    c.alignment = styles.align_left
    c.fill = styles.fill_section_bg
    ws.row_dimensions[sec_finding_row].height = 26

    low_score_q = [q for q in questions if q["score"] is not None and q["score"] <= 0.5]
    near_pass_q = [q for q in questions if q["score"] is not None and 0.7 <= q["score"] < 0.8]
    body_row = sec_finding_row + 1
    ws.merge_cells(start_row=body_row, start_column=1, end_row=body_row, end_column=8)
    template_finding = _build_finding_text(qs, verdicts, correctness_pass_rate, total_q,
                                           len(failed_q), len(zero_q), len(low_score_q), len(near_pass_q))
    finding = _extract_core_finding(td.get("summary", ""), fallback=template_finding, max_len=200)
    c = ws.cell(row=body_row, column=1, value=finding)
    c.font = styles.font_data
    c.alignment = styles.align_left_top
    c.border = styles.border
    ws.row_dimensions[body_row].height = 60

    # ── 报告配置项 ──（空行 + section 标题）
    sec_cfg_row = body_row + 2
    ws.merge_cells(start_row=sec_cfg_row, start_column=1, end_row=sec_cfg_row, end_column=8)
    c = ws.cell(row=sec_cfg_row, column=1, value="报告配置项")
    c.font = styles.font_section
    c.alignment = styles.align_left
    c.fill = styles.fill_section_bg
    ws.row_dimensions[sec_cfg_row].height = 26

    dim_flags = td["dimension_flags"]
    dim_str = " ".join(
        f"{name}{'✓' if v else '✗'}" for name, v in dim_flags.items()
    )
    metrics_str = ", ".join(td.get("selected_metrics") or []) or "—"
    config_lines = [
        f"任务ID: {td['task_id']}  |  Agent版本: {td['agent_version']}  |  评估模式: {'在线评估' if td['evaluation_mode']=='online' else '离线评估'}  |  推理轮次: {td['inference_rounds']}  |  评测模型: {td['eval_model']}  |  Temperature: {td['temperature'] if td['temperature'] is not None else '—'}",
        f"创建时间: {_format_dt(td.get('created_at'))}  |  完成时间: {_format_dt(td.get('completed_at'))}",
        f"维度开关: {dim_str}  |  质量指标: {metrics_str}  |  数据集: {td['dataset_name']}",
    ]
    for i, line in enumerate(config_lines):
        r = sec_cfg_row + 1 + i
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=8)
        c = ws.cell(row=r, column=1, value=line)
        c.font = styles.font_footnote
        c.alignment = styles.align_left
        c.border = styles.border
        ws.row_dimensions[r].height = 20

    ws.sheet_view.showGridLines = False


def _build_dim_verdict_rows(
    verdicts: Dict[str, Any], qs: Dict[str, Any],
    correctness_pass_rate: Optional[float], total_q: int, failed_count: int
) -> List[Tuple[str, str, str, str, str]]:
    """构建四维结论行。"""
    rows: List[Tuple[str, str, str, str, str]] = []

    q_dim = verdicts.get("quality") or {}
    q_metrics = q_dim.get("metrics") or {}
    metric_pass_rates = q_metrics.get("metric_pass_rates") or {}

    # 计算所有指标的平均通过率
    all_pass_rates = []
    all_avg_scores = []
    metric_details = []
    for metric_name, stats in metric_pass_rates.items():
        if isinstance(stats, dict):
            pr = stats.get("pass_rate")
            avg = stats.get("avg_score")
            if pr is not None:
                all_pass_rates.append(pr)
            if avg is not None:
                all_avg_scores.append(avg)
            metric_details.append({
                "name": metric_name,
                "pass_rate": pr,
                "avg_score": avg,
            })

    overall_pass_rate = sum(all_pass_rates) / len(all_pass_rates) if all_pass_rates else None
    overall_avg_score = sum(all_avg_scores) / len(all_avg_scores) if all_avg_scores else None

    result_score = qs.get("result_score")
    total_score = qs.get("total_score")

    q_status = q_dim.get("status", "na")

    # 质量总分
    rows.append((
        "质量总分", q_status,
        f"{total_score:.2f}" if total_score is not None else "—",
        "80%",
        f"结果分 {_fmt_score(result_score)}；综合通过率 {_fmt_pct(overall_pass_rate)}"
    ))

    # 质量指标（所有评测指标）
    score_str = "—"
    if overall_pass_rate is not None:
        score_str = f"{_fmt_score(overall_avg_score)} / {_fmt_pct(overall_pass_rate)}"

    # 构建说明：显示各指标的通过情况
    desc_parts = []
    for md in metric_details:
        metric_display = _metric_display_name(md["name"])
        pr = md.get("pass_rate")
        desc_parts.append(f"{metric_display} {_fmt_pct(pr)}")
    desc = "；".join(desc_parts) if desc_parts else f"{total_q - failed_count}/{total_q} 题通过" if total_q else "—"

    rows.append((
        "质量指标", q_status, score_str, "80%", desc
    ))

    # 过程一致性
    pc = verdicts.get("process_consistency") or {}
    rows.append((
        "过程一致性", pc.get("status", "na"),
        f"{pc.get('score'):.2f}" if pc.get("score") is not None else "—",
        "—",
        pc.get("headline", "—")
    ))

    # 场景子步骤交付
    sd = verdicts.get("scenario_delivery") or {}
    rows.append((
        "场景子步骤交付", sd.get("status", "na"),
        f"{sd.get('score'):.2f}" if sd.get('score') is not None else "—",
        "80%",
        sd.get("headline", "—")
    ))

    # 运行稳定性
    rs = verdicts.get("runtime_stability") or {}
    perf = rs.get("performance") or {}
    struct = rs.get("structural") or {}
    perf_status = perf.get("status", "na")
    headline_parts = []
    if struct.get("headline") and struct.get("status") not in ("na", "disabled"):
        headline_parts.append(f"结构: {struct.get('headline')}")
    if perf.get("headline"):
        headline_parts.append(f"性能: {perf.get('headline')}")
    rows.append((
        "运行稳定性", rs.get("status", "na") if rs.get("status") != "descriptive" else perf_status,
        "—",
        "—",
        "；".join(headline_parts) if headline_parts else rs.get("headline", "—")
    ))

    return rows


def _parse_llm_summary_sections(summary: str) -> List[tuple]:
    """解析 LLM 生成的 task.summary markdown 为 [(title, body), ...]。

    支持两种格式：
    - 格式 A: ### 总标题 + **N. 小节名** + 小节正文
    - 格式 B: ## 总标题 + ### N. 小节名 + 空行 + 小节正文
    """
    if not summary or not summary.strip():
        return []
    import re
    # 匹配小节标题行：可选 # 前缀，可选 **，数字编号，标题文本，可选 **
    pattern = re.compile(
        r"^(?:#{1,4}\s*)?(?:\*\*)?\s*(\d+)[.、]\s*([^\n*]+?)(?:\*\*)?\s*$",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(summary))
    if not matches:
        return []
    sections = []
    for i, m in enumerate(matches):
        title = m.group(2).strip().rstrip(":：").strip()
        # 去除标题尾部的 markdown 残留
        title = title.replace("**", "").replace("##", "").strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(summary)
        body = summary[start:end].strip()
        # 剥除正文开头的 markdown 残留
        body = body.lstrip("\n").strip()
        # 去除正文中的 ** 加粗标记，保留可读文本
        body = body.replace("**", "")
        if title and body:
            sections.append((title, body))
    return sections


def _extract_core_finding(summary: str, fallback: str, max_len: int = 200) -> str:
    """从 LLM summary 抽取第 1 节正文首句作为核心发现；失败回退到 fallback。"""
    if not summary or not summary.strip():
        return fallback
    sections = _parse_llm_summary_sections(summary)
    if not sections:
        return fallback
    body = sections[0][1]
    if not body:
        return fallback
    # 取首句（第一个中文句号之前）
    first_sentence_end = -1
    for sep in ["。", "！", "？"]:
        idx = body.find(sep)
        if idx != -1 and (first_sentence_end == -1 or idx < first_sentence_end):
            first_sentence_end = idx + 1
    if first_sentence_end > 0:
        body = body[:first_sentence_end]
    if len(body) > max_len:
        body = body[:max_len].rstrip() + "..."
    return body or fallback


def _estimate_row_height(text: str, base: int = 20, per_line: int = 15, max_height: int = 240) -> int:
    """根据文本长度粗略估算行高。"""
    if not text:
        return base
    lines = text.count("\n") + 1
    extra = max(0, len(text) - 60) // 50
    total_lines = lines + extra
    return min(base + total_lines * per_line, max_height)


def _build_finding_text(
    qs: Dict[str, Any], verdicts: Dict[str, Any],
    correctness_pass_rate: Optional[float], total_q: int,
    failed_count: int, zero_count: int, low_count: int, near_count: int
) -> str:
    """构建核心发现文字。"""
    parts = []

    # 质量指标综合通过率
    q_dim = verdicts.get("quality") or {}
    q_metrics = q_dim.get("metrics") or {}
    metric_pass_rates = q_metrics.get("metric_pass_rates") or {}
    all_pass_rates = []
    metric_details = []
    for metric_name, stats in metric_pass_rates.items():
        if isinstance(stats, dict):
            pr = stats.get("pass_rate")
            if pr is not None:
                all_pass_rates.append(pr)
                metric_details.append((metric_name, pr))
    overall_pass_rate = sum(all_pass_rates) / len(all_pass_rates) if all_pass_rates else None

    if overall_pass_rate is not None:
        passed = total_q - failed_count
        parts.append(f"综合通过率{overall_pass_rate:.1f}%（{passed}/{total_q}题）")
        # 各指标详情
        metric_strs = []
        for mname, pr in metric_details:
            metric_strs.append(f"{_metric_display_name(mname)}{pr:.1f}%")
        if metric_strs:
            parts.append(f"各指标：{'、'.join(metric_strs)}")
    if zero_count:
        parts.append(f"0分题目{zero_count}题")
    if low_count:
        parts.append(f"低分题目（≤0.5）{low_count}题")
    if near_count:
        parts.append(f"接近及格题目（0.7-0.8）{near_count}题")

    # 四维状态补充
    pc = verdicts.get("process_consistency") or {}
    if pc.get("status") == "warn":
        parts.append("过程一致性需关注")
    elif pc.get("status") == "fail":
        parts.append("过程一致性未达标")

    sd = verdicts.get("scenario_delivery") or {}
    if sd.get("status") == "warn":
        parts.append("场景交付需关注")
    elif sd.get("status") == "fail":
        parts.append("场景交付未达标")

    rs = verdicts.get("runtime_stability") or {}
    perf = rs.get("performance") or {}
    if perf.get("status") == "elevated":
        parts.append("运行稳定性延迟偏高")
    elif perf.get("status") == "severe":
        parts.append("运行稳定性延迟严重")

    return "。".join(parts) + "。" if parts else "—"


def _build_failed_questions_sheet(wb, td: Dict[str, Any], styles):
    """Sheet 2: 未通过题目汇总（以指标为粒度，一行 = 一题目×一轮次×一未通过指标）。"""
    ws = wb.create_sheet("未通过题目汇总")
    _set_col_widths(ws, {"A": 6, "B": 10, "C": 8, "D": 14, "E": 8, "F": 14, "G": 12, "H": 60, "I": 40})

    _write_header_row(ws, 1, ["序号", "题目ID", "轮次", "指标", "得分", "推理时延(ms)", "Token消耗", "题目完整内容", "评语"], styles)
    ws.row_dimensions[1].height = 30

    inference_rounds = td.get("inference_rounds", 1)

    def _write_fail_row(row_num, q, values, score_col=5):
        """写入一行未通过数据并设置样式。"""
        r_idx = row_num + 1
        zebra = styles.fill_zebra if r_idx % 2 == 0 else styles.fill_white
        score = values[score_col - 1]
        fill_color, font_color = _score_fill_font(score)
        for col_idx, val in enumerate(values, start=1):
            c = ws.cell(row=r_idx, column=col_idx, value=val)
            c.border = styles.border
            if col_idx == score_col:
                c.fill = styles.fill_for(fill_color)
                c.font = styles.font_for(font_color, size=FONT_SIZE_SM)
                c.alignment = styles.align_center
            else:
                c.fill = zebra
                c.font = styles.font_data
                c.alignment = styles.align_center if col_idx <= 7 else styles.align_left_top
        ws.row_dimensions[r_idx].height = 80

    # 展开为指标级行，只保留未通过的指标
    row_num = 0
    for q in td["questions"]:
        metric_scores = q.get("metric_scores") or {}
        metric_passed = q.get("metric_passed") or {}
        metric_reasons = q.get("metric_reasons") or {}
        question_text = _truncate_text(q.get("question", ""), 2000)
        run_label = f"R{q.get('run_index', 0) + 1}" if inference_rounds > 1 else ""
        # 运行效能指标
        latency_val = metric_scores.get("runtime.inference_latency_ms")
        tokens_val = metric_scores.get("runtime.tokens_total")
        try:
            latency_str = f"{float(latency_val):.0f}" if latency_val is not None else "—"
        except (TypeError, ValueError):
            latency_str = "—"
        try:
            tokens_str = f"{float(tokens_val):.0f}" if tokens_val is not None else "—"
        except (TypeError, ValueError):
            tokens_str = "—"
        # 如果题目完全没有指标明细，按老逻辑用题目级 passed 判断
        if not metric_scores:
            if not q["passed"]:
                row_num += 1
                score = q["score"]
                values = [
                    row_num, f"Q{q['qid']}", run_label, "—",
                    round(score, 2) if score is not None else None,
                    latency_str, tokens_str,
                    question_text,
                    _truncate_text(q.get("eval_reason", ""), 1000),
                ]
                _write_fail_row(row_num, q, values)
            continue
        for mname, mscore in metric_scores.items():
            passed = metric_passed.get(mname, True)
            if passed:
                continue
            row_num += 1
            reason = metric_reasons.get(mname) or q.get("eval_reason", "")
            values = [
                row_num, f"Q{q['qid']}", run_label,
                _metric_display_name(mname, td.get("metric_name_map")),
                round(mscore, 2) if mscore is not None else None,
                latency_str, tokens_str,
                question_text,
                _truncate_text(reason, 1000),
            ]
            _write_fail_row(row_num, q, values)

    ws.freeze_panes = "A2"
    ws.sheet_view.showGridLines = False


def _build_question_details_sheet(wb, td: Dict[str, Any], styles):
    """Sheet 3: 题目详情（以指标为粒度，一行 = 一题目×一轮次×一指标）。"""
    ws = wb.create_sheet("题目详情")
    _set_col_widths(ws, {"A": 6, "B": 10, "C": 8, "D": 14, "E": 8, "F": 50, "G": 65, "H": 55, "I": 25})

    _write_header_row(ws, 1, ["序号", "题目ID", "轮次", "指标", "得分", "题目内容", "Agent回答", "标准答案", "评语"], styles)
    ws.row_dimensions[1].height = 30

    inference_rounds = td.get("inference_rounds", 1)

    def _write_detail_row(row_num, q, values, score_col=5):
        """写入一行详情数据并设置样式。"""
        r_idx = row_num + 1
        zebra = styles.fill_zebra if r_idx % 2 == 0 else styles.fill_white
        score = values[score_col - 1]
        fill_color, font_color = _score_fill_font(score)
        for col_idx, val in enumerate(values, start=1):
            c = ws.cell(row=r_idx, column=col_idx, value=val)
            c.border = styles.border
            if col_idx == score_col:
                c.fill = styles.fill_for(fill_color)
                c.font = styles.font_for(font_color, size=FONT_SIZE_SM)
                c.alignment = styles.align_center
            else:
                c.fill = zebra
                c.font = styles.font_data
                c.alignment = styles.align_center if col_idx <= 4 else styles.align_left_top
        ws.row_dimensions[r_idx].height = 120

    row_num = 0
    for q in td["questions"]:
        metric_scores = q.get("metric_scores") or {}
        metric_passed = q.get("metric_passed") or {}
        metric_reasons = q.get("metric_reasons") or {}
        # 截断超长文本
        agent_answer = _truncate_text(q.get("agent_answer", ""), 5000)
        ground_truth = _truncate_text(_try_format_json(q.get("ground_truth", "")), 5000)
        question_text = _truncate_text(q.get("question", ""), 2000)
        run_label = f"R{q.get('run_index', 0) + 1}" if inference_rounds > 1 else ""

        # 如果题目完全没有指标明细，按老逻辑出一行
        if not metric_scores:
            row_num += 1
            score = q["score"]
            values = [
                row_num, f"Q{q['qid']}", run_label, "—",
                round(score, 2) if score is not None else None,
                question_text, agent_answer, ground_truth,
                _truncate_text(q.get("eval_reason", ""), 3000),
            ]
            _write_detail_row(row_num, q, values)
            continue

        for mname, mscore in metric_scores.items():
            row_num += 1
            reason = metric_reasons.get(mname) or q.get("eval_reason", "")
            values = [
                row_num, f"Q{q['qid']}", run_label,
                _metric_display_name(mname, td.get("metric_name_map")),
                round(mscore, 2) if mscore is not None else None,
                question_text, agent_answer, ground_truth,
                _truncate_text(reason, 3000),
            ]
            _write_detail_row(row_num, q, values)

    ws.freeze_panes = "A2"
    ws.sheet_view.showGridLines = False


def _try_format_json(text: str) -> str:
    """若文本是 JSON 字符串，格式化（缩进）后返回；否则原样返回。"""
    if not text:
        return text
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return text
    try:
        parsed = json.loads(stripped)
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, TypeError):
        return text


def _truncate_text(text: str, max_len: int) -> str:
    """截断超长文本，防止 Excel 单元格溢出。"""
    if not text:
        return ""
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text



def _build_quality_sheet(wb, td: Dict[str, Any], styles):
    """Sheet 5: 质量指标 — 维度结论 + 指标通过率汇总表。"""
    ws = wb.create_sheet("质量指标")
    _set_col_widths(ws, {"A": 22, "B": 14, "C": 12, "D": 12})

    qs = td["quality_score"]
    verdicts = td["verdicts"]
    q_dim = verdicts.get("quality") or {}
    q_metrics = q_dim.get("metrics") or {}
    metric_name_map = td.get("metric_name_map") or {}

    # ── 区域1: 维度结论卡片 ──
    ws.merge_cells("A1:D1")
    ws["A1"] = "质量指标维度结论"
    ws["A1"].font = styles.font_section
    ws["A1"].alignment = styles.align_left
    ws["A1"].fill = styles.fill_section_bg
    ws.row_dimensions[1].height = 26

    rows = [
        ("状态", _status_label(q_dim.get("status", "na"))),
    ]
    total_score = qs.get("total_score")
    result_score = qs.get("result_score")
    if total_score is not None:
        rows.append(("质量总分", f"{total_score:.2f}"))
    if result_score is not None:
        rows.append(("结果分", f"{result_score:.2f}"))
    headline = q_dim.get("headline") or ""
    if headline:
        rows.append(("结论", headline))

    for i, (label, val) in enumerate(rows, start=2):
        zebra = styles.fill_zebra if i % 2 == 0 else styles.fill_white
        ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=4)
        c = ws.cell(row=i, column=1, value=label)
        c.font = styles.font_data_bold; c.alignment = styles.align_left; c.border = styles.border; c.fill = zebra
        c = ws.cell(row=i, column=2, value=val)
        c.font = styles.font_data; c.alignment = styles.align_left_top; c.border = styles.border; c.fill = zebra
        ws.cell(row=i, column=3).border = styles.border
        ws.cell(row=i, column=3).fill = zebra
        ws.cell(row=i, column=4).border = styles.border
        ws.cell(row=i, column=4).fill = zebra
        if label == "状态":
            status = q_dim.get("status", "na")
            fill_color, font_color = _status_fill_font(status)
            c.fill = styles.fill_for(fill_color)
            c.font = styles.font_for(font_color)
        ws.row_dimensions[i].height = 22

    # ── 区域2: 指标通过率汇总表 ──
    metric_pass_rates = q_metrics.get("metric_pass_rates") or {}
    if metric_pass_rates:
        header_row = len(rows) + 3  # 留一行空行
        ws.merge_cells(start_row=header_row, start_column=1, end_row=header_row, end_column=4)
        ws.cell(row=header_row, column=1, value="指标通过率汇总").font = styles.font_section
        ws.cell(row=header_row, column=1).alignment = styles.align_left
        ws.cell(row=header_row, column=1).fill = styles.fill_section_bg
        ws.cell(row=header_row, column=1).border = styles.border
        ws.row_dimensions[header_row].height = 24

        col_header_row = header_row + 1
        _write_header_row(ws, col_header_row, ["指标", "通过率", "平均分", "有效评分数"], styles)

        data_row = col_header_row + 1
        for mname, stats in metric_pass_rates.items():
            display_name = _metric_display_name(mname, metric_name_map)
            pass_rate = stats.get("pass_rate")
            avg_score = stats.get("avg_score")
            scored_count = stats.get("scored_count", 0)
            zebra = styles.fill_zebra if data_row % 2 == 0 else styles.fill_white
            values = [
                display_name,
                f"{pass_rate:.1f}%" if pass_rate is not None else "—",
                f"{avg_score:.2f}" if avg_score is not None else "—",
                scored_count,
            ]
            for col_idx, val in enumerate(values, start=1):
                c = ws.cell(row=data_row, column=col_idx, value=val)
                c.font = styles.font_data
                c.alignment = styles.align_center if col_idx > 1 else styles.align_left
                c.border = styles.border
                c.fill = zebra
            ws.row_dimensions[data_row].height = 22
            data_row += 1

    ws.sheet_view.showGridLines = False


def _build_process_consistency_sheet(wb, td: Dict[str, Any], styles):
    """Sheet 6: 过程一致性 — 维度结论 + 过程点通过率汇总。"""
    ws = wb.create_sheet("过程一致性")
    _set_col_widths(ws, {"A": 22, "B": 14, "C": 14, "D": 14})

    verdicts = td["verdicts"]
    pc = verdicts.get("process_consistency") or {}
    m = pc.get("metrics") or {}

    # ── 区域1: 维度结论卡片 ──
    ws.merge_cells("A1:D1")
    ws["A1"] = "过程一致性维度结论"
    ws["A1"].font = styles.font_section
    ws["A1"].alignment = styles.align_left
    ws["A1"].fill = styles.fill_section_bg
    ws.row_dimensions[1].height = 26

    rows = [
        ("状态", _status_label(pc.get("status", "na"))),
    ]
    if pc.get("score") is not None:
        rows.append(("过程分", f"{pc['score']:.2f}"))
    if m.get("chain_pass_rate") is not None:
        rows.append(("链条通过率", f"{m['chain_pass_rate']}%"))
    if m.get("green") is not None:
        rows.append(("绿/黄/红", f"{m.get('green')}/{m.get('yellow')}/{m.get('red')}"))
    headline = pc.get("headline") or ""
    if headline:
        rows.append(("结论", headline))

    for i, (label, val) in enumerate(rows, start=2):
        zebra = styles.fill_zebra if i % 2 == 0 else styles.fill_white
        ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=4)
        c = ws.cell(row=i, column=1, value=label)
        c.font = styles.font_data_bold; c.alignment = styles.align_left; c.border = styles.border; c.fill = zebra
        c = ws.cell(row=i, column=2, value=val)
        c.font = styles.font_data; c.alignment = styles.align_left_top; c.border = styles.border; c.fill = zebra
        ws.cell(row=i, column=3).border = styles.border
        ws.cell(row=i, column=3).fill = zebra
        ws.cell(row=i, column=4).border = styles.border
        ws.cell(row=i, column=4).fill = zebra
        if label == "状态":
            status = pc.get("status", "na")
            fill_color, font_color = _status_fill_font(status)
            c.fill = styles.fill_for(fill_color)
            c.font = styles.font_for(font_color)
        ws.row_dimensions[i].height = 22

    # ── 区域2: 过程点通过率汇总（跨轮次聚合） ──
    pc_data = td.get("process_consistency_data") or {}
    questions = pc_data.get("questions") or []
    if questions:
        # 聚合过程点通过率
        checkpoint_stats: Dict[str, Dict[str, Any]] = {}
        for q in questions:
            for run in q.get("runs") or []:
                for item in run.get("items") or []:
                    name = item.get("name") or item.get("checkpoint_id") or "未知过程点"
                    if name not in checkpoint_stats:
                        checkpoint_stats[name] = {
                            "required": bool(item.get("required", False)),
                            "passed": 0,
                            "total": 0,
                        }
                    checkpoint_stats[name]["total"] += 1
                    if item.get("passed"):
                        checkpoint_stats[name]["passed"] += 1

        if checkpoint_stats:
            header_row = len(rows) + 3  # 留一行空行
            ws.merge_cells(start_row=header_row, start_column=1, end_row=header_row, end_column=4)
            ws.cell(row=header_row, column=1, value="过程点通过率汇总").font = styles.font_section
            ws.cell(row=header_row, column=1).alignment = styles.align_left
            ws.cell(row=header_row, column=1).fill = styles.fill_section_bg
            ws.cell(row=header_row, column=1).border = styles.border
            ws.row_dimensions[header_row].height = 24

            col_header_row = header_row + 1
            _write_header_row(ws, col_header_row, ["过程点", "类型", "通过率", "命中数"], styles)

            data_row = col_header_row + 1
            for cp_name, stats in checkpoint_stats.items():
                total = stats["total"]
                passed = stats["passed"]
                pass_rate = (passed / total * 100) if total > 0 else 0
                cp_type = "必达" if stats["required"] else "可选"
                zebra = styles.fill_zebra if data_row % 2 == 0 else styles.fill_white
                values = [
                    cp_name,
                    cp_type,
                    f"{pass_rate:.1f}%",
                    f"{passed}/{total}",
                ]
                for col_idx, val in enumerate(values, start=1):
                    c = ws.cell(row=data_row, column=col_idx, value=val)
                    c.font = styles.font_data
                    c.alignment = styles.align_center if col_idx > 1 else styles.align_left
                    c.border = styles.border
                    c.fill = zebra
                ws.row_dimensions[data_row].height = 22
                data_row += 1

    ws.sheet_view.showGridLines = False


def _build_scenario_steps_sheet(wb, td: Dict[str, Any], styles):
    """Sheet 7: 场景子步骤交付 — 维度结论 + 步骤通过率汇总 + 未通过步骤明细。"""
    ws = wb.create_sheet("场景子步骤交付")
    _set_col_widths(ws, {"A": 22, "B": 14, "C": 14, "D": 14})

    verdicts = td["verdicts"]
    sd = verdicts.get("scenario_delivery") or {}
    m = sd.get("metrics") or {}

    # ── 区域1: 维度结论卡片 ──
    ws.merge_cells("A1:D1")
    ws["A1"] = "场景子步骤交付维度结论"
    ws["A1"].font = styles.font_section
    ws["A1"].alignment = styles.align_left
    ws["A1"].fill = styles.fill_section_bg
    ws.row_dimensions[1].height = 26

    rows = [
        ("状态", _status_label(sd.get("status", "na"))),
    ]
    if sd.get("score") is not None:
        rows.append(("场景综合分", f"{sd['score']:.2f}"))
    if m.get("required_pass_rate") is not None:
        rp = m["required_pass_rate"]
        rows.append(("必做步骤通过率", f"{rp:.1f}% ({m.get('required_passed', 0)}/{m.get('required_total', 0)})"))
    if m.get("volatile_question_count"):
        rows.append(("波动题数", str(m["volatile_question_count"])))
    headline = sd.get("headline") or ""
    if headline:
        rows.append(("结论", headline))

    for i, (label, val) in enumerate(rows, start=2):
        zebra = styles.fill_zebra if i % 2 == 0 else styles.fill_white
        ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=4)
        c = ws.cell(row=i, column=1, value=label)
        c.font = styles.font_data_bold; c.alignment = styles.align_left; c.border = styles.border; c.fill = zebra
        c = ws.cell(row=i, column=2, value=val)
        c.font = styles.font_data; c.alignment = styles.align_left_top; c.border = styles.border; c.fill = zebra
        ws.cell(row=i, column=3).border = styles.border
        ws.cell(row=i, column=3).fill = zebra
        ws.cell(row=i, column=4).border = styles.border
        ws.cell(row=i, column=4).fill = zebra
        if label == "状态":
            status = sd.get("status", "na")
            fill_color, font_color = _status_fill_font(status)
            c.fill = styles.fill_for(fill_color)
            c.font = styles.font_for(font_color)
        ws.row_dimensions[i].height = 22

    current_row = len(rows) + 2

    # ── 区域2: 步骤通过率汇总（从 scenario_matrix 聚合） ──
    scenario_matrix = td.get("scenario_matrix") or {}
    sq_questions = scenario_matrix.get("questions") or []
    if sq_questions:
        step_stats: Dict[str, Dict[str, Any]] = {}
        for sq in sq_questions:
            cells = sq.get("cells") or {}
            for member_id, cell in cells.items():
                if not isinstance(cell, dict):
                    continue
                steps = cell.get("steps") or []
                for step in steps:
                    step_key = step.get("step_key") or step.get("step_id") or ""
                    step_name = step.get("name") or step_key
                    if not step_key:
                        continue
                    if step_key not in step_stats:
                        step_stats[step_key] = {
                            "name": step_name,
                            "required": bool(step.get("required", False)),
                            "passed": 0,
                            "total": 0,
                            "score_sum": 0.0,
                        }
                    step_stats[step_key]["total"] += 1
                    if step.get("passed"):
                        step_stats[step_key]["passed"] += 1
                    step_stats[step_key]["score_sum"] += float(step.get("score", 0) or 0)

        if step_stats:
            ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=4)
            ws.cell(row=current_row, column=1, value="步骤通过率汇总").font = styles.font_section
            ws.cell(row=current_row, column=1).alignment = styles.align_left
            ws.cell(row=current_row, column=1).fill = styles.fill_section_bg
            ws.cell(row=current_row, column=1).border = styles.border
            ws.row_dimensions[current_row].height = 24
            current_row += 1

            _write_header_row(ws, current_row, ["步骤", "类型", "通过率", "平均分"], styles)
            current_row += 1

            for step_key, stats in step_stats.items():
                total = stats["total"]
                passed = stats["passed"]
                pass_rate = (passed / total * 100) if total > 0 else 0
                avg_score = stats["score_sum"] / total if total > 0 else 0
                step_type = "必达" if stats["required"] else "可选"
                zebra = styles.fill_zebra if current_row % 2 == 0 else styles.fill_white
                values = [
                    stats["name"],
                    step_type,
                    f"{pass_rate:.1f}%",
                    f"{avg_score:.2f}",
                ]
                for col_idx, val in enumerate(values, start=1):
                    c = ws.cell(row=current_row, column=col_idx, value=val)
                    c.font = styles.font_data
                    c.alignment = styles.align_center if col_idx > 1 else styles.align_left
                    c.border = styles.border
                    c.fill = zebra
                ws.row_dimensions[current_row].height = 22
                current_row += 1
            current_row += 1  # 空行间隔

    # ── 区域3: 未通过步骤明细（复用 scenario_fails） ──
    scenario_fails = td.get("scenario_fails") or []
    if scenario_fails:
        ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=4)
        ws.cell(row=current_row, column=1, value="未通过步骤明细").font = styles.font_section
        ws.cell(row=current_row, column=1).alignment = styles.align_left
        ws.cell(row=current_row, column=1).fill = styles.fill_section_bg
        ws.cell(row=current_row, column=1).border = styles.border
        ws.row_dimensions[current_row].height = 24
        current_row += 1

        _write_header_row(ws, current_row, ["题目", "步骤", "得分", "原因"], styles)
        current_row += 1

        for sf in scenario_fails:
            zebra = styles.fill_zebra if current_row % 2 == 0 else styles.fill_white
            qid = sf.get("qid", "")
            step_name = sf.get("step_name") or sf.get("step_key", "")
            score = sf.get("score")
            reason = _truncate_text(sf.get("reason", ""), 200)
            values = [
                f"Q{qid}" if qid else "—",
                step_name,
                f"{score:.2f}" if score is not None else "—",
                reason,
            ]
            for col_idx, val in enumerate(values, start=1):
                c = ws.cell(row=current_row, column=col_idx, value=val)
                c.font = styles.font_data
                c.alignment = styles.align_center if col_idx == 3 else styles.align_left
                c.border = styles.border
                c.fill = zebra
            ws.row_dimensions[current_row].height = 22
            current_row += 1

    ws.sheet_view.showGridLines = False


def _build_stability_sheet(wb, td: Dict[str, Any], styles):
    """Sheet 8: 运行稳定性 — 维度结论 + 三个子维度详情。"""
    ws = wb.create_sheet("运行稳定性")
    _set_col_widths(ws, {"A": 22, "B": 14, "C": 18, "D": 18})

    verdicts = td["verdicts"]
    rs = verdicts.get("runtime_stability") or {}

    # 运行稳定性状态特殊处理：用 performance 子维度的 status 作为主状态
    perf = rs.get("performance") or {}
    perf_status = perf.get("status", "na")
    if perf_status in ("na", "disabled"):
        main_status = rs.get("status", "na")
    else:
        main_status = perf_status

    # ── 区域1: 维度结论卡片 ──
    ws.merge_cells("A1:D1")
    ws["A1"] = "运行稳定性维度结论"
    ws["A1"].font = styles.font_section
    ws["A1"].alignment = styles.align_left
    ws["A1"].fill = styles.fill_section_bg
    ws.row_dimensions[1].height = 26

    rows = [("状态", _status_label(main_status))]
    headline = rs.get("headline") or ""
    if headline:
        rows.append(("结论", headline))

    for i, (label, val) in enumerate(rows, start=2):
        zebra = styles.fill_zebra if i % 2 == 0 else styles.fill_white
        ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=4)
        c = ws.cell(row=i, column=1, value=label)
        c.font = styles.font_data_bold; c.alignment = styles.align_left; c.border = styles.border; c.fill = zebra
        c = ws.cell(row=i, column=2, value=val)
        c.font = styles.font_data; c.alignment = styles.align_left_top; c.border = styles.border; c.fill = zebra
        ws.cell(row=i, column=3).border = styles.border
        ws.cell(row=i, column=3).fill = zebra
        ws.cell(row=i, column=4).border = styles.border
        ws.cell(row=i, column=4).fill = zebra
        if label == "状态":
            fill_color, font_color = _status_fill_font(main_status)
            c.fill = styles.fill_for(fill_color)
            c.font = styles.font_for(font_color)
        ws.row_dimensions[i].height = 22

    current_row = len(rows) + 2

    # ── 辅助函数：写子维度区域 ──
    def _write_sub_dimension_section(title, status, details_rows):
        nonlocal current_row
        if status in ("na", "disabled"):
            return
        # 小标题
        ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=4)
        ws.cell(row=current_row, column=1, value=title).font = styles.font_section
        ws.cell(row=current_row, column=1).alignment = styles.align_left
        ws.cell(row=current_row, column=1).fill = styles.fill_section_bg
        ws.cell(row=current_row, column=1).border = styles.border
        ws.row_dimensions[current_row].height = 22
        current_row += 1
        # 状态行
        ws.cell(row=current_row, column=1, value="状态").font = styles.font_data_bold
        ws.cell(row=current_row, column=1).alignment = styles.align_left
        ws.cell(row=current_row, column=1).border = styles.border
        ws.cell(row=current_row, column=1).fill = styles.fill_zebra
        fill_color, font_color = _status_fill_font(status)
        c = ws.cell(row=current_row, column=2, value=_status_label(status))
        c.font = styles.font_for(font_color)
        c.fill = styles.fill_for(fill_color)
        c.alignment = styles.align_left
        c.border = styles.border
        ws.merge_cells(start_row=current_row, start_column=2, end_row=current_row, end_column=4)
        ws.cell(row=current_row, column=3).border = styles.border
        ws.cell(row=current_row, column=3).fill = styles.fill_zebra
        ws.cell(row=current_row, column=4).border = styles.border
        ws.cell(row=current_row, column=4).fill = styles.fill_zebra
        ws.row_dimensions[current_row].height = 22
        current_row += 1
        # 详情行
        for label, val in details_rows:
            zebra = styles.fill_zebra if current_row % 2 == 0 else styles.fill_white
            ws.merge_cells(start_row=current_row, start_column=2, end_row=current_row, end_column=4)
            ws.cell(row=current_row, column=1, value=label).font = styles.font_data_bold
            ws.cell(row=current_row, column=1).alignment = styles.align_left
            ws.cell(row=current_row, column=1).border = styles.border
            ws.cell(row=current_row, column=1).fill = zebra
            ws.cell(row=current_row, column=2, value=val).font = styles.font_data
            ws.cell(row=current_row, column=2).alignment = styles.align_left
            ws.cell(row=current_row, column=2).border = styles.border
            ws.cell(row=current_row, column=2).fill = zebra
            ws.cell(row=current_row, column=3).border = styles.border
            ws.cell(row=current_row, column=3).fill = zebra
            ws.cell(row=current_row, column=4).border = styles.border
            ws.cell(row=current_row, column=4).fill = zebra
            ws.row_dimensions[current_row].height = 22
            current_row += 1
        current_row += 1  # 空行间隔

    # ── 区域2: 性能稳定性 ──
    perf_metrics = perf.get("metrics") or {}
    perf_details = []
    if perf_metrics.get("latency_pass_rate") is not None:
        perf_details.append(("时延达标率", f"{perf_metrics['latency_pass_rate']}%"))
    if perf_metrics.get("tokens_pass_rate") is not None:
        perf_details.append(("Token达标率", f"{perf_metrics['tokens_pass_rate']}%"))
    if perf_metrics.get("avg_latency_ms") is not None:
        target = perf_metrics.get("target_latency_ms")
        val_str = f"{perf_metrics['avg_latency_ms']:.0f}ms"
        if target is not None:
            val_str += f" (目标 ≤{target:.0f}ms)"
        perf_details.append(("平均时延", val_str))
    if perf_metrics.get("avg_tokens") is not None:
        target = perf_metrics.get("target_tokens")
        val_str = f"{perf_metrics['avg_tokens']:.0f}"
        if target is not None:
            val_str += f" (目标 ≤{target:.0f})"
        perf_details.append(("平均Token", val_str))
    _write_sub_dimension_section("性能稳定性", perf.get("status", "na"), perf_details)

    # ── 区域3: 质量稳定性 ──
    qs_sub = rs.get("quality_stability") or {}
    qsm = qs_sub.get("metrics") or {}
    qs_details = []
    if qsm.get("pass_rate") is not None:
        qs_details.append(("多轮正确率", f"{qsm['pass_rate']:.1f}%"))
    if qsm.get("avg_std") is not None:
        qs_details.append(("平均标准差", f"{qsm['avg_std']:.4f}"))
    if qsm.get("max_std") is not None:
        qs_details.append(("最大标准差", f"{qsm['max_std']:.4f}"))
    if qsm.get("min_score") is not None:
        qs_details.append(("最低分", f"{qsm['min_score']:.2f}"))
    if qsm.get("round_count") is not None and qsm.get("round_count", 0) > 1:
        qs_details.append(("推理轮次", str(qsm["round_count"])))
    _write_sub_dimension_section("质量稳定性", qs_sub.get("status", "na"), qs_details)

    # ── 区域4: 结构稳定性 ──
    struct = rs.get("structural") or {}
    sm = struct.get("metrics") or {}
    struct_details = []
    if sm.get("multi_round_question_count") is not None:
        struct_details.append(("多轮题数", str(sm["multi_round_question_count"])))
    if sm.get("drift_question_count") is not None:
        total = sm.get("multi_round_question_count", 0)
        struct_details.append(("漂移题数", f"{sm['drift_question_count']}/{total}"))
    if sm.get("sequence_jaccard_min") is not None:
        struct_details.append(("最小序列相似度", f"{sm['sequence_jaccard_min']:.2%}"))
    _write_sub_dimension_section("结构稳定性", struct.get("status", "na"), struct_details)

    ws.sheet_view.showGridLines = False


def _build_findings_sheet(wb, td: Dict[str, Any], styles):
    """Sheet 9: 关键发现与建议。优先用 LLM 生成的 task.summary 渲染；为空时回退到模板逻辑。"""
    ws = wb.create_sheet("关键发现与建议")
    _set_col_widths(ws, {"A": 6, "B": 28, "C": 18, "D": 60})

    ws.merge_cells("A1:D1")
    ws["A1"] = "关键发现与改进建议"
    ws["A1"].font = styles.font_title
    ws["A1"].alignment = styles.align_left
    ws.row_dimensions[1].height = 32

    # 优先使用 LLM 总结
    sections = _parse_llm_summary_sections(td.get("summary", ""))

    if sections:
        row = 3
        for idx, (title, body) in enumerate(sections, start=1):
            # 标题行
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
            c = ws.cell(row=row, column=1, value=f"{idx}. {title}")
            c.font = styles.font_section
            c.alignment = styles.align_left
            c.fill = styles.fill_section_bg
            c.border = styles.border
            ws.row_dimensions[row].height = 26
            row += 1
            # 正文行
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
            c = ws.cell(row=row, column=1, value=body)
            c.font = styles.font_data
            c.alignment = styles.align_left_top
            c.border = styles.border
            ws.row_dimensions[row].height = _estimate_row_height(body)
            row += 2  # 空一行间隔
        foot_row = row
    else:
        # 回退到模板逻辑
        foot_row = _render_findings_sheet_template(ws, td, styles)

    # 脚注
    ws.merge_cells(start_row=foot_row, start_column=1, end_row=foot_row, end_column=4)
    c = ws.cell(row=foot_row, column=1,
               value=f"报告生成时间: {_format_dt(td.get('completed_at'))[:10]}  |  数据来源: Onto-Eval 本体评测平台  |  任务ID: {td['task_id']}")
    c.font = styles.font_footnote; c.alignment = styles.align_left
    ws.row_dimensions[foot_row].height = 18

    ws.sheet_view.showGridLines = False


def _render_findings_sheet_template(ws, td: Dict[str, Any], styles) -> int:
    """当 task.summary 为空时，回退到模板渲染 5 条关键发现 + 5 条改进建议。返回脚注所在行号。"""
    # 关键发现
    ws.merge_cells("A3:D3")
    ws["A3"] = "关键发现"
    ws["A3"].font = styles.font_section
    ws["A3"].alignment = styles.align_left
    ws["A3"].fill = styles.fill_section_bg
    ws.row_dimensions[3].height = 26

    qs = td["quality_score"]
    verdicts = td["verdicts"]
    questions = td["questions_summary"]
    failed_q = [q for q in questions if not q["passed"]]
    zero_q = [q for q in questions if q["score"] is not None and q["score"] <= 0]
    low_q = [q for q in questions if q["score"] is not None and q["score"] <= 0.5]
    near_q = [q for q in questions if q["score"] is not None and 0.7 <= q["score"] < 0.8]

    q_dim = verdicts.get("quality") or {}
    q_metrics = q_dim.get("metrics") or {}
    metric_pass_rates = q_metrics.get("metric_pass_rates") or {}
    # 计算综合通过率
    all_pass_rates = []
    for stats in metric_pass_rates.values():
        if isinstance(stats, dict):
            pr = stats.get("pass_rate")
            if pr is not None:
                all_pass_rates.append(pr)
    overall_pass_rate = sum(all_pass_rates) / len(all_pass_rates) if all_pass_rates else None
    total_score = qs.get("total_score")

    findings = [
        (f"0分题目{len(zero_q)}题" if zero_q else "无0分题目",
         f"{'Agent回答为空或完全错误，涉及推理逻辑缺陷' if zero_q else '所有题目均产出有效回答'}"),
        (f"低分题目（≤0.5分）{len(low_q)}题",
         f"{'这些题目需要深入排查Agent的推理链路和脚本调用问题' if low_q else '无低分题目'}"),
        (f"综合通过率{overall_pass_rate:.1f}%" if overall_pass_rate is not None else "综合通过率—",
         f"{len(questions) - len(failed_q)}/{len(questions)}题通过，{'已达标' if (overall_pass_rate or 0) >= 80 else '未达标'}" if questions else "—"),
        (f"接近及格题目（0.7-0.8分）{len(near_q)}题",
         f"{'通过针对性修复可较快达到通过线，是短期优化重点' if near_q else '无接近及格题目'}"),
        (f"质量总分{total_score:.2f}" if total_score is not None else "质量总分—",
         f"结果分{qs.get('result_score', 0):.2f}" if qs.get("result_score") else "—"),
    ]
    for i, (b, d) in enumerate(findings, start=4):
        c = ws.cell(row=i, column=1, value=i - 3)
        c.font = styles.font_data_bold; c.alignment = styles.align_center; c.border = styles.border
        c.fill = styles.fill_primary_soft
        ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=3)
        c = ws.cell(row=i, column=2, value=b)
        c.font = styles.font_data_bold; c.alignment = styles.align_left; c.border = styles.border; c.fill = styles.fill_primary_soft
        ws.cell(row=i, column=3).border = styles.border; ws.cell(row=i, column=3).fill = styles.fill_primary_soft
        c = ws.cell(row=i, column=4, value=d)
        c.font = styles.font_data; c.alignment = styles.align_left_top; c.border = styles.border
        ws.row_dimensions[i].height = 40

    # 改进建议
    sug_start = len(findings) + 6
    ws.merge_cells(start_row=sug_start, start_column=1, end_row=sug_start, end_column=4)
    ws.cell(row=sug_start, column=1, value="改进建议").font = styles.font_section
    ws.cell(row=sug_start, column=1).alignment = styles.align_left
    ws.cell(row=sug_start, column=1).fill = styles.fill_section_bg
    ws.row_dimensions[sug_start].height = 26

    suggestions = [
        ("修复0分题目", f"{len(zero_q)}道0分题目需排查脚本调用失败和推理逻辑错误" if zero_q else "无0分题目需修复"),
        ("提升接近及格题目", f"{len(near_q)}道0.7-0.8分题目通过细节优化即可通过，投入产出比最高" if near_q else "无接近及格题目"),
        ("深入分析低分根因", f"{len(low_q)}道低分题目需逐题分析Agent回答与标准答案的差异" if low_q else "无低分题目"),
        ("关注过程一致性波动", "需排查多轮推理路径稳定性" if verdicts.get("process_consistency", {}).get("status") == "warn" else "过程一致性达标"),
        ("优化推理延迟", "延迟通过率偏低，需排查慢请求和重试逻辑" if verdicts.get("runtime_stability", {}).get("performance", {}).get("status") == "elevated" else "延迟达标"),
    ]
    for i, (b, d) in enumerate(suggestions, start=sug_start + 1):
        c = ws.cell(row=i, column=1, value=i - sug_start)
        c.font = styles.font_data_bold; c.alignment = styles.align_center; c.border = styles.border
        c.fill = styles.fill_primary_soft
        ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=3)
        c = ws.cell(row=i, column=2, value=b)
        c.font = styles.font_data_bold; c.alignment = styles.align_left; c.border = styles.border; c.fill = styles.fill_primary_soft
        ws.cell(row=i, column=3).border = styles.border; ws.cell(row=i, column=3).fill = styles.fill_primary_soft
        c = ws.cell(row=i, column=4, value=d)
        c.font = styles.font_data; c.alignment = styles.align_left_top; c.border = styles.border
        ws.row_dimensions[i].height = 40

    return sug_start + len(suggestions) + 2


# ═══════════════════════════════════════════════════════════
# 主构建函数
# ═══════════════════════════════════════════════════════════

def build_xlsx(tasks_data: List[Dict[str, Any]]) -> bytes:
    """根据收集的任务数据生成多 sheet xlsx 二进制。"""
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)  # 删除默认 sheet

    styles = _init_styles()

    # 单任务：生成完整 8 sheet
    # 多任务：第一个任务生成完整 sheet，后续任务追加到对应 sheet（简化处理）
    for td in tasks_data:
        _normalize_task_data(td)  # 安全兜底：确保所有数值字段为 float
        _build_overview_sheet(wb, td, styles)
        _build_failed_questions_sheet(wb, td, styles)
        _build_question_details_sheet(wb, td, styles)
        _build_quality_sheet(wb, td, styles)
        _build_process_consistency_sheet(wb, td, styles)
        _build_scenario_steps_sheet(wb, td, styles)
        _build_stability_sheet(wb, td, styles)
        _build_findings_sheet(wb, td, styles)
        break  # 多任务只处理第一个，避免 sheet 名冲突

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# 公开别名：供 AI 助手工具直接调用，避免下划线私有跨模块访问
collect_task_data = _collect_task_data


async def build_export_report(
    session: AsyncSession, task_ids: List[str]
) -> Optional[bytes]:
    """生成多任务评测报告 xlsx。"""
    if not task_ids:
        return None

    tasks_data: List[Dict[str, Any]] = []
    for tid in task_ids:
        td = await _collect_task_data(session, tid)
        if td:
            tasks_data.append(td)

    if not tasks_data:
        return None

    return build_xlsx(tasks_data)
