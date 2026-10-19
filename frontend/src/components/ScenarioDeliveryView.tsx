/**
 * 场景交付视图 — 严格对标 demo eval-detail-left-right.html 的 viewScenario()
 * 数据来源：/api/scenario-eval/tasks/{taskId}/matrix + /api/scenario-eval/runs/{runId}/steps-detail（按轮次）
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { formatDuration } from '../utils/dateUtils'
import type { DimensionVerdictsPayload } from '../types/dimensionVerdict'
import { PreviewBlock, type ScenarioStepsDetailPayload, type ScenarioStepDetail } from './ScenarioStepDrilldown'
import { DrawerShell } from './DrawerShell'
import { LangfuseTraceBlock } from './LangfuseTraceBlock'

const API_BASE = '/api'

const MATRIX_CACHE_KEY = (taskId: string) => `scenario-matrix-cache:${taskId}`
// sessionStorage 配额不足时降级为内存缓存（同会话内有效）
const _inMemoryMatrixCache = new Map<string, any>()

/* ===== 后端 matrix 类型 ===== */
interface StepColumn {
  step_key: string
  name: string
  weight?: number
  required?: boolean
}

interface RunRef {
  agent_run_id: string
  run_index: number
  composite_score: number
  steps?: ScenarioStepDetail[]
  langfuse?: { trace_url?: string | null; trace_id?: string | null; langfuse_project_id?: string | null; langfuse_host?: string | null } | null
}

interface StepResult {
  step_key: string
  name?: string
  step_order?: number
  score: number
  passed: boolean
  round_stats?: {
    round_count?: number
    pass_count?: number
    pass_rate?: number
    mean_score?: number
    min_score?: number
    std_score?: number
  }
  evidence?: Record<string, unknown>
  trace_binding?: Record<string, unknown>
}

interface CellData {
  agent_run_id: string
  composite_score: number
  steps: StepResult[]
  scenario_key?: string
  round_count?: number
  stability?: { mean?: number; std?: number; min?: number; round_count?: number }
  chain_pass_rate?: number
  chain_all_rounds_ok?: boolean
  runs: RunRef[]
  langfuse?: { trace_url?: string | null; trace_id?: string | null; langfuse_project_id?: string | null; langfuse_host?: string | null }
}

interface QuestionData {
  question_key: string
  label?: string
  question_id?: number
  scenario_key?: string
  step_columns?: StepColumn[]
  cells: Record<string, CellData | null>
  winner_label?: string
}

interface Member {
  member_id: string
  agent_id?: string
  agent_name?: string
  agent_version?: string
  label?: string
  is_baseline?: boolean
}

interface StepPerfSummary {
  step_key: string
  name: string
  avg_latency_ms?: number
  avg_tokens?: number
  sample_count?: number
}

interface ChainStability {
  chain_pass_rate?: number | null
  questions_all_rounds_ok?: number
  question_count?: number
  weakest_chain?: { name?: string; pass_rate?: string } | null
}

interface MatrixData {
  members: Member[]
  member_ids: string[]
  step_columns: StepColumn[]
  questions: QuestionData[]
  step_performance_summary?: StepPerfSummary[]
  chain_stability?: ChainStability
  empty_reason?: string | null
  warnings?: string[]
  baseline_member_id?: string
  inference_rounds?: number
  multi_round_mode?: boolean
}

interface Props {
  taskId: string
  scenarioVerdict?: DimensionVerdictsPayload['scenario_delivery'] | null
  /** 深链定位：从评估结果「场景子步骤」跳转时，定位到具体题目的某个子步骤行 */
  initialStepLocator?: { qid: string; step: string }
  /** matrix 接口触发实时打分并持久化后，通知父组件静默刷新 detail（同步顶部 badge / verdict） */
  onRefreshDetail?: () => void
}

/* ===== 辅助函数 ===== */
function scoreColor(v: number): string {
  return v >= 0.75 ? 'var(--edp-green)' : v >= 0.6 ? 'var(--edp-amber)' : 'var(--edp-red)'
}
function scoreBg(v: number): string {
  return v >= 0.75 ? 'var(--edp-green-soft)' : v >= 0.6 ? 'var(--edp-amber-soft)' : 'var(--edp-red-soft)'
}

interface DrawerState {
  qLabel: string
  qNumber: number | null
  runIndex: number
  stepKey: string  // 'overall' for composite, otherwise step_key
  stepDetail: ScenarioStepDetail | null
  stepsDetail: ScenarioStepsDetailPayload | null
  runComposite: number
  agentRunId: string
  traceUrl?: string | null
  traceId?: string | null
  langfuseProjectId?: string | null
  langfuseHost?: string | null
  stepColumns: StepColumn[]  // 题目级步骤列（不同题目可能用不同模板）
}

