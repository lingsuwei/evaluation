"""版本/场景对比报告 HTML 渲染。

消费 build_cross_task_scenario_matrix 已构建的 payload，渲染为自包含 HTML
（内联 CSS、无外链），离线可看。不重算任何分数。
"""
from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Dict, List, Optional


# ── 颜色 / 文案（与前端 dimensionVerdict 对齐）──

_VERDICT_COLOR = {
    "pass": "#28a745",
    "warn": "#e6a817",
    "fail": "#dc3545",
    "na": "#888",
}
_VERDICT_LABEL = {
    "pass": "达标",
    "warn": "需关注",
    "fail": "未达标",
    "na": "不适用",
}


def _verdict_color(status: Optional[str]) -> str:
    return _VERDICT_COLOR.get(status or "", "#888")


def _verdict_label(status: Optional[str]) -> str:
    return _VERDICT_LABEL.get(status or "", status or "—")


# ── 格式化 ──

def _fmt_score(v: Any, digits: int = 3) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_int(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return str(int(round(float(v))))
    except (TypeError, ValueError):
        return "—"


def _fmt_delta(v: Any, digits: int = 2) -> str:
    if v is None:
        return "—"
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if fv > 0 else ""
    return f"{sign}{fv:.{digits}f}"


def _esc(text: Any) -> str:
    if text is None:
        return ""
    return html.escape(str(text))


def _score_bg(score: Optional[float]) -> str:
    if score is None:
        return "#eee"
    if score >= 0.8:
        return "#d4edda"
    if score >= 0.5:
        return "#fff3cd"
    return "#f8d7da"


# ── 区块渲染 ──

def _render_overall_verdict(payload: Dict[str, Any]) -> str:
    verdict = payload.get("overall_verdict") or {}
    status = verdict.get("status")
    score = verdict.get("score")
    headline = verdict.get("headline") or ""
    members = payload.get("members") or []
    member_labels = " · ".join(_esc(m.get("label") or m.get("member_id") or "") for m in members)

    score_html = (
        f'<span class="v-score">综合分 {_fmt_score(score, 2)}</span>' if score is not None else ""
    )
    return f"""
    <section class="card verdict-card">
      <div class="verdict-head">
        <strong>综合结论</strong>
        <span class="badge" style="background:{_verdict_color(status)}">{_verdict_label(status)}</span>
        {score_html}
        <span class="muted">· 对比：{member_labels}</span>
      </div>
      {f'<p class="verdict-headline">{_esc(headline)}</p>' if headline else ''}
    </section>
    """


def _render_outcome_summary(payload: Dict[str, Any]) -> str:
    s = payload.get("outcome_summaries") or {}
    rows = s.get("rows") or []
    if not rows:
        return ""
    head = (
        "<tr><th>任务 / 版本</th><th>正确性均分</th><th>通过率</th>"
        "<th>平均 Tokens</th><th>平均时延(ms)</th>"
        "<th>Δ 正确性</th><th>Δ Tokens</th><th>Δ 时延(ms)</th></tr>"
    )
    body = []
    for r in rows:
        vs = r.get("vs_baseline") or {}
        base_tag = '<span class="base-tag">基线</span>' if r.get("is_baseline") else ""
        delta_cols = (
            "<td>—</td><td>—</td><td>—</td>"
            if r.get("is_baseline")
            else (
                f"<td>{_fmt_delta(vs.get('correctness_delta'))}</td>"
                f"<td>{_fmt_delta(vs.get('tokens_delta'), 0)}</td>"
                f"<td>{_fmt_delta(vs.get('latency_delta_ms'), 0)}</td>"
            )
        )
        body.append(
            f"<tr>"
            f"<td class='left'>{_esc(r.get('label'))}{base_tag}</td>"
            f"<td>{_fmt_score(r.get('avg_correctness'))}</td>"
            f"<td>{_fmt_pct(r.get('pass_rate'))}</td>"
            f"<td>{_fmt_int(r.get('avg_tokens_total'))}</td>"
            f"<td>{_fmt_int(r.get('avg_inference_latency_ms'))}</td>"
            f"{delta_cols}"
            f"</tr>"
        )
    return f"""
    <section class="card">
      <h3>任务级 Outcome / 效率摘要</h3>
      <p class="hint">各任务（Agent 版本）整体质量与运行效率；Δ 为相对基线任务的差值。</p>
      <table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>
    </section>
    """


def _render_performance_summary(payload: Dict[str, Any]) -> str:
    s = payload.get("performance_summaries") or {}
    rows = s.get("rows") or []
    if not rows:
        return ""
    head = (
        "<tr><th>任务 / 版本</th><th>LLM 合计</th><th>工具合计</th>"
        "<th>均 LLM/题</th><th>均工具/题</th><th>均 Tokens</th>"
        "<th>均时延(ms)</th><th>必达合规率</th>"
        "<th>Δ LLM</th><th>Δ 工具</th><th>Δ 必达合规</th></tr>"
    )
    body = []
    for r in rows:
        vs = r.get("vs_baseline") or {}
        base_tag = '<span class="base-tag">基线</span>' if r.get("is_baseline") else ""
        if r.get("is_baseline"):
            d_llm = d_tool = d_comp = "—"
        else:
            d_llm = _fmt_delta(vs.get("llm_delta"), 0)
            d_tool = _fmt_delta(vs.get("tool_delta"), 0)
            d_comp = _fmt_pct(vs.get("compliance_delta")) if vs.get("compliance_delta") is not None else "—"
        body.append(
            f"<tr>"
            f"<td class='left'>{_esc(r.get('label'))}{base_tag}</td>"
            f"<td>{_fmt_int(r.get('llm_call_count'))}</td>"
            f"<td>{_fmt_int(r.get('tool_call_count'))}</td>"
            f"<td>{_fmt_score(r.get('avg_llm_per_question'), 1)}</td>"
            f"<td>{_fmt_score(r.get('avg_tool_per_question'), 1)}</td>"
            f"<td>{_fmt_int(r.get('avg_tokens_total'))}</td>"
            f"<td>{_fmt_int(r.get('avg_inference_latency_ms'))}</td>"
            f"<td>{_fmt_pct(r.get('checkpoint_compliance_rate'))}</td>"
            f"<td>{d_llm}</td><td>{d_tool}</td><td>{d_comp}</td>"
            f"</tr>"
        )
    return f"""
    <section class="card perf-card">
      <h3>跨任务运行效能结构对比</h3>
      <p class="hint">LLM/工具调用次数与均 Tokens、时延；合规率为必达过程点合规。不参与质量总分。</p>
      <table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>
    </section>
    """


def _render_stability(payload: Dict[str, Any]) -> str:
    s = payload.get("stability_summaries") or {}
    rows = s.get("rows") or []
    if not rows:
        return ""
    has_multi = any(r.get("has_multi_round") for r in rows)
    if not has_multi:
        return ""
    head = (
        "<tr><th>任务 / 版本</th><th>链条全轮通过率</th>"
        "<th>全轮通过题数</th><th>最大轮次</th><th>最弱链条</th></tr>"
    )
    body = []
    for r in rows:
        if not r.get("has_multi_round"):
            continue
        base_tag = '<span class="base-tag">基线</span>' if r.get("is_baseline") else ""
        weakest = r.get("weakest_chain") or {}
        weakest_txt = ""
        if weakest:
            weakest_txt = f"{_esc(weakest.get('name') or '')}（通过率 {_esc(weakest.get('pass_rate') or '—')}）"
        body.append(
            f"<tr>"
            f"<td class='left'>{_esc(r.get('label'))}{base_tag}</td>"
            f"<td>{_fmt_pct(r.get('chain_pass_rate'))}</td>"
            f"<td>{_fmt_int(r.get('questions_all_rounds_ok'))}/{_fmt_int(r.get('question_count'))}</td>"
            f"<td>{_fmt_int(r.get('max_rounds_per_question'))}</td>"
            f"<td>{weakest_txt or '—'}</td>"
            f"</tr>"
        )
    if not body:
        return ""
    return f"""
    <section class="card">
      <h3>链条稳定性对比</h3>
      <p class="hint">多轮推理场景下，各版本全轮通过率与最弱链条。</p>
      <table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>
    </section>
    """


def _render_scenario_matrix(payload: Dict[str, Any]) -> str:
    members = payload.get("members") or []
    questions = payload.get("questions") or []
    if not members or not questions:
        return ""
    base_tag = '<span class="base-tag">基线</span>'
    head_cells = "".join(
        f"<th>{_esc(m.get('label') or m.get('member_id'))}"
        f"{base_tag if m.get('is_baseline') else ''}</th>"
        for m in members
    )
    head = f"<tr><th class='left'>场景题</th>{head_cells}</tr>"

    body = []
    for q in questions:
        cells = q.get("cells") or {}
        row_cells = []
        for m in members:
            cell = cells.get(m.get("member_id"))
            if not cell:
                row_cells.append('<td class="na">—</td>')
                continue
            score = cell.get("composite_score")
            score_val = None
            try:
                score_val = float(score) if score is not None else None
            except (TypeError, ValueError):
                score_val = None
            bg = _score_bg(score_val)
            row_cells.append(
                f'<td style="background:{bg}"><span class="cell-score">{_fmt_score(score, 2)}</span></td>'
            )
        body.append(
            f"<tr><td class='left q-label'>{_esc(q.get('label') or q.get('question_key'))}</td>"
            f"{''.join(row_cells)}</tr>"
        )

    step_columns = payload.get("step_columns") or []
    step_note = ""
    if step_columns:
        names = "、".join(_esc(c.get("name") or c.get("step_key") or "") for c in step_columns)
        step_note = f'<p class="hint">场景子步骤：{names}</p>'

    return f"""
    <section class="card">
      <h3>场景子步骤综合分矩阵</h3>
      <p class="hint">行=场景题，列=任务（Agent 版本），单元格=该题场景子步骤综合分（0~1）。</p>
      {step_note}
      <table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table>
    </section>
    """


# ── 主入口 ──

_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", "微软雅黑", Arial, sans-serif; margin: 0; background: #f5f6fa; color: #333; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 28px 24px 60px; }
.report-header { margin-bottom: 20px; }
.report-header h1 { font-size: 22px; margin: 0 0 8px; color: #1a1a2e; }
.report-header .meta { font-size: 13px; color: #888; line-height: 1.7; }
.report-header .meta span { margin-right: 14px; }
.card { background: #fff; border: 1px solid #e8eaf0; border-radius: 8px; padding: 14px 16px; margin-bottom: 18px; }
.card h3 { margin: 0 0 6px; font-size: 15px; font-weight: 600; color: #333; }
.perf-card { background: #fff8f0; border-color: #ffe0b2; }
.perf-card h3 { color: #e65100; }
.hint { margin: 0 0 12px; font-size: 12px; color: #888; }
.muted { color: #999; font-size: 12px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 8px 10px; border-top: 1px solid #eef0f5; text-align: center; vertical-align: middle; }
thead th { background: #f0f2f8; font-weight: 600; }
td.left, th.left { text-align: left; }
.q-label { max-width: 360px; }
.base-tag { display: inline-block; margin-left: 6px; padding: 1px 6px; font-size: 11px; border-radius: 3px; background: #eef; color: #667eea; font-weight: 600; }
.verdict-card { background: #f8f9fc; }
.verdict-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.verdict-head strong { font-size: 14px; }
.badge { color: #fff; font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 4px; }
.v-score { font-size: 13px; font-weight: 600; color: #667eea; }
.verdict-headline { margin: 10px 0 0; font-size: 13px; color: #444; line-height: 1.6; }
.cell-score { font-weight: 600; }
td.na { color: #bbb; }
.footer { margin-top: 26px; font-size: 12px; color: #aaa; text-align: center; }
"""


def build_comparison_html(payload: Dict[str, Any], *, title: Optional[str] = None) -> bytes:
    """渲染对比报告 HTML（utf-8 bytes）。"""
    members = payload.get("members") or []
    dataset_name = payload.get("dataset_name") or "—"
    member_labels = " · ".join(
        _esc(m.get("label") or m.get("member_id") or "") for m in members
    )
    gen_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    report_title = title or "版本对比报告"

    sections = [
        _render_overall_verdict(payload),
        _render_outcome_summary(payload),
        _render_performance_summary(payload),
        _render_stability(payload),
        _render_scenario_matrix(payload),
    ]

    header = f"""
    <div class="report-header">
      <h1>{_esc(report_title)}</h1>
      <div class="meta">
        <span>评测集：{_esc(dataset_name)}</span>
        <span>对比：{member_labels}</span>
        <span>生成时间：{gen_time}</span>
      </div>
    </div>
    """

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(report_title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
{header}
{''.join(sections)}
<div class="footer">由 Onto-Eval 本体评测平台生成 · {gen_time}</div>
</div>
</body>
</html>
"""
    return html_doc.encode("utf-8")
