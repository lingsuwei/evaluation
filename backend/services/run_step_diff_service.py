"""同 Agent 同题多轮 Run 的 Step 对齐与差异提炼（P0）。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

StepDict = Dict[str, Any]


def _step_key_at(steps: List[StepDict], idx: int) -> str:
    return steps[idx].get("step_key") or f"idx:{idx}"


def _lcs_align_pairs(
    left_steps: List[StepDict],
    right_steps: List[StepDict],
) -> List[Tuple[Optional[StepDict], Optional[StepDict], Optional[int], Optional[int]]]:
    """LCS 序列对齐：保留执行顺序，缺失侧留空位。"""
    n, m = len(left_steps), len(right_steps)
    if n == 0 and m == 0:
        return []

    left_keys = [_step_key_at(left_steps, i) for i in range(n)]
    right_keys = [_step_key_at(right_steps, j) for j in range(m)]

    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if left_keys[i] == right_keys[j]:
                dp[i][j] = 1 + dp[i + 1][j + 1]
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])

    pairs: List[
        Tuple[Optional[StepDict], Optional[StepDict], Optional[int], Optional[int]]
    ] = []
    i, j = 0, 0
    while i < n and j < m:
        if left_keys[i] == right_keys[j]:
            pairs.append((left_steps[i], right_steps[j], i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            pairs.append((left_steps[i], None, i, None))
            i += 1
        else:
            pairs.append((None, right_steps[j], None, j))
            j += 1
    while i < n:
        pairs.append((left_steps[i], None, i, None))
        i += 1
    while j < m:
        pairs.append((None, right_steps[j], None, j))
        j += 1
    return pairs


def _metric_deltas(L: StepDict, R: StepDict) -> Tuple[Optional[int], Optional[int]]:
    d_ms: Optional[int] = None
    d_tok: Optional[int] = None
    l_ms, r_ms = L.get("latency_ms"), R.get("latency_ms")
    l_tok, r_tok = L.get("tokens"), R.get("tokens")
    if l_ms is not None and r_ms is not None:
        d_ms = int(r_ms) - int(l_ms)
    if l_tok is not None and r_tok is not None:
        d_tok = int(r_tok) - int(l_tok)
    return d_ms, d_tok


def _row_status(
    L: Optional[StepDict],
    R: Optional[StepDict],
    li: Optional[int],
    ri: Optional[int],
    d_ms: Optional[int],
    d_tok: Optional[int],
) -> str:
    if L and not R:
        return "removed"
    if R and not L:
        return "added"
    if L and R:
        if li is not None and ri is not None and li != ri:
            return "order"
        if (d_ms is not None and abs(d_ms) > 150) or (
            d_tok is not None and abs(d_tok) > 100
        ):
            return "changed"
    return "same"


def align_steps(
    left_steps: List[StepDict],
    right_steps: List[StepDict],
) -> List[Dict[str, Any]]:
    """按执行顺序对齐两轮步骤（LCS + 空位），展示全部 Step 行。"""
    pairs = _lcs_align_pairs(left_steps, right_steps)
    rows: List[Dict[str, Any]] = []
    for row_idx, (L, R, li, ri) in enumerate(pairs, start=1):
        d_ms: Optional[int] = None
        d_tok: Optional[int] = None
        if L and R:
            d_ms, d_tok = _metric_deltas(L, R)
        key = (
            (L or {}).get("step_key")
            or (R or {}).get("step_key")
            or f"row:{row_idx}"
        )
        rows.append(
            {
                "index": row_idx,
                "step_key": key,
                "left_index": (li + 1) if li is not None else None,
                "right_index": (ri + 1) if ri is not None else None,
                "left": _public_step(L),
                "right": _public_step(R),
                "status": _row_status(L, R, li, ri, d_ms, d_tok),
                "delta_latency_ms": d_ms,
                "delta_tokens": d_tok,
            }
        )
    return rows


def _public_step(s: Optional[StepDict]) -> Optional[Dict[str, Any]]:
    if not s:
        return None
    return {
        "step_key": s.get("step_key"),
        "kind": s.get("kind"),
        "label": s.get("label"),
        "latency_ms": s.get("latency_ms"),
        "tokens": s.get("tokens"),
        "observation_id": s.get("observation_id"),
    }


def build_diff_summary(
    aligned: List[Dict[str, Any]],
    *,
    left_meta: Optional[Dict[str, Any]] = None,
    right_meta: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    bullets: List[Dict[str, Any]] = []
    if left_meta and right_meta:
        llm_d = int(right_meta.get("llm_call_count") or 0) - int(
            left_meta.get("llm_call_count") or 0
        )
        tool_d = int(right_meta.get("tool_call_count") or 0) - int(
            left_meta.get("tool_call_count") or 0
        )
        if llm_d or tool_d:
            bullets.append(
                {
                    "type": "changed",
                    "text": f"调用次数：LLM {llm_d:+d}，工具 {tool_d:+d}",
                }
            )
        l_steps = int(left_meta.get("step_count") or 0)
        r_steps = int(right_meta.get("step_count") or 0)
        if l_steps or r_steps:
            d_steps = r_steps - l_steps
            if d_steps:
                bullets.insert(
                    0,
                    {
                        "type": "changed",
                        "text": f"总步数：{l_steps} → {r_steps}（{d_steps:+d}）",
                    },
                )

    for row in aligned:
        key = row["step_key"]
        st = row["status"]
        if st == "removed":
            label = (row.get("left") or {}).get("label") or key
            bullets.append(
                {
                    "type": "removed",
                    "text": f"对比轮省略步骤 {key}（{label}）",
                    "step_key": key,
                }
            )
        elif st == "added":
            label = (row.get("right") or {}).get("label") or key
            bullets.append(
                {
                    "type": "added",
                    "text": f"对比轮新增步骤 {key}（{label}）",
                    "step_key": key,
                }
            )
        elif st == "order":
            li = row.get("left_index")
            ri = row.get("right_index")
            pos_text = (
                f"（基线第{li}步 → 对比第{ri}步）" if li and ri else ""
            )
            bullets.append(
                {
                    "type": "order",
                    "text": f"步骤 {key} 顺序变化{pos_text}",
                    "step_key": key,
                }
            )
        elif st == "changed":
            d_ms = row.get("delta_latency_ms")
            d_tok = row.get("delta_tokens")
            parts = []
            if d_ms is not None:
                parts.append(f"耗时 {d_ms:+d}ms")
            if d_tok is not None:
                parts.append(f"Tokens {d_tok:+d}")
            bullets.append(
                {
                    "type": "changed",
                    "text": f"步骤 {key} 指标变化：{', '.join(parts) or '有变化'}",
                    "step_key": key,
                }
            )

    if not bullets and not aligned:
        bullets.append({"type": "same", "text": "暂无 Step 可对齐。"})
    return bullets


def build_round_stability_matrix(
    run_sequences: List[Tuple[str, int, List[StepDict]]],
) -> Dict[str, Any]:
    """
    run_sequences: [(run_id, run_index, steps), ...]
    返回全轮次 stepKey 稳定性矩阵。
    """
    if len(run_sequences) < 2:
        return {"columns": [], "rows": []}

    all_keys: List[str] = []
    seen: set[str] = set()
    for _, _, steps in run_sequences:
        for s in steps:
            k = s.get("step_key") or ""
            if k and k not in seen:
                all_keys.append(k)
                seen.add(k)

    columns = [
        {"run_id": rid, "run_index": ridx}
        for rid, ridx, _ in run_sequences
    ]
    rows: List[Dict[str, Any]] = []
    for key in all_keys:
        cells = []
        latencies: List[int] = []
        present = 0
        for rid, ridx, steps in run_sequences:
            hit = next((s for s in steps if s.get("step_key") == key), None)
            if hit:
                present += 1
                ms = hit.get("latency_ms")
                if ms is not None:
                    latencies.append(int(ms))
                cells.append(
                    {
                        "run_id": rid,
                        "run_index": ridx,
                        "present": True,
                        "latency_ms": ms,
                    }
                )
            else:
                cells.append(
                    {
                        "run_id": rid,
                        "run_index": ridx,
                        "present": False,
                        "latency_ms": None,
                    }
                )
        stability = "stable"
        note = "稳定"
        total = len(run_sequences)
        if present < total:
            stability = "unstable"
            note = f"不稳定 · {present}/{total} 轮出现"
        elif latencies and max(latencies) - min(latencies) > 400:
            stability = "variable"
            note = f"耗时不稳定 · Δ{max(latencies) - min(latencies)}ms"
        rows.append(
            {
                "step_key": key,
                "cells": cells,
                "stability": stability,
                "stability_note": note,
            }
        )
    return {"columns": columns, "rows": rows}


def _sequence_keys(steps: List[StepDict]) -> List[str]:
    keys: List[str] = []
    for s in steps:
        k = s.get("step_key") or ""
        if k:
            keys.append(k)
    return keys


def _jaccard(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    union = sa | sb
    if not union:
        return 1.0
    return len(sa & sb) / len(union)


def build_structure_summary(
    run_sequences: List[Tuple[str, int, List[StepDict]]],
) -> Dict[str, Any]:
    """
    K 轮 Step 结构差异汇总（步数、偶发 step、headline）。
    run_sequences: [(run_id, run_index, steps), ...]
    """
    if not run_sequences:
        return {
            "step_counts_by_run": [],
            "distinct_step_keys": 0,
            "always_present": [],
            "sometimes_missing": [],
            "only_in_some_runs": [],
            "sequence_jaccard_min_pair": None,
            "headline": "无 Run 数据",
        }

    k = len(run_sequences)
    step_counts_by_run = [
        {"run_id": rid, "run_index": ridx, "count": len(steps)}
        for rid, ridx, steps in run_sequences
    ]
    counts = [item["count"] for item in step_counts_by_run]

    all_keys: List[str] = []
    seen: set[str] = set()
    key_presence: Dict[str, List[int]] = {}
    for rid, ridx, steps in run_sequences:
        present_keys = set()
        for s in steps:
            key = s.get("step_key") or ""
            if not key:
                continue
            if key not in seen:
                all_keys.append(key)
                seen.add(key)
            present_keys.add(key)
            key_presence.setdefault(key, []).append(ridx)
        for key in all_keys:
            if key not in present_keys:
                key_presence.setdefault(key, [])

    always_present: List[str] = []
    sometimes_missing: List[Dict[str, Any]] = []
    only_in_some_runs: List[Dict[str, Any]] = []

    for key in all_keys:
        runs_with = key_presence.get(key, [])
        present_n = len(runs_with)
        if present_n == k:
            always_present.append(key)
        elif present_n == 0:
            continue
        elif present_n < k:
            entry = {"step_key": key, "present": f"{present_n}/{k}", "runs": runs_with}
            sometimes_missing.append(entry)
            if present_n <= max(1, k // 2):
                only_in_some_runs.append(entry)

    seqs = [_sequence_keys(steps) for _, _, steps in run_sequences]
    min_j: Optional[float] = None
    if len(seqs) >= 2:
        for i in range(len(seqs)):
            for j in range(i + 1, len(seqs)):
                jac = _jaccard(seqs[i], seqs[j])
                if min_j is None or jac < min_j:
                    min_j = round(jac, 4)

    count_str = " / ".join(str(c) for c in counts)
    parts = [f"{k} 轮步数 {count_str}"]
    parts.append(f"共 {len(all_keys)} 个 step_key")
    if sometimes_missing:
        names = "、".join(
            f"{x['step_key']} {x['present']}" for x in sometimes_missing[:3]
        )
        suffix = "…" if len(sometimes_missing) > 3 else ""
        parts.append(f"{len(sometimes_missing)} 个非全轮出现（{names}{suffix}）")
    elif k > 1:
        parts.append("各轮 step_key 全集一致")

    return {
        "step_counts_by_run": step_counts_by_run,
        "distinct_step_keys": len(all_keys),
        "always_present": always_present,
        "sometimes_missing": sometimes_missing,
        "only_in_some_runs": only_in_some_runs,
        "sequence_jaccard_min_pair": min_j,
        "headline": "；".join(parts),
    }
