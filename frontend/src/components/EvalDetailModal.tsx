import { useState, useEffect, useCallback } from 'react'
import axios from 'axios'
import { formatDate, formatDuration } from '../utils/dateUtils'
import { isLangfuseTraceReady } from '../utils/langfuseReadiness'
import {
  OFFLINE_NA_LABEL,
  OFFLINE_NA_TOOLTIP,
  offlineNaTextStyle,
} from '../utils/evaluationDisplay'
import { ScenarioEvalPanel } from './ScenarioEvalPanel'
import { RuntimePerformanceReport, type PerfDeepLink } from './RuntimePerformanceReport'
import { ProcessConsistencyOverview } from './ProcessConsistencyOverview'
import { DimensionVerdictBar } from './DimensionVerdictBar'
import { DimensionVerdictHeadline } from './DimensionVerdictHeadline'
import { buildQualityWeightTooltip } from './qualityWeightTooltip'
import type { DimensionTabId, DimensionVerdictsPayload } from '../types/dimensionVerdict'
import { HelpTip, HistoricalPercentileTipBody } from './HelpTip'

const API_BASE = '/api'

const SYNC_STATUS_META: Record<string, { label: string; bg: string; color: string }> = {
  pending_sync: { label: '待同步', bg: 'var(--edp-amber-soft)', color: 'var(--edp-amber)' },
  synced: { label: '已物化', bg: 'var(--edp-green-soft)', color: 'var(--edp-green)' },
  synced_incomplete: { label: '数据不完整', bg: 'var(--edp-amber-soft)', color: 'var(--edp-amber)' },
  failed: { label: '物化失败', bg: 'var(--edp-red-soft)', color: 'var(--edp-red)' },
  skipped: { label: '离线/无 Trace', bg: 'var(--edp-border-soft)', color: 'var(--edp-text2)' },
  pending: { label: '排队中', bg: 'var(--edp-blue-soft)', color: 'var(--edp-blue)' },
  unknown: { label: '未知', bg: 'var(--edp-border)', color: 'var(--edp-text2)' },
}

// 失败类别元数据：retryable=false 的类别重试无意义，前端应警告
const FAILURE_CATEGORY_META: Record<string, { label: string; bg: string; color: string; retryable: boolean }> = {
  trace_not_found:     { label: 'Trace 已删除',   bg: 'var(--edp-red-soft)',     color: 'var(--edp-red)',    retryable: false },
  ownership_mismatch:  { label: '归属不匹配',     bg: 'var(--edp-red-soft)',     color: 'var(--edp-red)',    retryable: false },
  agent_run_missing:   { label: 'Run 记录缺失',   bg: 'var(--edp-amber-soft)',   color: 'var(--edp-amber)',  retryable: false },
  config_error:        { label: '配置错误',       bg: 'var(--edp-amber-soft)',   color: 'var(--edp-amber)',  retryable: false },
  auth_error:          { label: '鉴权失败',       bg: 'var(--edp-red-soft)',     color: 'var(--edp-red)',    retryable: false },
  transient:           { label: '瞬时故障',       bg: 'var(--edp-green-soft)',   color: 'var(--edp-green)',  retryable: true },
  incomplete_materialization: { label: '物化不完整', bg: 'var(--edp-amber-soft)', color: 'var(--edp-amber)', retryable: true },
  unknown:             { label: '未知',           bg: 'var(--edp-border-soft)',  color: 'var(--edp-text2)',  retryable: true },
}

function getFailureBreakdown(categories?: Record<string, number>): { retryable: number; nonRetryable: number; total: number } {
  if (!categories) return { retryable: 0, nonRetryable: 0, total: 0 }
  let retryable = 0, nonRetryable = 0
  for (const [code, count] of Object.entries(categories)) {
    const meta = FAILURE_CATEGORY_META[code] || FAILURE_CATEGORY_META.unknown
    if (meta.retryable) retryable += count
    else nonRetryable += count
  }
  return { retryable, nonRetryable, total: retryable + nonRetryable }
}

function SyncStatusBadge({ status }: { status?: string | null }) {
  if (!status) return null
  const meta = SYNC_STATUS_META[status] || SYNC_STATUS_META.unknown
  return (
    <span
      title={`trace_sync_status: ${status}`}
      style={{
        padding: '2px 8px',
        borderRadius: 'var(--edp-radius-sm)',
        fontSize: 'var(--edp-font-base)',
        background: meta.bg,
        color: meta.color,
        whiteSpace: 'nowrap',
      }}
    >
      {meta.label}
    </span>
  )
}

interface RoundDetail {
  round_num: number
  score: number
  reason: string
  duration_ms: number
}

interface QuestionResult {
  result_id: number
  question_id: number
  avg_score: number | null
  passed: boolean
  inference_duration_ms: number
  agent_run_id?: string | null
  run_index?: number
  trace_sync_status?: string | null
  trace_id?: string | null
  tokens_total?: number | null
  tokens_llm?: number | null
  tokens_tool?: number | null
  rounds: RoundDetail[]
}

interface MetricData {
  metric_name: string
  threshold: number
  results: QuestionResult[]
}

interface MetricSummary {
  metric_name: string
  avg_score: number | null
  pass_rate: number | null
  passed_count: number
  total_count: number
  skipped_count?: number
  scored_count?: number
  threshold: number
  eval_method?: string
  runtime_avg_value?: number | null
  runtime_unit?: string | null
  runtime_target_source?: 'system_default' | 'task_custom' | string
}

const METRIC_DISPLAY_NAMES: Record<string, string> = {
  faithfulness: '忠诚度',
  answer_relevancy: '回答相关性',
  correctness: '正确性',
  inference_latency: '推理时延',
  tokens_total: 'Tokens总消耗',
}

const METRIC_TAB_ORDER: Record<string, number> = {
  correctness: 0,
  answer_relevancy: 1,
  faithfulness: 2,
}

function metricSortIndex(metricName: string): number {
  return METRIC_TAB_ORDER[metricName] ?? 99
}

const RUNTIME_METRICS = new Set(['inference_latency', 'tokens_total'])

function isRuntimeMetric(metric?: Pick<MetricSummary, 'metric_name' | 'eval_method'>) {
  return Boolean(metric && (metric.eval_method === 'runtime' || RUNTIME_METRICS.has(metric.metric_name)))
}

function formatScore(score?: number | null): string {
  return score == null ? '—' : score.toFixed(2)
}

