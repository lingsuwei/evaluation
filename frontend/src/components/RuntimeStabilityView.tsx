/**
 * 运行稳定性视图 — 严格对标 demo eval-detail-left-right.html 的 viewPerf()
 * 数据来源：/api/tasks/{taskId}/performance-report
 * 字段动态刷新，不写死。
 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { formatDuration } from '../utils/dateUtils'
import { scoreColor } from '../utils/verdictBadge'
import type { DimensionVerdictsPayload } from '../types/dimensionVerdict'

const API_BASE = '/api'

/* ===== 后端 performance-report 类型（仅取所需字段） ===== */
interface RunRow {
  agent_run_id: string
  question_id?: number | null
  run_index?: number
  llm_call_count?: number
  tool_call_count?: number
  latency_ms?: number | null
  tokens_total?: number | null
  correctness_score?: number | null
  correctness_passed?: boolean | null
}

interface PerfSummary {
  run_count?: number
  question_count?: number
  inference_rounds?: number
  multi_round_mode?: boolean
  llm_call_count?: number
  tool_call_count?: number
  avg_latency_ms?: number | null
  tokens_total?: number | null
}

interface TargetComparison {
  inference_latency_ms?: { current_ms?: number | null; target_ms?: number; status?: string | null }
  tokens_total?: { current_avg?: number | null; target?: number; status?: string | null }
}

interface StabilityQuestion {
  question_id: number
  round_count?: number
  latency_min_ms?: number | null
  latency_max_ms?: number | null
  tokens_min?: number | null
  tokens_max?: number | null
  llm_call_min?: number | null
  llm_call_max?: number | null
  tool_call_min?: number | null
  tool_call_max?: number | null
  correctness_mean?: number | null
  correctness_std?: number | null
  correctness_pass_count?: number
  correctness_pass_rate?: number | null
  is_focus?: boolean
}

export interface PerformanceReport {
  summary?: PerfSummary
  run_rows?: RunRow[]
  target_comparison?: TargetComparison
  stability_questions?: StabilityQuestion[]
}

interface ChartQuestion {
  id: number
  text: string
  color: string
  toolColor: string
}

interface Props {
  taskId: string
  runtimeStabilityVerdict?: DimensionVerdictsPayload['runtime_stability'] | null
  onOpenQuestion?: (qid: number) => void
  /** 上层已预取的 performance-report；传入则本组件不再重复请求 */
  perfReport?: PerformanceReport | null
  /** 稳定性分双子分：质量稳定性 + 性能稳定性 */
  stabilityBreakdown?: {
    quality_stability?: number | null
    performance_stability?: number | null
  } | null
}

/* ===== 辅助函数 ===== */
export interface StabilityResult {
  latTarget: number
  tokTarget: number
  latRate: number
  tokRate: number
  correctAvg: number
  correctStd: number
  latAvg: number
  llmTotal: number
  toolTotal: number
  n: number
  latPass: number
  tokPass: number
  verdict: string
  vColor: string
  vDesc: string
  maxLat: number
  maxTok: number
}

