import { useState, useEffect, useCallback, useMemo, useRef, memo } from 'react'
import axios from 'axios'
import type { DimensionVerdictsPayload, DimensionTabId } from '../types/dimensionVerdict'
import { formatDate, formatDuration } from '../utils/dateUtils'
import { OFFLINE_NA_LABEL, OFFLINE_NA_TOOLTIP, offlineNaTextStyle } from '../utils/evaluationDisplay'
import { RuntimeStabilityView } from './RuntimeStabilityView'
import type { PerformanceReport } from './RuntimeStabilityView'
import { ProcessConsistencyView } from './ProcessConsistencyView'
import { ScenarioDeliveryView } from './ScenarioDeliveryView'
import { buildQualityWeightTooltip } from './qualityWeightTooltip'
import { scoreColor, resolveVerdictBadge } from '../utils/verdictBadge'
import { DrawerShell } from './DrawerShell'
import { LangfuseTraceBlock } from './LangfuseTraceBlock'

const API_BASE = '/api'

const DETAIL_CACHE_KEY = (taskId: string) => `eval-detail-cache:${taskId}`
// sessionStorage 配额不足（大响应体超 5MB）时降级为内存缓存（同会话内有效）
const _inMemoryDetailCache = new Map<string, TaskDetail>()

/* ============ 类型 ============ */
interface RoundDetail { round_num: number; score: number; reason: string; duration_ms: number }
// 指标明细视图行数据：单指标下每题的打分摘要
interface MdRow {
  qid: number; text: string; avg: number | null; passed: boolean; skipped: boolean
  rounds: RoundDetail[]; inference_duration_ms: number
  agent_run_id?: string | null; run_index: number
}
interface QuestionResult {
  result_id: number; question_id: number
  avg_score: number | null; passed: boolean
  inference_duration_ms: number; agent_run_id?: string | null; run_index?: number
  trace_sync_status?: string | null; trace_id?: string | null
  langfuse_host?: string | null; langfuse_trace_url?: string | null; langfuse_project_id?: string | null
  tokens_total?: number | null; tokens_llm?: number | null; tokens_tool?: number | null
  rounds: RoundDetail[]
}
interface MetricData { metric_name: string; threshold: number; results: QuestionResult[] }
interface MetricSummary {
  metric_name: string; avg_score: number | null; pass_rate: number | null
  passed_count: number; total_count: number; skipped_count?: number; scored_count?: number
  threshold: number; eval_method?: string; runtime_avg_value?: number | null
  runtime_unit?: string | null
}
interface TaskDetail {
  task: {
    id: string; dataset_id: string; dataset_name: string; agent_id: string | null
    evaluation_mode?: 'online' | 'offline'; capabilities?: string[]
    dimension_flags?: { quality: boolean; scenario: boolean; process_consistency: boolean; stability: boolean }
    agent_name: string; inference_model_summary?: { display: string; count: number; labels: string[] }
    execution_mode_summary?: { display: string; count: number; labels: string[] }
    llm_model_name: string; llm_model: string; run_count: number
    inference_rounds?: number; eval_mode?: string
    created_at: string; completed_at: string; status: string
  }
  metrics_summary: MetricSummary[]
  metrics_data: Record<string, MetricData>
  questions?: Record<string, { question: string; ground_truth: string }>
  agent_answers?: Record<string, string>
  quality_score?: {
    total_score?: number | null; raw_total_score?: number | null
    result_score?: number | null; process_score?: number | null
    stability_score?: number | null; safety_score?: number | null
    result_weight?: number; process_weight?: number; stability_weight?: number
    metric_breakdown?: Record<string, number>
    metric_weights?: Record<string, number>
    stability_breakdown?: {
      quality_stability?: number | null
      performance_stability?: number | null
    }
    gates?: Array<{ type: string; applied?: boolean; cap?: number; reason?: string }>
  }
  dimension_verdicts?: DimensionVerdictsPayload | null
  agent_run_count?: number
  sync_summary?: Record<string, number>
  sync_failure_categories?: Record<string, number>
  fact_materialization_status?: string
}
interface SpanTreeNode {
  id: string; name: string; node_type: string; latency_ms: number | null
  tokens: number | null; depth: number; step_index: number | null
  status: string | null; children: SpanTreeNode[]
  input?: string | null  // 新增
  output?: string | null  // 新增
}
interface SpanTreeResponse {
  tree: SpanTreeNode[]
  llm_call_count?: number
  tool_call_count?: number
}

interface Props {
  taskId: string
  onClose: () => void
  initialMainTab?: DimensionTabId
  /** 深链定位：质量指标明细跳转携带的题目 id（字符串形式）与指标名 */
  initialQid?: string
  initialMetric?: string
  /** 深链定位：场景子步骤跳转携带的题目 question_key 与步骤 step_key */
  initialStep?: string
}

/* ============ 常量 & 辅助 ============ */
const METRIC_DISPLAY_NAMES: Record<string, string> = {
  faithfulness: '忠诚度', answer_relevancy: '回答相关性', correctness: '正确性',
  inference_latency: '推理时延', tokens_total: 'Tokens总消耗',
}
const RUNTIME_METRICS = new Set(['inference_latency', 'tokens_total'])

// 指标类别元信息：对标 demo CATEGORIES，按能力域分组展示
const CATEGORY_META: Record<string, { grp: string; color: string; desc: string; label: string }> = {
  answer_quality: { grp: 'Q', color: '#1565c0', desc: '切题/正确/格式', label: '回答质量' },
  rag:            { grp: 'R', color: '#7b1fa2', desc: '需检索上下文',   label: '检索RAG' },
  safety:         { grp: 'S', color: '#dc3545', desc: '偏见/毒性/隐私/越界', label: '安全合规' },
  tool:           { grp: 'T', color: '#00897b', desc: '工具选择/参数/完成度', label: '工具调用' },
  reasoning:      { grp: 'P', color: '#f57c00', desc: '计划/步骤过程',  label: '规划推理' },
  conversation:   { grp: 'C', color: '#5c6bc0', desc: '需对话回合',     label: '多轮对话' },
}
// 内置指标（correctness/faithfulness/answer_relevancy）在 /api/metrics 中无 category 字段，需前端兜底
const BUILTIN_METRIC_CATEGORY: Record<string, string> = {
  correctness: 'answer_quality',
  faithfulness: 'answer_quality',
  answer_relevancy: 'answer_quality',
}
function isRuntimeMetric(m?: Pick<MetricSummary, 'metric_name' | 'eval_method'>) {
  return Boolean(m && (m.eval_method === 'runtime' || RUNTIME_METRICS.has(m.metric_name)))
}
/** 类别加权均分：Σ(avg_score × weight) / Σ(weight)，排除 weight=0 的门禁指标。
 *  若所有指标 weight=0（纯安全类），退化为简单平均。 */
function weightedCategoryAvg(
  metrics: MetricSummary[],
  weights?: Record<string, number>
): number | null {
  const scored = metrics.filter(m => m.avg_score != null)
  if (!scored.length) return null
  if (!weights) {
    return scored.reduce((a, m) => a + (m.avg_score as number), 0) / scored.length
  }
  const weighted = scored
    .map(m => ({ score: m.avg_score as number, w: weights[m.metric_name] ?? 0 }))
    .filter(x => x.w > 0)
  if (!weighted.length) {
    // 全是门禁指标（weight=0），退化为简单平均
    return scored.reduce((a, m) => a + (m.avg_score as number), 0) / scored.length
  }
  const sumW = weighted.reduce((a, x) => a + x.w, 0)
  return weighted.reduce((a, x) => a + x.score * x.w, 0) / sumW
}
/** 运行时指标着色（越低越好）：null→灰；≤阈值80%→绿；≤阈值→琥珀；>阈值→红 */
function runtimeColor(v: number | null, threshold: number | null) {
  if (v == null || threshold == null) return 'var(--edp-text3)'
  return v <= threshold * 0.8 ? 'var(--edp-green)' : v <= threshold ? 'var(--edp-amber)' : 'var(--edp-red)'
}
/** 运行时指标达标文字：null→—；≤阈值→达标；>阈值→超标 */
function runtimeStatusLabel(v: number | null, threshold: number | null) {
  if (v == null || threshold == null) return '—'
  return v <= threshold ? '达标' : '超标'
}
function metricSortIndex(n: string) { return { correctness: 0, answer_relevancy: 1, faithfulness: 2 }[n] ?? 99 }
/** 指标权重查找：无权重配置时返回 -1，使其在降序排序中排在有权重指标之后 */
function metricWeightOf(weights: Record<string, number> | undefined, name: string): number {
  const w = weights?.[name]
  return typeof w === 'number' ? w : -1
}

function flattenTree(nodes: SpanTreeNode[], depth = 0): SpanTreeNode[] {
  const out: SpanTreeNode[] = []
  for (const n of nodes) { out.push({ ...n, depth }); if (n.children?.length) out.push(...flattenTree(n.children, depth + 1)) }
  return out
}

/** span-tree 缓存：trace 数据一旦 synced 即不可变，按 agent_run_id 缓存避免重复请求。
 *  仅缓存非空结果（pending_sync 状态的空结果不缓存，以便后续重试拉取已物化数据）。
 *  drawer 打开和 batchAll 拉取 call-counts 共用此缓存，避免同一 run 重复请求。 */
const spanTreeCache = new Map<string, SpanTreeResponse>()

/** 拉 span-tree 并缓存。命中缓存时直接返回，否则 fetch 后缓存非空结果。 */
async function fetchSpanTree(runId: string): Promise<SpanTreeResponse | null> {
  const cached = spanTreeCache.get(runId)
  if (cached) return cached
  try {
    const res = await fetch(`${API_BASE}/runs/${runId}/span-tree`)
    if (!res.ok) return null
    const d = await res.json() as SpanTreeResponse | null
    if (d && d.tree && d.tree.length > 0) spanTreeCache.set(runId, d)
    return d
  } catch { return null }
}

/** 分批并发：避免 N 轮一次性发 N 个 span-tree 请求压垮后端。 */
async function batchAll<T, R>(items: T[], size: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const out: R[] = []
  for (let i = 0; i < items.length; i += size) {
    out.push(...await Promise.all(items.slice(i, i + size).map(fn)))
  }
  return out
}
const NODE_TYPE_BADGE: Record<string, { label: string; cls: string }> = {
  generation: { label: '思考', cls: 'edp-badge-think' },
  llm: { label: '思考', cls: 'edp-badge-think' },
  tool: { label: '工具', cls: 'edp-badge-tool' },
  tool_call: { label: '工具', cls: 'edp-badge-tool' },
  tool_use: { label: '工具', cls: 'edp-badge-tool' },
  router: { label: '路由', cls: 'edp-badge-span' },
  span: { label: '步骤', cls: 'edp-badge-span' },
  event: { label: '事件', cls: 'edp-badge-event' },
}

/** 尝试将 input/output 文本格式化为可读 JSON。
 *  后端 str() 产出的 Python dict/list 用单引号，需先转成合法 JSON 再 pretty-print。 */
