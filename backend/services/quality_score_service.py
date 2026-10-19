"""任务质量分聚合：结果分 + 过程分 + 总分（v1 简化规则）。"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import EvaluationResult
from services.dimension_verdict_constants import (
    CORRECTNESS_PASS_RATE_GATE,
    METRIC_DISPLAY_NAMES,
    QUALITY_METRIC_CAP,
    QUALITY_METRIC_PASS_RATE_GATE,
    QUALITY_STD_GATE,
    SAFETY_CAP,
    SAFETY_PASS_RATE_GATE,
    STABILITY_STD_ZERO,
)
from services.process_consistency_service import (
    build_process_consistency,
    compute_consistency_score_metrics,
    empty_consistency_score_metrics,
    score_metrics_from_consistency_payload,
)

RESULT_WEIGHT = 0.6
PROCESS_WEIGHT = 0.2
STABILITY_WEIGHT = 0.2

# 稳定性分子分权重
QUALITY_STABILITY_WEIGHT = 0.5
PERF_STABILITY_WEIGHT = 0.5
# 质量稳定性子分内部
QS_PASS_RATE_W = 0.6
QS_STD_W = 0.4
# 性能稳定性子分内部
PS_LATENCY_W = 0.5
PS_TOKENS_W = 0.5

RUNTIME_METRICS = {"inference_latency", "tokens_total"}
# 安全合规指标：不参与 result_score 加权，作为独立门禁
# 与 evaluators/deepeval_metrics/deepeval_suite.py category=="safety" 同步
SAFETY_METRICS = {
    "bias", "toxicity", "pii_leakage", "role_violation", "non_advice", "misuse",
}
METRIC_WEIGHTS = {
    "correctness": 0.7,
    "faithfulness": 0.2,
    "answer_relevancy": 0.1,
}
# 未在 METRIC_WEIGHTS 中配置的质量指标（DeepEval / 自定义）的默认权重
DEFAULT_UNKNOWN_METRIC_WEIGHT = 0.1

REQUIRED_CHECKPOINT_PASS_GATE = 0.8


def _round_score(value: Optional[float]) -> Optional[float]:
    return round(float(value), 4) if value is not None else None


def _avg(values: Iterable[float]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _is_quality_metric(row: EvaluationResult) -> bool:
    name = (row.metric_name or "").strip()
    if not name or name in RUNTIME_METRICS:
        return False
    if (getattr(row, "eval_method", None) or "").strip() == "runtime":
        return False
    return True


async def compute_task_quality_score(
    session: AsyncSession,
    task_id: str,
    *,
    result_weight: float = RESULT_WEIGHT,
    process_weight: float = PROCESS_WEIGHT,
    stability_weight: float = STABILITY_WEIGHT,
    results: Optional[List[EvaluationResult]] = None,
) -> Dict[str, Any]:
    """计算任务级质量分。

    v2 规则：
    - Result Score：质量类 EvaluationResult 按指标权重聚合。
    - Process Score：过程一致性（必达过程点 + 多轮链条）按题聚合（仅在线）。
    - Stability Score：多轮正确性稳定性 + 链条稳定性（仅多轮且 enable_stability 时生效）。
    - Total Score：默认 0.6*Result + 0.2*Process + 0.2*Stability；
      单轮/维度未启用/离线无过程分时，对应权重并入结果分。
    - Gate：结果分 < 0.5 封顶 0.55；必达过程点通过率 < 80% 封顶 0.75；
      多轮通过率 < 60% 封顶 0.50；质量波动 std > 0.15 封顶 0.70。
    """
    from database import EvaluationTask
    from services.evaluation_mode import is_offline_task

    task = await session.get(EvaluationTask, task_id)
    offline = task is not None and is_offline_task(task)
    enable_pc = bool(getattr(task, "enable_process_consistency", True)) if task else True
    enable_stab = bool(getattr(task, "enable_stability", True)) if task else True
    include_process = enable_pc and (
        (not offline) or bool(getattr(task, "process_eval_completed", False))
    )

    result_score, metric_breakdown, metric_pass_rates = await _compute_result_score(
        session, task_id, results=results
    )
    if not include_process:
        process = empty_consistency_score_metrics()
    else:
        process = await compute_consistency_score_metrics(session, task_id)

    correctness_stability = await _compute_quality_stability(session, task_id, results=results)
    runtime_pass_rates = await _compute_runtime_pass_rates(session, task_id)

    return _build_quality_score_payload(
        result_score,
        metric_breakdown,
        process,
        correctness_stability=correctness_stability,
        runtime_pass_rates=runtime_pass_rates,
        result_weight=result_weight,
        process_weight=process_weight,
        stability_weight=stability_weight,
        enable_stability=enable_stab,
        offline=offline,
        include_process=include_process,
        metric_pass_rates=metric_pass_rates,
    )


async def compute_task_quality_scores(
    session: AsyncSession,
    task_ids: List[str],
    *,
    result_weight: float = RESULT_WEIGHT,
    process_weight: float = PROCESS_WEIGHT,
    stability_weight: float = STABILITY_WEIGHT,
) -> Dict[str, Dict[str, Any]]:
    """批量计算任务级质量分，供历史列表等高频页面使用。"""
    from database import EvaluationTask
    from services.evaluation_mode import is_offline_task

    ids = [str(tid) for tid in task_ids if tid]
    if not ids:
        return {}

    tasks = (
        await session.execute(select(EvaluationTask).where(EvaluationTask.id.in_(ids)))
    ).scalars().all()
    task_by_id = {t.id: t for t in tasks}
    offline_ids = {t.id for t in tasks if is_offline_task(t)}

    result_scores = await _compute_result_scores(session, ids)
    process_scores = await _compute_consistency_scores(session, ids)

    out: Dict[str, Dict[str, Any]] = {}
    for task_id in ids:
        offline = task_id in offline_ids
        offline_with_process = bool(
            getattr(task_by_id.get(task_id), "process_eval_completed", False)
        )
        enable_pc = bool(
            getattr(task_by_id.get(task_id), "enable_process_consistency", True)
        )
        enable_stab = bool(
            getattr(task_by_id.get(task_id), "enable_stability", True)
        )
        cs = await _compute_quality_stability(session, task_id)
        rt_rates = await _compute_runtime_pass_rates(session, task_id)
        out[task_id] = _build_quality_score_payload(
            result_scores.get(task_id, (None, {}, None))[0],
            result_scores.get(task_id, (None, {}, None))[1],
            process_scores.get(task_id, empty_consistency_score_metrics()),
            correctness_stability=cs,
            runtime_pass_rates=rt_rates,
            result_weight=result_weight,
            process_weight=process_weight,
            stability_weight=stability_weight,
            enable_stability=enable_stab,
            offline=offline,
            include_process=enable_pc and (offline_with_process or not offline),
            metric_pass_rates=result_scores.get(task_id, (None, {}, None))[2],
        )
    return out



def _compute_stability_score_v2(
    correctness_stability: Optional[Dict[str, Any]],
    runtime_pass_rates: Optional[Dict[str, float]],
    enable_stability: bool,
) -> Dict[str, Any]:
    """稳定性分 = 质量稳定性 × 0.5 + 性能稳定性 × 0.5。

    质量稳定性 = pass_rate × 0.6 + std_component × 0.4
      std_component = clamp(1 - avg_std / STABILITY_STD_ZERO, 0, 1)
    性能稳定性 = 时延达标率 × 0.5 + token达标率 × 0.5

    退化：
    - enable_stability=False → None（0.2 权重并入结果分）
    - 单轮（round_count<=1）→ 质量子分 None，权重并入性能子分
    - 无运行时指标 → 性能子分 None，权重并入质量子分
    - 二者都无 → None
    各分量缺值时按可用分量归一化。
    """
    if not enable_stability:
        return {
            "score": None,
            "is_valid": False,
            "breakdown": {"quality_stability": None, "performance_stability": None},
        }

    cs = correctness_stability or {}
    round_count = int(cs.get("round_count", 0) or 0)

    # 单轮推理：stability 维度 disabled（权重并入 result）
    # 性能达标率仍在 dimension_verdicts.runtime_stability.performance 展示，只是不计入 total
    if round_count <= 1:
        return {
            "score": None,
            "is_valid": False,
            "breakdown": {"quality_stability": None, "performance_stability": None},
        }

    # 质量子分：仅多轮时生效
    quality_stability: Optional[float] = None
    if round_count > 1:
        pass_rate = cs.get("pass_rate")
        avg_std = cs.get("avg_std")
        std_component = None
        if avg_std is not None:
            std_component = max(0.0, min(1.0, 1.0 - float(avg_std) / STABILITY_STD_ZERO))
        components, weights = [], []
        if pass_rate is not None:
            components.append(float(pass_rate)); weights.append(QS_PASS_RATE_W)
        if std_component is not None:
            components.append(std_component); weights.append(QS_STD_W)
        if components:
            total_w = sum(weights)
            quality_stability = sum(c * w for c, w in zip(components, weights)) / total_w

    # 性能子分：有运行时达标率时生效
    performance_stability: Optional[float] = None
    if runtime_pass_rates:
        lat_rate = runtime_pass_rates.get("latency")
        tok_rate = runtime_pass_rates.get("tokens")
        components, weights = [], []
        if lat_rate is not None:
            components.append(float(lat_rate)); weights.append(PS_LATENCY_W)
        if tok_rate is not None:
            components.append(float(tok_rate)); weights.append(PS_TOKENS_W)
        if components:
            total_w = sum(weights)
            performance_stability = sum(c * w for c, w in zip(components, weights)) / total_w

    # 合成：按有效子分归一化权重
    parts, part_weights = [], []
    if quality_stability is not None:
        parts.append(quality_stability); part_weights.append(QUALITY_STABILITY_WEIGHT)
    if performance_stability is not None:
        parts.append(performance_stability); part_weights.append(PERF_STABILITY_WEIGHT)

    if not parts:
        score = None
        is_valid = False
    else:
        total_w = sum(part_weights)
        score = sum(p * w for p, w in zip(parts, part_weights)) / total_w
        is_valid = True

    return {
        "score": _round_score(score) if score is not None else None,
        "is_valid": is_valid,
        "breakdown": {
            "quality_stability": _round_score(quality_stability) if quality_stability is not None else None,
            "performance_stability": _round_score(performance_stability) if performance_stability is not None else None,
        },
    }


async def _compute_runtime_pass_rates(
    session: AsyncSession, task_id: str
) -> Optional[Dict[str, float]]:
    """从 EvaluationResult 聚合运行时指标达标率。

    复用 EvaluationResult.passed（运行时指标写入时已按 runtime_metric_targets 阈值判定）。
    返回 {"latency": float, "tokens": float}（达标率 0-1）。
    离线任务或无运行时指标 → None。
    """
    from database import EvaluationTask
    from services.evaluation_mode import is_offline_task

    task = await session.get(EvaluationTask, task_id)
    if task is None or is_offline_task(task):
        return None

    rows = (
        await session.execute(
            select(EvaluationResult).where(
                EvaluationResult.task_id == task_id,
                EvaluationResult.metric_name.in_(RUNTIME_METRICS),
            )
        )
    ).scalars().all()
    if not rows:
        return None

    lat_passed = sum(1 for r in rows if r.metric_name == "inference_latency" and r.passed)
    lat_total = sum(1 for r in rows if r.metric_name == "inference_latency")
    tok_passed = sum(1 for r in rows if r.metric_name == "tokens_total" and r.passed)
    tok_total = sum(1 for r in rows if r.metric_name == "tokens_total")

    rates: Dict[str, float] = {}
    if lat_total > 0:
        rates["latency"] = lat_passed / lat_total
    if tok_total > 0:
        rates["tokens"] = tok_passed / tok_total
    return rates if rates else None


def _build_quality_score_payload(
    result_score: Optional[float],
    metric_breakdown: Dict[str, float],
    process: Dict[str, Any],
    *,
    correctness_stability: Optional[Dict[str, Any]] = None,
    runtime_pass_rates: Optional[Dict[str, float]] = None,
    result_weight: float,
    process_weight: float,
    stability_weight: float,
    enable_stability: bool = True,
    offline: bool = False,
    include_process: bool = True,
    metric_pass_rates: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    process_score = process["process_score"] if include_process else None

    # 稳定性分计算（v2：质量+性能双子分）
    stability_metrics = _compute_stability_score_v2(
        correctness_stability, runtime_pass_rates, enable_stability
    )
    stability_score = stability_metrics["score"]
    stability_valid = stability_metrics["is_valid"]

    # 有效权重：未生效的维度权重并入结果分
    eff_process = process_weight if (include_process and process_score is not None) else 0.0
    eff_stability = stability_weight if (stability_valid and stability_score is not None) else 0.0
    eff_result = result_weight + (process_weight - eff_process) + (stability_weight - eff_stability)

    # safety_score：安全指标 pass_rate 平均（与 safety gate 同源）。
    # 不进 total 加权（安全保持"独立门禁"定位），仅作展示 + gate 锚点。
    safety_score: Optional[float] = None
    if metric_pass_rates:
        safety_rates = [r for n, r in metric_pass_rates.items() if n in SAFETY_METRICS]
        if safety_rates:
            safety_score = sum(safety_rates) / len(safety_rates)

    gates: List[Dict[str, Any]] = []
    if result_score is not None and result_score < 0.5:
        gates.append(
            {
                "type": "low_result_score",
                "applied": True,
                "cap": 0.55,
                "reason": "结果分低于 0.5，总分封顶 0.55",
            }
        )
    if (
        include_process
        and process["required_total"] > 0
        and process["required_pass_rate"] is not None
        and process["required_pass_rate"] < REQUIRED_CHECKPOINT_PASS_GATE
    ):
        gates.append(
            {
                "type": "required_checkpoint_pass_rate_low",
                "applied": True,
                "cap": 0.75,
                "reason": "必达过程点通过率低于 80%，总分封顶 0.75",
            }
        )

    # 过程一致性 fail gate（red>0 即触发，与 required_checkpoint_pass_rate_low 互补）
    # 只在配置了必达过程点且有失败时才触发（required_total>0 且 red>0）
    if include_process and process.get("red", 0) > 0 and process.get("required_total", 0) > 0:
        gates.append(
            {
                "type": "process_consistency_fail",
                "applied": True,
                "cap": 0.75,
                "reason": f"过程一致性 {process['red']} 题未达标，总分封顶 0.75",
            }
        )

    # 多轮质量波动 Gate（仅多轮推理 round_count>1 时触发）
    # 注：correctness pass_rate<60% 已由 quality_metric_pass_rate_low 统一处理（cap 0.50）
    cs = correctness_stability or {}
    if cs.get("round_count", 0) > 1:
        avg_std = cs.get("avg_std")
        if avg_std is not None and avg_std > QUALITY_STD_GATE:
            gates.append(
                {
                    "type": "quality_variance_high",
                    "applied": True,
                    "cap": 0.70,
                    "reason": f"质量波动 std {avg_std:.2f} > {QUALITY_STD_GATE}，总分封顶 0.70",
                }
            )

    # 指标 pass_rate 门禁（通用，单轮也触发，与 verdict fail 判定对齐）
    if metric_pass_rates:
        # 安全指标门禁（比质量指标更严厉）
        safety_rates = {n: r for n, r in metric_pass_rates.items() if n in SAFETY_METRICS}
        failed_safety = [
            METRIC_DISPLAY_NAMES.get(n, n) for n, r in safety_rates.items()
            if r < SAFETY_PASS_RATE_GATE
        ]
        if failed_safety:
            gates.append(
                {
                    "type": "safety_pass_rate_low",
                    "applied": True,
                    "cap": SAFETY_CAP,
                    "reason": f"安全指标 {'、'.join(failed_safety)} 通过率低于 {SAFETY_PASS_RATE_GATE * 100:.0f}%，总分封顶 {SAFETY_CAP}",
                }
            )
        # 质量指标 pass_rate 门禁（补 CORRECTNESS_PASS_RATE_GATE 单轮空白）
        quality_rates = {n: r for n, r in metric_pass_rates.items() if n not in SAFETY_METRICS}
        failed_quality = [
            METRIC_DISPLAY_NAMES.get(n, n) for n, r in quality_rates.items()
            if r < QUALITY_METRIC_PASS_RATE_GATE
        ]
        if failed_quality:
            gates.append(
                {
                    "type": "quality_metric_pass_rate_low",
                    "applied": True,
                    "cap": QUALITY_METRIC_CAP,
                    "reason": f"质量指标 {'、'.join(failed_quality)} 通过率低于 {QUALITY_METRIC_PASS_RATE_GATE * 100:.0f}%，总分封顶 {QUALITY_METRIC_CAP}",
                }
            )

    # 类别分封顶（设计范式）：归属某类别的指标触发的 gate，先封该类别的类别分，再传导到 total。
    # 避免"类别分高 / 总分低"撕裂——gate 由该类别指标触发，应先反映到类别分。
    # 映射：
    #   quality_metric_pass_rate_low / low_result_score → result_score
    #   process_consistency_fail / required_checkpoint_pass_rate_low → process_score
    #   quality_variance_high → stability_score
    #   safety_pass_rate_low → safety_score（不进 total，仅显示层封顶）
    # low_result_score 触发条件本就是 result_score<0.5，cap=0.55 恒大于 result_score，
    # 封顶数值无效果但语义对齐。
    if result_score is not None:
        for gate in gates:
            if gate["type"] in ("quality_metric_pass_rate_low", "low_result_score"):
                result_score = min(result_score, float(gate["cap"]))
    if process_score is not None:
        for gate in gates:
            if gate["type"] in ("process_consistency_fail", "required_checkpoint_pass_rate_low"):
                process_score = min(process_score, float(gate["cap"]))
    if stability_score is not None:
        for gate in gates:
            if gate["type"] == "quality_variance_high":
                stability_score = min(stability_score, float(gate["cap"]))
    if safety_score is not None:
        for gate in gates:
            if gate["type"] == "safety_pass_rate_low":
                safety_score = min(safety_score, float(gate["cap"]))

    # 原始总分（加权求和，仅累加非 None 分量；类别分已按 gate 封顶）
    raw_total: Optional[float] = None
    if result_score is not None or (process_score is not None and eff_process > 0) or (stability_score is not None and eff_stability > 0):
        raw_total = 0.0
        if result_score is not None:
            raw_total += eff_result * result_score
        if process_score is not None and eff_process > 0:
            raw_total += eff_process * process_score
        if stability_score is not None and eff_stability > 0:
            raw_total += eff_stability * stability_score

    total_score = raw_total
    if total_score is not None:
        for gate in gates:
            total_score = min(total_score, float(gate["cap"]))

    return {
        "total_score": _round_score(total_score),
        "raw_total_score": _round_score(raw_total),
        "result_score": _round_score(result_score),
        "process_score": _round_score(process_score) if include_process else None,
        "stability_score": _round_score(stability_score),
        "safety_score": _round_score(safety_score),
        "result_weight": round(eff_result, 4),
        "process_weight": round(eff_process, 4),
        "stability_weight": round(eff_stability, 4),
        "evaluation_mode": "offline" if offline else "online",
        "required_pass_rate": _round_score(process["required_pass_rate"]) if include_process else None,
        "required_passed": process["required_passed"] if include_process else None,
        "required_total": process["required_total"] if include_process else None,
        "process_run_count": process["process_run_count"] if include_process else 0,
        "metric_breakdown": metric_breakdown,
        "metric_weights": {
            name: (0.0 if name in SAFETY_METRICS else METRIC_WEIGHTS.get(name, DEFAULT_UNKNOWN_METRIC_WEIGHT))
            for name in metric_breakdown
        },
        "correctness_stability": correctness_stability,
        "stability_breakdown": stability_metrics["breakdown"],
        "gates": gates,
    }


async def _compute_consistency_scores(
    session: AsyncSession,
    task_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    computed: Dict[str, Dict[str, Any]] = {}
    for task_id in task_ids:
        payload = await build_process_consistency(session, task_id)
        computed[task_id] = score_metrics_from_consistency_payload(payload)
    return computed


def _collect_by_metric(
    rows: Iterable[EvaluationResult],
) -> Tuple[Dict[str, List[float]], Dict[str, List[int]]]:
    """遍历 rows，返回 (by_metric_scores, pass_counts)。

    by_metric_scores: {metric_name: [scores]} 含所有非 runtime 指标（含安全）
    pass_counts: {metric_name: [passed, total]} 同上
    """
    by_metric: Dict[str, List[float]] = defaultdict(list)
    pass_counts: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        if not _is_quality_metric(row):
            continue
        name = (row.metric_name or "").strip()
        val = row.avg_score if row.avg_score is not None else row.score
        if val is None:
            continue
        by_metric[name].append(float(val))
        pass_counts[name][1] += 1
        if row.passed:
            pass_counts[name][0] += 1
    return by_metric, pass_counts


def _aggregate_result_metric_scores(
    by_metric: Dict[str, List[float]],
    pass_counts: Dict[str, List[int]],
) -> Tuple[Optional[float], Dict[str, float], Optional[Dict[str, float]]]:
    metric_scores = {name: _avg(vals) for name, vals in by_metric.items()}
    metric_scores = {k: v for k, v in metric_scores.items() if v is not None}
    if not metric_scores:
        return None, {}, None

    # result_score 只用质量指标加权（排除安全指标——安全指标作为门禁，不参与加权）
    quality_scores = {k: v for k, v in metric_scores.items() if k not in SAFETY_METRICS}
    if not quality_scores:
        return None, {k: round(float(v), 4) for k, v in metric_scores.items()}, None
    weights = {
        name: METRIC_WEIGHTS.get(name, DEFAULT_UNKNOWN_METRIC_WEIGHT)
        for name in quality_scores
    }
    total_weight = sum(weights.values())
    score = sum(quality_scores[name] * weights[name] for name in quality_scores) / total_weight

    # pass_rate：所有非 runtime 指标（含安全，供 gate 用），0-1 小数
    pass_rates: Optional[Dict[str, float]] = None
    if pass_counts:
        rates = {name: p / t for name, (p, t) in pass_counts.items() if t > 0}
        if rates:
            pass_rates = {k: round(v, 4) for k, v in rates.items()}

    return score, {k: round(float(v), 4) for k, v in metric_scores.items()}, pass_rates


async def _compute_quality_stability(
    session: AsyncSession,
    task_id: str,
    *,
    results: Optional[List[EvaluationResult]] = None,
) -> Dict[str, Any]:
    """计算所有 LLM 质量指标的多轮稳定性指标（pass_rate / std / min）。

    多轮推理下所有质量指标（correctness / faithfulness / answer_relevancy）
    对每轮答案都 Judge，每轮一条 EvaluationResult（按 agent_run_id 区分轮次）。
    单轮时 round_count=1，指标退化为单值；仅多轮（round_count>1）时 Gate 生效。
    """
    stability_metrics = [m for m in METRIC_WEIGHTS if m not in RUNTIME_METRICS]
    if results is None:
        rows = (
            await session.execute(
                select(EvaluationResult).where(
                    EvaluationResult.task_id == task_id,
                    EvaluationResult.metric_name.in_(stability_metrics),
                )
            )
        ).scalars().all()
    else:
        # 复用调用方已查的 results，在内存过滤 metric_name
        metric_set = set(stability_metrics)
        rows = [r for r in results if (r.metric_name or "") in metric_set]

    empty: Dict[str, Any] = {
        "pass_rate": None,
        "avg_std": None,
        "max_std": None,
        "min_score": None,
        "round_count": 0,
        "question_count": 0,
        "judge_total": 0,
        "pass_total": 0,
        "per_question": [],
    }
    if not rows:
        return empty

    # 按 (question_id, metric_name) 分组，正确区分轮次和指标
    # （同一题不同指标不是多轮，同一题同指标的多次记录才是多轮）
    by_qm: Dict[Tuple[int, str], List[Tuple[float, bool]]] = defaultdict(list)
    pass_total = 0
    judge_total = 0
    for r in rows:
        val = r.avg_score if r.avg_score is not None else r.score
        if val is None:
            continue
        key = (r.question_id, r.metric_name or "")
        by_qm[key].append((float(val), bool(r.passed)))
        judge_total += 1
        if r.passed:
            pass_total += 1

    stds: List[float] = []
    mins: List[float] = []
    per_question: List[Dict[str, Any]] = []
    for (qid, metric), items in by_qm.items():
        if not items:
            continue
        scores = [s for s, _ in items]
        pass_count = sum(1 for _, p in items if p)
        std_q = statistics.pstdev(scores) if len(scores) > 1 else 0.0
        stds.append(std_q)
        mins.append(min(scores))
        per_question.append(
            {
                "question_id": qid,
                "metric_name": metric,
                "round_count": len(scores),
                "mean": round(sum(scores) / len(scores), 4),
                "std": round(std_q, 4),
                "min": round(min(scores), 4),
                "max": round(max(scores), 4),
                "pass_count": pass_count,
                "pass_rate": round(pass_count / len(scores), 4) if scores else None,
            }
        )

    round_count = max((len(v) for v in by_qm.values()), default=0)
    return {
        "pass_rate": (pass_total / judge_total) if judge_total else None,
        "avg_std": round(sum(stds) / len(stds), 4) if stds else None,
        "max_std": round(max(stds), 4) if stds else None,
        "min_score": round(min(mins), 4) if mins else None,
        "round_count": round_count,
        "question_count": len({qid for qid, _ in by_qm}),
        "judge_total": judge_total,
        "pass_total": pass_total,
        "per_question": per_question,
    }


async def _compute_result_scores(
    session: AsyncSession,
    task_ids: List[str],
) -> Dict[str, Tuple[Optional[float], Dict[str, float], Optional[Dict[str, float]]]]:
    rows = (
        await session.execute(
            select(EvaluationResult).where(EvaluationResult.task_id.in_(task_ids))
        )
    ).scalars().all()

    by_task_rows: Dict[str, List[EvaluationResult]] = defaultdict(list)
    for row in rows:
        if not _is_quality_metric(row):
            continue
        if row.task_id:
            by_task_rows[str(row.task_id)].append(row)

    return {
        task_id: _aggregate_result_metric_scores(*_collect_by_metric(task_rows))
        for task_id, task_rows in by_task_rows.items()
    }


async def _compute_result_score(
    session: AsyncSession,
    task_id: str,
    *,
    results: Optional[List[EvaluationResult]] = None,
) -> Tuple[Optional[float], Dict[str, float], Optional[Dict[str, float]]]:
    if results is None:
        results = (
            await session.execute(
                select(EvaluationResult).where(EvaluationResult.task_id == task_id)
            )
        ).scalars().all()
    by_metric, pass_counts = _collect_by_metric(results)
    return _aggregate_result_metric_scores(by_metric, pass_counts)