/** 根据多轮 run_rows 计算全局稳定性结论（稳定/需关注/不稳定），供顶部四维结论条与稳定性视图复用 */
export function computeStability(data: PerformanceReport | null): StabilityResult {
  const rows = data?.run_rows ?? []
  const tc = data?.target_comparison
  const latTarget = tc?.inference_latency_ms?.target_ms ?? 2000
  const tokTarget = tc?.tokens_total?.target ?? 4000

  let latPass = 0, tokPass = 0, correctSum = 0, n = 0
  let correctVals: number[] = [], latVals: number[] = []
  let llmTotal = 0, toolTotal = 0

  for (const r of rows) {
    n++
    if (r.latency_ms != null) {
      latVals.push(r.latency_ms)
      if (r.latency_ms <= latTarget) latPass++
    }
    if (r.tokens_total != null && r.tokens_total <= tokTarget) tokPass++
    if (r.correctness_score != null) {
      correctSum += r.correctness_score
      correctVals.push(r.correctness_score)
    }
    llmTotal += r.llm_call_count ?? 0
    toolTotal += r.tool_call_count ?? 0
  }

  const latRate = n > 0 ? Math.round(latPass / n * 100) : 0
  const tokRate = n > 0 ? Math.round(tokPass / n * 100) : 0
  const correctAvg = n > 0 ? correctSum / n : 0
  const correctStd = correctVals.length > 1
    ? Math.sqrt(correctVals.reduce((s, v) => s + (v - correctAvg) ** 2, 0) / correctVals.length)
    : 0
  const latAvg = latVals.length > 0 ? latVals.reduce((s, v) => s + v, 0) / latVals.length : 0

  // 稳定性结论
  let verdict: string, vColor: string, vDesc: string
  if (latRate >= 90 && tokRate >= 80 && correctStd <= 0.08 && correctAvg >= 0.8) {
    verdict = '稳定'; vColor = 'var(--edp-green)'; vDesc = '多轮时延/Token/正确性波动小，交付稳定'
  } else if (latRate >= 75 && tokRate >= 60 && correctStd <= 0.15) {
    verdict = '需关注'; vColor = 'var(--edp-amber)'; vDesc = '部分轮次波动较大，建议关注异常轮次'
  } else {
    verdict = '不稳定'; vColor = 'var(--edp-red)'; vDesc = '多轮波动明显，存在异常轮次需排查'
  }

  return { latTarget, tokTarget, latRate, tokRate, correctAvg, correctStd, latAvg, llmTotal, toolTotal, n, latPass, tokPass, verdict, vColor, vDesc, maxLat: Math.max(...latVals, 1), maxTok: Math.max(...(rows.map(r => r.tokens_total ?? 0)), 1) }
}

function fmtTokens(v?: number | null) {
  if (v == null) return '—'
  return v.toLocaleString()
}

/* ===== 可点击图例项：单击切换隐藏/显示对应数据系列 ===== */
function LegendItem({ color, hidden, onToggle, children }: {
  color: string
  hidden: boolean
  onToggle: () => void
  children: ReactNode
}) {
  return (
    <span onClick={onToggle} style={{ cursor: 'pointer', opacity: hidden ? 0.35 : 1, textDecoration: hidden ? 'line-through' : 'none' }}>
      <i style={{ background: color }}></i>{children}
    </span>
  )
}