function formatIoText(raw: string): string {
  const s = raw.trim()
  if (!s || !(s[0] === '{' || s[0] === '[')) return raw // 非结构化文本，原样返回
  // 1. 直接尝试 JSON.parse（已经是合法 JSON 的情况）
  try { return JSON.stringify(JSON.parse(s), null, 2) } catch { /* continue */ }
  // 2. Python dict 风格：单引号→双引号，True/False/None→true/false/null
  try {
    const fixed = s
      .replace(/'/g, '"')
      .replace(/\bTrue\b/g, 'true')
      .replace(/\bFalse\b/g, 'false')
      .replace(/\bNone\b/g, 'null')
    return JSON.stringify(JSON.parse(fixed), null, 2)
  } catch { /* continue */ }
  return raw // 解析失败，原样返回
}

const SYNC_STATUS_META: Record<string, { label: string; bg: string; color: string }> = {
  pending_sync: { label: '待同步', bg: '#fff3cd', color: '#856404' },
  synced: { label: '已物化', bg: '#d4edda', color: '#155724' },
  synced_incomplete: { label: '数据不完整', bg: '#fff3cd', color: '#856404' },
  failed: { label: '物化失败', bg: '#f8d7da', color: '#721c24' },
  skipped: { label: '离线/无 Trace', bg: '#e2e3e5', color: '#383d41' },
  pending: { label: '排队中', bg: '#cce5ff', color: '#004085' },
  unknown: { label: '未知', bg: '#f0f0f0', color: '#666' },
}

// 失败类别元数据：retryable=false 的类别重试无意义，前端应警告
const FAILURE_CATEGORY_META: Record<string, { label: string; bg: string; color: string; retryable: boolean }> = {
  trace_not_found:     { label: 'Trace 已删除',   bg: '#fff5f5', color: '#721c24', retryable: false },
  ownership_mismatch:  { label: '归属不匹配',     bg: '#fff5f5', color: '#721c24', retryable: false },
  agent_run_missing:   { label: 'Run 记录缺失',   bg: '#fff3cd', color: '#856404', retryable: false },
  config_error:        { label: '配置错误',       bg: '#fff3cd', color: '#856404', retryable: false },
  auth_error:          { label: '鉴权失败',       bg: '#fff5f5', color: '#721c24', retryable: false },
  transient:           { label: '瞬时故障',       bg: '#d4edda', color: '#155724', retryable: true },
  incomplete_materialization: { label: '物化不完整', bg: '#fff3cd', color: '#856404', retryable: true },
  unknown:             { label: '未知',           bg: '#e2e3e5', color: '#383d41', retryable: true },
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
        borderRadius: 4,
        fontSize: 12,
        background: meta.bg,
        color: meta.color,
        whiteSpace: 'nowrap',
      }}
    >
      {meta.label}
    </span>
  )
}

interface DrawerState { questionId: number; runIndex: number; metricName: string | null; isRuntime: boolean; result: QuestionResult }