/* ===== 逐轮场景综合分柱状图（对标 demo scenarioBarChart） ===== */
function ScenarioBarChart({ questions, onBarClick }: {
  questions: { qid: string; label: string; qNumber?: number | null; runs: RunRef[] }[]
  onBarClick: (qid: string, runIndex: number) => void
}) {
  const w = 560, hh = 150, pad = 34, bw = 9, gap = 2
  const plotW = w - pad - 14
  const maxRounds = Math.max(...questions.map(q => q.runs.length), 1)
  const colWActual = plotW / maxRounds

  let svg = `<svg width="${w}" height="${hh + 28}" style="overflow:visible">`
  // y 网格
  for (let g = 0; g <= 4; g++) {
    const y = hh - pad / 2 - (g / 4) * (hh - pad)
    svg += `<line x1="${pad}" y1="${y}" x2="${w - 10}" y2="${y}" stroke="var(--edp-border-soft)"/>`
    svg += `<text x="${pad - 6}" y="${y + 3}" text-anchor="end" font-size="9" fill="var(--edp-text3)">${g * 25}%</text>`
  }
  // 0.6 达标线
  const ty = hh - pad / 2 - 0.6 * (hh - pad)
  svg += `<line x1="${pad}" y1="${ty}" x2="${w - 10}" y2="${ty}" stroke="var(--edp-red)" stroke-width="1" stroke-dasharray="4 3" opacity=".5"/>`
  svg += `<text x="${w - 12}" y="${ty - 3}" text-anchor="end" font-size="9" fill="var(--edp-red)">0.60</text>`

  questions.forEach((q, qi) => {
    q.runs.forEach((run, ri) => {
      const x = pad + 4 + ri * colWActual + (qi ? bw + gap : 0)
      const sc = run.composite_score
      const bh = sc * (hh - pad)
      const fill = qi ? 'var(--edp-amber)' : 'var(--edp-primary)'
      const op = sc < 0.6 ? 1 : qi ? 0.8 : 1
      const qPrefix = q.qNumber != null ? `Q${q.qNumber} · ` : ''
      svg += `<g class="edp-bar-group" data-qid="${q.qid}" data-ri="${ri}" style="cursor:pointer"><rect x="${x}" y="${hh - pad / 2 - bh}" width="${bw}" height="${bh}" fill="${fill}" rx="2" opacity="${op}"><title>${qPrefix}${q.label} R${ri + 1}: ${(sc * 100).toFixed(0)}%</title></rect></g>`
    })
    if (qi === 0) {
      for (let r = 0; r < maxRounds; r++) {
        svg += `<text x="${pad + 4 + r * colWActual + bw / 2 + gap}" y="${hh + 8}" text-anchor="middle" font-size="8" fill="var(--edp-text3)">R${r + 1}</text>`
      }
    }
  })
  svg += '</svg>'
  return <div dangerouslySetInnerHTML={{ __html: svg }} onClick={(e) => {
    const g = (e.target as Element).closest('[data-qid]') as HTMLElement | null
    if (g) onBarClick(g.dataset.qid!, +g.dataset.ri!)
  }} />
}