/* ===== 逐轮柱状对比图：双题并排柱 + 阈值线 ===== */
function BarChartPerf({
  runsByQ,
  questions,
  metricKey,
  max,
  threshold,
  unit,
}: {
  runsByQ: Map<number, RunRow[]>
  questions: ChartQuestion[]
  metricKey: 'latency_ms' | 'tokens_total' | 'correctness_score'
  max: number
  threshold?: number | null
  unit: string
}) {
  if (questions.length === 0 || max <= 0) return <p style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text3)' }}>暂无数据</p>
  const hh = 150, pad = 34, bw = 8, gap = 2
  // 每轮列宽随题数自适应；整体宽度随轮数动态扩展，Q 多时由外层滚动容器横向滚动
  const maxRounds = Math.max(...questions.map(q => (runsByQ.get(q.id) ?? []).length), 1)
  const colW = Math.max(44, questions.length * 11 + 12)
  const w = Math.max(340, pad + 14 + maxRounds * colW)
  const svgParts: string[] = []

  // y 网格 + 刻度
  for (let g = 0; g <= 4; g++) {
    const y = hh - pad / 2 - (g / 4) * (hh - pad)
    svgParts.push(`<line x1="${pad}" y1="${y}" x2="${w - 10}" y2="${y}" stroke="var(--edp-border-soft)"/>`)
    const lab = metricKey === 'correctness_score' ? (g * 25) + '%' : Math.round(g * max / 4)
    svgParts.push(`<text x="${pad - 6}" y="${y + 3}" text-anchor="end" font-size="9" fill="var(--edp-text3)">${lab}</text>`)
  }
  // 阈值线
  if (threshold != null && threshold > 0) {
    const y = hh - pad / 2 - (threshold / max) * (hh - pad)
    svgParts.push(`<line x1="${pad}" y1="${y}" x2="${w - 10}" y2="${y}" stroke="var(--edp-red)" stroke-dasharray="4 3" opacity=".5"/>`)
    svgParts.push(`<text x="${w - 12}" y="${y - 4}" text-anchor="end" font-size="9" fill="var(--edp-red)">阈值 ${metricKey === 'latency_ms' ? formatDuration(threshold) : threshold + (unit ? ' ' + unit : '')}</text>`)
  }
  // 柱：每轮各题并排
  for (let r = 0; r < maxRounds; r++) {
    const xCenter = pad + 4 + (r + 0.5) * colW
    questions.forEach((q, qi) => {
      const runs = runsByQ.get(q.id) ?? []
      const run = runs[r]
      if (!run) return
      let v: number | null = null
      if (metricKey === 'correctness_score') v = run.correctness_score ?? null
      else if (metricKey === 'latency_ms') v = run.latency_ms ?? null
      else v = run.tokens_total ?? null
      if (v == null) return
      const norm = Math.min(v / max, 1)
      const bh = Math.max(norm * (hh - pad), 1)
      const bx = xCenter - bw - gap / 2 + qi * (bw + gap)
      const by = hh - pad / 2 - bh
      const isOver = threshold != null && v > threshold
      const fill = isOver ? 'var(--edp-red)' : q.color
      const op = 0.9
      const qid = q.id, round = r + 1
      const vtxt = metricKey === 'correctness_score' ? v.toFixed(2) : metricKey === 'latency_ms' ? formatDuration(v) : String(v)
      svgParts.push(`<g class="edp-bar-group" data-q="${qid}" data-r="${round}"><rect x="${bx}" y="${by}" width="${bw}" height="${bh}" fill="${fill}" rx="1.5" opacity="${op}"><title>Q${qid} R${round}: ${vtxt}</title></rect></g>`)
    })
    svgParts.push(`<text x="${xCenter}" y="${hh + 8}" text-anchor="middle" font-size="9" fill="var(--edp-text3)">R${r + 1}</text>`)
  }
  return <svg width={w} height={hh + 28} style={{ overflow: 'visible' }} dangerouslySetInnerHTML={{ __html: svgParts.join('') }} />
}

/* ===== LLM / 工具 调用次数柱点图 ===== */
function CallCountChart({
  runsByQ,
  questions,
}: {
  runsByQ: Map<number, RunRow[]>
  questions: ChartQuestion[]
}) {
  const allVals: number[] = []
  for (const q of questions) {
    for (const r of (runsByQ.get(q.id) ?? [])) {
      allVals.push(r.llm_call_count ?? 0, r.tool_call_count ?? 0)
    }
  }
  const maxC = Math.max(9, ...allVals)
  const hh = 140, pad = 26, bw = 10
  // 宽度随 总轮次×题数 动态扩展，Q 多时由外层滚动容器横向滚动
  const totalRuns = questions.reduce((s, q) => s + (runsByQ.get(q.id)?.length ?? 0), 0)
  const w = Math.max(340, pad + 4 + (totalRuns + questions.length) * (bw + 4) + 10)
  const svgParts: string[] = []
  for (let g = 0; g <= 3; g++) {
    const y = hh - pad / 2 - (g / 3) * (hh - pad)
    svgParts.push(`<line x1="${pad}" y1="${y}" x2="${w - 10}" y2="${y}" stroke="var(--edp-border-soft)"/>`)
    svgParts.push(`<text x="${pad - 6}" y="${y + 3}" text-anchor="end" font-size="9" fill="var(--edp-text3)">${Math.round(g * maxC / 3)}</text>`)
  }
  let i = 0
  questions.forEach((q, qi) => {
    const runs = runsByQ.get(q.id) ?? []
    for (let r = 0; r < runs.length; r++) {
      const run = runs[r]
      const x = pad + 4 + i * (bw + 4)
      const llm = run.llm_call_count ?? 0
      const tool = run.tool_call_count ?? 0
      const bh = (llm / maxC) * (hh - pad)
      svgParts.push(`<rect x="${x}" y="${hh - pad / 2 - bh}" width="${bw}" height="${bh}" fill="${q.color}" rx="2" opacity="0.9"><title>Q${q.id} R${r + 1}: LLM ${llm}次, 工具 ${tool}次</title></rect>`)
      const ty = hh - pad / 2 - (tool / maxC) * (hh - pad)
      svgParts.push(`<circle cx="${x + bw / 2}" cy="${ty}" r="3.5" fill="${q.toolColor}"/>`)
      if (qi === 0) svgParts.push(`<text x="${x + bw / 2}" y="${hh + 8}" text-anchor="middle" font-size="8" fill="var(--edp-text3)">R${r + 1}</text>`)
      i++
    }
    i += 1 // 题间留白
  })
  return <svg width={w} height={hh + 22} style={{ overflow: 'visible' }} dangerouslySetInnerHTML={{ __html: svgParts.join('') }} />
}