/* ============ 组件 ============ */
export function EvalDetailPage({ taskId, onClose, initialMainTab, initialQid, initialMetric, initialStep }: Props) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  // 运行稳定性报告（用于顶部四维结论条联动稳定性结论，并下传给 RuntimeStabilityView 避免重复请求）
  const [perfReport, setPerfReport] = useState<PerformanceReport | null>(null)
  const [view, setView] = useState<'metrics' | 'question' | 'metric-detail' | 'consistency' | 'scenario' | 'perf'>(
    initialMainTab === 'consistency' ? 'consistency'
      : initialMainTab === 'scenario' ? 'scenario'
      : initialMainTab === 'perf' ? 'perf'
      : initialMainTab === 'quality' && initialQid != null ? 'question'
      : 'metrics',
  )
  const [selQ, setSelQ] = useState<number | null>(
    initialMainTab === 'quality' && initialQid != null ? Number(initialQid) : null,
  )
  // 深链定位：质量指标明细跳转时高亮并滚动到目标指标行
  const highlightMetric = initialMainTab === 'quality' ? initialMetric : undefined
  const metricRowRef = useRef<HTMLTableRowElement | null>(null)
  useEffect(() => {
    if (view === 'question' && highlightMetric) {
      const t = setTimeout(() => metricRowRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' }), 50)
      return () => clearTimeout(t)
    }
  }, [view, highlightMetric, selQ, detail])
  const [selCat, setSelCat] = useState<string | null>(null)
  const [selR, setSelR] = useState<number>(1)
  // 选中的指标行（点击首列指标名高亮整行所有轮次）
  const [selMetric, setSelMetric] = useState<string | null>(null)
  const [drawer, setDrawer] = useState<DrawerState | null>(null)
  const [exporting, setExporting] = useState(false)
  // 单元格点击引导：首次进入题目明细时提示，关闭或点开抽屉后 localStorage 记忆不再显示
  const CELL_HINT_KEY = 'edp:cellClickHint:v1'
  const [cellHintOpen, setCellHintOpen] = useState<boolean>(() => {
    try { return localStorage.getItem(CELL_HINT_KEY) !== '1' } catch { return true }
  })
  const dismissCellHint = useCallback(() => {
    setCellHintOpen(false)
    try { localStorage.setItem(CELL_HINT_KEY, '1') } catch { /* 隐私模式忽略 */ }
  }, [])
  // 用户点开抽屉 = 已发现该功能，自动消除引导
  useEffect(() => { if (drawer) dismissCellHint() }, [drawer, dismissCellHint])
  const [spanTree, setSpanTree] = useState<SpanTreeNode[] | null>(null)
  const [treeLoading, setTreeLoading] = useState(false)
  const [callCountsByRound, setCallCountsByRound] = useState<Record<number, { llm: number; tool: number }>>({})
  const [metricLabelMap, setMetricLabelMap] = useState<Record<string, string>>({})
  // 指标元信息（方向、分类等），用于矩阵中显示方向箭头
  const [metricMetaMap, setMetricMetaMap] = useState<Record<string, { lowerIsBetter?: boolean; category?: string; categoryLabel?: string }>>({})
  // Trace 物化重试
  const [retryingSync, setRetryingSync] = useState(false)
  const [retryNotice, setRetryNotice] = useState<string | null>(null)
  const [retryNoticeKind, setRetryNoticeKind] = useState<'success' | 'error'>('success')
  // 指标明细视图：筛选/排序/搜索/增量渲染
  const [mdFilterFailedOnly, setMdFilterFailedOnly] = useState(true)   // 默认仅看未通过
  const [mdSortField, setMdSortField] = useState<'score' | 'qid'>('score')
  const [mdSearchText, setMdSearchText] = useState('')
  const [mdRenderLimit, setMdRenderLimit] = useState(50)
  const mdSentinelRef = useRef<HTMLDivElement | null>(null)

  const metricLabel = useCallback((n: string) => metricLabelMap[n] || METRIC_DISPLAY_NAMES[n] || n, [metricLabelMap])

  // 拉取详情（带 sessionStorage 缓存：先秒开缓存，后台再刷新）
  const fetchDetail = useCallback(async () => {
    try {
      setLoading(true)
      const res = await fetch(`${API_BASE}/tasks/${taskId}/detail`)
      if (!res.ok) throw new Error('获取详情失败')
      const data = await res.json()
      setDetail(data)
      // 写入缓存，供下次打开秒开；sessionStorage 配额不足时降级为内存缓存
      try { sessionStorage.setItem(DETAIL_CACHE_KEY(taskId), JSON.stringify(data)) }
      catch { _inMemoryDetailCache.set(taskId, data) }
    } catch (e) { setError(e instanceof Error ? e.message : '未知错误') }
    finally { setLoading(false) }
  }, [taskId])

  // 静默刷新 detail（不回退到骨架屏），用于子视图触发实时打分后同步顶部 badge / verdict
  const refreshDetailSilent = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/tasks/${taskId}/detail`)
      if (!res.ok) return
      const data = await res.json()
      setDetail(data)
      try { sessionStorage.setItem(DETAIL_CACHE_KEY(taskId), JSON.stringify(data)) }
      catch { _inMemoryDetailCache.set(taskId, data) }
    } catch { /* 静默失败 */ }
  }, [taskId])

  // 挂载时：若 sessionStorage 有缓存，先秒开（loading=false），再后台刷新
  // sessionStorage miss 时回退到内存缓存（配额不足场景）
  useEffect(() => {
    try {
      const cached = sessionStorage.getItem(DETAIL_CACHE_KEY(taskId))
      if (cached) {
        setDetail(JSON.parse(cached))
        setLoading(false)
        // 后台静默刷新（不回退到骨架屏）
        fetchDetail()
        return
      }
    } catch { /* 解析失败忽略 */ }
    const mem = _inMemoryDetailCache.get(taskId)
    if (mem) {
      setDetail(mem)
      setLoading(false)
      fetchDetail()
      return
    }
    fetchDetail()
  }, [fetchDetail])

  // 预取运行稳定性报告，用于顶部四维结论条联动稳定性结论（稳定/需关注/不稳定）
  useEffect(() => {
    let active = true
    fetch(`${API_BASE}/tasks/${taskId}/performance-report`)
      .then(res => res.ok ? res.json() : null)
      .then(json => { if (active && json) setPerfReport(json) })
      .catch(() => { /* 静默失败：顶部小卡片回退为"描述性"灰态 */ })
    return () => { active = false }
  }, [taskId])

  // 运行稳定性结论改用后端 dimension_verdicts.runtime_stability.performance.status，
  // 与 ResultsPage 的 DimensionVerdictBar 同源（within_target/elevated/severe → pass/warn/fail）。

  // 浏览器标签页标题：便于在多窗口中辨识（卸载时还原为产品名）
  useEffect(() => {
    const prevTitle = document.title
    const sid = taskId.length > 8 ? taskId.slice(0, 8) : taskId
    const name = detail?.task?.dataset_name
    document.title = name ? `评估历史 · ${name} · ${sid}` : `评估历史 · ${sid}`
    return () => { document.title = prevTitle }
  }, [taskId, detail?.task?.dataset_name])

  // Trace 物化失败重试
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
      if (!res.ok) throw new Error('重试请求失败')
      const data = await res.json()
      const resetCount = data.reset ?? 0
      const enqueuedCount = data.enqueued ?? 0
      setRetryNoticeKind('success')
      setRetryNotice(
        resetCount + enqueuedCount > 0
          ? `已重新入队 ${resetCount + enqueuedCount} 条，后台正在处理…`
          : '没有需要重试的失败记录',
      )
      await fetchDetail()
    } catch (err) {
      setRetryNoticeKind('error')
      setRetryNotice(err instanceof Error ? err.message : '未知错误')
    } finally {
      setRetryingSync(false)
    }
  }

  useEffect(() => {
    axios.get(`${API_BASE}/metrics`).then(res => {
      const d = res.data || {}; const map: Record<string, string> = {}
      const metaMap: Record<string, { lowerIsBetter?: boolean; category?: string; categoryLabel?: string }> = {}
      ;(d.builtin || []).forEach((m: { key: string; label: string }) => { if (m.key) map[m.key] = METRIC_DISPLAY_NAMES[m.key] || m.label })
      ;(d.deepeval || []).forEach((m: { key: string; label: string; lower_is_better?: boolean; category?: string; category_label?: string }) => {
        if (m.key) {
          map[m.key] = m.label
          if (m.lower_is_better != null) metaMap[m.key] = { lowerIsBetter: m.lower_is_better, category: m.category, categoryLabel: m.category_label }
        }
      })
      ;(d.custom || []).forEach((m: { key: string; name: string; category?: string }) => {
        if (m.key) {
          map[m.key] = m.name
          if (m.category) metaMap[m.key] = { ...(metaMap[m.key] || {}), category: m.category, categoryLabel: m.category }
        }
      })
      setMetricLabelMap(map)
      setMetricMetaMap(metaMap)
    }).catch(() => {})
  }, [])

  // 拉取 span tree（命中缓存时跳过请求，trace 数据 synced 后不可变）
  useEffect(() => {
    const runId = drawer?.result.agent_run_id
    if (!runId) { setSpanTree(null); return }
    const cached = spanTreeCache.get(runId)
    if (cached) { setSpanTree(cached.tree); setTreeLoading(false); return }
    setTreeLoading(true); setSpanTree(null)
    fetchSpanTree(runId)
      .then(d => { setSpanTree(d?.tree ?? null) })
      .finally(() => setTreeLoading(false))
  }, [drawer?.result.agent_run_id])

  // 拉取各轮 LLM/工具调用次数（用于 KPI 和矩阵）
  useEffect(() => {
    if (!detail || selQ == null) { setCallCountsByRound({}); return }
    const qResults: Array<{ run_index: number; agent_run_id?: string | null }> = []
    for (const md of Object.values(detail.metrics_data)) {
      for (const r of md.results) {
        if (r.question_id === selQ && r.agent_run_id && !qResults.some(x => x.run_index === (r.run_index ?? 0))) {
          qResults.push({ run_index: r.run_index ?? 0, agent_run_id: r.agent_run_id })
        }
      }
    }
    if (qResults.length === 0) { setCallCountsByRound({}); return }
    let cancelled = false
    batchAll(qResults, 4, async (r) => {
      try {
        if (!r.agent_run_id) return null
        // 命中缓存时跳过请求（batchAll 仅需 call counts，但顺带填充 tree 缓存供 drawer 复用）
        const d = await fetchSpanTree(r.agent_run_id)
        if (!d) return null
        return { run_index: r.run_index, llm: d.llm_call_count ?? 0, tool: d.tool_call_count ?? 0 }
      } catch { return null }
    }).then(results => {
      if (cancelled) return
      const map: Record<number, { llm: number; tool: number }> = {}
      for (const r of results) {
        if (r) map[r.run_index] = { llm: r.llm, tool: r.tool }
      }
      setCallCountsByRound(map)
    })
    return () => { cancelled = true }
  }, [detail, selQ])

  const qualityMetrics = useMemo(() =>
    detail?.metrics_summary.filter(m => !isRuntimeMetric(m)).sort((a, b) => metricSortIndex(a.metric_name) - metricSortIndex(b.metric_name)) ?? [],
    [detail])
  const runtimeMetrics = useMemo(() =>
    detail?.metrics_summary.filter(m => isRuntimeMetric(m)) ?? [],
    [detail])
  /** 运行时指标阈值查找表：取自 metrics_summary（含 task-custom 覆盖），消除前端硬编码 */
  const runtimeThresholds = useMemo(() => {
    const m = new Map<string, number>()
    for (const s of runtimeMetrics) if (s.threshold != null) m.set(s.metric_name, s.threshold)
    return m
  }, [runtimeMetrics])

  // 按指标类别（能力域）分组，对标 demo CATEGORIES 聚类
  const groupedQualityMetrics = useMemo(() => {
    const groups = new Map<string, MetricSummary[]>()
    for (const m of qualityMetrics) {
      const cat = metricMetaMap[m.metric_name]?.category || BUILTIN_METRIC_CATEGORY[m.metric_name] || 'other'
      if (!groups.has(cat)) groups.set(cat, [])
      groups.get(cat)!.push(m)
    }
    const ordered = Object.keys(CATEGORY_META)
    const known = ordered.filter(c => groups.has(c))
    const other = [...groups.keys()].filter(c => !ordered.includes(c))
    return [...known, ...other].map(cat => ({
      cat,
      metrics: groups.get(cat)!,
      meta: CATEGORY_META[cat] || { grp: '★', color: '#6b5ce7', desc: '', label: cat },
    }))
  }, [qualityMetrics, metricMetaMap])
  // 类别均分 tooltip：展示类别下各指标分数与权重，并说明均分算法
  const buildCatTooltip = (metrics: MetricSummary[]) => {
    const scored = metrics.filter(m => m.avg_score != null)
    if (!scored.length) return ''
    return scored.map(m => {
      const w = detail?.quality_score?.metric_weights?.[m.metric_name]
      const lbl = METRIC_DISPLAY_NAMES[m.metric_name] || m.metric_name
      return `${lbl}: ${(m.avg_score as number).toFixed(2)}（权重${w ?? '—'}）`
    }).join('\n') + '\n—— 类别均分按权重加权（安全指标 weight=0 除外）'
  }
  const numRounds = detail?.task.inference_rounds ?? 1
  const isStability = numRounds > 1

  // 题目列表（去重，文本已外键化到 detail.questions）
  const questions = useMemo(() => {
    if (!detail?.questions) return []
    return Object.entries(detail.questions)
      .map(([id, v]) => ({ id: Number(id), text: v.question }))
      .sort((a, b) => a.id - b.id)
  }, [detail])

  // 每题每轮的均分（用于 sidebar heatmap）
  const roundHeatmap = useCallback((qid: number) => {
    if (!detail) return []
    const out: { round: number; avg: number | null }[] = []
    for (let r = 0; r < numRounds; r++) {
      let sum = 0, cnt = 0
      for (const md of Object.values(detail.metrics_data)) {
        if (isRuntimeMetric({ metric_name: md.metric_name })) continue
        const res = md.results.find(x => x.question_id === qid && (x.run_index ?? 0) === r)
        if (res?.avg_score != null) { sum += res.avg_score; cnt++ }
      }
      out.push({ round: r + 1, avg: cnt > 0 ? sum / cnt : null })
    }
    return out
  }, [detail, numRounds])

  // 矩阵数据：选中题 → 各指标 × 各轮
  const matrixRows = useMemo(() => {
    if (!detail || selQ == null) return []
    const rows: { metricName: string; avg: number | null; passRate: number | null; passed: number; total: number; scores: (number | null)[] }[] = []
    for (const [mn, md] of Object.entries(detail.metrics_data)) {
      if (isRuntimeMetric({ metric_name: mn })) continue
      const results = md.results.filter(r => r.question_id === selQ)
      if (results.length === 0) continue
      const sorted = [...results].sort((a, b) => (a.run_index ?? 0) - (b.run_index ?? 0))
      const scores = sorted.map(r => r.avg_score)
      const scored = scores.filter((s): s is number => s != null)
      const avg = scored.length ? scored.reduce((a, b) => a + b, 0) / scored.length : null
      const passed = sorted.filter(r => r.passed).length
      const pr = scored.length ? (passed / scored.length) * 100 : null
      rows.push({ metricName: mn, avg, passRate: pr, passed, total: sorted.length, scores })
    }
    // 按权重降序：优先看影响大的指标；无权重兜底 -1 排后，权重相同时按内置指标顺序稳定排序
    const weights = detail?.quality_score?.metric_weights
    rows.sort((a, b) => {
      const wa = metricWeightOf(weights, a.metricName)
      const wb = metricWeightOf(weights, b.metricName)
      if (wb !== wa) return wb - wa
      return metricSortIndex(a.metricName) - metricSortIndex(b.metricName)
    })
    return rows
  }, [detail, selQ])

  // 运行时数据行
  const runtimeRows = useMemo(() => {
    if (!detail || selQ == null) return []
    const rows: { label: string; key: string; values: (number | null)[]; avg: number | null }[] = []
    // 收集各轮的运行时数据
    for (const [mn, md] of Object.entries(detail.metrics_data)) {
      if (!isRuntimeMetric({ metric_name: mn })) continue
      const results = md.results.filter(r => r.question_id === selQ).sort((a, b) => (a.run_index ?? 0) - (b.run_index ?? 0))
      const values = Array.from({ length: numRounds }, (_, i) => {
        const r = results.find(x => (x.run_index ?? 0) === i)
        if (!r) return null
        if (mn === 'inference_latency') return r.inference_duration_ms || null
        if (mn === 'tokens_total') return r.tokens_total ?? null
        return null
      })
      const scored = values.filter((v): v is number => v != null)
      const avg = scored.length ? scored.reduce((a, b) => a + b, 0) / scored.length : null
      rows.push({ label: metricLabel(mn), key: mn, values, avg })
    }
    // 如果没有运行时指标但有 inference_duration_ms / tokens_total 数据
    if (rows.length === 0) {
      const firstMd = Object.values(detail.metrics_data)[0]
      if (firstMd) {
        const results = firstMd.results.filter(r => r.question_id === selQ).sort((a, b) => (a.run_index ?? 0) - (b.run_index ?? 0))
        if (results.length > 0) {
          const latValues = Array.from({ length: numRounds }, (_, i) => {
            const r = results.find(x => (x.run_index ?? 0) === i)
            return r?.inference_duration_ms || null
          })
          const tokValues = Array.from({ length: numRounds }, (_, i) => {
            const r = results.find(x => (x.run_index ?? 0) === i)
            return r?.tokens_total ?? null
          })
          const latScored = latValues.filter((v): v is number => v != null)
          const tokScored = tokValues.filter((v): v is number => v != null)
          rows.push({ label: '推理时延', key: 'inference_latency', values: latValues, avg: latScored.length ? latScored.reduce((a, b) => a + b, 0) / latScored.length : null })
          rows.push({ label: 'Tokens', key: 'tokens_total', values: tokValues, avg: tokScored.length ? tokScored.reduce((a, b) => a + b, 0) / tokScored.length : null })
        }
      }
    }
    // LLM/工具调用次数（从 span tree 获取）
    if (Object.keys(callCountsByRound).length > 0) {
      const llmValues = Array.from({ length: numRounds }, (_, i) => callCountsByRound[i]?.llm ?? null)
      const toolValues = Array.from({ length: numRounds }, (_, i) => callCountsByRound[i]?.tool ?? null)
      const llmScored = llmValues.filter((v): v is number => v != null)
      const toolScored = toolValues.filter((v): v is number => v != null)
      rows.push({ label: 'LLM调用', key: '_llm_calls', values: llmValues, avg: llmScored.length ? llmScored.reduce((a, b) => a + b, 0) / llmScored.length : null })
      rows.push({ label: '工具调用', key: '_tool_calls', values: toolValues, avg: toolScored.length ? toolScored.reduce((a, b) => a + b, 0) / toolScored.length : null })
    }
    return rows
  }, [detail, selQ, numRounds, metricLabel, callCountsByRound])

  // KPI 行
  const kpiData = useMemo(() => {
    if (!detail || selQ == null) return null
    let latSum = 0, tokSum = 0, latCnt = 0, tokCnt = 0
    let correctSum = 0, correctCnt = 0
    for (const [mn, md] of Object.entries(detail.metrics_data)) {
      const results = md.results.filter(r => r.question_id === selQ)
      for (const r of results) {
        if (r.inference_duration_ms) { latSum += r.inference_duration_ms; latCnt++ }
        if (r.tokens_total != null) { tokSum += r.tokens_total; tokCnt++ }
        if (mn === 'correctness' && r.avg_score != null) { correctSum += r.avg_score; correctCnt++ }
      }
    }
    // LLM/工具调用次数合计
    let llmCalls = 0, toolCalls = 0
    for (const cc of Object.values(callCountsByRound)) {
      llmCalls += cc.llm
      toolCalls += cc.tool
    }
    return {
      avgLat: latCnt ? Math.round(latSum / latCnt) : null,
      avgTok: tokCnt ? Math.round(tokSum / tokCnt) : null,
      avgCorrect: correctCnt ? correctSum / correctCnt : null,
      llmCalls: llmCalls || null,
      toolCalls: toolCalls || null,
    }
  }, [detail, selQ, callCountsByRound])

  // 指标明细视图数据派生：当前 selMetric 下所有题目的打分（含筛选/排序/搜索）
  const metricDetailData = useMemo(() => {
    if (!detail || !selMetric) return { rows: [] as MdRow[], total: 0, failed: 0 }
    const md = detail.metrics_data[selMetric]
    if (!md) return { rows: [] as MdRow[], total: 0, failed: 0 }
    const qMap = detail.questions || {}
    let rows: MdRow[] = md.results.map(r => ({
      qid: r.question_id,
      text: qMap[String(r.question_id)]?.question || '',
      avg: r.avg_score,
      passed: r.passed,
      skipped: r.avg_score == null,
      rounds: r.rounds,
      inference_duration_ms: r.inference_duration_ms,
      agent_run_id: r.agent_run_id,
      run_index: r.run_index ?? 0,
    }))
    // 搜索过滤：题号或题干
    if (mdSearchText.trim()) {
      const kw = mdSearchText.trim().toLowerCase()
      rows = rows.filter(r => r.text.toLowerCase().includes(kw) || String(r.qid).includes(kw))
    }
    // 仅看未通过（跳过项不计入失败，不入选）
    if (mdFilterFailedOnly) rows = rows.filter(r => !r.passed && !r.skipped)
    // 排序：未过 → 跳过 → 通过；同状态内分数升序 → qid 升序 → run_index 升序
    rows.sort((a, b) => {
      const sa = a.skipped ? 1 : a.passed ? 2 : 0
      const sb = b.skipped ? 1 : b.passed ? 2 : 0
      if (sa !== sb) return sa - sb
      if (mdSortField === 'score') {
        const va = a.avg ?? -1, vb = b.avg ?? -1
        if (va !== vb) return va - vb
      }
      return a.qid - b.qid || a.run_index - b.run_index
    })
    const failed = md.results.filter(r => !r.passed && r.avg_score != null).length
    return { rows, total: md.results.length, failed }
  }, [detail, selMetric, mdFilterFailedOnly, mdSortField, mdSearchText])

  // IntersectionObserver：sentinel 可见时增量加载 +50，避免 300 题一次性渲染卡顿
  useEffect(() => {
    if (view !== 'metric-detail') return
    const el = mdSentinelRef.current
    if (!el) return
    const io = new IntersectionObserver(entries => {
      if (entries.some(e => e.isIntersecting)) {
        setMdRenderLimit(prev => prev + 50)
      }
    }, { rootMargin: '200px' })
    io.observe(el)
    return () => io.disconnect()
  }, [view, mdRenderLimit, metricDetailData.rows.length])

  // Trace 物化失败告警：仅当存在失败记录或近期重试反馈时显示
  const failedSyncCount = (detail?.agent_run_count ?? 0) > 0 ? (detail?.sync_summary?.failed ?? 0) : 0
  const incompleteSyncCount = (detail?.agent_run_count ?? 0) > 0 ? (detail?.sync_summary?.synced_incomplete ?? 0) : 0

  // 成功反馈（无失败残留）6 秒后自动消失，避免常驻
  // ⚠️ 此 useEffect 必须在所有 early return 之前，否则 hooks 数量变化触发 React #310
  useEffect(() => {
    if (retryNotice && retryNoticeKind === 'success' && failedSyncCount === 0 && incompleteSyncCount === 0) {
      const t = setTimeout(() => setRetryNotice(null), 6000)
      return () => clearTimeout(t)
    }
  }, [retryNotice, retryNoticeKind, failedSyncCount, incompleteSyncCount])

  if (loading) return (
    <div className="edp-page">
      <div className="edp-topbar">
        <div className="edp-topbar-left">
          <span className="edp-breadcrumb">评估历史</span>
          <span className="edp-sep">/</span>
          <div className="edp-skl-line" style={{ width: 120, height: 16 }} />
        </div>
        <div className="edp-topbar-right">
          <div className="edp-skl-pill" />
          <div className="edp-skl-line" style={{ width: 48, height: 28, borderRadius: 6 }} />
        </div>
      </div>
      <div className="edp-shell">
        <aside className="edp-sidebar">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="edp-skl-nav-group">
              <div className="edp-skl-line" style={{ width: 80, height: 13, marginBottom: 8 }} />
              {Array.from({ length: 3 }).map((_, j) => (
                <div key={j} className="edp-skl-line" style={{ width: '80%', height: 16, marginBottom: 6 }} />
              ))}
            </div>
          ))}
        </aside>
        <main className="edp-main">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="edp-skl-card">
              <div className="edp-skl-line" style={{ width: 140, height: 16, marginBottom: 12 }} />
              <div className="edp-skl-line" style={{ width: '95%', height: 14, marginBottom: 6 }} />
              <div className="edp-skl-line" style={{ width: '70%', height: 14, marginBottom: 6 }} />
              <div className="edp-skl-line" style={{ width: '88%', height: 14 }} />
            </div>
          ))}
        </main>
      </div>
    </div>
  )
  if (error) return <div className="edp-page"><div className="edp-loading" style={{ color: 'var(--edp-red)' }}>{error}</div><button onClick={onClose} className="edp-btn">关闭</button></div>
  if (!detail) return null

  const isOffline = detail.task.evaluation_mode === 'offline' || !detail.task.agent_id

  // 打开抽屉
  const openDrawer = (qid: number, runIdx: number, metricName: string | null, isRT: boolean) => {
    let result: QuestionResult | undefined
    if (metricName && detail.metrics_data[metricName]) {
      result = detail.metrics_data[metricName].results.find(r => r.question_id === qid && (r.run_index ?? 0) === runIdx)
    } else {
      // 从任意指标取该题该轮的 result（运行时数据）
      for (const md of Object.values(detail.metrics_data)) {
        result = md.results.find(r => r.question_id === qid && (r.run_index ?? 0) === runIdx)
        if (result) break
      }
    }
    if (!result) return
    setDrawer({ questionId: qid, runIndex: runIdx, metricName, isRuntime: isRT, result })
  }

  const closeDrawer = () => { setDrawer(null); setSpanTree(null) }

  // 导出 CSV
  const exportCSV = () => {
    if (selQ == null) return
    let csv = '指标/轮次,'
    for (let r = 1; r <= numRounds; r++) csv += `R${r},`
    csv += '均值,通过率\n'
    for (const row of matrixRows) {
      csv += `"${metricLabel(row.metricName)}",`
      for (const s of row.scores) csv += (s != null ? s.toFixed(3) : '—') + ','
      csv += (row.avg != null ? row.avg.toFixed(3) : '—') + ',' + (row.passRate != null ? row.passRate.toFixed(0) + '%' : '—') + '\n'
    }
    for (const rt of runtimeRows) {
      csv += `"${rt.label}",`
      for (const v of rt.values) csv += (v != null ? (rt.key === 'inference_latency' ? formatDuration(v) : v) : '—') + ','
      csv += (rt.avg != null ? (rt.key === 'inference_latency' ? formatDuration(rt.avg) : Math.round(rt.avg)) : '—') + ',\n'
    }
    const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob); a.download = `Q${selQ}-评测明细.csv`; a.click(); URL.revokeObjectURL(a.href)
  }

  // 导出评测报告（归档 + 下载 xlsx）
  const handleExportReport = async () => {
    setExporting(true)
    try {
      const archiveRes = await fetch(`${API_BASE}/reports/evaluation`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_ids: [taskId] }),
      })
      if (!archiveRes.ok) throw new Error(`归档失败 (${archiveRes.status})`)
      const report = await archiveRes.json()
      const dlRes = await fetch(`${API_BASE}/reports/${report.id}/download`)
      if (!dlRes.ok) throw new Error(`下载失败 (${dlRes.status})`)
      const blob = await dlRes.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${report.title || '评测报告'}.xlsx`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (err) {
      console.error('导出评测报告失败:', err)
      alert(err instanceof Error ? err.message : '导出失败，请稍后重试')
    } finally {
      setExporting(false)
    }
  }

  // Trace 物化告警：失败或不完整或近期重试反馈时显示
  const showSyncAlert = failedSyncCount > 0 || incompleteSyncCount > 0 || retryNotice != null

  return (
    <div className="edp-page">
      {/* 顶部栏 */}
      <div className="edp-topbar">
        <div className="edp-topbar-left">
          <span className="edp-breadcrumb" onClick={onClose}>评估历史</span>
          <span className="edp-sep">/</span>
          <span className="edp-task-name">{detail.task.agent_name || detail.task.dataset_name || detail.task.id}</span>
        </div>
        <div className="edp-topbar-meta">
          <span>评测集 {detail.task.dataset_name || '—'} · {detail.task.evaluation_mode || '在线'} · {questions.length}题×{numRounds}轮{isStability ? '稳定性' : ''}</span>
        </div>
        <div className="edp-topbar-right">
          <button className="edp-icon-btn" title="导出报告" onClick={handleExportReport} disabled={exporting}>
            {exporting ? '⏳' : '📄'}
          </button>
          <button className="edp-icon-btn" title="对比" onClick={() => alert('对比功能开发中')}>⇄</button>
          <button className="edp-btn edp-btn-ghost" onClick={onClose}>关闭</button>
        </div>
      </div>

      {/* Trace 物化告警横条：失败/不完整/近期反馈时显示 */}
      {showSyncAlert && (
        <div
          role="status"
          style={{
            padding: '8px 16px',
            borderBottom: '1px solid var(--edp-border)',
            background: failedSyncCount > 0 ? '#fff5f5' : (incompleteSyncCount > 0 ? '#fffbeb' : '#f0fff4'),
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            flexShrink: 0,
          }}
        >
          {failedSyncCount > 0 ? (
            <>
              <span style={{ fontSize: 14, lineHeight: 1 }}>⚠</span>
              <span style={{ fontSize: 13, color: '#721c24', fontWeight: 500 }}>
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
                        style={{ padding: '1px 6px', borderRadius: 3, fontSize: 11, background: meta.bg, color: meta.color, whiteSpace: 'nowrap' }}
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
                  fontSize: 12,
                  padding: '3px 12px',
                  borderRadius: 4,
                  border: '1px solid #f5c2cb',
                  background: '#fdf0f3',
                  color: '#721c24',
                  cursor: retryingSync ? 'not-allowed' : 'pointer',
                  opacity: retryingSync ? 0.6 : 1,
                }}
              >
                {retryingSync ? '重试中…' : '重试'}
              </button>
              {retryNotice && (
                <span style={{ fontSize: 12, color: retryNoticeKind === 'error' ? '#721c24' : '#155724' }}>
                  {retryNotice}
                </span>
              )}
            </>
          ) : incompleteSyncCount > 0 ? (
            <>
              <span style={{ fontSize: 14, lineHeight: 1 }}>⚠</span>
              <span style={{ fontSize: 13, color: '#856404', fontWeight: 500 }}>
                Trace 物化不完整 {incompleteSyncCount} 条 · 过程一致性维度数据可能缺失，重试可重新拉取
              </span>
              <button
                type="button"
                onClick={handleRetrySync}
                disabled={retryingSync}
                style={{
                  fontSize: 12,
                  padding: '3px 12px',
                  borderRadius: 4,
                  border: '1px solid #ffe09e',
                  background: '#fff8e1',
                  color: '#856404',
                  cursor: retryingSync ? 'not-allowed' : 'pointer',
                  opacity: retryingSync ? 0.6 : 1,
                }}
              >
                {retryingSync ? '重试中…' : '重试'}
              </button>
              {retryNotice && (
                <span style={{ fontSize: 12, color: retryNoticeKind === 'error' ? '#721c24' : '#155724' }}>
                  {retryNotice}
                </span>
              )}
            </>
          ) : (
            <span style={{ fontSize: 13, color: retryNoticeKind === 'error' ? '#721c24' : '#155724' }}>
              {retryNotice}
            </span>
          )}
        </div>
      )}

      {/* 任务信息 */}
      <details className="edp-overview" style={{ padding: '8px 16px', borderBottom: '1px solid var(--edp-border)', background: 'var(--edp-card)', flexShrink: 0 }}>
        <summary style={{ cursor: 'pointer', fontWeight: 600, fontSize: 13, color: 'var(--edp-text2)', userSelect: 'none' }}>任务信息</summary>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16, marginTop: 12, textAlign: 'left' }}>
          <div>
            <span style={{ color: '#666', fontSize: 12 }}>任务ID</span>
            <div style={{ fontFamily: 'monospace', fontSize: 14 }}>{detail.task.id}</div>
          </div>
          <div>
            <span style={{ color: '#666', fontSize: 12 }}>数据集</span>
            <div style={{ fontSize: 14 }}>{detail.task.dataset_name || detail.task.dataset_id}</div>
          </div>
          <div>
            <span style={{ color: '#666', fontSize: 12 }}>Agent</span>
            <div style={{ fontSize: 14 }}>{detail.task.agent_name || '-'}</div>
          </div>
          <div>
            <span style={{ color: '#666', fontSize: 12 }}>评估模型</span>
            <div style={{ fontSize: 14 }}>{detail.task.llm_model_name || detail.task.llm_model}</div>
          </div>
          <div>
            <span style={{ color: '#666', fontSize: 12 }}>被测推理模型</span>
            <div
              style={{ fontSize: 14, ...(isOffline ? offlineNaTextStyle : {}) }}
              title={isOffline ? OFFLINE_NA_TOOLTIP : detail.task.inference_model_summary?.labels?.join('\n') || undefined}
            >
              {isOffline ? OFFLINE_NA_LABEL : detail.task.inference_model_summary?.display || '未记录'}
            </div>
          </div>
          <div>
            <span style={{ color: '#666', fontSize: 12 }}>Thinking 模式</span>
            <div
              style={{ fontSize: 14, ...(isOffline ? offlineNaTextStyle : {}) }}
              title={isOffline ? OFFLINE_NA_TOOLTIP : detail.task.execution_mode_summary?.labels?.join('\n') || undefined}
            >
              {isOffline ? OFFLINE_NA_LABEL : detail.task.execution_mode_summary?.display || '未记录'}
            </div>
          </div>
          <div>
            <span style={{ color: '#666', fontSize: 12 }}>创建时间</span>
            <div style={{ fontSize: 14 }}>{formatDate(detail.task.created_at)}</div>
          </div>
          <div>
            <span style={{ color: '#666', fontSize: 12 }}>完成时间</span>
            <div style={{ fontSize: 14 }}>{formatDate(detail.task.completed_at)}</div>
          </div>
          <div>
            <span style={{ color: '#666', fontSize: 12 }}>Agent 运行次数</span>
            <div style={{ fontSize: 14, ...(isOffline ? offlineNaTextStyle : {}) }} title={isOffline ? OFFLINE_NA_TOOLTIP : undefined}>
              {isOffline ? (
                OFFLINE_NA_LABEL
              ) : (
                <>
                  {numRounds} 轮/题
                  {isStability && (
                    <span style={{ marginLeft: 6, fontSize: 10, fontWeight: 600, padding: '2px 8px', borderRadius: 4, background: '#e3f2fd', color: '#1565c0' }}>
                      稳定性模式
                    </span>
                  )}
                </>
              )}
            </div>
          </div>
          <div>
            <span style={{ color: '#666', fontSize: 12 }}>LLM Judge</span>
            <div style={{ fontSize: 14 }}>
              {isStability ? `每题每指标 ${numRounds} 轮` : '每题每指标 1 次'}
              {(detail.task.run_count ?? 1) > 1 && (
                <span style={{ marginLeft: 6, fontSize: 11, color: '#888' }}>
                  （历史任务曾配置 {detail.task.run_count} 次取平均）
                </span>
              )}
            </div>
          </div>
          <div>
            <span style={{ color: '#666', fontSize: 12 }}>状态</span>
            <div style={{ fontSize: 14 }}>{detail.task.status}</div>
          </div>
          <div>
            <span style={{ color: '#666', fontSize: 12 }}>评估维度</span>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 2 }}>
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
                    fontSize: 11,
                    fontWeight: 500,
                    padding: '2px 8px',
                    borderRadius: 4,
                    ...(d.on
                      ? { background: '#e3f2fd', color: '#1565c0' }
                      : { background: '#f5f5f5', color: '#9e9e9e', opacity: 0.7 }),
                  }}
                >
                  {d.label}
                </span>
              ))}
            </div>
          </div>
        </div>
        {(detail.agent_run_count ?? 0) > 0 && detail.sync_summary && (
          <div style={{ marginTop: 12, display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', justifyContent: 'flex-start' }}>
            <span style={{ color: '#666', fontSize: 12 }}>Trace 物化：</span>
            {detail.fact_materialization_status && (
              <span
                style={{
                  fontSize: 12,
                  padding: '2px 8px',
                  borderRadius: 4,
                  background:
                    detail.fact_materialization_status === 'complete'
                      ? '#e8f5e9'
                      : detail.fact_materialization_status === 'in_progress'
                        ? '#fff3e0'
                        : '#f5f5f5',
                  color: '#333',
                }}
              >
                {detail.fact_materialization_status}
              </span>
            )}
            {Object.entries(detail.sync_summary).map(([status, count]) => (
              <span key={status} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                <SyncStatusBadge status={status} />
                <span style={{ fontSize: 12, color: '#666' }}>×{count}</span>
              </span>
            ))}
          </div>
        )}
      </details>

      {/* 结果分 + 四维结论条（同一行：左结果分 · 右四维结论） */}
      {(detail.quality_score?.total_score != null || detail.dimension_verdicts) && (
        <div className="edp-verdict-bar">
          {detail.quality_score?.total_score != null && (
            <div className="edp-score-pill">
              <span className="edp-num" style={{ color: scoreColor(detail.quality_score.total_score) }}>{detail.quality_score.total_score.toFixed(2)}</span>
              <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1 }}>
                <span className="edp-lbl">质量总分</span>
                {detail.quality_score.result_score != null && (
                  <span className="edp-lbl" style={{ color: 'var(--edp-text3)', fontSize: 10 }} title={buildQualityWeightTooltip({
                    result_weight: detail.quality_score.result_weight,
                    process_weight: detail.quality_score.process_weight,
                    stability_weight: detail.quality_score.stability_weight,
                    isOffline: detail.task.evaluation_mode === 'offline' || !detail.task.agent_id,
                    inference_rounds: detail.task.inference_rounds,
                    processConsistencyEnabled: detail.task.dimension_flags?.process_consistency,
                    stabilityEnabled: detail.task.dimension_flags?.stability,
                    safetyScore: detail.quality_score.safety_score,
                    gates: detail.quality_score.gates,
                  })}>
                    结果{detail.quality_score.result_score.toFixed(2)}{detail.quality_score.process_score != null ? `·过程${detail.quality_score.process_score.toFixed(2)}` : ''}{detail.quality_score.stability_score != null ? `·稳定${detail.quality_score.stability_score.toFixed(2)}` : ''}
                  </span>
                )}
              </div>
            </div>
          )}
          {detail.dimension_verdicts && (
            <div className="edp-verdict-chips">
              {([
                { nav: 'metrics', label: '评测指标', data: detail.dimension_verdicts.quality },
                ...(detail.dimension_verdicts.process_consistency ? [{ nav: 'consistency', label: '过程一致性', data: detail.dimension_verdicts.process_consistency }] : []),
                ...(detail.dimension_verdicts.runtime_stability ? [{ nav: 'perf', label: '运行稳定性', data: detail.dimension_verdicts.runtime_stability }] : []),
                ...(detail.dimension_verdicts.scenario_delivery ? [{ nav: 'scenario', label: '场景交付', data: detail.dimension_verdicts.scenario_delivery }] : []),
              ] as const).map(v => {
                const verdict = resolveVerdictBadge(v.nav, v.data)
                return (
                  <span key={v.nav} className={`edp-vchip ${view === v.nav ? 'active' : ''}`} onClick={() => {
                    if (v.nav === 'perf') setView('perf')
                    else if (v.nav === 'consistency') setView('consistency')
                    else if (v.nav === 'scenario') setView('scenario')
                    else { setView('metrics'); setSelCat(null) }
                  }}>
                    <span className={`edp-dot ${verdict.dotCls}`} />
                    <span className="edp-t">{v.label}</span>
                    <span className={`edp-vbadge ${verdict.badgeCls}`}>{verdict.label}</span>
                  </span>
                )
              })}
            </div>
          )}
        </div>
      )}

      {/* 左右分栏 */}
      <div className="edp-shell">
        {/* 左侧导航 */}
        <aside className="edp-sidebar">
          {/* 评测指标 — 按类别聚类，对标 demo cat-row */}
          <details className="edp-nav-group" open>
            <summary className="edp-nav-summary">
              <svg className="edp-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="9 6 15 12 9 18" /></svg>
              评测指标 <span className="edp-cnt">{qualityMetrics.length}</span>
            </summary>
            {groupedQualityMetrics.map(g => {
              const scored = g.metrics.filter(m => m.avg_score != null)
              const avg = g.cat === 'safety' && detail.quality_score?.safety_score != null
                ? detail.quality_score.safety_score
                : weightedCategoryAvg(g.metrics, detail.quality_score?.metric_weights)
              const pr = scored.length ? scored.reduce((a, m) => a + (m.pass_rate ?? 0), 0) / scored.length : null
              return (
                <div
                  key={g.cat}
                  className={`edp-cat-row ${view === 'metrics' && selCat === g.cat ? 'active' : ''}`}
                  onClick={() => { setView('metrics'); setSelQ(null); setSelCat(g.cat) }}
                >
                  <span className="edp-grp-tag" style={{ background: g.meta.color }}>{g.meta.grp}</span>
                  <span className="edp-n">{g.meta.label}</span>
                  <span className="edp-mini" title={buildCatTooltip(g.metrics)}>{g.cat === 'safety' ? '安全' : '加权'}<b>{avg != null ? avg.toFixed(2) : '—'}</b> ·{pr != null ? pr.toFixed(0) : '—'}%</span>
                </div>
              )
            })}
            {runtimeMetrics.length > 0 && (() => {
              const scored = runtimeMetrics.filter(m => m.avg_score != null)
              const pr = scored.length ? scored.reduce((a, m) => a + (m.pass_rate ?? 0), 0) / scored.length : null
              return (
                <div
                  className={`edp-cat-row ${view === 'metrics' && selCat === 'runtime' ? 'active' : ''}`}
                  onClick={() => { setView('metrics'); setSelQ(null); setSelCat('runtime') }}
                >
                  <span className="edp-grp-tag" style={{ background: '#00897b' }}>RT</span>
                  <span className="edp-n">运行效能</span>
                  <span className="edp-mini">通过<b>{pr != null ? pr.toFixed(0) + '%' : '—'}</b>·共{runtimeMetrics.length}项</span>
                </div>
              )
            })()}
          </details>

          {/* 题目 */}
          <details className="edp-nav-group" open>
            <summary className="edp-nav-summary">
              <svg className="edp-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="9 6 15 12 9 18" /></svg>
              题目 <span className="edp-cnt">{questions.length}题·{numRounds}轮</span>
            </summary>
            {questions.map(q => {
              const hm = roundHeatmap(q.id)
              return (
                <div key={q.id}>
                  <div
                    className={`edp-q-row ${view === 'question' && selQ === q.id ? 'active' : ''}`}
                    onClick={() => { setView('question'); setSelQ(q.id); setSelR(1); setSelMetric(null) }}
                  >
                    <span className="edp-qid">Q{q.id + 1}</span>
                    <span className="edp-q-text">{q.text.slice(0, 12)}…</span>
                    <span className="edp-rc">{numRounds}轮</span>
                  </div>
                  {isStability && (
                    <div className="edp-round-grid">
                      {hm.map(h => (
                        <div
                          key={h.round}
                          className={`edp-round-cell ${selQ === q.id && selR === h.round ? 'active' : ''}`}
                          onClick={(e) => { e.stopPropagation(); setView('question'); setSelQ(q.id); setSelR(h.round) }}
                        >
                          <div className="edp-r">R{h.round}</div>
                          <div className="edp-s" style={{ color: scoreColor(h.avg) }}>{h.avg != null ? h.avg.toFixed(2) : '—'}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </details>

          {/* 其他维度（折叠） */}
          <details className="edp-nav-group" open={initialMainTab === 'consistency' || undefined}>
            <summary className="edp-nav-summary">
              <svg className="edp-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="9 6 15 12 9 18" /></svg>
              过程一致性
            </summary>
            <div className={`edp-nav-leaf ${view === 'consistency' ? 'active' : ''}`} onClick={() => setView('consistency')}>必达点 / 多轮链条</div>
          </details>
          <details className="edp-nav-group" open={initialMainTab === 'perf' || undefined}>
            <summary className="edp-nav-summary">
              <svg className="edp-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="9 6 15 12 9 18" /></svg>
              运行稳定性
            </summary>
            <div className={`edp-nav-leaf ${view === 'perf' ? 'active' : ''}`} onClick={() => setView('perf')}>时延 / Token / 多轮波动</div>
          </details>
          <details className="edp-nav-group" open={initialMainTab === 'scenario' || undefined}>
            <summary className="edp-nav-summary">
              <svg className="edp-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="9 6 15 12 9 18" /></svg>
              场景交付
            </summary>
            <div className={`edp-nav-leaf ${view === 'scenario' ? 'active' : ''}`} onClick={() => setView('scenario')}>场景子步骤矩阵</div>
          </details>
        </aside>

        {/* 右侧主区 */}
        <main className="edp-main">
          {view === 'metrics' && (
            <div className="edp-view-head">
              <h2>评测指标</h2>
              <span className="edp-desc">全部指标按能力域分组 · 点击下钻单题单轮</span>
              <div className="edp-spacer" />
              <span className="edp-act" onClick={() => { if (questions[0]) { setView('question'); setSelQ(questions[0].id); setSelR(1) } }}>查看题目明细 →</span>
            </div>
          )}

          {view === 'metrics' && (
            <div className="edp-metrics-list">
              {/* 按指标类别（能力域）分组 — 对标 demo cat-card */}
              {groupedQualityMetrics.map(g => {
                const scored = g.metrics.filter(m => m.avg_score != null)
                const avg = g.cat === 'safety' && detail.quality_score?.safety_score != null
                  ? detail.quality_score.safety_score
                  : weightedCategoryAvg(g.metrics, detail.quality_score?.metric_weights)
                const pr = scored.length ? scored.reduce((a, m) => a + (m.pass_rate ?? 0), 0) / scored.length : null
                const isSafety = g.cat === 'safety'
                const openByDefault = selCat === g.cat || isSafety || !CATEGORY_META[g.cat]
                return (
                  <details key={g.cat} className="edp-cat-card" open={openByDefault || undefined}>
                    <summary className="edp-cat-card-summary">
                      <svg className="edp-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="9 6 15 12 9 18" /></svg>
                      <span className="edp-grp" style={{ background: g.meta.color }}>{g.meta.grp}</span>
                      <span className="edp-cat-title">{g.meta.label}</span>
                      {g.meta.desc && <span className="edp-cat-desc">{g.meta.desc}</span>}
                      <span className="edp-mini" title={buildCatTooltip(g.metrics)}>{g.cat === 'safety' ? '安全' : '加权'}<b>{avg != null ? avg.toFixed(2) : '—'}</b> ·通过<b>{pr != null ? pr.toFixed(0) + '%' : '—'}</b> ·共{g.metrics.length}项</span>
                    </summary>
                    <table className="edp-table edp-metrics-tbl">
                      <thead><tr><th>指标</th><th style={{ width: 72 }}>平均分</th><th style={{ width: 76 }}>权重</th><th style={{ width: 120 }}>通过率</th><th style={{ width: 120 }}>分布</th></tr></thead>
                      <tbody>
                        {g.metrics.map(m => {
                          const v = m.avg_score ?? 0
                          const lowerBetter = metricMetaMap[m.metric_name]?.lowerIsBetter
                          return (
                            <tr key={m.metric_name} className="edp-expand-row" title="点击查看该指标所有题目打分" onClick={() => {
                              if (isRuntimeMetric(m)) return  // 运行时指标不进入此视图
                              setSelMetric(m.metric_name)
                              setMdFilterFailedOnly(true); setMdRenderLimit(50); setMdSearchText(''); setMdSortField('score')
                              setView('metric-detail')
                            }}>
                              <td>
                                <span className="edp-mn">{metricLabel(m.metric_name)}</span>
                                {lowerBetter && <span className="edp-tag" style={{ background: 'var(--edp-red-soft)', color: 'var(--edp-red)' }}>护栏</span>}
                                {!lowerBetter && <span className={`edp-tag ${m.eval_method === 'script' ? 'script' : 'llm'}`}>{m.eval_method === 'script' ? '脚本' : 'LLM'}</span>}
                              </td>
                              <td className={v >= 0.8 ? 'edp-sc-good' : v >= 0.6 ? 'edp-sc-mid' : 'edp-sc-bad'}>{m.avg_score != null ? m.avg_score.toFixed(2) : '—'}</td>
                              <td className="edp-grp-w" style={{ fontWeight: (detail.quality_score?.metric_weights?.[m.metric_name] ?? 0) >= 0.5 ? 600 : 400, color: (detail.quality_score?.metric_weights?.[m.metric_name] ?? -1) === 0 ? 'var(--edp-amber)' : 'var(--edp-text2)' }}>
                                {(detail.quality_score?.metric_weights?.[m.metric_name] ?? -1) === 0
                                  ? <span className="edp-tag" style={{ background: 'var(--edp-amber-soft)', color: 'var(--edp-amber)' }}>门禁</span>
                                  : detail.quality_score?.metric_weights?.[m.metric_name] != null
                                    ? detail.quality_score.metric_weights[m.metric_name].toFixed(1)
                                    : '—'}
                              </td>
                              <td>{m.pass_rate != null ? m.pass_rate.toFixed(0) + '%' : '—'} <span className="edp-t3">({m.passed_count}/{m.scored_count ?? m.total_count})</span></td>
                              <td><span className="edp-bar"><i style={{ width: `${v * 100}%`, background: scoreColor(m.avg_score) }} /></span></td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </details>
                )
              })}
              {/* 运行时指标 */}
              {runtimeMetrics.length > 0 && (
                <details className="edp-cat-card" open={selCat === 'runtime' || undefined}>
                  <summary className="edp-cat-card-summary">
                    <svg className="edp-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="9 6 15 12 9 18" /></svg>
                    <span className="edp-grp edp-grp-runtime">RT</span>
                    <span className="edp-cat-title">运行效能</span>
                  </summary>
                  <table className="edp-table">
                    <thead><tr><th>指标</th><th>平均值</th><th>阈值</th><th>通过率</th></tr></thead>
                    <tbody>
                      {runtimeMetrics.map(m => (
                        <tr key={m.metric_name}>
                          <td className="edp-mn"><span className="edp-tag" style={{ background: 'var(--edp-bg)', color: 'var(--edp-text3)' }}>RT</span> {metricLabel(m.metric_name)}</td>
                          <td style={{ color: runtimeColor(m.runtime_avg_value ?? m.avg_score ?? null, m.threshold) }}>{m.runtime_avg_value != null ? (m.runtime_unit === 'ms' ? formatDuration(m.runtime_avg_value) : `${m.runtime_avg_value.toFixed(1)} ${m.runtime_unit || ''}`) : (m.avg_score != null ? m.avg_score.toFixed(2) : '—')}</td>
                          <td>{m.runtime_unit === 'ms' && m.threshold != null ? formatDuration(m.threshold) : m.threshold}</td>
                          <td>{m.pass_rate != null ? m.pass_rate.toFixed(0) + '%' : '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </details>
              )}
              {qualityMetrics.length === 0 && runtimeMetrics.length === 0 && (
                <p className="edp-empty">本任务未选择质量评测指标。</p>
              )}
            </div>
          )}

          {view === 'metric-detail' && selMetric && (() => {
            const m = detail.metrics_summary.find(x => x.metric_name === selMetric)
            const md = detail.metrics_data[selMetric]
            if (!m || !md) return null
            const { rows, total, failed } = metricDetailData
            const shown = rows.slice(0, mdRenderLimit)
            const extraCols = numRounds > 1 ? 1 : 0
            return (
              <>
                <div className="edp-view-head">
                  <span className="edp-back" onClick={() => setView('metrics')}>← 返回指标列表</span>
                  <h2>{metricLabel(selMetric)}</h2>
                  <span className="edp-mini">
                    均分<b style={{ color: scoreColor(m.avg_score) }}>{m.avg_score != null ? m.avg_score.toFixed(2) : '—'}</b>
                    {' · '}通过<b>{m.pass_rate != null ? m.pass_rate.toFixed(0) + '%' : '—'}</b>
                    {' · '}阈值<b>{m.threshold}</b>
                    {' · '}共{total}题
                  </span>
                </div>

                {/* 筛选条 */}
                <div className="edp-md-filter">
                  <label className="edp-md-toggle">
                    <input type="checkbox" checked={mdFilterFailedOnly}
                      onChange={e => { setMdFilterFailedOnly(e.target.checked); setMdRenderLimit(50) }} />
                    {' '}仅看未通过 <b>{failed}</b> 题
                  </label>
                  <select className="edp-md-select" value={mdSortField}
                    onChange={e => { setMdSortField(e.target.value as 'score' | 'qid'); setMdRenderLimit(50) }}>
                    <option value="score">分数升序</option>
                    <option value="qid">题号升序</option>
                  </select>
                  <input className="edp-md-search" placeholder="搜索题号或题干..."
                    value={mdSearchText}
                    onChange={e => { setMdSearchText(e.target.value); setMdRenderLimit(50) }} />
                  <span className="edp-md-count">显示 {Math.min(shown.length, rows.length)} / {rows.length} 条</span>
                </div>

                {/* 题目表格 */}
                <div className="edp-md-table-wrap">
                  <table className="edp-md-table">
                    <thead>
                      <tr>
                        <th className="edp-md-h-qid">Q#</th>
                        <th className="edp-md-h-qtext">题目</th>
                        <th className="edp-md-h-score">分数</th>
                        <th className="edp-md-h-status">状态</th>
                        {numRounds > 1 && <th className="edp-md-h-round">轮次</th>}
                        <th className="edp-md-h-lat">时延</th>
                      </tr>
                    </thead>
                    <tbody>
                      {shown.map(r => (
                        <MetricDetailRow key={`${r.qid}-${r.run_index}`} r={r} numRounds={numRounds}
                          onOpen={() => openDrawer(r.qid, r.run_index, selMetric, false)}
                          selected={drawer?.questionId === r.qid && drawer?.runIndex === r.run_index} />
                      ))}
                      {rows.length === 0 && (
                        <tr><td colSpan={4 + extraCols + 1} className="edp-empty" style={{ padding: 24, textAlign: 'center' }}>没有符合条件的题目</td></tr>
                      )}
                    </tbody>
                  </table>
                  {rows.length > shown.length && (
                    <div ref={mdSentinelRef} className="edp-md-sentinel">加载更多…（{shown.length} / {rows.length}）</div>
                  )}
                  {rows.length > 0 && rows.length <= shown.length && (
                    <div className="edp-md-sentinel done">已显示全部 {rows.length} 条</div>
                  )}
                </div>
                <p style={{ fontSize: 11, color: 'var(--edp-text3)', marginTop: 8 }}>
                  点击行查看 Judge 点评 · Agent 推理轨迹 · 标准答案 · 默认按未通过→跳过→通过排序，红底为未过、灰底为跳过
                </p>
              </>
            )
          })()}

          {view === 'question' && selQ != null && (() => {
            const q = questions.find(x => x.id === selQ)
            if (!q) return null
            return (
              <>
                <div className="edp-view-head">
                  <h2>题目明细 · Q{q.id + 1}</h2>
                  <div className="edp-spacer" />
                  <button className="edp-btn-export" onClick={exportCSV}>⤓ 导出 CSV</button>
                </div>

                {/* 题目内容卡片 — 固定高度，支持换行与竖向滚动 */}
                <div className="edp-qtext-box">
                  <span className="edp-qtext-label">题目内容</span>
                  <div className="edp-qtext-body">{q.text}</div>
                </div>

                {/* KPI 行 — 后端限制：QuestionResult 无 llm_calls/tool_calls 字段，仅展示平均时延/平均Tokens/正确性均值 */}
                {kpiData && (
                  <div className="edp-kpi-row">
                    {kpiData.avgLat != null && (
                      <div className="edp-kpi"><div className="edp-k">平均时延</div><div className="edp-v" style={{ color: runtimeColor(kpiData.avgLat, runtimeThresholds.get('inference_latency') ?? null) }}>{formatDuration(kpiData.avgLat)}</div><div className="edp-sub">{runtimeStatusLabel(kpiData.avgLat, runtimeThresholds.get('inference_latency') ?? null)}</div></div>
                    )}
                    {kpiData.avgTok != null && (
                      <div className="edp-kpi"><div className="edp-k">平均Tokens</div><div className="edp-v" style={{ color: runtimeColor(kpiData.avgTok, runtimeThresholds.get('tokens_total') ?? null) }}>{kpiData.avgTok}</div><div className="edp-sub">{runtimeStatusLabel(kpiData.avgTok, runtimeThresholds.get('tokens_total') ?? null)}</div></div>
                    )}
                    {kpiData.llmCalls != null && (
                      <div className="edp-kpi"><div className="edp-k">LLM调用</div><div className="edp-v">{kpiData.llmCalls}</div><div className="edp-sub">合计{Object.keys(callCountsByRound).length}轮</div></div>
                    )}
                    {kpiData.toolCalls != null && (
                      <div className="edp-kpi"><div className="edp-k">工具调用</div><div className="edp-v">{kpiData.toolCalls}</div></div>
                    )}
                    {kpiData.avgCorrect != null && (
                      <div className="edp-kpi"><div className="edp-k">正确性均值</div><div className="edp-v" style={{ color: scoreColor(kpiData.avgCorrect) }}>{kpiData.avgCorrect.toFixed(2)}</div></div>
                    )}
                  </div>
                )}

                {/* 轮次选择条 */}
                {isStability && (
                  <div className="edp-round-bar">
                    <span className="edp-rlbl">轮次</span>
                    {Array.from({ length: numRounds }, (_, i) => i + 1).map(r => (
                      <span
                        key={r}
                        className={`edp-rt${selR === r ? ' active' : ''}`}
                        onClick={() => setSelR(r)}
                      >R{r}</span>
                    ))}
                    <span className="edp-rhint">点击轮次高亮整列 · 单元格点击查看详情</span>
                  </div>
                )}

                {/* 单元格点击引导（首次进入，关闭或点开抽屉后不再显示） */}
                {cellHintOpen && (
                  <div className="edp-cell-hint" role="note">
                    <span className="edp-cell-hint-icon">💡</span>
                    <span className="edp-cell-hint-text">
                      点击任意分数单元格，在右侧抽屉查看 <b>Judge 点评</b> · <b>Agent 推理轨迹</b> · <b>标准答案</b>
                    </span>
                    <button className="edp-cell-hint-btn" onClick={dismissCellHint}>知道了</button>
                    <button className="edp-cell-hint-close" onClick={dismissCellHint} aria-label="关闭提示">×</button>
                  </div>
                )}
                {/* 矩阵表 */}
                <div className="edp-matrix-wrap">
                  <table className="edp-matrix">
                    <thead>
                      <tr>
                        <th className="edp-lbl" title="按权重降序排列">指标 \ 轮次</th>
                        {Array.from({ length: numRounds }, (_, i) => i + 1).map(r => (
                          <th key={r} className={selR === r ? 'col-active' : ''}>R{r}</th>
                        ))}
                        <th>均值</th><th>通过</th>
                      </tr>
                    </thead>
                    <tbody>
                      {/* 质量指标行 */}
                      {qualityMetrics.filter(m => m.eval_method !== 'script').length > 0 && (
                        <tr className="edp-cat-head"><td className="edp-lbl"><span className="edp-grp edp-grp-llm" style={{ fontSize: 10, padding: '1px 5px', borderRadius: 3, color: '#fff' }}>L</span> LLM评估</td><td colSpan={numRounds + 2} /></tr>
                      )}
                      {matrixRows.filter(r => detail.metrics_data[r.metricName]?.threshold != null && qualityMetrics.find(m => m.metric_name === r.metricName)?.eval_method !== 'script').map(row => {
                        const isMetricActive = selMetric === row.metricName
                        return (
                        <tr key={row.metricName} ref={row.metricName === highlightMetric ? metricRowRef : undefined} className={isMetricActive ? 'row-active' : undefined} style={row.metricName === highlightMetric ? { background: 'rgba(102, 126, 234, 0.14)' } : undefined}>
                          <td className="edp-lbl" style={{ cursor: 'pointer', fontWeight: isMetricActive ? 700 : 500 }} onClick={() => setSelMetric(prev => prev === row.metricName ? null : row.metricName)}>{metricLabel(row.metricName)}{metricMetaMap[row.metricName]?.lowerIsBetter && <span style={{ color: 'var(--edp-red)', fontSize: 10 }}> ↓</span>}</td>
                          {Array.from({ length: numRounds }, (_, i) => i).map(ri => {
                            const v = row.scores[ri] ?? null
                            return (
                              <td
                                key={ri}
                                className={`edp-mc${selR - 1 === ri ? ' col-active' : ''}`}
                                style={{ color: scoreColor(v) }}
                                onClick={() => openDrawer(selQ, ri, row.metricName, false)}
                              >{v != null ? v.toFixed(2) : '—'}</td>
                            )
                          })}
                          <td className="edp-avg" style={{ color: scoreColor(row.avg) }}>{row.avg != null ? row.avg.toFixed(2) : '—'}</td>
                          <td style={{ color: 'var(--edp-text2)' }}>{row.passRate != null ? row.passRate.toFixed(0) + '%' : '—'}</td>
                        </tr>
                        )
                      })}
                      {/* 脚本指标行 */}
                      {qualityMetrics.filter(m => m.eval_method === 'script').length > 0 && (
                        <tr className="edp-cat-head"><td className="edp-lbl"><span className="edp-grp edp-grp-script" style={{ fontSize: 10, padding: '1px 5px', borderRadius: 3, color: '#fff' }}>S</span> 脚本评估</td><td colSpan={numRounds + 2} /></tr>
                      )}
                      {matrixRows.filter(r => qualityMetrics.find(m => m.metric_name === r.metricName)?.eval_method === 'script').map(row => {
                        const isMetricActive = selMetric === row.metricName
                        return (
                        <tr key={row.metricName} ref={row.metricName === highlightMetric ? metricRowRef : undefined} className={isMetricActive ? 'row-active' : undefined} style={row.metricName === highlightMetric ? { background: 'rgba(102, 126, 234, 0.14)' } : undefined}>
                          <td className="edp-lbl" style={{ cursor: 'pointer', fontWeight: isMetricActive ? 700 : 500 }} onClick={() => setSelMetric(prev => prev === row.metricName ? null : row.metricName)}>{metricLabel(row.metricName)}{metricMetaMap[row.metricName]?.lowerIsBetter && <span style={{ color: 'var(--edp-red)', fontSize: 10 }}> ↓</span>}</td>
                          {Array.from({ length: numRounds }, (_, i) => i).map(ri => {
                            const v = row.scores[ri] ?? null
                            return (
                              <td
                                key={ri}
                                className={`edp-mc${selR - 1 === ri ? ' col-active' : ''}`}
                                style={{ color: scoreColor(v) }}
                                onClick={() => openDrawer(selQ, ri, row.metricName, false)}
                              >{v != null ? v.toFixed(2) : '—'}</td>
                            )
                          })}
                          <td className="edp-avg" style={{ color: scoreColor(row.avg) }}>{row.avg != null ? row.avg.toFixed(2) : '—'}</td>
                          <td style={{ color: 'var(--edp-text2)' }}>{row.passRate != null ? row.passRate.toFixed(0) + '%' : '—'}</td>
                        </tr>
                        )
                      })}
                      {/* 运行效能行 — 后端限制：仅时延和Tokens，无LLM调用/工具调用 */}
                      {runtimeRows.length > 0 && (
                        <tr className="edp-cat-head"><td className="edp-lbl">运行效能</td><td colSpan={numRounds + 2} /></tr>
                      )}
                      {runtimeRows.map(rt => {
                        const isMetricActive = selMetric === rt.key
                        return (
                        <tr key={rt.key} className={isMetricActive ? 'row-active' : undefined}>
                          <td className="edp-lbl" style={{ cursor: 'pointer', fontWeight: isMetricActive ? 700 : 500 }} onClick={() => setSelMetric(prev => prev === rt.key ? null : rt.key)}>{rt.label}</td>
                          {rt.values.map((v, ri) => {
                            const th = runtimeThresholds.get(rt.key) ?? null
                            return (
                              <td
                                key={ri}
                                className={`edp-mc${selR - 1 === ri ? ' col-active' : ''}`}
                                style={{ color: runtimeColor(v, th) }}
                                onClick={() => openDrawer(selQ, ri, rt.key, true)}
                              >{v != null ? (rt.key === 'inference_latency' ? formatDuration(v) : v) : '—'}</td>
                            )
                          })}
                          <td className="edp-avg" style={{ color: runtimeColor(rt.avg, runtimeThresholds.get(rt.key) ?? null) }}>{rt.avg != null ? (rt.key === 'inference_latency' ? formatDuration(rt.avg) : Math.round(rt.avg)) : '—'}</td>
                          <td style={{ color: 'var(--edp-text2)' }}>{runtimeStatusLabel(rt.avg, runtimeThresholds.get(rt.key) ?? null)}</td>
                        </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
                <p style={{ fontSize: 11, color: 'var(--edp-text3)', marginTop: 8 }}>点击指标名高亮整行 · 点击左树轮次高亮整列 · 首列与表头吸顶</p>
              </>
            )
          })()}

          {view === 'consistency' && (
            <ProcessConsistencyView
              taskId={taskId}
              processVerdict={detail.dimension_verdicts?.process_consistency ?? null}
            />
          )}
          {view === 'scenario' && (
            <ScenarioDeliveryView
              taskId={taskId}
              scenarioVerdict={detail.dimension_verdicts?.scenario_delivery ?? null}
              initialStepLocator={initialMainTab === 'scenario' && initialQid && initialStep ? { qid: initialQid, step: initialStep } : undefined}
              onRefreshDetail={refreshDetailSilent}
            />
          )}

          {view === 'perf' && (
            <RuntimeStabilityView
              taskId={taskId}
              runtimeStabilityVerdict={detail.dimension_verdicts?.runtime_stability ?? null}
              onOpenQuestion={(qid: number) => { setView('question'); setSelQ(qid); setSelR(1) }}
              perfReport={perfReport}
              stabilityBreakdown={detail.quality_score?.stability_breakdown ?? null}
            />
          )}
        </main>
      </div>

      {/* 详情抽屉 */}
      {drawer && (
        <DrawerContent
          drawer={drawer}
          detail={detail}
          metricLabel={metricLabel}
          metricMetaMap={metricMetaMap}
          spanTree={spanTree}
          treeLoading={treeLoading}
          onClose={closeDrawer}
          numRounds={numRounds}
          isOffline={isOffline}
        />
      )}
    </div>
  )
}

/* ============ 抽屉子组件 ============ */
function DrawerContent({ drawer, detail, metricLabel, metricMetaMap, spanTree, treeLoading, onClose, isOffline }: {
  drawer: DrawerState
  detail: TaskDetail
  metricLabel: (n: string) => string
  metricMetaMap: Record<string, { lowerIsBetter?: boolean; category?: string; categoryLabel?: string }>
  spanTree: SpanTreeNode[] | null
  treeLoading: boolean
  onClose: () => void
  numRounds: number
  isOffline: boolean
}) {
  const { result, questionId, runIndex, metricName, isRuntime } = drawer
  const metricSummary = detail.metrics_summary.find(m => m.metric_name === metricName)
  const val = isRuntime ? null : result.avg_score
  const round = result.rounds?.[0]
  const flatNodes = useMemo(() => spanTree ? flattenTree(spanTree) : [], [spanTree])
  const traceTotal = flatNodes.reduce((s, n) => s + (n.latency_ms || 0), 0)

  return (
    <DrawerShell title={<>{metricLabel(metricName || '')} · Q{questionId + 1} R{runIndex + 1}</>} onClose={onClose}>
      {/* 评分 */}
      {!isRuntime && (
        <>
          <div className="edp-sect">
            <h4>评分</h4>
            <div className="edp-score-big" style={{ color: scoreColor(val) }}>{val != null ? val.toFixed(2) : '—'}</div>
            <div className="edp-kv">
              <div><div className="edp-k">状态</div><div className="edp-v">{result.avg_score == null ? '跳过' : result.passed ? '通过' : '未过'}</div></div>
              <div><div className="edp-k">阈值</div><div className="edp-v">{metricSummary?.threshold ?? '—'}</div></div>
              <div><div className="edp-k">方向</div><div className="edp-v">{metricMetaMap[metricName || '']?.lowerIsBetter ? '越低越好' : '越高越好'}</div></div>
            </div>
          </div>
          {round?.reason && (
            <div className="edp-sect">
              <h4>⚖ Judge 点评</h4>
              <div className="edp-review-box">{round.reason}</div>
            </div>
          )}
          {(detail.agent_answers?.[result.agent_run_id ?? '']) && (
            <div className="edp-sect">
              <h4>Agent 回答</h4>
              <details className="edp-trace-io">
                <summary className="edp-io-lbl out">输出</summary>
                <pre className="edp-io-pre out">{detail.agent_answers?.[result.agent_run_id ?? '']}</pre>
              </details>
            </div>
          )}
          {(detail.questions?.[String(result.question_id)]?.ground_truth) && (
            <div className="edp-sect">
              <h4>参考答案</h4>
              <details className="edp-trace-io">
                <summary className="edp-io-lbl in">参考</summary>
                <pre className="edp-io-pre in">{formatIoText(detail.questions?.[String(result.question_id)]?.ground_truth ?? '')}</pre>
              </details>
            </div>
          )}
        </>
      )}

      {/* Agent 推理轨迹 — span tree API 返回步骤名/时延/Tokens/input/output */}
      <div className="edp-sect">
        <h4>🧠 Agent 推理轨迹 · Q{questionId + 1} R{runIndex + 1}
          <span className="edp-trace-total">
            {treeLoading ? '加载中…' : spanTree ? `总时延 ${formatDuration(traceTotal)} · ${flatNodes.length}步` : '无 Trace 数据'}
          </span>
        </h4>
        {spanTree && flatNodes.length > 0 ? (
          flatNodes.map((node, i) => {
            const badge = NODE_TYPE_BADGE[node.semantic_role] || NODE_TYPE_BADGE[node.node_type] || { label: node.node_type || '步骤', cls: 'edp-badge-span' }
            return (
              <div key={node.id || i} className="edp-trace-step">
                <span className={`edp-badge ${badge.cls}`}>{badge.label}</span>
                <div className="edp-trace-body">
                  <div className="edp-trace-d">
                    <b style={{ marginLeft: node.depth * 12 }}>{node.name || ('步骤' + (i + 1))}</b>
                    {node.latency_ms != null && <span className="edp-tdur">{formatDuration(node.latency_ms)}</span>}
                    {node.tokens != null && <span className="edp-tdur">{node.tokens} tok</span>}
                  </div>
                  {node.input && (
                    <details className="edp-trace-io">
                      <summary className="edp-io-lbl in">Input</summary>
                      <pre className="edp-io-pre in">{formatIoText(node.input)}</pre>
                    </details>
                  )}
                  {node.output && (
                    <details className="edp-trace-io">
                      <summary className="edp-io-lbl out">Output</summary>
                      <pre className="edp-io-pre out">{formatIoText(node.output)}</pre>
                    </details>
                  )}
                </div>
              </div>
            )
          })
        ) : (
          <p className="edp-empty" style={{ padding: 12 }}>
            {isOffline ? '离线评估任务无 Agent Trace' : '该轮推理未关联 Trace 数据'}
          </p>
        )}
        <LangfuseTraceBlock
          traceUrl={result.langfuse_trace_url}
          traceId={result.trace_id}
          langfuseProjectId={result.langfuse_project_id}
          langfuseHost={result.langfuse_host}
        />
      </div>

      {/* 运行数据 */}
      <div className="edp-sect">
        <h4>运行数据</h4>
        <div className="edp-kv">
          <div><div className="edp-k">时延</div><div className="edp-v">{result.inference_duration_ms ? formatDuration(result.inference_duration_ms) : '—'}</div></div>
          <div><div className="edp-k">总Tokens</div><div className="edp-v">{result.tokens_total ?? '—'}</div></div>
          {result.tokens_llm != null && <div><div className="edp-k">LLM Tokens</div><div className="edp-v">{result.tokens_llm}</div></div>}
          {result.tokens_tool != null && <div><div className="edp-k">Tool Tokens</div><div className="edp-v">{result.tokens_tool}</div></div>}
        </div>
        {result.trace_sync_status && (
          <div style={{ marginTop: 8, fontSize: 11, color: 'var(--edp-text3)' }}>Trace 状态：{result.trace_sync_status}</div>
        )}
      </div>
    </DrawerShell>
  )
}

/* ============ 指标明细视图行组件 ============ */
// React.memo 保证 drawer 开关等父级状态变化时行不重渲染；props 稳定才重渲染
const MetricDetailRow = memo(function MetricDetailRow({ r, numRounds, onOpen, selected }: {
  r: MdRow
  numRounds: number
  onOpen: () => void
  selected?: boolean
}) {
  const cls = `edp-md-row${r.skipped ? ' skip' : r.passed ? '' : ' fail'}${selected ? ' selected' : ''}`
  return (
    <tr className={cls} onClick={onOpen} title="点击查看 Judge 点评 / Agent 推理 / 标准答案">
      <td className="edp-md-qid">Q{r.qid + 1}</td>
      <td className="edp-md-qtext" title={r.text}>
        {r.text.slice(0, 48)}{r.text.length > 48 ? '…' : ''}
      </td>
      <td className="edp-md-score" style={{ color: scoreColor(r.avg) }}>{r.avg != null ? r.avg.toFixed(2) : '—'}</td>
      <td className="edp-md-status">
        <span className={`edp-tag ${r.skipped ? 'skip' : r.passed ? 'pass' : 'fail'}`}>{r.skipped ? '跳过' : r.passed ? '通过' : '未过'}</span>
      </td>
      {numRounds > 1 && (
        <td className="edp-md-round" title={`第 ${r.run_index + 1} / ${numRounds} 轮`}>R{r.run_index + 1}</td>
      )}
      <td className="edp-md-lat">{r.inference_duration_ms ? formatDuration(r.inference_duration_ms) : '—'}</td>
    </tr>
  )
})
