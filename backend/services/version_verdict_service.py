"""版本综合裁决服务：从场景对比矩阵 payload 计算综合排名、维度裁决与注意事项。

被 ``cross_task_scenario_service.build_cross_task_scenario_matrix`` 调用，
职责有二：
1. 计算综合裁决（排名/维度裁决/caveats/推荐文本）写入 ``payload["overall_verdict"]``
2. 从 ``stability_summaries`` 提取多轮稳定性信息，注入 payload 顶层字段
   ``inference_rounds`` / ``multi_round_mode`` / ``chain_stability``，
   供前端 ``ScenarioMatrixView`` 多轮稳定性横幅与不稳定高亮使用。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from services.dimension_verdict_constants import SCENARIO_SCORE_STD_VOLATILE

# ── 维度权重（合计 1.0）─────────────────────────────────────────
_DIMENSION_WEIGHTS: Dict[str, float] = {
    "quality": 0.35,
    "scenario": 0.25,
    "process": 0.15,
    "stability": 0.15,
    "cost": 0.10,
}

_DIMENSION_DISPLAY: Dict[str, str] = {
    "quality": "质量正确性",
    "scenario": "场景交付",
    "process": "过程合规",
    "stability": "多轮稳定性",
    "cost": "成本效率",
}

# 各维度差异显著性阈值（超出则标记 better/worse，否则 tie）
_TIE_THRESHOLDS: Dict[str, float] = {
    "quality": 0.02,
    "scenario": 0.02,
    "process": 0.05,
    "stability": 0.05,
    "cost": 0.10,
}

_LOWER_IS_BETTER = {"cost"}

_DIMENSION_ORDER = ["quality", "scenario", "process", "stability", "cost"]


# ── 工具 ────────────────────────────────────────────────────────


def _round(v: Optional[float], digits: int = 4) -> Optional[float]:
    return round(v, digits) if v is not None else None


def _parse_pass_rate(pr: str) -> float:
    """解析 ``"3/10"`` 格式的 pass_rate，返回 0-1 浮点。"""
    if not pr or "/" not in pr:
        try:
            return float(pr)
        except (ValueError, TypeError):
            return 1.0
    parts = pr.split("/")
    try:
        return float(parts[0]) / float(parts[-1])
    except (ValueError, ZeroDivisionError):
        return 1.0


# ── 数据提取 ────────────────────────────────────────────────────


def _extract_member_values(
    payload: Dict[str, Any],
) -> Tuple[Dict[str, Dict[str, Optional[float]]], str, List[Dict[str, Any]]]:
    """从 payload 各摘要中提取每成员每维度的原始值。"""
    members: List[Dict[str, Any]] = payload.get("members") or []
    member_ids = [m["member_id"] for m in members]
    baseline_id = payload.get("baseline_member_id") or (
        member_ids[0] if member_ids else ""
    )

    outcome_rows = (payload.get("outcome_summaries") or {}).get("rows") or []
    perf_rows = (payload.get("performance_summaries") or {}).get("rows") or []
    stability_rows = (payload.get("stability_summaries") or {}).get("rows") or []

    outcome_by = {r["member_id"]: r for r in outcome_rows}
    perf_by = {r["member_id"]: r for r in perf_rows}
    stab_by = {r["member_id"]: r for r in stability_rows}

    # 场景综合分：从 questions cells 取各成员 avg composite_score
    scenario_scores: Dict[str, List[float]] = {mid: [] for mid in member_ids}
    for q in payload.get("questions") or []:
        cells = q.get("cells") or {}
        for mid in member_ids:
            cell = cells.get(mid)
            if cell and cell.get("composite_score") is not None:
                scenario_scores[mid].append(float(cell["composite_score"]))

    values: Dict[str, Dict[str, Optional[float]]] = {}
    for mid in member_ids:
        o = outcome_by.get(mid, {})
        p = perf_by.get(mid, {})
        s = stab_by.get(mid, {})

        scen_avg = None
        if scenario_scores.get(mid):
            scen_avg = sum(scenario_scores[mid]) / len(scenario_scores[mid])

        values[mid] = {
            "quality": o.get("avg_correctness"),
            "scenario": scen_avg,
            "process": p.get("checkpoint_compliance_rate"),
            "stability": s.get("chain_pass_rate"),
            "cost": o.get("avg_tokens_total"),
        }

    return values, baseline_id, members


# ── 归一化 ──────────────────────────────────────────────────────


def _normalize_dimension(
    dim: str,
    member_values: Dict[str, Optional[float]],
) -> Dict[str, Optional[float]]:
    """min-max 归一化到 0-1（最低=0，最高=1）。"""
    raw = {mid: v for mid, v in member_values.items() if v is not None}
    if not raw:
        return {mid: None for mid in member_values}

    hi = max(raw.values())
    lo = min(raw.values())

    if hi == lo:
        return {mid: (1.0 if member_values[mid] is not None else None) for mid in member_values}

    out: Dict[str, Optional[float]] = {}
    for mid, v in member_values.items():
        if v is None:
            out[mid] = None
            continue
        if dim in _LOWER_IS_BETTER:
            out[mid] = round((hi - v) / (hi - lo), 4)
        else:
            out[mid] = round((v - lo) / (hi - lo), 4)
    return out


# ── 维度状态 ───────────────────────────────────────────────────


def _status_for_member(
    dim: str,
    value: Optional[float],
    baseline_value: Optional[float],
    is_baseline: bool,
) -> Tuple[str, Optional[float], bool]:
    """返回 (status, delta_vs_baseline, significant)。

    0-1 维度（quality/scenario/process/stability）用绝对阈值；
    cost 维度用相对阈值（baseline 的百分比），因为 tokens 量级远大于 0-1。
    """
    if value is None or baseline_value is None:
        return "na", None, False
    if is_baseline:
        return "tie", 0.0, False

    lower_better = dim in _LOWER_IS_BETTER

    if lower_better:
        delta = baseline_value - value  # 正值=更省=更好
    else:
        delta = value - baseline_value  # 正值=更高=更好

    # cost 维度：相对阈值（baseline 的百分比）
    if dim == "cost" and baseline_value:
        threshold = abs(baseline_value) * _TIE_THRESHOLDS.get("cost", 0.10)
    else:
        threshold = _TIE_THRESHOLDS.get(dim, 0.05)

    if delta > threshold:
        return "better", round(delta, 4), True
    if delta < -threshold:
        return "worse", round(delta, 4), True
    return "tie", round(delta, 4), False


# ── 维度裁决 ───────────────────────────────────────────────────


def _build_dimension_verdicts(
    values: Dict[str, Dict[str, Optional[float]]],
    baseline_id: str,
    members: List[Dict[str, Any]],
    has_multi_round: bool,
) -> List[Dict[str, Any]]:
    verdicts: List[Dict[str, Any]] = []

    for dim in _DIMENSION_ORDER:
        member_vals = {mid: values[mid].get(dim) for mid in values}
        normalized = _normalize_dimension(dim, member_vals)
        baseline_val = member_vals.get(baseline_id)

        applicable = any(v is not None for v in member_vals.values())
        if dim == "stability" and not has_multi_round:
            applicable = False

        per_member: List[Dict[str, Any]] = []
        for m in members:
            mid = m["member_id"]
            v = member_vals.get(mid)
            status, delta, sig = _status_for_member(
                dim, v, baseline_val, m.get("is_baseline", False)
            )
            per_member.append(
                {
                    "member_id": mid,
                    "label": m.get("label") or mid,
                    "value": _round(v),
                    "normalized_score": _round(normalized.get(mid)),
                    "status": status,
                    "delta_vs_baseline": delta,
                    "significant": sig,
                }
            )

        # 维度胜者：normalized_score 最高的；全持平时不设
        winner_mid = None
        winner_label = None
        best_norm = -1.0
        for pm in per_member:
            ns = pm.get("normalized_score")
            if ns is not None and ns > best_norm:
                best_norm = ns
                winner_mid = pm["member_id"]
                winner_label = pm["label"]
        if best_norm >= 0:
            norm_vals = [
                pm["normalized_score"]
                for pm in per_member
                if pm["normalized_score"] is not None
            ]
            if len(norm_vals) > 1 and max(norm_vals) == min(norm_vals):
                winner_mid = None
                winner_label = "持平"

        verdicts.append(
            {
                "dimension": dim,
                "display_name": _DIMENSION_DISPLAY.get(dim, dim),
                "per_member": per_member,
                "winner_member_id": winner_mid,
                "winner_label": winner_label,
                "tie_threshold": _TIE_THRESHOLDS.get(dim, 0.05),
                "applicable": applicable,
                "weight": _DIMENSION_WEIGHTS.get(dim, 0.0),
            }
        )

    return verdicts


# ── 排名 ───────────────────────────────────────────────────────


def _build_ranking(
    dimension_verdicts: List[Dict[str, Any]],
    members: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    # 每成员总分（适用维度的 normalized_score * weight 求和）
    totals: Dict[str, float] = {}
    dim_status: Dict[str, Dict[str, str]] = {}

    for m in members:
        mid = m["member_id"]
        total = 0.0
        w_sum = 0.0
        dim_status[mid] = {}

        for d in dimension_verdicts:
            dim = d["dimension"]
            pm = next((p for p in d["per_member"] if p["member_id"] == mid), None)
            if pm is None:
                dim_status[mid][dim] = "na"
                continue
            dim_status[mid][dim] = pm["status"]
            if not d.get("applicable", True):
                continue
            ns = pm.get("normalized_score")
            w = d.get("weight", 0.0)
            if ns is not None:
                total += ns * w
                w_sum += w

        totals[mid] = round(total / w_sum, 4) if w_sum > 0 else 0.0

    # 降序排列
    sorted_pairs = sorted(
        [(m, totals.get(m["member_id"], 0.0)) for m in members],
        key=lambda x: x[1],
        reverse=True,
    )
    ranking: List[Dict[str, Any]] = []
    for i, (m, score) in enumerate(sorted_pairs):
        mid = m["member_id"]
        if i > 0 and abs(score - sorted_pairs[i - 1][1]) < 0.001:
            rank = ranking[-1]["rank"]
        else:
            rank = i + 1
        ranking.append(
            {
                "member_id": mid,
                "label": m.get("label") or mid,
                "total_score": round(score, 4),
                "rank": rank,
                "dimension_status": dim_status.get(mid, {}),
            }
        )

    # 综合胜者：唯一 rank=1
    first_rank = [r for r in ranking if r["rank"] == 1]
    overall_winner = None
    if len(first_rank) == 1:
        top = first_rank[0]
        rest_scores = [r["total_score"] for r in ranking if r["rank"] != 1]
        margin = None
        significant = False
        if rest_scores:
            margin = round(top["total_score"] - max(rest_scores), 4)
            significant = margin > 0.02
        overall_winner = {
            "member_id": top["member_id"],
            "label": top["label"],
            "total_score": top["total_score"],
            "rank": 1,
            "margin": margin,
            "is_significant": significant,
        }

    return ranking, overall_winner


# ── caveats ────────────────────────────────────────────────────


def _build_caveats(
    dimension_verdicts: List[Dict[str, Any]],
    payload: Dict[str, Any],
    has_multi_round: bool,
    overall_winner: Optional[Dict[str, Any]],
) -> List[str]:
    caveats: List[str] = []

    # 维度数据缺失
    for d in dimension_verdicts:
        if not d.get("applicable"):
            continue
        missing = [pm["label"] for pm in d["per_member"] if pm["value"] is None]
        if missing:
            caveats.append(
                f"{d['display_name']}维度数据缺失：{', '.join(missing)}"
            )

    # 多轮波动 + 最弱链条
    if has_multi_round:
        questions = payload.get("questions") or []
        volatile_labels: List[str] = []
        for q in questions:
            cells = q.get("cells") or {}
            for _mid, cell in cells.items():
                if not cell:
                    continue
                std = (cell.get("stability") or {}).get("std")
                if std is not None and std > SCENARIO_SCORE_STD_VOLATILE:
                    volatile_labels.append(q.get("label") or q.get("question_key"))
                    break
        if volatile_labels:
            suffix = "…" if len(volatile_labels) > 3 else ""
            caveats.append(
                f"{len(volatile_labels)} 题多轮综合分波动较大"
                f"（σ>{SCENARIO_SCORE_STD_VOLATILE}）："
                f"{', '.join(volatile_labels[:3])}{suffix}"
            )

        stability_rows = (payload.get("stability_summaries") or {}).get("rows") or []
        weak: List[str] = []
        for r in stability_rows:
            wc = r.get("weakest_chain")
            if wc and wc.get("pass_rate"):
                if _parse_pass_rate(wc["pass_rate"]) < 1.0:
                    weak.append(f"{r.get('label')}: {wc.get('name')} {wc.get('pass_rate')}")
        if weak:
            caveats.append(
                f"关键链条通过率偏低：{'; '.join(weak[:3])}"
            )

    # 胜者不显著
    if overall_winner and not overall_winner.get("is_significant"):
        caveats.append(
            f"综合最优版本 {overall_winner['label']} 与基线差距不显著（Δ<0.02）"
        )

    # 成本显著恶化
    cost_dim = next(
        (d for d in dimension_verdicts if d["dimension"] == "cost"), None
    )
    if cost_dim and cost_dim.get("applicable"):
        for pm in cost_dim["per_member"]:
            if pm["status"] == "worse" and pm.get("significant"):
                caveats.append(
                    f"{pm['label']} 成本显著高于基线（Δ{pm.get('delta_vs_baseline')}）"
                )
                break

    return caveats


# ── 推荐文本 ───────────────────────────────────────────────────


def _build_recommendation(
    ranking: List[Dict[str, Any]],
    overall_winner: Optional[Dict[str, Any]],
    dimension_verdicts: List[Dict[str, Any]],
    baseline_id: str,
) -> str:
    if not ranking:
        return "暂无足够数据进行版本对比。"

    if not overall_winner:
        first_rank = [r for r in ranking if r["rank"] == 1]
        if len(first_rank) >= 2:
            labels = [r["label"] for r in first_rank]
            return (
                f"各版本综合得分接近（{'、'.join(labels)}），"
                f"未形成显著差异，建议结合各维度明细判断。"
            )
        return "暂无法判定综合最优版本。"

    w = overall_winner
    label = w["label"]
    is_baseline = w["member_id"] == baseline_id

    leading_dims = []
    for d in dimension_verdicts:
        if not d.get("applicable"):
            continue
        pm = next(
            (p for p in d["per_member"] if p["member_id"] == w["member_id"]), None
        )
        if pm and pm["status"] == "better" and pm.get("significant"):
            leading_dims.append(d["display_name"])

    parts: List[str] = [f"综合推荐版本 {label}"]
    if is_baseline:
        parts.append("（基线版本）")
    else:
        parts.append("，")
        if leading_dims:
            parts.append(f"在{'、'.join(leading_dims[:2])}维度显著领先基线")
        else:
            parts.append("综合得分领先基线")

    if not w.get("is_significant"):
        parts.append("，但与基线差距不显著")

    parts.append("。")
    return "".join(parts)


# ── 主入口 ─────────────────────────────────────────────────────


async def build_overall_verdict(payload: Dict[str, Any]) -> Dict[str, Any]:
    """从场景对比矩阵 payload 计算综合裁决，并注入多轮稳定性顶层字段。"""
    values, baseline_id, members = _extract_member_values(payload)

    # ── 注入多轮稳定性顶层字段 ──
    stability_rows = (payload.get("stability_summaries") or {}).get("rows") or []
    max_rounds = max(
        (r.get("max_rounds_per_question") or 1 for r in stability_rows),
        default=1,
    )
    has_multi_round = max_rounds >= 2

    # 用基线任务数据，max_rounds 取全局最大，weakest_chain 取全局最弱
    baseline_stab = next(
        (r for r in stability_rows if r.get("is_baseline")),
        stability_rows[0] if stability_rows else None,
    )
    chain_stability: Optional[Dict[str, Any]] = None
    if baseline_stab:
        global_weakest = None
        global_weakest_rate = 2.0
        for r in stability_rows:
            wc = r.get("weakest_chain")
            if wc and wc.get("pass_rate"):
                rate = _parse_pass_rate(wc["pass_rate"])
                if rate < global_weakest_rate:
                    global_weakest_rate = rate
                    global_weakest = wc

        chain_stability = {
            "chain_pass_rate": baseline_stab.get("chain_pass_rate"),
            "questions_all_rounds_ok": baseline_stab.get("questions_all_rounds_ok"),
            "question_count": baseline_stab.get("question_count"),
            "max_rounds_per_question": max_rounds,
            "weakest_chain": global_weakest,
        }

    payload["inference_rounds"] = max_rounds
    payload["multi_round_mode"] = has_multi_round
    payload["chain_stability"] = chain_stability

    # ── 维度裁决 ──
    dimension_verdicts = _build_dimension_verdicts(
        values, baseline_id, members, has_multi_round
    )

    # ── 排名 ──
    ranking, overall_winner = _build_ranking(dimension_verdicts, members)

    # ── caveats ──
    caveats = _build_caveats(
        dimension_verdicts, payload, has_multi_round, overall_winner
    )

    # ── 推荐文本 ──
    recommendation_text = _build_recommendation(
        ranking, overall_winner, dimension_verdicts, baseline_id
    )

    return {
        "overall_winner": overall_winner,
        "ranking": ranking,
        "dimension_verdicts": dimension_verdicts,
        "recommendation_text": recommendation_text,
        "caveats": caveats,
        "weights": dict(_DIMENSION_WEIGHTS),
        "has_multi_round": has_multi_round,
    }