/* ===== 单题稳定性卡 ===== */
function QStabCard({
  q,
  sq,
  latTarget,
  tokTarget,
  onClick,
}: {
  q: { id: number; text: string }
  sq?: StabilityQuestion
  latTarget: number
  tokTarget: number
  onClick: () => void
}) {
  const runs = sq?.round_count ?? 0
  // 从 stability_questions 取值；若缺失则显示 —
  const latMin = sq?.latency_min_ms ?? null
  const latMax = sq?.latency_max_ms ?? null
  const latAvg = latMin != null && latMax != null ? Math.round((latMin + latMax) / 2) : null
  const tokMin = sq?.tokens_min ?? null
  const tokMax = sq?.tokens_max ?? null
  const tokAvg = tokMin != null && tokMax != null ? Math.round((tokMin + tokMax) / 2) : null
  const correctMean = sq?.correctness_mean ?? null
  const correctStd = sq?.correctness_std ?? null
  const llmCalls = sq?.llm_call_max ?? null // 用 max 作为上界近似
  const toolCalls = sq?.tool_call_max ?? null

  // 稳定性判定
  const latOk = latAvg != null && latAvg <= latTarget
  const correctOk = correctMean != null && correctMean >= 0.8 && (correctStd ?? 0) <= 0.08
  const latRate = latAvg != null && latAvg <= latTarget
  let qV: string, qC: string
  if ((latOk || latRate) && correctOk) { qV = '稳定'; qC = 'var(--edp-green)' }
  else if (latAvg != null && latAvg <= latTarget * 1.5 && (correctStd ?? 0) <= 0.15) { qV = '需关注'; qC = 'var(--edp-amber)' }
  else { qV = '不稳定'; qC = 'var(--edp-red)' }

  const fields = [
    { k: '平均时延', v: latAvg != null ? formatDuration(latAvg) : '—', s: latAvg != null ? (latAvg <= latTarget ? '达标' : '超标') : '', sc: latAvg != null ? (latAvg <= latTarget ? 'var(--edp-green)' : 'var(--edp-red)') : 'var(--edp-text3)' },
    { k: '时延σ', v: latMin != null && latMax != null ? formatDuration(Math.round(latMax - latMin)) : '—', s: (latMax ?? 0) - (latMin ?? 0) <= 300 ? '波动小' : '波动大', sc: 'var(--edp-text3)' },
    { k: '平均Tokens', v: tokAvg != null ? fmtTokens(tokAvg) : '—', s: tokAvg != null ? (tokAvg <= tokTarget ? '达标' : '超标') : '', sc: tokAvg != null ? (tokAvg <= tokTarget ? 'var(--edp-green)' : 'var(--edp-red)') : 'var(--edp-text3)' },
    { k: '正确性', v: correctMean != null ? correctMean.toFixed(2) : '—', s: correctStd != null ? `σ ${correctStd.toFixed(3)}` : '', sc: 'var(--edp-text3)' },
    { k: 'LLM调用', v: llmCalls != null ? String(llmCalls) : '—', s: `${runs}轮`, sc: 'var(--edp-text3)' },
    { k: '工具调用', v: toolCalls != null ? String(toolCalls) : '—', s: `${runs}轮`, sc: 'var(--edp-text3)' },
  ]

  return (
    <div className="edp-qstab-card" onClick={onClick}>
      <div className="edp-qs-head">
        <span className="edp-qs-qid">Q{q.id + 1}</span>
        <span className="edp-qs-text">{q.text.slice(0, 22)}…</span>
        <span className="edp-qs-verdict" style={{ background: qC }}>{qV}</span>
      </div>
      <div className="edp-qs-fields">
        {fields.map(f => (
          <div key={f.k} className="edp-qs-f">
            <span className="edp-fk">{f.k}</span>
            <span className="edp-fv" style={f.k === '正确性' ? { color: scoreColor(correctMean) } : undefined}>{f.v}</span>
            {f.s && <span className="edp-fs" style={{ color: f.sc }}>{f.s}</span>}
          </div>
        ))}
      </div>
      <div className="edp-qs-foot">点击查看 {runs}轮矩阵 · Judge点评 · Agent推理轨迹 →</div>
    </div>
  )
}