/* ===== 主组件 ===== */
export function ScenarioDeliveryView({ taskId, scenarioVerdict, initialStepLocator, onRefreshDetail }: Props) {
  const [matrix, setMatrix] = useState<MatrixData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [drawer, setDrawer] = useState<DrawerState | null>(null)
  const [activeRound, setActiveRound] = useState<number>(1)
  // 深链定位：高亮并滚动到目标子步骤行
  const stepRowRef = useRef<HTMLTableRowElement | null>(null)

  // 加载 matrix（silent 模式不回退到 loading 状态，用于后台静默刷新）
  const loadMatrix = useCallback(async (silent: boolean = false) => {
    if (!silent) {
      setLoading(true)
      setError(null)
    }
    try {
      const res = await fetch(`${API_BASE}/scenario-eval/tasks/${taskId}/matrix`)
      if (!res.ok) throw new Error('加载场景矩阵失败')
      const data: MatrixData = await res.json()
      setMatrix(data)
      try { sessionStorage.setItem(MATRIX_CACHE_KEY(taskId), JSON.stringify(data)) }
      catch { _inMemoryMatrixCache.set(taskId, data) }
    } catch (e) {
      if (!silent) setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      if (!silent) setLoading(false)
    }
  }, [taskId])

  // 挂载时：若 sessionStorage 有缓存，先秒开（loading=false），再后台静默刷新
  useEffect(() => {
    try {
      const cached = sessionStorage.getItem(MATRIX_CACHE_KEY(taskId))
      if (cached) {
        setMatrix(JSON.parse(cached))
        setLoading(false)
        void loadMatrix(true)
        return
      }
    } catch { /* 解析失败忽略 */ }
    const mem = _inMemoryMatrixCache.get(taskId)
    if (mem) {
      setMatrix(mem)
      setLoading(false)
      void loadMatrix(true)
      return
    }
    void loadMatrix()
  }, [loadMatrix])

  // matrix 接口会触发实时打分并持久化 ScenarioStepResult。若顶部结论仍为 na（首次打开时
  // Pipeline 自动打分未产出），通知父组件静默刷新 detail，使顶部 badge 与 verdict 同步。
  const refreshDetailCalledRef = useRef(false)
  useEffect(() => {
    if (
      matrix &&
      matrix.questions.length > 0 &&
      scenarioVerdict?.status === 'na' &&
      onRefreshDetail &&
      !refreshDetailCalledRef.current
    ) {
      refreshDetailCalledRef.current = true
      onRefreshDetail()
    }
  }, [matrix, scenarioVerdict?.status, onRefreshDetail])

  // 选定 baseline member
  const baselineMember = useMemo(() => {
    if (!matrix) return null
    return matrix.members.find(m => m.is_baseline) || matrix.members[0] || null
  }, [matrix])

  // 从矩阵响应中直接构建 runDetails（后端已在 runs[] 中直出 steps，无需 N+1 请求）
  const runDetails = useMemo(() => {
    if (!matrix || !baselineMember) return new Map<string, Map<number, ScenarioStepsDetailPayload>>()
    const map = new Map<string, Map<number, ScenarioStepsDetailPayload>>()
    for (const q of matrix.questions) {
      const cell = q.cells[baselineMember.member_id]
      if (!cell) continue
      const inner = new Map<number, ScenarioStepsDetailPayload>()
      for (const run of cell.runs ?? []) {
        if (run.steps) {
          inner.set(run.run_index, {
            agent_run_id: run.agent_run_id,
            scenario_key: cell.scenario_key ?? '',
            composite_score: run.composite_score ?? 0,
            langfuse: run.langfuse ?? null,
            steps: run.steps,
          } as ScenarioStepsDetailPayload)
        }
      }
      if (inner.size > 0) map.set(q.question_key, inner)
    }
    return map
  }, [matrix, baselineMember])

  // 深链定位：高亮并滚动到目标子步骤行
  useEffect(() => {
    if (initialStepLocator) {
      const t = setTimeout(() => stepRowRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' }), 80)
      return () => clearTimeout(t)
    }
  }, [initialStepLocator, matrix, runDetails])

  // 计算全局场景指标
  const scenarioStats = useMemo(() => {
    if (!matrix || !baselineMember) return null
    const mId = baselineMember.member_id
    const stepCols = matrix.step_columns
    const allScores: { qid: string; r: number; score: number }[] = []
    let mandatoryPass = 0, mandatoryTotal = 0
    const qAvgs: Record<string, number> = {}

    for (const q of matrix.questions) {
      const cell = q.cells[mId]
      if (!cell) continue
      let qSum = 0
      const qStepCols = q.step_columns || stepCols
      const cellRuns = cell.runs ?? []
      for (const run of cellRuns) {
        const rd = runDetails.get(q.question_key)?.get(run.run_index)
        const sc = rd?.composite_score ?? run.composite_score ?? 0
        allScores.push({ qid: q.question_key, r: run.run_index, score: sc })
        qSum += sc
        // 必做步骤通过率
        for (const sc2 of qStepCols) {
          if (!sc2.required) continue
          const stepDetail = rd?.steps.find(s => s.step_key === sc2.step_key)
          if (stepDetail) {
            mandatoryTotal++
            if (stepDetail.score >= 0.6) mandatoryPass++
          }
        }
      }
      const rc = cellRuns.length || 1
      qAvgs[q.question_key] = qSum / rc
    }

    const overallAvg = allScores.length > 0 ? allScores.reduce((s, c) => s + c.score, 0) / allScores.length : 0
    const mandatoryRate = mandatoryTotal > 0 ? Math.round((mandatoryPass / mandatoryTotal) * 100) : 0
    const variance = allScores.length > 0 ? allScores.reduce((s, c) => s + (c.score - overallAvg) ** 2, 0) / allScores.length : 0
    const std = Math.sqrt(variance)
    allScores.sort((a, b) => a.score - b.score)
    const weakest = allScores[0]
    const strongest = allScores[allScores.length - 1]
    const qEntries = Object.entries(qAvgs)
    const wq = qEntries.length > 0 ? qEntries.reduce((a, b) => a[1] < b[1] ? a : b)[0] : ''
    const sq = qEntries.length > 0 ? qEntries.reduce((a, b) => a[1] > b[1] ? a : b)[0] : ''

    // 薄弱步骤
    const weakSteps = stepCols.filter(sc => {
      let sum = 0, n = 0
      for (const q of matrix.questions) {
        const cell = q.cells[mId]
        if (!cell) continue
        for (const run of cell.runs ?? []) {
          const rd = runDetails.get(q.question_key)?.get(run.run_index)
          const sd = rd?.steps.find(s => s.step_key === sc.step_key)
          if (sd) { sum += sd.score; n++ }
        }
      }
      return n > 0 && sum / n < 0.65
    })

    return { overallAvg, mandatoryRate, mandatoryPass, mandatoryTotal, std, weakest, strongest, qAvgs, wq, sq, weakSteps }
  }, [matrix, baselineMember, runDetails])

  if (loading) return <p style={{ padding: 'var(--edp-space-6)', fontSize: 'var(--edp-font-md)', color: 'var(--edp-text2)' }}>加载场景交付数据…</p>
  if (error) return <p style={{ padding: 'var(--edp-space-6)', color: 'var(--edp-red)', fontSize: 'var(--edp-font-md)' }}>{error}</p>

  if (!matrix || matrix.questions.length === 0 || !baselineMember) {
    return (
      <div style={{ padding: 'var(--edp-space-6)' }}>
        <div className="edp-view-head">
          <h2>场景交付</h2>
          <span className="edp-desc">场景子步骤加权综合分 + 多轮交付能力</span>
        </div>
        <div style={{ padding: '14px 16px', background: 'var(--edp-bg)', border: '1px solid var(--edp-border-soft)', borderRadius: 'var(--edp-radius-sm)', fontSize: 'var(--edp-font-md)', color: 'var(--edp-text2)', lineHeight: 1.7 }}>
          <p style={{ margin: '0 0 8px' }}>{matrix?.empty_reason || '未配置场景子步骤模板，无法生成场景交付视图。'}</p>
          <p style={{ margin: 0, fontSize: 'var(--edp-font-base)', color: 'var(--edp-text3)' }}>请在场景评估配置中为数据集/题目配置子步骤模板（步骤名、权重、判分规则）。</p>
        </div>
      </div>
    )
  }

  const mId = baselineMember.member_id
  const stepCols = matrix.step_columns
  const stats = scenarioStats!

  // 结论
  const dimVerdict = scenarioVerdict
  const verdictLabel = dimVerdict?.status === 'pass' ? '达标' : dimVerdict?.status === 'warn' ? '需关注' : dimVerdict?.status === 'fail' ? '不达标' : '—'
  const verdictColor = dimVerdict?.status === 'pass' ? 'var(--edp-green)' : dimVerdict?.status === 'warn' ? 'var(--edp-amber)' : dimVerdict?.status === 'fail' ? 'var(--edp-red)' : 'var(--edp-text3)'
  const verdictDesc = dimVerdict?.headline || (
    stats.overallAvg >= 0.75 && stats.mandatoryRate >= 85 && stats.std <= 0.12
      ? '场景综合分达标，必做步骤通过率高，多轮波动小'
      : stats.overallAvg >= 0.6 && stats.mandatoryRate >= 70
        ? '部分步骤或轮次表现不稳定，建议关注薄弱环节'
        : '场景交付能力不足，必做步骤未通过或综合分偏低'
  )

  // 逐轮数据 for bar chart
  const barQuestions = matrix.questions.map(q => {
    const cell = q.cells[mId]
    return {
      qid: q.question_key,
      label: q.label || q.question_key,
      qNumber: q.question_id != null ? q.question_id + 1 : null,
      runs: cell?.runs || [],
    }
  })

  // 打开抽屉
  const openDrawer = (qid: string, runIndex: number, stepKey: string) => {
    const q = matrix.questions.find(x => x.question_key === qid)
    if (!q) return
    const cell = q.cells[mId]
    if (!cell) return
    const run = (cell.runs ?? []).find(r => r.run_index === runIndex)
    if (!run) return
    const rd = runDetails.get(qid)?.get(runIndex)
    const stepDetail = stepKey !== 'overall' ? rd?.steps.find(s => s.step_key === stepKey) || null : null
    setDrawer({
      qLabel: q.label || qid,
      qNumber: q.question_id != null ? q.question_id + 1 : null,
      runIndex,
      stepKey,
      stepDetail,
      stepsDetail: rd || null,
      runComposite: run.composite_score,
      agentRunId: run.agent_run_id,
      traceUrl: rd?.langfuse?.trace_url || cell.langfuse?.trace_url || null,
      traceId: rd?.langfuse?.trace_id || cell.langfuse?.trace_id || null,
      langfuseProjectId: rd?.langfuse?.langfuse_project_id || cell.langfuse?.langfuse_project_id || null,
      langfuseHost: rd?.langfuse?.langfuse_host || cell.langfuse?.langfuse_host || null,
      stepColumns: q.step_columns || stepCols,
    })
  }

  return (
    <div style={{ padding: 0 }}>
      {/* 视图标题 */}
      <div className="edp-view-head">
        <h2>场景交付</h2>
        <span className="edp-desc">场景子步骤加权综合分 + 多轮交付能力 · 点击单元格查看步骤评分与指标分解</span>
      </div>

      {/* 结论条 */}
      <div className="edp-perf-verdict" style={{ borderLeftColor: verdictColor }}>
        <div className="edp-pv-badge" style={{ background: verdictColor }}>{verdictLabel}</div>
        <div className="edp-pv-body">
          <div className="edp-pv-t">场景交付结论{dimVerdict?.headline ? ' · 后端裁决' : ''}</div>
          <div className="edp-pv-d">{verdictDesc}</div>
        </div>
        <div className="edp-pv-flags">
          {stats.mandatoryRate < 85 && <span className="edp-pv-flag" style={{ color: 'var(--edp-amber)' }}>必做步骤通过率 {stats.mandatoryRate}%</span>}
          {stats.std > 0.12 && <span className="edp-pv-flag" style={{ color: 'var(--edp-amber)' }}>多轮σ {stats.std.toFixed(3)}</span>}
          {stats.weakest && stats.weakest.score < 0.6 && <span className="edp-pv-flag" style={{ color: 'var(--edp-red)' }}>最弱轮 {stats.weakest.qid}·R{stats.weakest.r + 1} {(stats.weakest.score * 100).toFixed(0)}%</span>}
          {stats.weakSteps.map(s => <span key={s.step_key} className="edp-pv-flag" style={{ color: 'var(--edp-amber)' }}>{s.name}均分偏低</span>)}
          {stats.mandatoryRate >= 85 && stats.std <= 0.12 && (!stats.weakest || stats.weakest.score >= 0.6) && stats.weakSteps.length === 0 && <span className="edp-pv-flag" style={{ color: 'var(--edp-green)' }}>各项均达标</span>}
        </div>
      </div>

      {/* KPI 行 */}
      <div className="edp-kpi-row">
        <div className="edp-kpi">
          <div className="edp-k">场景综合分</div>
          <div className="edp-v" style={{ color: scoreColor(stats.overallAvg) }}>{stats.overallAvg.toFixed(2)}</div>
          <div className="edp-sub">加权平均</div>
        </div>
        <div className="edp-kpi">
          <div className="edp-k">必做步骤通过率</div>
          <div className="edp-v" style={{ color: stats.mandatoryRate >= 85 ? 'var(--edp-green)' : stats.mandatoryRate >= 70 ? 'var(--edp-amber)' : 'var(--edp-red)' }}>{stats.mandatoryRate}%</div>
          <div className="edp-sub">{stats.mandatoryPass}/{stats.mandatoryTotal} 步次</div>
        </div>
        <div className="edp-kpi">
          <div className="edp-k">多轮波动</div>
          <div className="edp-v" style={{ color: stats.std <= 0.12 ? 'var(--edp-green)' : stats.std <= 0.2 ? 'var(--edp-amber)' : 'var(--edp-red)' }}>σ{stats.std.toFixed(3)}</div>
          <div className="edp-sub">{stats.weakest ? `最弱 ${stats.weakest.qid}·R${stats.weakest.r + 1}` : '—'}</div>
        </div>
        <div className="edp-kpi">
          <div className="edp-k">最强/最弱题</div>
          <div className="edp-v" style={{ fontSize: 'var(--edp-font-lg)' }}>{stats.sq ? `${stats.sq}·${stats.qAvgs[stats.sq]?.toFixed(2)}` : '—'}</div>
          <div className="edp-sub">{stats.wq ? `最弱 ${stats.wq}·${stats.qAvgs[stats.wq]?.toFixed(2)}` : '—'}</div>
        </div>
      </div>

      {/* 场景子步骤矩阵 */}
      <div className="edp-chart-card">
        <h3>场景子步骤矩阵 <span className="edp-hint">行=步骤，列=轮次，色=得分 · 点击单元格查看步骤评分+指标分解+推理轨迹</span></h3>
        {matrix.questions.map(q => {
          const cell = q.cells[mId]
          if (!cell) return null
          const qStepCols = q.step_columns || stepCols
          const runs = cell.runs ?? []
          const qAvg = stats.qAvgs[q.question_key] || 0
          const qDetailsLoaded = runDetails.has(q.question_key)
          return (
            <div key={q.question_key} style={{ marginBottom: 'var(--edp-space-4)' }}>
              <div style={{ fontSize: 'var(--edp-font-base)', fontWeight: 600, color: 'var(--edp-primary)', marginBottom: 'var(--edp-space-2)', display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)' }}>
                {q.question_id != null ? `Q${q.question_id + 1} · ` : ''}{q.label || q.question_key}
                <span style={{ fontSize: 'var(--edp-font-sm)', fontWeight: 700, color: scoreColor(qAvg), background: scoreBg(qAvg), padding: '2px 8px', borderRadius: 'var(--edp-radius-sm)' }}>
                  综合 {(qAvg * 100).toFixed(0)}%
                </span>
              </div>
              <div className="edp-matrix-wrap">
                <table className="edp-matrix">
                  <thead>
                    <tr>
                      <th className="edp-lbl" style={{ minWidth: 200 }}>步骤</th>
                      {runs.map(r => (
                        <th key={r.run_index} className={r.run_index === activeRound ? 'col-active' : ''}>R{r.run_index + 1}</th>
                      ))}
                      <th>均值</th>
                    </tr>
                  </thead>
                  <tbody>
                    {qStepCols.map(step => {
                      let sSum = 0, sN = 0
                      for (const r of runs) {
                        const rd = runDetails.get(q.question_key)?.get(r.run_index)
                        const sd = rd?.steps.find(s => s.step_key === step.step_key)
                        if (sd) { sSum += sd.score; sN++ }
                      }
                      const sAvg = sN > 0 ? sSum / sN : 0
                      return (
                        <tr key={step.step_key} ref={initialStepLocator && q.question_key === initialStepLocator.qid && step.step_key === initialStepLocator.step ? stepRowRef : undefined} style={initialStepLocator && q.question_key === initialStepLocator.qid && step.step_key === initialStepLocator.step ? { background: 'rgba(102, 126, 234, 0.14)' } : undefined}>
                          <td className="edp-lbl" style={{ minWidth: 200 }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                              {step.required
                                ? <span style={{ fontSize: 9, fontWeight: 700, color: 'var(--edp-red)', background: 'var(--edp-red-soft)', padding: '1px 5px', borderRadius: 3 }}>必做</span>
                                : <span style={{ fontSize: 9, color: 'var(--edp-text3)', background: 'var(--edp-bg)', padding: '1px 5px', borderRadius: 3 }}>可选</span>
                              }
                              <span style={{ fontWeight: 400, whiteSpace: 'normal', wordBreak: 'break-word', minWidth: 0, flex: '1 1 auto', maxWidth: 180, lineHeight: 1.4 }}>{step.name}</span>
                              {step.weight != null && <span style={{ fontSize: 'var(--edp-font-xs)', color: 'var(--edp-text3)', flexShrink: 0 }}>w{(step.weight * 100).toFixed(0)}%</span>}
                            </div>
                          </td>
                          {runs.map(r => {
                            const rd = runDetails.get(q.question_key)?.get(r.run_index)
                            const sd = rd?.steps.find(s => s.step_key === step.step_key)
                            const sc = sd?.score ?? 0
                            const cellLoading = !rd
                            return (
                              <td
                                key={r.run_index}
                                className={`edp-mc ${r.run_index === activeRound ? 'col-active' : ''}`}
                                data-qid={q.question_key}
                                data-ri={r.run_index}
                                data-step={step.step_key}
                                style={{
                                  color: cellLoading ? 'var(--edp-text3)' : scoreColor(sc),
                                  background: cellLoading ? 'var(--edp-bg)' : scoreBg(sc),
                                  cursor: 'pointer',
                                }}
                                onClick={() => { setActiveRound(r.run_index); openDrawer(q.question_key, r.run_index, step.step_key) }}
                              >
                                {cellLoading ? '…' : sc.toFixed(2)}
                              </td>
                            )
                          })}
                          <td className="edp-avg" style={{ color: qDetailsLoaded ? scoreColor(sAvg) : 'var(--edp-text3)', fontWeight: 700 }}>{qDetailsLoaded ? sAvg.toFixed(2) : '…'}</td>
                        </tr>
                      )
                    })}
                    {/* 综合分行 */}
                    <tr style={{ background: 'var(--edp-bg)' }}>
                      <td className="edp-lbl" style={{ minWidth: 200, fontWeight: 700, color: 'var(--edp-primary)' }}>综合分</td>
                      {runs.map(r => {
                        const sc = r.composite_score
                        return (
                          <td
                            key={r.run_index}
                            className={`edp-mc ${r.run_index === activeRound ? 'col-active' : ''}`}
                            style={{ color: scoreColor(sc), fontWeight: 700, cursor: 'pointer' }}
                            onClick={() => { setActiveRound(r.run_index); openDrawer(q.question_key, r.run_index, 'overall') }}
                          >
                            {sc.toFixed(2)}
                          </td>
                        )
                      })}
                      <td className="edp-avg" style={{ color: scoreColor(qAvg), fontWeight: 700 }}>{qAvg.toFixed(2)}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          )
        })}
        <div className="edp-legend">
          <div className="edp-lk"><i style={{ background: 'var(--edp-green-soft)', border: '1px solid var(--edp-green)' }}></i>≥0.75 达标</div>
          <div className="edp-lk"><i style={{ background: 'var(--edp-amber-soft)', border: '1px solid var(--edp-amber)' }}></i>0.60-0.74 需关注</div>
          <div className="edp-lk"><i style={{ background: 'var(--edp-red-soft)', border: '1px solid var(--edp-red)' }}></i>&lt;0.60 不达标</div>
        </div>
      </div>

      {/* 逐轮场景综合分柱状图 */}
      <div className="edp-chart-card">
        <h3>逐轮场景综合分 <span className="edp-hint">柱=各题加权综合分，虚线=0.6达标线 · 点击柱查看详情</span></h3>
        <ScenarioBarChart
          questions={barQuestions}
          onBarClick={(qid, ri) => openDrawer(qid, ri + 1, 'overall')}
        />
        <div className="edp-legend">
          {matrix.questions.map((q, i) => (
            <div key={q.question_key} className="edp-lk">
              <i style={{ background: i === 0 ? 'var(--edp-primary)' : 'var(--edp-amber)' }}></i>{q.question_id != null ? `Q${q.question_id + 1} · ` : ''}{q.label || q.question_key}
            </div>
          ))}
          <div className="edp-lk"><i style={{ background: 'var(--edp-red)' }}></i>低于0.6</div>
        </div>
      </div>

      {/* 多轮交付指标通过率（补充信息） */}
      {matrix.step_performance_summary && matrix.step_performance_summary.length > 0 && (
        <div className="edp-chart-card">
          <h3>多轮交付指标（补充信息） <span className="edp-hint">步骤级性能汇总</span></h3>
          <table className="edp-mtab">
            <thead>
              <tr><th style={{ width: 200, fontWeight: 400 }}>步骤</th><th>样本数</th><th>平均时延</th><th>平均Tokens</th></tr>
            </thead>
            <tbody>
              {matrix.step_performance_summary.map(s => (
                <tr key={s.step_key}>
                  <td className="edp-mn" style={{ fontWeight: 400, width: 200 }}>{s.name}</td>
                  <td>{s.sample_count ?? '—'}</td>
                  <td>{s.avg_latency_ms != null ? formatDuration(s.avg_latency_ms) : '—'}</td>
                  <td>{s.avg_tokens != null ? Math.round(s.avg_tokens) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* 抽屉 */}
      {drawer && (
        <ScenarioDrawer
          drawer={drawer}
          onClose={() => setDrawer(null)}
        />
      )}
    </div>
  )
}

/* ===== 抽屉子组件 ===== */
function ScenarioDrawer({ drawer, onClose }: {
  drawer: DrawerState
  onClose: () => void
}) {
  const { qLabel, qNumber, runIndex, stepKey, stepDetail, stepsDetail, runComposite, agentRunId, traceUrl, traceId, langfuseProjectId, langfuseHost, stepColumns } = drawer
  const isOverall = stepKey === 'overall'
  const stepCol = stepColumns.find(s => s.step_key === stepKey)

  return (
    <DrawerShell
      title={`${isOverall ? '场景综合分' : `步骤分析 · ${stepCol?.name || stepKey}`} · ${qNumber != null ? `Q${qNumber} ` : ''}${qLabel} R${runIndex + 1}`}
      onClose={onClose}
    >
          {isOverall ? (
            // 综合分分解
            <>
              <div className="edp-sect">
                <h4>场景综合分</h4>
                <div className="edp-score-big" style={{ color: scoreColor(runComposite) }}>{runComposite.toFixed(2)}</div>
                <div className="edp-kv">
                  <div><div className="edp-k">题号</div><div className="edp-v">{qNumber != null ? `Q${qNumber}` : qLabel}</div></div>
                  <div><div className="edp-k">轮次</div><div className="edp-v">R{runIndex + 1}</div></div>
                </div>
              </div>
              <div className="edp-sect">
                <h4>📋 步骤分解</h4>
                {stepsDetail?.steps.map((s, i) => {
                  const sc = stepColumns.find(c => c.step_key === s.step_key)
                  return (
                    <div key={s.step_key} style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)', fontSize: 'var(--edp-font-base)', padding: '5px 8px', borderRadius: 5, background: 'var(--edp-bg)', marginBottom: 3 }}>
                      {sc?.required
                        ? <span style={{ fontSize: 9, fontWeight: 700, color: 'var(--edp-red)', background: 'var(--edp-red-soft)', padding: '1px 5px', borderRadius: 3 }}>必做</span>
                        : <span style={{ fontSize: 9, color: 'var(--edp-text3)', background: 'var(--edp-bg)', padding: '1px 5px', borderRadius: 3 }}>可选</span>
                      }
                      <span style={{ fontWeight: 400, minWidth: 110 }}>{s.name || sc?.name || `步骤${i + 1}`}</span>
                      <span style={{ color: scoreColor(s.score), fontWeight: 700 }}>{s.score.toFixed(2)}</span>
                      {sc?.weight != null && <span style={{ color: 'var(--edp-text3)', fontSize: 'var(--edp-font-sm)', marginLeft: 'auto' }}>w{(sc.weight * 100).toFixed(0)}%</span>}
                    </div>
                  )
                }) || <p className="edp-empty" style={{ padding: 'var(--edp-space-3)' }}>无步骤数据</p>}
              </div>
            </>
          ) : stepDetail ? (
            // 单步骤分析
            <>
              <div className="edp-sect">
                <h4>步骤评分</h4>
                <div className="edp-score-big" style={{ color: scoreColor(stepDetail.score) }}>{stepDetail.score.toFixed(2)}</div>
                <div className="edp-kv">
                  <div><div className="edp-k">步骤</div><div className="edp-v">{stepCol?.name || stepDetail.name || stepKey}</div></div>
                  <div><div className="edp-k">必做</div><div className="edp-v">{stepCol?.required ? '是' : '否'}</div></div>
                  <div><div className="edp-k">权重</div><div className="edp-v">{stepCol?.weight != null ? `${(stepCol.weight * 100).toFixed(0)}%` : '—'}</div></div>
                  <div><div className="edp-k">通过</div><div className="edp-v" style={{ color: stepDetail.passed ? 'var(--edp-green)' : 'var(--edp-red)' }}>{stepDetail.passed ? '✓' : '✗'}</div></div>
                </div>
              </div>
              {/* Judge点评（紧跟打分，与评测指标页一致） */}
              {(stepDetail.evidence?.reason || stepDetail.evidence?.llm_judge_reason) && (
                <div className="edp-sect">
                  <h4>Judge点评</h4>
                  <div className="edp-review-box">{(stepDetail.evidence?.reason || stepDetail.evidence?.llm_judge_reason) as string}</div>
                </div>
              )}
              {/* 评估方式 */}
              {stepDetail.evidence?.eval_mode && (
                <div className="edp-sect">
                  <h4>评估方式</h4>
                  <div className="edp-kv">
                    <div><div className="edp-k">方式</div><div className="edp-v">{stepDetail.evidence.eval_mode as string}</div></div>
                    {stepDetail.evidence.score_source && <div><div className="edp-k">来源</div><div className="edp-v">{stepDetail.evidence.score_source as string}</div></div>}
                    {stepDetail.evidence.match_ratio != null && <div><div className="edp-k">匹配率</div><div className="edp-v">{((stepDetail.evidence.match_ratio as number) * 100).toFixed(0)}%</div></div>}
                  </div>
                </div>
              )}
              {/* 命中关键词 */}
              {stepDetail.evidence?.matched_keywords && Array.isArray(stepDetail.evidence.matched_keywords) && (stepDetail.evidence.matched_keywords as string[]).length > 0 && (
                <div className="edp-sect">
                  <h4>命中关键词</h4>
                  <div style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)' }}>{(stepDetail.evidence.matched_keywords as string[]).join(', ')}</div>
                </div>
              )}
              {/* 判分要点 */}
              {stepDetail.gt_criteria && (() => {
                const items = Array.isArray(stepDetail.gt_criteria) ? stepDetail.gt_criteria : [stepDetail.gt_criteria]
                if (items.length === 0) return null
                return (
                  <div className="edp-sect">
                    <h4>📋 判分要点</h4>
                    <div className="edp-review-box">{items.join('；')}</div>
                  </div>
                )
              })()}
              {/* Input/Output 预览 */}
              {stepDetail.trace_binding && (() => {
                const tb = stepDetail.trace_binding
                const obsType = (tb.observation_type as string | null | undefined)?.toUpperCase() || ''
                const isMerged = obsType === 'MERGED'
                const bindSource = (stepDetail.evidence?.bind_source as string | undefined) || ''
                // 评分实际用 Agent 终局答案（未绑定独立 span I/O），drawer 不应展示
                // 空的 Input/Output 误导用户。S0 这种 final_answer_no_span 即使持久化了
                // trace_binding，input/output_preview 也是空字符串。
                const finalAnswerSources = new Set([
                  'final_answer_no_span',
                  'final_answer_fallback',
                  'final_answer_unbound',
                ])
                const isFinalAnswerScored = finalAnswerSources.has(bindSource)
                  || (bindSource === 'unbound' && (stepDetail.score ?? 0) > 0)
                const hasIoPreview = !!(tb.input_preview || tb.output_preview)
                // 旧持久化行（迁移前）trace_binding 为空 dict {}，前端兜底提示
                const hasTbKeys = Object.keys(tb).length > 0
                return (
                  <div className="edp-sect">
                    <h4>🔄 绑定 Span · Input/Output</h4>
                    {tb.observation_name && (
                      <p style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text)', marginBottom: 'var(--edp-space-2)', display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)', flexWrap: 'wrap' }}>
                        <span>绑定 Span：<code>{tb.observation_name as string}</code>
                        {tb.observation_id ? ` (${(tb.observation_id as string).slice(0, 8)}…)` : ''}</span>
                        {obsType && (
                          <span
                            title={isMerged ? '多个 observation 的输出合并绑定，observation_id 为合并集合首项' : 'Langfuse observation 类型'}
                            style={{
                              fontSize: 'var(--edp-font-xs)',
                              fontWeight: 600,
                              padding: '1px 6px',
                              borderRadius: 'var(--edp-radius-pill)',
                              background: isMerged ? 'var(--edp-blue-soft)' : 'var(--edp-bg)',
                              color: isMerged ? 'var(--edp-blue)' : 'var(--edp-text3)',
                              border: isMerged ? 'none' : '1px solid var(--edp-border)',
                            }}
                          >{obsType}</span>
                        )}
                      </p>
                    )}
                    {isFinalAnswerScored && !hasIoPreview ? (
                      <div
                        style={{
                          padding: 'var(--edp-space-2)',
                          background: 'var(--edp-blue-soft)',
                          borderRadius: 'var(--edp-radius)',
                          border: '1px solid var(--edp-blue)',
                          color: 'var(--edp-blue)',
                          fontSize: 'var(--edp-font-base)',
                          lineHeight: 1.6,
                        }}
                      >
                        ℹ️ 本步骤使用 Agent 终局答案评测，未单独绑定 span 的 Input/Output。
                        <br />
                        <span style={{ color: 'var(--edp-text2)' }}>
                          原因：关键词未命中任何候选 observation（或步骤属于终局汇总步骤），
                          评分时改用 Agent 最终答案文本。
                        </span>
                      </div>
                    ) : !hasTbKeys ? (
                      <div
                        style={{
                          padding: 'var(--edp-space-2)',
                          background: 'var(--edp-bg)',
                          borderRadius: 'var(--edp-radius)',
                          border: '1px dashed var(--edp-border)',
                          color: 'var(--edp-text3)',
                          fontSize: 'var(--edp-font-base)',
                          lineHeight: 1.6,
                        }}
                      >
                        矩阵视图未含 trace_binding 预览（该步骤评分结果未持久化绑定详情）。
                        如需查看完整 I/O，请重新触发该任务的场景评测。
                      </div>
                    ) : (
                      <>
                        <details className="edp-trace-io" open>
                          <summary className="edp-io-lbl in">Input</summary>
                          <PreviewBlock
                            label="Input"
                            raw={tb.input_preview as string | null | undefined}
                            emptyHint={isMerged ? '合并绑定无 Input（多 observation 输出已合并到 Output）' : '该 observation 无 Input'}
                            maxHeight={180}
                          />
                        </details>
                        <details className="edp-trace-io" open>
                          <summary className="edp-io-lbl out">Output</summary>
                          <PreviewBlock
                            label="Output"
                            raw={tb.output_preview as string | null | undefined}
                            emptyHint="该 observation 无 Output"
                            maxHeight={240}
                          />
                        </details>
                      </>
                    )}
                    {(() => {
                      if (!bindSource) return null
                      const explain: Record<string, string> = {
                        evidence_set: '按本体工作流 evidence 集合绑定，多个 observation 输出已合并评分',
                        step_index: '按 Langfuse observation 的 step_index 元数据匹配该步骤序号',
                        agent_name: '按 observation 的 agent 名称匹配该步骤绑定的 agent',
                        agent_step_id: 'agent 名称 + step_id 双重匹配',
                        heuristic: '关键词启发式命中最优 observation',
                        heuristic_no_keyword: '无关键词命中，回退到最高分候选 observation',
                        final_answer_no_span: '未命中 span，用 Agent 终局答案评分',
                        final_answer_fallback: '终局答案兜底（步骤属于 final_answer steps）',
                        fallback_observation: '兜底 observation',
                        unbound: '未绑定任何 observation',
                      }
                      return (
                        <details className="edp-trace-io" style={{ marginTop: 'var(--edp-space-2)' }}>
                          <summary
                            className="edp-io-lbl"
                            style={{ background: 'var(--edp-bg)', color: 'var(--edp-text2)' }}
                          >
                            绑定来源 · {bindSource}
                          </summary>
                          <div
                            style={{
                              padding: 'var(--edp-space-2)',
                              fontSize: 'var(--edp-font-base)',
                              color: 'var(--edp-text2)',
                              lineHeight: 1.6,
                            }}
                          >
                            {explain[bindSource] || '未知绑定来源'}
                          </div>
                        </details>
                      )
                    })()}
                  </div>
                )
              })()}
            </>
          ) : (
            <p className="edp-empty" style={{ padding: 'var(--edp-space-3)' }}>无步骤详情数据</p>
          )}

          {/* Langfuse 链接 */}
          <LangfuseTraceBlock
            traceUrl={traceUrl}
            traceId={traceId}
            langfuseProjectId={langfuseProjectId}
            langfuseHost={langfuseHost}
          />

          {/* 运行数据 */}
          <div className="edp-sect">
            <h4>运行数据</h4>
            <div className="edp-kv">
              <div><div className="edp-k">综合分</div><div className="edp-v">{runComposite.toFixed(2)}</div></div>
              <div><div className="edp-k">轮次</div><div className="edp-v">R{runIndex + 1}</div></div>
              <div><div className="edp-k">Run ID</div><div className="edp-v" style={{ fontSize: 'var(--edp-font-xs)' }}>{agentRunId.slice(0, 12)}…</div></div>
            </div>
          </div>
    </DrawerShell>
  )
}