function formatPercent(value?: number | null): string {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`
}

interface TaskDetail {
  task: {
    id: string
    dataset_id: string
    dataset_name: string
    agent_id: string | null
    evaluation_mode?: 'online' | 'offline'
    capabilities?: string[]
    dimension_flags?: { quality: boolean; scenario: boolean; process_consistency: boolean; stability: boolean }
    agent_name: string
    inference_model_summary?: {
      display: string
      count: number
      labels: string[]
    }
    execution_mode_summary?: {
      display: string
      count: number
      labels: string[]
      deep_thinking_enabled?: boolean | null
    }
    llm_model_name: string
    llm_model: string
    run_count: number
    inference_rounds?: number
    eval_mode?: string
    created_at: string
    completed_at: string
    status: string
  }
  metrics_summary: MetricSummary[]
  metrics_data: Record<string, MetricData>
  questions?: Record<string, { question: string; ground_truth: string }>
  agent_answers?: Record<string, string>
  sync_summary?: Record<string, number>
  sync_failure_categories?: Record<string, number>
  fact_materialization_status?: string
  agent_run_count?: number
  checkpoint_report?: {
    task_id: string
    summary: {
      run_count: number
      runs_with_checkpoints: number
      avg_compliance_rate?: number | null
      avg_required_compliance_rate?: number | null
      avg_optional_hit_rate?: number | null
    }
    runs: Array<{
      agent_run_id: string
      run_index?: number
      question_id?: number | null
      agent_name?: string | null
      checkpoint_total: number
      checkpoint_passed: number
      compliance_rate?: number | null
      required_total?: number
      required_passed?: number
      required_compliance_rate?: number | null
      optional_total?: number
      optional_hit?: number
      optional_hit_rate?: number | null
      trace_sync_status?: string | null
      items: Array<{
        checkpoint_id: string
        name: string
        order?: number | null
        required?: boolean | null
        passed: boolean
        hit?: boolean | null
        order_ok?: boolean | null
        outcome?: string | null
      }>
    }>
  }
  quality_score?: {
    total_score?: number | null
    raw_total_score?: number | null
    result_score?: number | null
    process_score?: number | null
    stability_score?: number | null
    safety_score?: number | null
    result_weight?: number
    process_weight?: number
    stability_weight?: number
    required_pass_rate?: number | null
    required_passed?: number
    required_total?: number
    trace_evidence_coverage?: number | null
    process_run_count?: number
    gates?: Array<{
      type: string
      applied?: boolean
      cap?: number
      reason?: string
    }>
  }
  dimension_verdicts?: DimensionVerdictsPayload | null
}

interface Props {
  taskId: string
  onClose: () => void
  /** 任务进行中打开：仅展示已落库结果，汇总可能不完整 */
  isPartial?: boolean
  initialMainTab?: DimensionTabId
  initialPerfDeepLink?: PerfDeepLink | null
  /**
   * 渲染形态：
   * - 'modal'（默认）：浮层弹窗，带 overlay + maxHeight:90vh，适合临时查看
   * - 'page'：作为主内容区页面渲染，无 overlay，无 maxHeight，使用页面自然滚动
   *   用于「评估历史 → 详情」tab 页签，避免双层滚动条
   */
  variant?: 'modal' | 'page'
}

export function EvalDetailModal({
  taskId,
  onClose,
  isPartial = false,
  initialMainTab,
  initialPerfDeepLink = null,
  variant = 'modal',
}: Props) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [activeMetric, setActiveMetric] = useState<string>('')
  const [expandedQuestions, setExpandedQuestions] = useState<Set<number>>(new Set())
  const [expandedRounds, setExpandedRounds] = useState<Set<string>>(new Set())
  const [expandedRoundAnswers, setExpandedRoundAnswers] = useState<Set<number>>(new Set())
  const [qualityPage, setQualityPage] = useState(0)
  const qualityPageSize = 20
  const [mainTab, setMainTab] = useState<'quality' | 'perf' | 'scenario' | 'consistency'>('quality')
  const [targetEditorOpen, setTargetEditorOpen] = useState(false)
  const [latencyTargetInput, setLatencyTargetInput] = useState('')
  const [tokensTargetInput, setTokensTargetInput] = useState('')
  const [targetSaving, setTargetSaving] = useState(false)
  const [targetNotice, setTargetNotice] = useState<string | null>(null)
  const [targetSuggestions, setTargetSuggestions] = useState<{
    inference_latency_ms?: { p75?: number | null; p90?: number | null; sample_count?: number }
    tokens_total?: { p75?: number | null; p90?: number | null; sample_count?: number }
    reason?: string
    sample_count?: number
  } | null>(null)
  const [taskAgentLangfuseReady, setTaskAgentLangfuseReady] = useState(true)
  const [perfDeepLink, setPerfDeepLink] = useState<PerfDeepLink | null>(null)
  const [perfReportRevision, setPerfReportRevision] = useState(0)
  const [pendingQualityScrollQuestionId, setPendingQualityScrollQuestionId] = useState<number | null>(null)
  const [retryingSync, setRetryingSync] = useState(false)
  const [retryNotice, setRetryNotice] = useState<string | null>(null)
  const [retryNoticeKind, setRetryNoticeKind] = useState<'success' | 'error'>('success')

  // 指标 key→中文名 映射（来源 /api/metrics，覆盖内置/deepeval/自定义指标）
  const [metricLabelMap, setMetricLabelMap] = useState<Record<string, string>>({})

  const handlePerfDeepLinkConsumed = useCallback(() => {
    setPerfDeepLink(null)
  }, [])

  // 拉取指标目录，构建 key→中文名 映射，用于指标名展示
  useEffect(() => {
    let cancelled = false
    axios.get(`${API_BASE}/metrics`).then(res => {
      if (cancelled) return
      const d = res.data || {}
      const map: Record<string, string> = {}
      ;(d.builtin || []).forEach((m: { key: string; label: string }) => { if (m.key) map[m.key] = METRIC_DISPLAY_NAMES[m.key] || m.label })
      ;(d.deepeval || []).forEach((m: { key: string; label: string }) => { if (m.key) map[m.key] = m.label })
      ;(d.custom || []).forEach((m: { key: string; name: string }) => { if (m.key) map[m.key] = m.name })
      setMetricLabelMap(map)
    }).catch(() => {})
    return () => { cancelled = true }
  }, [])

  // 指标中文名：优先查 /api/metrics 映射，其次内置常量，最后回退原 key
  const metricLabel = useCallback((name: string) => metricLabelMap[name] || METRIC_DISPLAY_NAMES[name] || name, [metricLabelMap])

  useEffect(() => {
    setPerfDeepLink(null)
    setPendingQualityScrollQuestionId(null)
    setMainTab(initialMainTab ?? 'quality')
    if (initialPerfDeepLink) {
      setPerfDeepLink(initialPerfDeepLink)
    }
  }, [taskId, initialMainTab, initialPerfDeepLink])

  useEffect(() => {
    if (mainTab !== 'quality') return
    if (pendingQualityScrollQuestionId == null) return
    const el = document.getElementById(`quality-question-${pendingQualityScrollQuestionId}`)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' })
      setPendingQualityScrollQuestionId(null)
    }
  }, [mainTab, pendingQualityScrollQuestionId])

  useEffect(() => {
    const agentId = detail?.task?.agent_id
    if (!agentId) {
      setTaskAgentLangfuseReady(true)
      return
    }
    let cancelled = false
    axios
      .get(`${API_BASE}/agents/${agentId}`)
      .then((res) => {
        if (!cancelled) setTaskAgentLangfuseReady(isLangfuseTraceReady(res.data))
      })
      .catch(() => {
        if (!cancelled) setTaskAgentLangfuseReady(true)
      })
    return () => {
      cancelled = true
    }
  }, [detail?.task?.agent_id])

  useEffect(() => {
    fetchDetail()
  }, [taskId])

  useEffect(() => {
    if (!detail) return
    const firstQualityMetric = detail.metrics_summary
      .filter(m => !isRuntimeMetric(m))
      .sort((a, b) => metricSortIndex(a.metric_name) - metricSortIndex(b.metric_name))[0]
    const activeMetricSummary = detail.metrics_summary.find(
      m => m.metric_name === activeMetric,
    )
    if (
      firstQualityMetric &&
      (!activeMetric || !activeMetricSummary || isRuntimeMetric(activeMetricSummary))
    ) {
      setActiveMetric(firstQualityMetric.metric_name)
    }
  }, [detail])

  useEffect(() => {
    setQualityPage(0)
  }, [activeMetric, detail])

  const fetchDetail = async () => {
    try {
      setLoading(true)
      const controller = new AbortController()
      const timeoutId = setTimeout(() => controller.abort(), 60000)
      const res = await fetch(`${API_BASE}/tasks/${taskId}/detail`, {
        signal: controller.signal,
      })
      clearTimeout(timeoutId)
      if (!res.ok) {
        throw new Error('获取详情失败')
      }
      const data = await res.json()
      setDetail(data)
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        setError('加载超时，请稍后重试')
      } else {
        setError(err instanceof Error ? err.message : '未知错误')
      }
    } finally {
      setLoading(false)
    }
  }

  const handleRetrySync = async () => {
    const breakdown = getFailureBreakdown(detail?.sync_failure_categories)
    if (breakdown.nonRetryable > 0) {
      const ok = window.confirm(
        `检测到 ${breakdown.nonRetryable} 条永久性失败（Trace 已删除/配置错误等），重试无意义。\n` +
        `是否继续重试 ${breakdown.retryable} 条可恢复项？`,
      )
      if (!ok) return
    }
    try {
      setRetryingSync(true)
      setRetryNotice(null)
      const res = await fetch(
        `${API_BASE}/trace/sync-jobs/retry?task_id=${encodeURIComponent(taskId)}`,
        { method: 'POST' },
      )
      if (!res.ok) {
        throw new Error('重试请求失败')
      }
      const data = await res.json()
      const resetCount = data.reset ?? 0
      const enqueuedCount = data.enqueued ?? 0
      setRetryNoticeKind('success')
      setRetryNotice(
        resetCount + enqueuedCount > 0
          ? `已重新入队 ${resetCount + enqueuedCount} 条，后台正在处理…`
          : '没有需要重试的失败记录',
      )
      // 刷新详情以反映状态变化
      await fetchDetail()
    } catch (err) {
      setRetryNoticeKind('error')
      setRetryNotice(err instanceof Error ? err.message : '未知错误')
    } finally {
      setRetryingSync(false)
    }
  }

  const openRuntimeTargetEditor = async () => {
    const latency = detail?.metrics_summary.find(m => m.metric_name === 'inference_latency')
    const tokens = detail?.metrics_summary.find(m => m.metric_name === 'tokens_total')
    setLatencyTargetInput(latency?.threshold ? String(Math.round(latency.threshold)) : '')
    setTokensTargetInput(tokens?.threshold ? String(Math.round(tokens.threshold)) : '')
    setTargetNotice(null)
    setTargetSuggestions(null)
    setTargetEditorOpen(true)
    try {
      const res = await fetch(`${API_BASE}/tasks/${taskId}/runtime-target-suggestions`)
      if (res.ok) {
        setTargetSuggestions(await res.json())
      }
    } catch {
      // 建议加载失败不阻断编辑
    }
  }

  const applySuggestedTarget = (metric: 'latency' | 'tokens', percentile: 'p75' | 'p90') => {
    if (!targetSuggestions) return
    if (metric === 'latency') {
      const value = targetSuggestions.inference_latency_ms?.[percentile]
      if (value != null) setLatencyTargetInput(String(Math.round(value)))
    } else {
      const value = targetSuggestions.tokens_total?.[percentile]
      if (value != null) setTokensTargetInput(String(Math.round(value)))
    }
  }

  const saveRuntimeTargets = async () => {
    const latency = Number(latencyTargetInput)
    const tokens = Number(tokensTargetInput)
    if (!Number.isFinite(latency) || latency <= 0 || !Number.isFinite(tokens) || tokens <= 0) {
      setTargetNotice('请输入大于 0 的推理时延目标和 Tokens 目标。')
      return
    }
    try {
      setTargetSaving(true)
      setTargetNotice(null)
      const res = await fetch(`${API_BASE}/tasks/${taskId}/runtime-targets`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          inference_latency_ms: latency,
          tokens_total: tokens,
        }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(typeof body.detail === 'string' ? body.detail : '运行时目标保存失败')
      }
      await fetchDetail()
      setPerfReportRevision(v => v + 1)
      setTargetNotice('已更新本任务运行时目标，并重新计算效率分。')
      setTargetEditorOpen(false)
    } catch (err) {
      setTargetNotice(err instanceof Error ? err.message : '运行时目标保存失败')
    } finally {
      setTargetSaving(false)
    }
  }

  // 使用 dateUtils 中的 formatDate 函数

  const toggleQuestion = (questionId: number) => {
    setExpandedQuestions(prev => {
      const newSet = new Set(prev)
      if (newSet.has(questionId)) {
        newSet.delete(questionId)
      } else {
        newSet.add(questionId)
      }
      return newSet
    })
  }

  const toggleRound = (resultId: number, roundNum: number) => {
    const key = `${resultId}-${roundNum}`
    setExpandedRounds(prev => {
      const newSet = new Set(prev)
      if (newSet.has(key)) {
        newSet.delete(key)
      } else {
        newSet.add(key)
      }
      return newSet
    })
  }

  const toggleRoundAnswer = (resultId: number) => {
    setExpandedRoundAnswers(prev => {
      const newSet = new Set(prev)
      if (newSet.has(resultId)) {
        newSet.delete(resultId)
      } else {
        newSet.add(resultId)
      }
      return newSet
    })
  }

  const openPerfForRun = (opts: {
    questionId: number
    agentRunId?: string
    view?: PerfDeepLink['view']
  }) => {
    setMainTab('perf')
    setPerfDeepLink({
      view: opts.view ?? 'run',
      questionId: opts.questionId,
      agentRunId: opts.agentRunId,
    })
  }

  const openQualityForQuestion = (questionId: number) => {
    setMainTab('quality')
    setExpandedQuestions(prev => new Set(prev).add(questionId))
    setPendingQualityScrollQuestionId(questionId)
  }

  const openConsistencyTab = () => {
    setMainTab('consistency')
  }

  const navigateFromVerdict = (tabId: DimensionTabId, options?: { perfView?: 'stability' | 'task' | 'run' }) => {
    setMainTab(tabId)
    if (tabId === 'perf' && options?.perfView) {
      setPerfDeepLink({ view: options.perfView })
    }
  }

  // 使用 dateUtils 中的 formatDuration 函数

  if (loading) {
    return (
      <div
        className={variant === 'page' ? 'eval-detail-page-overlay' : 'modal-overlay'}
        style={variant === 'page' ? undefined : { zIndex: 1000 }}
      >
        <div
          className={variant === 'page' ? 'eval-detail-page-content' : 'modal-content'}
          style={
            variant === 'page'
              ? { width: '100%', maxWidth: '1400px', margin: '0 auto' }
              : { maxWidth: '95vw', width: '1400px', maxHeight: '90vh', overflow: 'auto' }
          }
        >
          <div style={{ textAlign: 'center', padding: 40 }}>加载中...</div>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div
        className={variant === 'page' ? 'eval-detail-page-overlay' : 'modal-overlay'}
        style={variant === 'page' ? undefined : { zIndex: 1000 }}
      >
        <div
          className={variant === 'page' ? 'eval-detail-page-content' : 'modal-content'}
          style={
            variant === 'page'
              ? { width: '100%', maxWidth: '1400px', margin: '0 auto' }
              : { maxWidth: '95vw', width: '1400px' }
          }
        >
          <h3 style={{ color: 'var(--edp-red)', display: 'flex', alignItems: 'center', gap: '8px' }}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--edp-red)" strokeWidth="2">
                <circle cx="12" cy="12" r="10"/>
                <line x1="15" y1="9" x2="9" y2="15"/>
                <line x1="9" y1="9" x2="15" y2="15"/>
              </svg>
              加载失败
            </h3>
          <p>{error}</p>
          <button onClick={onClose}>关闭</button>
        </div>
      </div>
    )
  }

  if (!detail) return null

  const qualityMetrics = detail.metrics_summary
    .filter(metric => !isRuntimeMetric(metric))
    .sort((a, b) => metricSortIndex(a.metric_name) - metricSortIndex(b.metric_name))
  const currentMetricData = detail.metrics_data[activeMetric]
  const isStabilityTask = (detail.task.inference_rounds ?? 1) > 1

  // 多轮质量指标分组：同一 question_id 有多条结果 → 按轮次聚合
  // NOTE: 不能用 useMemo，因为此处在 if (!detail) return null 之后，
  // 会违反 React Hooks 规则导致白屏崩溃
  const multiRoundGroups = (() => {
    if (!currentMetricData || !isStabilityTask) return null
    const byQ = new Map<number, QuestionResult[]>()
    for (const r of currentMetricData.results) {
      const q = r.question_id
      if (!byQ.has(q)) byQ.set(q, [])
      byQ.get(q)!.push(r)
    }
    // 仅当存在多轮时才返回分组
    const hasMulti = [...byQ.values()].some(list => list.length > 1)
    if (!hasMulti) return null
    // 每组按 run_index 排序
    for (const list of byQ.values()) {
      list.sort((a, b) => (a.run_index ?? 0) - (b.run_index ?? 0))
    }
    return byQ
  })()

  const isOfflineTask =
    detail.task.evaluation_mode === 'offline' || !detail.task.agent_id
  const taskCapabilities =
    detail.task.capabilities ??
    (isOfflineTask ? ['quality'] : ['quality', 'runtime', 'trace', 'scenario', 'checkpoint'])
  const taskInProgress =
    isPartial || ['processing', 'paused', 'pending'].includes(detail.task.status)
  const hasRuntimeData =
    (detail.agent_run_count ?? 0) > 0 ||
    detail.metrics_summary.some(m => isRuntimeMetric(m))
  const showPerfTab =
    taskCapabilities.includes('runtime') && (!taskInProgress || hasRuntimeData)
  const showScenarioTab =
    taskCapabilities.includes('scenario') && !taskInProgress
  const showConsistencyTab =
    taskCapabilities.includes('checkpoint') && (!taskInProgress || (detail.checkpoint_report?.runs?.length ?? 0) > 0)
  // Trace 物化告警：失败/不完整/近期反馈时显示
  const failedSyncCount = (detail.agent_run_count ?? 0) > 0 ? (detail.sync_summary?.failed ?? 0) : 0
  const incompleteSyncCount = (detail.agent_run_count ?? 0) > 0 ? (detail.sync_summary?.synced_incomplete ?? 0) : 0
  const showSyncAlert = failedSyncCount > 0 || incompleteSyncCount > 0 || retryNotice != null
  // 成功反馈（无失败残留）6 秒后自动消失，避免常驻
  useEffect(() => {
    if (retryNotice && retryNoticeKind === 'success' && failedSyncCount === 0 && incompleteSyncCount === 0) {
      const t = setTimeout(() => setRetryNotice(null), 6000)
      return () => clearTimeout(t)
    }
  }, [retryNotice, retryNoticeKind, failedSyncCount, incompleteSyncCount])
  const mainTabs = [
    { id: 'quality' as const, label: '质量指标' },
    ...(showConsistencyTab ? [{ id: 'consistency' as const, label: '过程一致性' }] : []),
    ...(showPerfTab
      ? [{
          id: 'perf' as const,
          label: isStabilityTask ? '运行效能 · 稳定性' : '运行效能',
        }]
      : []),
    ...(showScenarioTab ? [{ id: 'scenario' as const, label: '场景子步骤' }] : []),
  ]
  const availableVerdictTabs = new Set(mainTabs.map(tab => tab.id))

  return (
    <div
      className={variant === 'page' ? 'eval-detail-page-overlay' : 'modal-overlay'}
      style={variant === 'page' ? undefined : { zIndex: 1000 }}
      onClick={variant === 'page' ? undefined : onClose}
    >
      <div
        className={variant === 'page' ? 'eval-detail-page-content' : 'modal-content'}
        style={
          variant === 'page'
            ? {
                width: '100%',
                maxWidth: '1400px',
                margin: '0 auto',
                padding: 0,
                display: 'flex',
                flexDirection: 'column',
              }
            : {
                maxWidth: '95vw',
                width: '1400px',
                maxHeight: '90vh',
                overflow: 'hidden',
                padding: 0,
                display: 'flex',
                flexDirection: 'column',
              }
        }
        onClick={variant === 'page' ? undefined : e => e.stopPropagation()}
      >
        {/* Header — 固定，滚动时关闭按钮始终可见 */}
        <div style={{ 
          display: 'flex', 
          justifyContent: 'space-between', 
          alignItems: 'center',
          padding: '16px 24px',
          borderBottom: '1px solid var(--edp-border)',
          background: 'linear-gradient(135deg, var(--edp-primary) 0%, var(--edp-primary) 100%)',
          color: 'white',
          flexShrink: 0,
        }}>
          <h2 style={{ margin: 0, fontSize: 'var(--edp-font-2xl)', display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
                <line x1="16" y1="13" x2="8" y2="13"/>
                <line x1="16" y1="17" x2="8" y2="17"/>
              </svg>
              评估结果详情
              {(detail.task.inference_rounds ?? 1) > 1 && (
                <span
                  style={{
                    fontSize: 'var(--edp-font-base)',
                    fontWeight: 600,
                    padding: '4px 10px',
                    borderRadius: 'var(--edp-radius-sm)',
                    background: 'rgba(255,255,255,0.25)',
                    border: '1px solid rgba(255,255,255,0.4)',
                  }}
                >
                  稳定性评测 · 每题 {detail.task.inference_rounds} 轮
                </span>
              )}
            </h2>
          <button 
            onClick={onClose}
            style={{ 
              background: 'rgba(255,255,255,0.2)', 
              border: 'none', 
              color: 'white',
              fontSize: 24,
              cursor: 'pointer',
              padding: '4px 12px',
              borderRadius: 'var(--edp-radius-sm)'
            }}
          >
            ×
          </button>
        </div>

        <div
          className="eval-detail-modal-body"
          style={
            variant === 'page'
              ? {
                  overflowX: 'hidden',
                }
              : {
                  flex: 1,
                  minHeight: 0,
                  overflowY: 'auto',
                  overflowX: 'hidden',
                }
          }
        >
          {taskInProgress && (
            <div className="eval-partial-banner" role="status">
              任务进行中 · 仅展示已落库结果；汇总、场景子步骤与终态总分可能在任务完成后才完整。
            </div>
          )}

          {/* Trace 物化告警横条：失败/不完整/近期反馈时显示 */}
          {showSyncAlert && (
            <div
              role="status"
              style={{
                padding: '8px 24px',
                borderBottom: '1px solid var(--edp-border)',
                background: failedSyncCount > 0 ? 'var(--edp-amber-soft)' : (incompleteSyncCount > 0 ? 'var(--edp-amber-soft)' : 'var(--edp-green-soft)'),
                display: 'flex',
                alignItems: 'center',
                gap: 10,
              }}
            >
              {failedSyncCount > 0 ? (
                <>
                  <span style={{ fontSize: 'var(--edp-font-lg)', lineHeight: 1 }}>⚠</span>
                  <span style={{ fontSize: 'var(--edp-font-md)', color: 'var(--edp-red)', fontWeight: 500 }}>
                    Trace 物化失败 {failedSyncCount} 条 · 影响过程一致性维度与推理轨迹展示
                  </span>
                  {detail?.sync_failure_categories && Object.keys(detail.sync_failure_categories).length > 0 && (
                    <span style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                      {Object.entries(detail.sync_failure_categories).map(([code, count]) => {
                        const meta = FAILURE_CATEGORY_META[code] || FAILURE_CATEGORY_META.unknown
                        return (
                          <span
                            key={code}
                            title={meta.label}
                            style={{ padding: '1px 6px', borderRadius: 'var(--edp-radius-sm)', fontSize: 'var(--edp-font-sm)', background: meta.bg, color: meta.color, whiteSpace: 'nowrap' }}
                          >
                            {count} {meta.label}
                          </span>
                        )
                      })}
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={handleRetrySync}
                    disabled={retryingSync}
                    title={
                      getFailureBreakdown(detail?.sync_failure_categories).nonRetryable > 0
                        ? `其中 ${getFailureBreakdown(detail?.sync_failure_categories).nonRetryable} 条为永久性失败（重试无意义）`
                        : undefined
                    }
                    style={{
                      fontSize: 'var(--edp-font-base)',
                      padding: '3px 12px',
                      borderRadius: 'var(--edp-radius-sm)',
                      border: '1px solid var(--edp-red-soft)',
                      background: 'var(--edp-red-soft)',
                      color: 'var(--edp-red)',
                      cursor: retryingSync ? 'not-allowed' : 'pointer',
                      opacity: retryingSync ? 0.6 : 1,
                    }}
                  >
                    {retryingSync ? '重试中…' : '重试'}
                  </button>
                  {retryNotice && (
                    <span style={{ fontSize: 'var(--edp-font-base)', color: retryNoticeKind === 'error' ? 'var(--edp-red)' : 'var(--edp-green)' }}>
                      {retryNotice}
                    </span>
                  )}
                </>
              ) : incompleteSyncCount > 0 ? (
                <>
                  <span style={{ fontSize: 'var(--edp-font-lg)', lineHeight: 1 }}>⚠</span>
                  <span style={{ fontSize: 'var(--edp-font-md)', color: 'var(--edp-amber)', fontWeight: 500 }}>
                    Trace 物化不完整 {incompleteSyncCount} 条 · 过程一致性维度数据可能缺失，重试可重新拉取
                  </span>
                  <button
                    type="button"
                    onClick={handleRetrySync}
                    disabled={retryingSync}
                    style={{
                      fontSize: 'var(--edp-font-base)',
                      padding: '3px 12px',
                      borderRadius: 'var(--edp-radius-sm)',
                      border: '1px solid var(--edp-amber-soft)',
                      background: 'var(--edp-amber-soft)',
                      color: 'var(--edp-amber)',
                      cursor: retryingSync ? 'not-allowed' : 'pointer',
                      opacity: retryingSync ? 0.6 : 1,
                    }}
                  >
                    {retryingSync ? '重试中…' : '重试'}
                  </button>
                  {retryNotice && (
                    <span style={{ fontSize: 'var(--edp-font-base)', color: retryNoticeKind === 'error' ? 'var(--edp-red)' : 'var(--edp-green)' }}>
                      {retryNotice}
                    </span>
                  )}
                </>
              ) : (
                <span style={{ fontSize: 'var(--edp-font-md)', color: retryNoticeKind === 'error' ? 'var(--edp-red)' : 'var(--edp-green)' }}>
                  {retryNotice}
                </span>
              )}
            </div>
          )}

        {/* Task Overview */}
        <div style={{ padding: '16px 24px', borderBottom: '1px solid var(--edp-border)', background: 'var(--edp-bg)' }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 'var(--edp-space-4)' }}>
            <div>
              <span style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>任务ID</span>
              <div style={{ fontFamily: 'monospace', fontSize: 'var(--edp-font-lg)' }}>{detail.task.id}</div>
            </div>
            <div>
              <span style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>数据集</span>
              <div style={{ fontSize: 'var(--edp-font-lg)' }}>{detail.task.dataset_name || detail.task.dataset_id}</div>
            </div>
            <div>
              <span style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>Agent</span>
              <div style={{ fontSize: 'var(--edp-font-lg)' }}>{detail.task.agent_name || '-'}</div>
            </div>
            <div>
              <span style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>评估模型</span>
              <div style={{ fontSize: 'var(--edp-font-lg)' }}>{detail.task.llm_model_name || detail.task.llm_model}</div>
            </div>
            <div>
              <span style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>被测推理模型</span>
              <div
                style={{ fontSize: 'var(--edp-font-lg)', ...(isOfflineTask ? offlineNaTextStyle : {}) }}
                title={
                  isOfflineTask
                    ? OFFLINE_NA_TOOLTIP
                    : detail.task.inference_model_summary?.labels?.join('\n') || undefined
                }
              >
                {isOfflineTask
                  ? OFFLINE_NA_LABEL
                  : detail.task.inference_model_summary?.display || '未记录'}
              </div>
            </div>
            <div>
              <span style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>Thinking 模式</span>
              <div
                style={{ fontSize: 'var(--edp-font-lg)', ...(isOfflineTask ? offlineNaTextStyle : {}) }}
                title={
                  isOfflineTask
                    ? OFFLINE_NA_TOOLTIP
                    : detail.task.execution_mode_summary?.labels?.join('\n') || undefined
                }
              >
                {isOfflineTask
                  ? OFFLINE_NA_LABEL
                  : detail.task.execution_mode_summary?.display || '未记录'}
              </div>
            </div>
            <div>
              <span style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>创建时间</span>
              <div style={{ fontSize: 'var(--edp-font-lg)' }}>{formatDate(detail.task.created_at)}</div>
            </div>
            <div>
              <span style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>完成时间</span>
              <div style={{ fontSize: 'var(--edp-font-lg)' }}>{formatDate(detail.task.completed_at)}</div>
            </div>
            <div>
              <span style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>Agent 运行次数</span>
              <div
                style={{ fontSize: 'var(--edp-font-lg)', ...(isOfflineTask ? offlineNaTextStyle : {}) }}
                title={isOfflineTask ? OFFLINE_NA_TOOLTIP : undefined}
              >
                {isOfflineTask ? (
                  OFFLINE_NA_LABEL
                ) : (
                  <>
                    {detail.task.inference_rounds ?? 1} 轮/题
                    {(detail.task.inference_rounds ?? 1) > 1 && (
                      <span
                        style={{
                          marginLeft: 6,
                          fontSize: 'var(--edp-font-xs)',
                          fontWeight: 600,
                          padding: '2px 8px',
                          borderRadius: 'var(--edp-radius-sm)',
                          background: 'var(--edp-blue-soft)',
                          color: 'var(--edp-blue)',
                        }}
                      >
                        稳定性模式
                      </span>
                    )}
                  </>
                )}
              </div>
            </div>
            <div>
              <span style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>LLM Judge</span>
              <div style={{ fontSize: 'var(--edp-font-lg)' }}>
                {isStabilityTask
                  ? `每题每指标 ${detail.task.inference_rounds ?? 1} 轮`
                  : '每题每指标 1 次'}
                {(detail.task.run_count ?? 1) > 1 && (
                  <span style={{ marginLeft: 6, fontSize: 'var(--edp-font-sm)', color: 'var(--edp-text3)' }}>
                    （历史任务曾配置 {detail.task.run_count} 次取平均）
                  </span>
                )}
              </div>
            </div>
            <div>
              <span style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>状态</span>
              <div style={{ fontSize: 'var(--edp-font-lg)' }}>{detail.task.status}</div>
            </div>
            <div>
              <span style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>评估维度</span>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--edp-space-1)', marginTop: 2 }}>
                {[
                  { label: '质量', on: detail.task.dimension_flags?.quality ?? true },
                  { label: '场景子步骤', on: detail.task.dimension_flags?.scenario ?? true },
                  { label: '过程一致性', on: detail.task.dimension_flags?.process_consistency ?? true },
                  { label: '运行稳定性', on: detail.task.dimension_flags?.stability ?? true },
                ].filter(d => d.on).map(d => (
                  <span
                    key={d.label}
                    title={d.on ? '已勾选' : '未勾选'}
                    style={{
                      fontSize: 'var(--edp-font-xs)',
                      fontWeight: 500,
                      padding: '2px 8px',
                      borderRadius: 'var(--edp-radius-sm)',
                      ...(d.on
                        ? { background: 'var(--edp-blue-soft)', color: 'var(--edp-blue)' }
                        : { background: 'var(--edp-bg)', color: 'var(--edp-text3)', opacity: 0.7 }),
                    }}
                  >
                    {d.label}
                  </span>
                ))}
              </div>
            </div>
          </div>
          {(detail.agent_run_count ?? 0) > 0 && detail.sync_summary && showPerfTab && (
            <div style={{ marginTop: 'var(--edp-space-3)', display: 'flex', flexWrap: 'wrap', gap: 'var(--edp-space-2)', alignItems: 'center' }}>
              <span style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>Trace 物化：</span>
              {detail.fact_materialization_status && (
                <span
                  style={{
                    fontSize: 'var(--edp-font-base)',
                    padding: '2px 8px',
                    borderRadius: 'var(--edp-radius-sm)',
                    background:
                      detail.fact_materialization_status === 'complete'
                        ? 'var(--edp-green-soft)'
                        : detail.fact_materialization_status === 'in_progress'
                          ? 'var(--edp-amber-soft)'
                          : 'var(--edp-bg)',
                    color: 'var(--edp-text)',
                  }}
                >
                  {detail.fact_materialization_status}
                </span>
              )}
              {Object.entries(detail.sync_summary).map(([status, count]) => (
                <span key={status} style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--edp-space-1)' }}>
                  <SyncStatusBadge status={status} />
                  <span style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)' }}>×{count}</span>
                </span>
              ))}
            </div>
          )}
        </div>

        {/* Quality Score */}
        {detail.quality_score && (
          <div style={{ padding: '16px 24px', borderBottom: '1px solid var(--edp-border)', background: 'var(--edp-card)' }}>
            <h3 style={{ margin: '0 0 12px 0', fontSize: 16 }}>质量总分</h3>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 'var(--edp-space-3)' }}>
              <div style={{ border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', padding: 'var(--edp-space-3)', background: 'var(--edp-bg)' }}>
                <div style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>总得分</div>
                <div style={{ fontSize: 24, fontWeight: 700, color: 'var(--edp-primary)' }}>
                  {formatScore(detail.quality_score.total_score)}
                </div>
                <div style={{ color: 'var(--edp-text3)', fontSize: 'var(--edp-font-sm)' }}>
                  {buildQualityWeightTooltip({
                    result_weight: detail.quality_score.result_weight,
                    process_weight: detail.quality_score.process_weight,
                    stability_weight: detail.quality_score.stability_weight,
                    isOffline: isOfflineTask,
                    inference_rounds: detail.task.inference_rounds,
                    processConsistencyEnabled: detail.task.dimension_flags?.process_consistency,
                    stabilityEnabled: detail.task.dimension_flags?.stability,
                  })}
                </div>
              </div>
              <div style={{ border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', padding: 'var(--edp-space-3)' }}>
                <div style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>结果得分</div>
                <div style={{ fontSize: 22, fontWeight: 600 }}>{formatScore(detail.quality_score.result_score)}</div>
                <div style={{ color: 'var(--edp-text3)', fontSize: 'var(--edp-font-sm)' }}>LLM/脚本指标（正确性等）</div>
              </div>
              {!isOfflineTask && (
              <div style={{ border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', padding: 'var(--edp-space-3)' }}>
                <div style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>过程得分</div>
                <div style={{ fontSize: 22, fontWeight: 600 }}>{formatScore(detail.quality_score.process_score)}</div>
                <div style={{ color: 'var(--edp-text3)', fontSize: 'var(--edp-font-sm)' }}>
                  过程一致性 · 全题全轮聚合（
                  <button
                    type="button"
                    onClick={openConsistencyTab}
                    style={{
                      fontSize: 'var(--edp-font-sm)',
                      color: 'var(--edp-primary)',
                      background: 'none',
                      border: 'none',
                      cursor: 'pointer',
                      textDecoration: 'underline',
                      padding: 0,
                    }}
                  >
                    查看必达命中率等明细 →
                  </button>
                  ）
                </div>
              </div>
              )}
              {detail.quality_score.stability_score != null && (
              <div style={{ border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', padding: 'var(--edp-space-3)' }}>
                <div style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>稳定性得分</div>
                <div style={{ fontSize: 22, fontWeight: 600 }}>{formatScore(detail.quality_score.stability_score)}</div>
                <div style={{ color: 'var(--edp-text3)', fontSize: 'var(--edp-font-sm)' }}>多轮一致性（通过率/波动/链条）</div>
              </div>
              )}
            </div>
          </div>
        )}

        {detail.dimension_verdicts && (
          <div style={{ padding: '0 24px', borderBottom: '1px solid var(--edp-border)', paddingTop: 16, paddingBottom: 16 }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                marginBottom: 10,
                gap: 'var(--edp-space-3)',
              }}
            >
              <span style={{ fontSize: 'var(--edp-font-md)', fontWeight: 600, color: 'var(--edp-text)' }}>四维结论</span>
            </div>
            <DimensionVerdictBar
              data={detail.dimension_verdicts}
              onNavigateTab={navigateFromVerdict}
              availableTabs={availableVerdictTabs}
            />
          </div>
        )}

        {/* 评测工作台主 Tab */}
        <div style={{ padding: '0 24px', borderBottom: '1px solid var(--edp-border)' }}>
          <div style={{ display: 'flex', gap: 0 }}>
            {(
              mainTabs
            ).map(tab => (
              <button
                key={tab.id}
                type="button"
                onClick={() => setMainTab(tab.id)}
                style={{
                  padding: '12px 20px',
                  border: 'none',
                  borderBottom: mainTab === tab.id ? '3px solid var(--edp-primary)' : '3px solid transparent',
                  background: 'transparent',
                  cursor: 'pointer',
                  fontSize: 'var(--edp-font-lg)',
                  fontWeight: mainTab === tab.id ? 600 : 400,
                  color: mainTab === tab.id ? 'var(--edp-primary)' : 'var(--edp-text2)',
                }}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>

        {mainTab === 'scenario' && (
          <ScenarioEvalPanel
            taskId={taskId}
            langfuseTraceReady={taskAgentLangfuseReady}
            scenarioVerdict={detail.dimension_verdicts?.scenario_delivery ?? null}
          />
        )}

        {mainTab === 'perf' && (
          <RuntimePerformanceReport
            key={perfReportRevision}
            taskId={taskId}
            deepLink={perfDeepLink}
            onDeepLinkConsumed={handlePerfDeepLinkConsumed}
            onOpenRuntimeTargets={() => void openRuntimeTargetEditor()}
            onOpenCheckpointCompliance={openConsistencyTab}
            onOpenQualityForQuestion={openQualityForQuestion}
            runtimeStabilityVerdict={detail.dimension_verdicts?.runtime_stability ?? null}
          />
        )}

        {mainTab === 'quality' && (
          <>
        {detail.dimension_verdicts?.quality &&
          detail.dimension_verdicts.quality.status !== 'na' && (
            <div style={{ padding: '16px 24px 0' }}>
              <DimensionVerdictHeadline
                title="质量指标结论"
                status={detail.dimension_verdicts.quality.status}
                headline={detail.dimension_verdicts.quality.headline}
                scoreLine={
                  detail.dimension_verdicts.quality.score != null
                    ? `结果分 ${formatScore(detail.dimension_verdicts.quality.score)}`
                    : null
                }
              />
            </div>
          )}
        {/* Metrics Summary */}
        <div style={{ padding: '16px 24px', borderBottom: '1px solid var(--edp-border)' }}>
          <h3 style={{ margin: '0 0 4px 0', fontSize: 16, display: 'flex', alignItems: 'center', gap: '8px' }}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="18" y1="20" x2="18" y2="10"/>
              <line x1="12" y1="20" x2="12" y2="4"/>
              <line x1="6" y1="20" x2="6" y2="14"/>
            </svg>
            质量指标概览
          </h3>
          <p style={{ margin: '0 0 12px', fontSize: 'var(--edp-font-base)', color: 'var(--edp-text3)' }}>
            以下为答案内容质量（与过程点、场景子步骤相互独立）
          </p>
          {qualityMetrics.length === 0 ? (
            <p style={{ margin: 0, fontSize: 'var(--edp-font-md)', color: 'var(--edp-text3)' }}>
              本任务未选择正确性、相关性、忠诚度等质量评测指标。
            </p>
          ) : (
            <table className="results-table" style={{ fontSize: 'var(--edp-font-md)' }}>
              <thead>
                <tr>
                  <th>指标名称</th>
                  <th>平均得分</th>
                  <th>通过率</th>
                  <th>阈值</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
              {qualityMetrics.map(metric => (
                <tr key={metric.metric_name}>
                  <td style={{ fontWeight: 500 }}>
                    {metric.eval_method === 'script' ? (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                        <span style={{ 
                          background: 'linear-gradient(135deg, var(--edp-primary), var(--edp-primary))',
                          color: 'var(--edp-card)', 
                          padding: '2px 8px', 
                          borderRadius: 'var(--edp-radius-sm)', 
                          fontSize: 'var(--edp-font-sm)',
                          fontWeight: 600
                        }}>
                          🐍 脚本评估
                        </span>
                        {metricLabel(metric.metric_name)}
                      </span>
                    ) : (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                        <span style={{ 
                          background: 'var(--edp-blue)',
                          color: 'var(--edp-card)', 
                          padding: '2px 8px', 
                          borderRadius: 'var(--edp-radius-sm)', 
                          fontSize: 'var(--edp-font-sm)',
                          fontWeight: 600
                        }}>
                          LLM评估
                        </span>
                        {metricLabel(metric.metric_name)}
                      </span>
                    )}
                  </td>
                  <td>{metric.avg_score != null ? metric.avg_score.toFixed(3) : '—'}</td>
                  <td>
                    {metric.pass_rate != null
                      ? `${metric.pass_rate.toFixed(1)}% (${metric.passed_count}/${metric.scored_count ?? metric.total_count})`
                      : `— (${metric.passed_count}/${metric.scored_count ?? metric.total_count})`}
                    {metric.skipped_count != null && metric.skipped_count > 0 && (
                      <span style={{ color: 'var(--edp-text2)', marginLeft: 'var(--edp-space-1)' }}>
                        跳过{metric.skipped_count}
                      </span>
                    )}
                  </td>
                  <td>{metric.threshold}</td>
                  <td>
                    {metric.pass_rate == null && (metric.skipped_count ?? 0) > 0 ? (
                      <span style={{ color: 'var(--edp-text2)' }}>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--edp-text2)" strokeWidth="2" style={{ verticalAlign: 'middle' }}>
                          <line x1="6" y1="18" x2="18" y2="6"/>
                          <line x1="6" y1="6" x2="18" y2="18"/>
                        </svg>
                        {' '}已跳过
                      </span>
                    ) : (
                    <span style={{
                      color: metric.pass_rate != null && metric.pass_rate >= 80 ? 'var(--edp-green)' : metric.pass_rate != null && metric.pass_rate >= 60 ? 'var(--edp-amber)' : 'var(--edp-red)'
                    }}>
                      {metric.pass_rate != null && metric.pass_rate >= 80 ? (
                      <>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--edp-green)" strokeWidth="2">
                          <polyline points="20 6 9 17 4 12"/>
                        </svg>
                        通过
                      </>
                    ) : metric.pass_rate != null && metric.pass_rate >= 60 ? (
                      <>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--edp-amber)" strokeWidth="2">
                          <circle cx="12" cy="12" r="10"/>
                          <line x1="12" y1="8" x2="12" y2="12"/>
                          <line x1="12" y1="16" x2="12.01" y2="16"/>
                        </svg>
                        部分通过
                      </>
                    ) : (
                      <>
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--edp-red)" strokeWidth="2">
                          <circle cx="12" cy="12" r="10"/>
                          <line x1="15" y1="9" x2="9" y2="15"/>
                          <line x1="9" y1="9" x2="15" y2="15"/>
                        </svg>
                        未通过
                      </>
                    )}
                    </span>
                    )}
                  </td>
                </tr>
              ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Metric Tabs */}
        <div style={{ padding: '0 24px', borderBottom: '1px solid var(--edp-border)' }}>
          <div style={{ display: 'flex', gap: 0 }}>
            {qualityMetrics.map(metric => (
              <button
                key={metric.metric_name}
                onClick={() => setActiveMetric(metric.metric_name)}
                style={{
                  padding: '12px 24px',
                  border: 'none',
                  borderBottom: activeMetric === metric.metric_name ? '3px solid var(--edp-primary)' : '3px solid transparent',
                  background: activeMetric === metric.metric_name && metric.eval_method === 'script'
                    ? 'rgba(111,66,193,0.08)' : 'transparent',
                  cursor: 'pointer',
                  fontSize: 'var(--edp-font-lg)',
                  fontWeight: activeMetric === metric.metric_name ? 600 : 400,
                  color: activeMetric === metric.metric_name 
                    ? (metric.eval_method === 'script' ? 'var(--edp-primary)' : 'var(--edp-primary)') 
                    : 'var(--edp-text2)'
                }}
              >
                {metric.eval_method === 'script' ? (
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--edp-space-1)' }}>
                    🐍 {metricLabel(metric.metric_name)}
                  </span>
                ) : metricLabel(metric.metric_name)}
              </button>
            ))}
          </div>
        </div>

        {/* Question Results */}
        {currentMetricData ? (
        <div style={{ padding: '16px 24px', maxHeight: variant === 'page' ? undefined : '50vh', overflow: 'auto' }}>
          <h3 style={{ margin: '0 0 12px 0', fontSize: 16, display: 'flex', alignItems: 'center', gap: '8px' }}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
              <line x1="16" y1="13" x2="8" y2="13"/>
            </svg>
            单题判分明细
          </h3>
          {multiRoundGroups && (
            <div style={{ marginBottom: 'var(--edp-space-4)', border: '1px solid var(--edp-primary-soft)', borderRadius: 'var(--edp-radius-sm)', overflow: 'hidden' }}>
              <div style={{ padding: '10px 14px', background: 'var(--edp-primary-soft)', fontSize: 'var(--edp-font-md)', fontWeight: 600, color: 'var(--edp-primary)' }}>
                多轮质量指标聚合
              </div>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 'var(--edp-font-md)' }}>
                <thead>
                  <tr style={{ background: 'var(--edp-primary-soft)', textAlign: 'left' }}>
                    <th style={{ padding: 'var(--edp-space-2)' }}>题号</th>
                    <th style={{ padding: 'var(--edp-space-2)', textAlign: 'right' }}>轮次</th>
                    <th style={{ padding: 'var(--edp-space-2)', textAlign: 'right' }}>均值</th>
                    <th style={{ padding: 'var(--edp-space-2)', textAlign: 'right' }}>通过率</th>
                    <th style={{ padding: 'var(--edp-space-2)', textAlign: 'right' }}>最低分</th>
                    <th style={{ padding: 'var(--edp-space-2)', textAlign: 'right' }}>标准差</th>
                    <th style={{ padding: 'var(--edp-space-2)' }}>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {[...multiRoundGroups.entries()].map(([qid, rounds]) => {
                    const scores = rounds.map(r => r.avg_score).filter(s => s != null) as number[]
                    const passCount = rounds.filter(r => r.passed).length
                    const mean = scores.length ? scores.reduce((a, b) => a + b, 0) / scores.length : null
                    const minScore = scores.length ? Math.min(...scores) : null
                    const std = scores.length >= 2
                      ? Math.sqrt(scores.map(s => (s - mean!) ** 2).reduce((a, b) => a + b, 0) / scores.length)
                      : 0
                    const passRate = scores.length ? passCount / scores.length : null
                    const isFail = passRate != null && passRate < 0.6
                    const isWarn = passRate != null && passRate >= 0.6 && passRate < 0.8
                    return (
                      <tr key={qid}>
                        <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)' }}>#{qid}</td>
                        <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)', textAlign: 'right' }}>{rounds.length}</td>
                        <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)', textAlign: 'right' }}>{mean != null ? mean.toFixed(3) : '—'}</td>
                        <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)', textAlign: 'right', fontWeight: 600, color: isFail ? 'var(--edp-red)' : isWarn ? 'var(--edp-amber)' : 'var(--edp-green)' }}>
                          {passRate != null ? `${(passRate * 100).toFixed(0)}%` : '—'}
                          <span style={{ fontWeight: 400, color: 'var(--edp-text3)' }}> ({passCount}/{rounds.length})</span>
                        </td>
                        <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)', textAlign: 'right', color: minScore != null && minScore < 0.5 ? 'var(--edp-red)' : undefined }}>
                          {minScore != null ? minScore.toFixed(3) : '—'}
                        </td>
                        <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)', textAlign: 'right', color: std > 0.15 ? 'var(--edp-red)' : undefined }}>
                          {std.toFixed(4)}
                        </td>
                        <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)' }}>
                          <span style={{
                            fontSize: 'var(--edp-font-sm)', fontWeight: 600, padding: '2px 8px', borderRadius: 'var(--edp-radius-sm)',
                            background: isFail ? 'var(--edp-red-soft)' : isWarn ? 'var(--edp-amber-soft)' : 'var(--edp-green-soft)',
                            color: isFail ? 'var(--edp-red)' : isWarn ? 'var(--edp-amber)' : 'var(--edp-green)',
                          }}>
                            {isFail ? '未达标' : isWarn ? '需关注' : '达标'}
                          </span>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
          {currentMetricData && (() => {
            const allResults = currentMetricData.results
            const totalPages = Math.ceil(allResults.length / qualityPageSize)
            const pageStart = qualityPage * qualityPageSize
            const pageResults = allResults.slice(pageStart, pageStart + qualityPageSize)
            return (
            <>
            {pageResults.map((result, _pi) => {
              const idx = pageStart + _pi
            return (
            <div 
              key={result.result_id}
              id={`quality-question-${result.question_id}`}
              style={{ 
                border: '1px solid var(--edp-border)', 
                borderRadius: 'var(--edp-radius-sm)', 
                marginBottom: 'var(--edp-space-3)',
                overflow: 'hidden'
              }}
            >
              {/* Question Header */}
              <div 
                onClick={() => toggleQuestion(result.question_id)}
                style={{ 
                  padding: '12px 16px',
                  background: expandedQuestions.has(result.question_id) ? 'var(--edp-primary-soft)' : 'var(--edp-card)',
                  cursor: 'pointer',
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center'
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-3)' }}>
                  <span style={{ 
                    background: 'var(--edp-primary)', 
                    color: 'white', 
                    padding: '2px 8px', 
                    borderRadius: 'var(--edp-radius-sm)',
                    fontSize: 'var(--edp-font-base)'
                  }}>
                    #{idx + 1}
                  </span>
                  <span style={{ 
                    maxWidth: 600, 
                    overflow: 'hidden', 
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                    color: 'var(--edp-text)'
                  }}>
                    {detail.questions?.[String(result.question_id)]?.question || '(No Question Content)'}
                  </span>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-4)' }}>
                  <span style={{
                    padding: '4px 12px',
                    borderRadius: 'var(--edp-radius-sm)',
                    background: result.avg_score == null ? 'var(--edp-border-soft)' : (result.passed ? 'var(--edp-green-soft)' : 'var(--edp-red-soft)'),
                    color: result.avg_score == null ? 'var(--edp-text2)' : (result.passed ? 'var(--edp-green)' : 'var(--edp-red)'),
                    fontSize: 'var(--edp-font-md)'
                  }}>
                    {result.avg_score == null
                      ? '已跳过'
                      : `${result.avg_score.toFixed(3)} | ${result.passed ? '✓ 通过' : '✗ 未通过'}`}
                  </span>
                  <span style={{ color: 'var(--edp-text3)' }}>{expandedQuestions.has(result.question_id) ? '▼' : '▶'}</span>
                </div>
              </div>

              {/* Question Details */}
              {expandedQuestions.has(result.question_id) && (
                <div style={{ padding: '16px', background: 'var(--edp-bg)', borderTop: '1px solid var(--edp-border)' }}>
                  {/* LLM Judge */}
                  <div>
                    {(() => {
                      // 多轮汇总模式：从 multiRoundGroups 取该题所有轮次数据
                      const roundResults = multiRoundGroups?.get(result.question_id)
                      if (roundResults && roundResults.length > 1) {
                        // Phase 5: 跳过的轮次(avg_score == null)不计入均分/通过率/波动等统计
                        const scoredResults = roundResults.filter(r => r.avg_score != null)
                        const skippedCount = roundResults.length - scoredResults.length
                        const scores = scoredResults.map(r => r.avg_score as number)
                        const passCount = scoredResults.filter(r => r.passed).length
                        const mean = scores.length > 0 ? scores.reduce((a, b) => a + b, 0) / scores.length : 0
                        const std = scores.length > 0 ? Math.sqrt(scores.reduce((s, v) => s + (v - mean) ** 2, 0) / scores.length) : 0
                        const minScore = scores.length > 0 ? Math.min(...scores) : 0
                        const maxScore = scores.length > 0 ? Math.max(...scores) : 0
                        const passRate = scoredResults.length > 0 ? (passCount / scoredResults.length) * 100 : 0
                        const hasScored = scoredResults.length > 0

                        return (
                          <>
                            <div style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)', marginBottom: 'var(--edp-space-2)' }}>
                              LLM Judge 评分（汇总 · {roundResults.length} 轮{skippedCount > 0 ? `，已跳过 ${skippedCount} 轮` : ''}）
                            </div>
                            <div style={{ border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', background: 'var(--edp-card)', overflow: 'hidden' }}>
                              {/* 汇总统计栏 */}
                              <div style={{
                                padding: '10px 14px',
                                display: 'flex',
                                flexWrap: 'wrap',
                                gap: '8px 24px',
                                alignItems: 'center',
                                background: 'var(--edp-bg)',
                                borderBottom: '1px solid var(--edp-border)',
                                fontSize: 'var(--edp-font-md)',
                              }}>
                                <span>均分: <strong style={{ color: 'var(--edp-blue)' }}>{hasScored ? mean.toFixed(3) : '—'}</strong></span>
                                <span>通过率: <strong style={{ color: hasScored && passRate >= 60 ? 'var(--edp-green)' : 'var(--edp-red)' }}>{hasScored ? `${passRate.toFixed(0)}%` : '—'}</strong></span>
                                <span>波动σ: <strong style={{ color: std > 0.15 ? 'var(--edp-amber)' : 'var(--edp-text2)' }}>{hasScored ? std.toFixed(3) : '—'}</strong></span>
                                <span>最低分: <strong style={{ color: minScore < 0.5 ? 'var(--edp-red)' : 'var(--edp-text2)' }}>{hasScored ? minScore.toFixed(3) : '—'}</strong></span>
                                <span>最高分: <strong style={{ color: 'var(--edp-text2)' }}>{hasScored ? maxScore.toFixed(3) : '—'}</strong></span>
                              </div>
                            </div>
                          </>
                        )
                      }

                      // 单轮模式：保持原有 Judge 卡片
                      const round = result.rounds[0]
                      return (
                        <>
                          <div style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)', marginBottom: 'var(--edp-space-2)' }}>
                            LLM Judge 评分
                            {result.rounds.length > 1
                              ? `（历史任务 ${result.rounds.length} 次取平均）`
                              : ''}
                          </div>
                          <div style={{ border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', background: 'var(--edp-card)', overflow: 'hidden' }}>
                            <div style={{
                              padding: '8px 12px',
                              display: 'flex',
                              justifyContent: 'space-between',
                              alignItems: 'center',
                              borderBottom: '1px solid var(--edp-border)',
                              background: 'var(--edp-bg)',
                            }}>
                              <span style={{ fontWeight: 500 }}>Judge 结果</span>
                              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-4)' }}>
                                <span>得分: <strong>{round?.score?.toFixed(3) || '-'}</strong></span>
                                {round?.duration_ms != null && (
                                  <span style={{ color: 'var(--edp-text2)' }}>耗时: {formatDuration(round.duration_ms)}</span>
                                )}
                              </div>
                            </div>
                            <div style={{ padding: 'var(--edp-space-3)' }}>
                              <div style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)', marginBottom: 'var(--edp-space-1)' }}>评估分析</div>
                              <div style={{
                                padding: 'var(--edp-space-3)',
                                background: 'var(--edp-bg)',
                                borderRadius: 'var(--edp-radius-sm)',
                                border: '1px solid var(--edp-border)',
                                whiteSpace: 'pre-wrap',
                                lineHeight: 1.6,
                              }}>
                                {round?.reason || '(无评估分析内容)'}
                              </div>
                            </div>
                          </div>
                        </>
                      )
                    })()}
                  </div>

                  {/* Predict + 逐轮对比 */}
                  <div style={{ marginBottom: 'var(--edp-space-4)' }}>
                    {multiRoundGroups && multiRoundGroups.has(result.question_id) ? (
                      <>
                        <div style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)', marginBottom: 'var(--edp-space-1)' }}>Agent推理结果 · 逐轮对比</div>
                        {multiRoundGroups.get(result.question_id)!.map(roundResult => {
                          const answerExpanded = expandedRoundAnswers.has(roundResult.result_id)
                          return (
                          <div key={roundResult.result_id} style={{ marginBottom: 'var(--edp-space-2)', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', overflow: 'hidden' }}>
                            {/* 轮次头：轮号 + 得分 */}
                            <div style={{
                              padding: '6px 12px',
                              background: 'var(--edp-bg)',
                              display: 'flex',
                              justifyContent: 'space-between',
                              alignItems: 'center',
                              fontSize: 'var(--edp-font-base)',
                            }}>
                              <span style={{ fontWeight: 500 }}>
                                第 {(roundResult.run_index ?? 0) + 1} 轮
                              </span>
                              <span style={{
                                fontWeight: 600,
                                color: 'var(--edp-blue)',
                              }}>
                                {roundResult.avg_score != null ? roundResult.avg_score.toFixed(3) : '—'}
                              </span>
                            </div>
                            {/* Judge 点评（始终显示） */}
                            {roundResult.rounds?.[0]?.reason && (
                              <div style={{
                                padding: '8px 12px',
                                background: 'var(--edp-bg)',
                                fontSize: 'var(--edp-font-base)',
                                color: 'var(--edp-text2)',
                                whiteSpace: 'pre-wrap',
                                lineHeight: 1.5,
                                borderBottom: '1px dashed var(--edp-border)',
                              }}>
                                <span style={{ fontWeight: 600, color: 'var(--edp-primary)' }}>
                                  Judge 点评：
                                </span>
                                {roundResult.rounds[0].reason}
                              </div>
                            )}
                            {/* 推理答案（默认收缩） */}
                            <div
                              onClick={() => toggleRoundAnswer(roundResult.result_id)}
                              style={{
                                padding: '6px 12px',
                                cursor: 'pointer',
                                display: 'flex',
                                alignItems: 'center',
                                gap: 'var(--edp-space-2)',
                                fontSize: 'var(--edp-font-base)',
                                color: 'var(--edp-text3)',
                                userSelect: 'none',
                                background: 'var(--edp-card)',
                              }}
                            >
                              <span>{answerExpanded ? '▼' : '▶'}</span>
                              <span>推理答案</span>
                            </div>
                            {answerExpanded && (
                              <div style={{ padding: 10, background: 'var(--edp-card)', whiteSpace: 'pre-wrap', lineHeight: 1.5, fontSize: 'var(--edp-font-md)', maxHeight: 300, overflow: 'auto', borderTop: '1px solid var(--edp-border)' }}>
                                {detail.agent_answers?.[roundResult.agent_run_id ?? ''] || '(无回答内容)'}
                              </div>
                            )}
                          </div>
                          )
                        })}
                      </>
                    ) : (
                      <>
                        <div style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)', marginBottom: 'var(--edp-space-1)' }}>Agent推理结果</div>
                        <div style={{ background: 'var(--edp-card)', padding: 'var(--edp-space-3)', borderRadius: 'var(--edp-radius-sm)', border: '1px solid var(--edp-border)', minHeight: 60, whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>
                          {detail.agent_answers?.[result.agent_run_id ?? ''] || '-'}
                        </div>
                      </>
                    )}
                    {isStabilityTask ? (
                      <p style={{ margin: '8px 0 0', fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)' }}>
                        所有质量指标均已逐轮 Judge。过程是否一致见
                        <button
                          type="button"
                          onClick={openConsistencyTab}
                          style={{
                            fontSize: 'var(--edp-font-base)',
                            color: 'var(--edp-primary)',
                            background: 'none',
                            border: 'none',
                            cursor: 'pointer',
                            textDecoration: 'underline',
                            padding: 0,
                          }}
                        >
                          「过程一致性」
                        </button>
                        ；时延/Token 波动见「效能波动」。
                        <button
                          type="button"
                          onClick={() =>
                            openPerfForRun({
                              questionId: result.question_id,
                              view: 'stability',
                            })
                          }
                          style={{
                            marginLeft: 'var(--edp-space-2)',
                            fontSize: 'var(--edp-font-base)',
                            color: 'var(--edp-primary)',
                            background: 'none',
                            border: 'none',
                            cursor: 'pointer',
                            textDecoration: 'underline',
                          }}
                        >
                          查看效能波动
                        </button>
                      </p>
                    ) : null}
                  </div>

                  {/* Ground Truth */}
                  {(detail.questions?.[String(result.question_id)]?.ground_truth) && (
                    <div style={{ marginBottom: 'var(--edp-space-4)' }}>
                      <div style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)', marginBottom: 'var(--edp-space-1)' }}>标准答案（Ground Truth）</div>
                      <div style={{ background: 'var(--edp-card)', padding: 'var(--edp-space-3)', borderRadius: 'var(--edp-radius-sm)', border: '1px solid var(--edp-border)', minHeight: 60, whiteSpace: 'pre-wrap', lineHeight: 1.5, fontSize: 'var(--edp-font-md)', maxHeight: 300, overflow: 'auto' }}>
                        {(() => {
                          const raw = detail.questions?.[String(result.question_id)]?.ground_truth?.trim() || ''
                          if (!raw) return '-'
                          try {
                            const parsed = JSON.parse(raw)
                            return JSON.stringify(parsed, null, 2)
                          } catch {
                            return raw
                          }
                        })()}
                      </div>
                    </div>
                  )}

                  {(result.inference_duration_ms != null || result.tokens_total != null) && (
                    <div style={{ marginBottom: 'var(--edp-space-4)' }}>
                      <button
                        type="button"
                        onClick={() =>
                          openPerfForRun({
                            questionId: result.question_id,
                            agentRunId: result.agent_run_id ?? undefined,
                          })
                        }
                        style={{
                          fontSize: 'var(--edp-font-base)',
                          padding: '6px 12px',
                          borderRadius: 'var(--edp-radius-sm)',
                          border: '1px solid var(--edp-amber-soft)',
                          background: 'var(--edp-amber-soft)',
                          color: 'var(--edp-amber)',
                          cursor: 'pointer',
                        }}
                      >
                        运行效能 · 题 #{result.question_id + 1}
                        {result.inference_duration_ms != null &&
                          ` · ${formatDuration(result.inference_duration_ms)}`}
                        {result.tokens_total != null &&
                          ` · ${result.tokens_total.toLocaleString()} tokens`}
                        {' →'}
                      </button>
                    </div>
                  )}

                  {/* Summary */}
                  <div style={{ 
                    display: 'flex', 
                    gap: 'var(--edp-space-6)', 
                    padding: 'var(--edp-space-3)', 
                    background: 'var(--edp-card)', 
                    borderRadius: 'var(--edp-radius-sm)',
                    border: '1px solid var(--edp-border)',
                    marginBottom: 'var(--edp-space-4)'
                  }}>
                    <div>
                      <span style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>平均得分</span>
                      <div style={{ fontSize: 18, fontWeight: 600, color: result.avg_score == null ? 'var(--edp-text2)' : 'var(--edp-primary)' }}>
                        {result.avg_score == null ? '已跳过' : result.avg_score.toFixed(3)}
                      </div>
                    </div>
                    <div>
                      <span style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)' }}>阈值</span>
                      <div style={{ fontSize: 18 }}>{currentMetricData.threshold}</div>
                    </div>
                  </div>
                </div>
              )}
            </div>
            )
          })}
          {totalPages > 1 && (
            <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 'var(--edp-space-3)', padding: '12px 0' }}>
              <button
                onClick={() => setQualityPage(Math.max(0, qualityPage - 1))}
                disabled={qualityPage === 0}
                style={{ padding: '6px 16px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', background: qualityPage === 0 ? 'var(--edp-bg)' : 'var(--edp-card)', cursor: qualityPage === 0 ? 'not-allowed' : 'pointer', fontSize: 'var(--edp-font-md)' }}
              >
                上一页
              </button>
              <span style={{ fontSize: 'var(--edp-font-md)', color: 'var(--edp-text2)' }}>
                第 {qualityPage + 1} / {totalPages} 页（共 {allResults.length} 条）
              </span>
              <button
                onClick={() => setQualityPage(Math.min(totalPages - 1, qualityPage + 1))}
                disabled={qualityPage >= totalPages - 1}
                style={{ padding: '6px 16px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', background: qualityPage >= totalPages - 1 ? 'var(--edp-bg)' : 'var(--edp-card)', cursor: qualityPage >= totalPages - 1 ? 'not-allowed' : 'pointer', fontSize: 'var(--edp-font-md)' }}
              >
                下一页
              </button>
            </div>
          )}
          </>
          )
          })()}
        </div>
        ) : (
          <div style={{ padding: '16px 24px', color: 'var(--edp-text3)', fontSize: 'var(--edp-font-md)' }}>
            暂无质量指标数据。请确认任务已选择正确性、相关性、忠诚度等评测因子；运行时数据请查看「运行效能」Tab。
          </div>
        )}
          </>
        )}

        {mainTab === 'consistency' && (
          <ProcessConsistencyOverview
            taskId={taskId}
            taskAgentId={detail.task.agent_id}
            inferenceRounds={detail.task.inference_rounds ?? 1}
            langfuseTraceReady={taskAgentLangfuseReady}
            checkpointReport={detail.checkpoint_report ?? null}
            processVerdict={detail.dimension_verdicts?.process_consistency ?? null}
          />
        )}

        {targetEditorOpen && (
          <div
            style={{
              position: 'fixed',
              inset: 0,
              background: 'rgba(0,0,0,0.35)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              zIndex: 1100,
            }}
            onClick={() => setTargetEditorOpen(false)}
          >
            <div
              className="eval-target-dialog"
              onClick={e => e.stopPropagation()}
            >
              <h3 className="eval-target-dialog__title">调整本任务运行时目标</h3>
              <p className="eval-target-dialog__desc">
                目标越低代表效率要求越高。该设置只影响本任务的运行时状态、达标率和效率分，不影响正确性、相关性、忠诚度等质量评分，也不会重新调用 Agent。
              </p>
              {targetSuggestions && (
                <div className="eval-target-dialog__suggestions">
                  <div className="eval-target-dialog__suggestions-head">
                    <HelpTip
                      label="历史建议（同 Agent + 数据集）"
                      tip={<HistoricalPercentileTipBody />}
                      align="left"
                    />
                  </div>
                  {targetSuggestions.reason === 'insufficient_history' ? (
                    <div style={{ color: 'var(--edp-text3)' }}>历史样本不足，暂无法给出 P75/P90 建议。</div>
                  ) : (
                    <>
                      <div className="eval-target-dialog__suggest-row">
                        <span className="eval-target-dialog__suggest-meta">
                          时延 P75: {targetSuggestions.inference_latency_ms?.p75 != null ? formatDuration(targetSuggestions.inference_latency_ms.p75) : '—'} · P90:{' '}
                          {targetSuggestions.inference_latency_ms?.p90 != null ? formatDuration(targetSuggestions.inference_latency_ms.p90) : '—'}
                          {targetSuggestions.inference_latency_ms?.sample_count != null && (
                            <span className="eval-target-dialog__suggest-meta-muted">
                              {' '}
                              (n={targetSuggestions.inference_latency_ms.sample_count})
                            </span>
                          )}
                        </span>
                        <span className="eval-target-dialog__suggest-actions">
                          <button
                            type="button"
                            className="eval-target-dialog__chip-btn"
                            onClick={() => applySuggestedTarget('latency', 'p75')}
                          >
                            应用 P75
                          </button>
                          <button
                            type="button"
                            className="eval-target-dialog__chip-btn"
                            onClick={() => applySuggestedTarget('latency', 'p90')}
                          >
                            应用 P90
                          </button>
                        </span>
                      </div>
                      <div className="eval-target-dialog__suggest-row">
                        <span className="eval-target-dialog__suggest-meta">
                          Tokens P75: {targetSuggestions.tokens_total?.p75 ?? '—'} · P90:{' '}
                          {targetSuggestions.tokens_total?.p90 ?? '—'}
                          {targetSuggestions.tokens_total?.sample_count != null && (
                            <span className="eval-target-dialog__suggest-meta-muted">
                              {' '}
                              (n={targetSuggestions.tokens_total.sample_count})
                            </span>
                          )}
                        </span>
                        <span className="eval-target-dialog__suggest-actions">
                          <button
                            type="button"
                            className="eval-target-dialog__chip-btn"
                            onClick={() => applySuggestedTarget('tokens', 'p75')}
                          >
                            应用 P75
                          </button>
                          <button
                            type="button"
                            className="eval-target-dialog__chip-btn"
                            onClick={() => applySuggestedTarget('tokens', 'p90')}
                          >
                            应用 P90
                          </button>
                        </span>
                      </div>
                    </>
                  )}
                </div>
              )}
              <div className="eval-target-dialog__fields">
                <label>
                  <span className="eval-target-dialog__field-label">推理时延目标（毫秒）</span>
                  <input
                    type="number"
                    min="1"
                    className="eval-target-dialog__input"
                    value={latencyTargetInput}
                    onChange={e => setLatencyTargetInput(e.target.value)}
                    placeholder="例如 10000"
                  />
                </label>
                <label>
                  <span className="eval-target-dialog__field-label">Tokens 总消耗目标</span>
                  <input
                    type="number"
                    min="1"
                    className="eval-target-dialog__input"
                    value={tokensTargetInput}
                    onChange={e => setTokensTargetInput(e.target.value)}
                    placeholder="例如 12000"
                  />
                </label>
              </div>
              {targetNotice && (
                <div className="eval-target-dialog__notice">{targetNotice}</div>
              )}
              <div className="eval-target-dialog__footer">
                <button
                  type="button"
                  className="eval-target-dialog__btn eval-target-dialog__btn--secondary"
                  onClick={() => setTargetEditorOpen(false)}
                >
                  取消
                </button>
                <button
                  type="button"
                  className="eval-target-dialog__btn eval-target-dialog__btn--primary"
                  disabled={targetSaving}
                  onClick={saveRuntimeTargets}
                >
                  {targetSaving ? '保存中...' : '保存并重算'}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Footer */}
        <div style={{ 
          padding: '16px 24px', 
          borderTop: '1px solid var(--edp-border)', 
          textAlign: 'right',
          background: 'var(--edp-bg)'
        }}>
          <button 
            onClick={onClose}
            style={{ 
              padding: '8px 24px',
              background: 'var(--edp-primary)',
              color: 'white',
              border: 'none',
              borderRadius: 'var(--edp-radius-sm)',
              cursor: 'pointer',
              fontSize: 'var(--edp-font-lg)'
            }}
          >
            关闭
          </button>
        </div>
        </div>
      </div>
    </div>
  )
}