/* ===== 主组件 ===== */
export function RuntimeStabilityView({ taskId, runtimeStabilityVerdict, onOpenQuestion, perfReport, stabilityBreakdown }: Props) {
  const [data, setData] = useState<PerformanceReport | null>(perfReport ?? null)
  const [loading, setLoading] = useState(!perfReport)
  const [error, setError] = useState<string | null>(null)
  // 被隐藏的题目 id（点击图例切换显示/隐藏）
  const [hiddenQids, setHiddenQids] = useState<Set<number>>(new Set())
  const toggleQid = useCallback((qid: number) => {
    setHiddenQids(prev => {
      const next = new Set(prev)
      if (next.has(qid)) next.delete(qid)
      else next.add(qid)
      return next
    })
  }, [])

  const load = useCallback(async () => {
    // 上层已预取则直接复用，避免重复请求
    if (perfReport) { setData(perfReport); setLoading(false); setError(null); return }
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${API_BASE}/tasks/${taskId}/performance-report`)
      if (!res.ok) throw new Error('加载运行效能报告失败')
      setData(await res.json())
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [taskId, perfReport])

  useEffect(() => { void load() }, [load])
  // 上层预取数据到达后同步覆盖
  useEffect(() => {
    if (perfReport) { setData(perfReport); setLoading(false); setError(null) }
  }, [perfReport])

  // run_rows 按 question_id 分组
  const runsByQuestion = useMemo(() => {
    const map = new Map<number, RunRow[]>()
    for (const r of data?.run_rows ?? []) {
      const q = r.question_id ?? 0
      if (!map.has(q)) map.set(q, [])
      map.get(q)!.push(r)
    }
    for (const [, list] of map) {
      list.sort((a, b) => (a.run_index ?? 0) - (b.run_index ?? 0))
    }
    return map
  }, [data])

  // 题目列表（从 run_rows 提取）
  const questions = useMemo(() => {
    const seen = new Map<number, string>()
    for (const r of data?.run_rows ?? []) {
      if (r.question_id != null && !seen.has(r.question_id)) {
        seen.set(r.question_id, `题目 ${r.question_id}`)
      }
    }
    const palette = ['var(--edp-primary)', 'var(--edp-amber)', 'var(--edp-blue)', 'var(--edp-green)', 'var(--edp-red)']
    const toolPalette = ['var(--edp-green)', 'var(--edp-red-soft)', 'var(--edp-blue)', 'var(--edp-green)', 'var(--edp-red)']
    return [...seen.entries()].sort((a, b) => a[0] - b[0]).map(([id, text], i) => ({
      id,
      text,
      color: palette[i % palette.length],
      toolColor: toolPalette[i % toolPalette.length],
    }))
  }, [data])

  // 可见图例（过滤掉被隐藏的题目），传给图表渲染
  const visibleQuestions = useMemo(
    () => questions.filter(q => !hiddenQids.has(q.id)),
    [questions, hiddenQids],
  )

  // 全局稳定性 KPI 计算（latRate/tokRate/latAvg 等原始指标，不含 verdict）
  const stability = useMemo(() => computeStability(data), [data])

  // dimension_verdicts 中的运行稳定性结论
  const dimVerdict = runtimeStabilityVerdict?.performance
  const dimHeadline = runtimeStabilityVerdict?.headline
  const qualityStab = runtimeStabilityVerdict?.quality_stability

  if (loading) return <p style={{ padding: 'var(--edp-space-6)', fontSize: 'var(--edp-font-md)', color: 'var(--edp-text2)' }}>加载运行稳定性报告…</p>
  if (error) return <p style={{ padding: 'var(--edp-space-6)', color: 'var(--edp-red)', fontSize: 'var(--edp-font-md)' }}>{error}</p>
  if (!data) return null

  const s = data.summary ?? {}
  const sq = data.stability_questions ?? []

  // 阈值（后端配置，不写死）
  const latTarget = stability.latTarget
  const tokTarget = stability.tokTarget

  // 后端 performance.status → 稳定性结论（与 chip / DimensionVerdictBar 同源）
  const perfStatus = dimVerdict?.status || 'na'
  const PERF_VERDICT_MAP: Record<string, { label: string; color: string; desc: string }> = {
    within_target: { label: '达标', color: 'var(--edp-green)', desc: '多轮时延/Token/正确性波动小，交付稳定' },
    elevated: { label: '需关注', color: 'var(--edp-amber)', desc: '部分轮次波动较大，建议关注异常轮次' },
    severe: { label: '未达标', color: 'var(--edp-red)', desc: '多轮波动明显，存在异常轮次需排查' },
    na: { label: '—', color: 'var(--edp-text3)', desc: '无运行时指标' },
    disabled: { label: '未启用', color: 'var(--edp-text3)', desc: '本任务未启用稳定性评测' },
  }
  const stabVerdict = PERF_VERDICT_MAP[perfStatus] || PERF_VERDICT_MAP.na

  return (
    <div style={{ padding: '0' }}>
      {/* 视图标题 */}
      <div className="edp-view-head">
        <h2>运行稳定性</h2>
        <span className="edp-desc">多轮 RUN 稳定性评测 · 时延/Token/正确性 + 调用关键字段</span>
      </div>

      {/* 稳定性结论条（所见即所得：一进页面即见结论） */}
      <div className="edp-perf-verdict" style={{ borderLeftColor: stabVerdict.color }}>
        <div className="edp-pv-badge" style={{ background: stabVerdict.color }}>{stabVerdict.label}</div>
        <div className="edp-pv-body">
          <div className="edp-pv-t">稳定性结论{dimVerdict && dimVerdict.status !== 'na' ? ' · 后端裁决' : ''}</div>
          <div className="edp-pv-d">
            {dimVerdict && dimVerdict.headline ? dimVerdict.headline : stabVerdict.desc}
          </div>
          {dimHeadline && (
            <div style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)', marginTop: 'var(--edp-space-1)', lineHeight: 1.5 }}>{dimHeadline}</div>
          )}
        </div>
        <div className="edp-pv-flags">
          {stability.latRate < 90 && <span className="edp-pv-flag" style={{ color: 'var(--edp-amber)' }}>时延达标率 {stability.latRate}%</span>}
          {stability.tokRate < 80 && <span className="edp-pv-flag" style={{ color: 'var(--edp-amber)' }}>Token达标率 {stability.tokRate}%</span>}
          {stability.correctStd > 0.08 && <span className="edp-pv-flag" style={{ color: 'var(--edp-amber)' }}>正确性σ {stability.correctStd.toFixed(3)}</span>}
          {qualityStab && qualityStab.status !== 'na' && qualityStab.metrics.pass_rate != null && (
            <span className="edp-pv-flag" style={{ color: qualityStab.status === 'pass' ? 'var(--edp-green)' : qualityStab.status === 'warn' ? 'var(--edp-amber)' : 'var(--edp-red)' }}>
              正确率 {(qualityStab.metrics.pass_rate * 100).toFixed(0)}%
            </span>
          )}
          {perfStatus === 'within_target' && (
            <span className="edp-pv-flag" style={{ color: 'var(--edp-green)' }}>各项均达标</span>
          )}
        </div>
      </div>

      {/* KPI 行 */}
      <div className="edp-kpi-row">
        <div className="edp-kpi">
          <div className="edp-k">时延达标率</div>
          <div className="edp-v" style={{ color: stability.latRate >= 90 ? 'var(--edp-green)' : 'var(--edp-amber)' }}>{stability.latRate}%</div>
          <div className="edp-sub">≤{formatDuration(latTarget)} · {stability.latPass}/{stability.n}</div>
        </div>
        <div className="edp-kpi">
          <div className="edp-k">Token达标率</div>
          <div className="edp-v" style={{ color: stability.tokRate >= 80 ? 'var(--edp-green)' : 'var(--edp-amber)' }}>{stability.tokRate}%</div>
          <div className="edp-sub">≤{fmtTokens(tokTarget)} · {stability.tokPass}/{stability.n}</div>
        </div>
        <div className="edp-kpi">
          <div className="edp-k">正确性均值</div>
          <div className="edp-v" style={{ color: scoreColor(stability.correctAvg) }}>{stability.correctAvg.toFixed(2)}</div>
          <div className="edp-sub">σ {stability.correctStd.toFixed(3)}</div>
        </div>
        <div className="edp-kpi">
          <div className="edp-k">平均时延</div>
          <div className="edp-v">{Math.round(stability.latAvg) > 0 ? formatDuration(Math.round(stability.latAvg)) : '—'}</div>
          <div className="edp-sub">{stability.latAvg <= latTarget ? '达标' : '超标'}</div>
        </div>
        <div className="edp-kpi">
          <div className="edp-k">LLM调用合计</div>
          <div className="edp-v">{stability.llmTotal}</div>
          <div className="edp-sub">{questions.length}题 · {s.inference_rounds ?? stability.n}轮</div>
        </div>
        <div className="edp-kpi">
          <div className="edp-k">工具调用合计</div>
          <div className="edp-v">{stability.toolTotal}</div>
          <div className="edp-sub">{questions.length}题 · {s.inference_rounds ?? stability.n}轮</div>
        </div>
      </div>

      {/* 稳定性子分双栏 */}
      {stabilityBreakdown && (stabilityBreakdown.quality_stability != null || stabilityBreakdown.performance_stability != null) && (
        <div className="edp-stability-subscores">
          <div className="edp-stability-subscore">
            <div className="edp-k">质量稳定性</div>
            <div className="edp-v" style={{ color: scoreColor(stabilityBreakdown.quality_stability ?? null) }}>
              {stabilityBreakdown.quality_stability != null ? stabilityBreakdown.quality_stability.toFixed(2) : '—'}
            </div>
            <div className="edp-sub">多轮通过率 + 质量波动</div>
          </div>
          <div className="edp-stability-subscore">
            <div className="edp-k">性能稳定性</div>
            <div className="edp-v" style={{ color: scoreColor(stabilityBreakdown.performance_stability ?? null) }}>
              {stabilityBreakdown.performance_stability != null ? stabilityBreakdown.performance_stability.toFixed(2) : '—'}
            </div>
            <div className="edp-sub">时延达标率 + Token达标率</div>
          </div>
        </div>
      )}

      {/* 逐轮柱状对比图（2 列紧凑布局，单图支持横向滚动以应对 Q 多） */}
      <p className="edp-hint" style={{ margin: '0 0 6px' }}>提示：点击图例可隐藏 / 显示对应题目的数据</p>
      <div className="edp-charts-grid">
        {/* 逐轮柱状对比图：耗时 */}
        <div className="edp-chart-card">
          <h3>逐轮耗时 <span className="edp-hint">柱=各题并排，虚线=阈值{formatDuration(latTarget)}</span></h3>
          <div className="edp-chart-scroll">
            <BarChartPerf runsByQ={runsByQuestion} questions={visibleQuestions} metricKey="latency_ms" max={Math.max(stability.maxLat * 1.1, latTarget * 1.2)} threshold={latTarget} unit="ms" />
          </div>
          <div className="edp-legend">
            {questions.map(q => <LegendItem key={q.id} color={q.color} hidden={hiddenQids.has(q.id)} onToggle={() => toggleQid(q.id)}>Q{q.id + 1}</LegendItem>)}
            <span><i style={{ background: 'var(--edp-red)' }}></i>超阈值</span>
          </div>
        </div>

        {/* 逐轮柱状对比图：Tokens */}
        <div className="edp-chart-card">
          <h3>逐轮 Tokens <span className="edp-hint">虚线=阈值{fmtTokens(tokTarget)}</span></h3>
          <div className="edp-chart-scroll">
            <BarChartPerf runsByQ={runsByQuestion} questions={visibleQuestions} metricKey="tokens_total" max={Math.max(stability.maxTok * 1.1, tokTarget * 1.2)} threshold={tokTarget} unit="" />
          </div>
          <div className="edp-legend">
            {questions.map(q => <LegendItem key={q.id} color={q.color} hidden={hiddenQids.has(q.id)} onToggle={() => toggleQid(q.id)}>Q{q.id + 1}</LegendItem>)}
            <span><i style={{ background: 'var(--edp-red)' }}></i>超阈值</span>
          </div>
        </div>

        {/* 逐轮柱状对比图：正确性 */}
        <div className="edp-chart-card">
          <h3>逐轮正确性 <span className="edp-hint">0-1 · 越高越好</span></h3>
          <div className="edp-chart-scroll">
            <BarChartPerf runsByQ={runsByQuestion} questions={visibleQuestions} metricKey="correctness_score" max={1} threshold={null} unit="" />
          </div>
          <div className="edp-legend">
            {questions.map(q => <LegendItem key={q.id} color={q.color} hidden={hiddenQids.has(q.id)} onToggle={() => toggleQid(q.id)}>Q{q.id + 1}</LegendItem>)}
          </div>
        </div>

        {/* LLM / 工具 调用次数 */}
        <div className="edp-chart-card">
          <h3>LLM / 工具 调用次数（每轮）<span className="edp-hint">柱=LLM调用，点=工具调用，双题分色</span></h3>
          <div className="edp-chart-scroll">
            <CallCountChart runsByQ={runsByQuestion} questions={visibleQuestions} />
          </div>
          <div className="edp-legend">
            {questions.map(q => <LegendItem key={q.id} color={q.color} hidden={hiddenQids.has(q.id)} onToggle={() => toggleQid(q.id)}>Q{q.id + 1}-LLM</LegendItem>)}
            {questions.map(q => <LegendItem key={`t${q.id}`} color={q.toolColor} hidden={hiddenQids.has(q.id)} onToggle={() => toggleQid(q.id)}>Q{q.id + 1}-工具</LegendItem>)}
          </div>
        </div>
      </div>

      {/* 单题 RUN 稳定性 */}
      <div className="edp-chart-card">
        <h3>单题 RUN 稳定性 <span className="edp-hint">点击查看完整多轮矩阵 · Judge点评 · Agent推理轨迹</span></h3>
        <div className="edp-qstab-grid">
          {questions.map(q => (
            <QStabCard
              key={q.id}
              q={q}
              sq={sq.find(x => x.question_id === q.id)}
              latTarget={latTarget}
              tokTarget={tokTarget}
              onClick={() => onOpenQuestion?.(q.id)}
            />
          ))}
        </div>
      </div>
    </div>
  )
}
