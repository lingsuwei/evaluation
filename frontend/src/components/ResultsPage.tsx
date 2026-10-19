import { useState, useEffect, type ReactNode } from 'react'
import {
  Chart as ChartJS,
  RadialLinearScale,
  PointElement,
  LineElement,
  Filler,
  Tooltip,
  Legend,
  ArcElement
} from 'chart.js'
import { Radar } from 'react-chartjs-2'
import { EvalDetailModal } from './EvalDetailModal'
import type { DimensionTabId, DimensionVerdictsPayload } from '../types/dimensionVerdict'
import type { PerfDeepLink } from './RuntimePerformanceReport'
import { PageHeader } from './PageHeader'
import { buildQualityWeightTooltip } from './qualityWeightTooltip'
import { formatDuration } from '../utils/dateUtils'
import { openDetailWindow } from '../utils/detailWindow'
import { scoreColor, resolveVerdictBadge } from '../utils/verdictBadge'

ChartJS.register(RadialLinearScale, PointElement, LineElement, Filler, Tooltip, Legend, ArcElement)

const API_BASE = '/api'

function ResultsSectionTitle({ children }: { children: ReactNode }) {
  return <h2 className="page-title results-section-title">{children}</h2>
}

interface PartialQualityPreview {
  is_partial?: boolean
  metric_result_count?: number
  completed_question_count?: number
  avg_score?: number | null
  pass_rate?: number | null
  skipped_count?: number
  scored_count?: number
}

interface Props {
  taskId: string
  /** 进行中只读：拉取 partial API，隐藏终态图表 */
  partialMode?: boolean
  /**
   * 打开「评估详情」tab 页签（多标签并存）。
   * - taskId：目标任务
   * - initialMainTab / initialPerfDeepLink：从结果页图表下钻时携带的初始视图
   * 未提供时回退到内嵌 EvalDetailModal 弹窗（保持向后兼容）。
   */
  onOpenDetail?: (
    taskId: string,
    initialMainTab?: DimensionTabId,
    initialPerfDeepLink?: PerfDeepLink | null,
  ) => void
}

interface MetricDetail {
  name: string
  score: number | null
  threshold: number
  passed: boolean
  duration_ms: number
  cot: string | null
}

interface ResultDetail {
  question_id: number
  question?: string
  model: string
  metrics: MetricDetail[]
}

interface RuntimeDetail {
  question_id: number
  question?: string
  inference_duration_ms?: number | null
  tokens_total?: number | null
  tokens_llm?: number | null
  tokens_tool?: number | null
  trace_sync_status?: string | null
}

interface HistoricalResult {
  task_id: string
  created_at: string
  metrics: Record<string, number>
  model: string
}

const METRIC_LABELS: Record<string, string> = {
  faithfulness: '忠诚度',
  answer_relevancy: '回答相关性',
  correctness: '正确性',
  inference_latency: '推理时延',
  tokens_total: 'Tokens 总消耗',
  tokens_llm: 'LLM Tokens',
  tokens_tool: '工具 Tokens',
}

const RUNTIME_METRICS = new Set(['inference_latency', 'tokens_total'])

interface RuntimeSummaryEntry {
  avg_raw: number
  unit: string
  normalized_avg?: number
  threshold?: number | null
  pass_rate?: number
  runtime_target_source?: 'task_custom' | 'system_default' | string
}

interface InferenceModelSummary {
  display: string
  count: number
  labels: string[]
}

interface QualityScore {
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
  gates?: Array<{
    type: string
    cap?: number
    reason?: string
  }>
}

const COLORS = [
  'rgba(102, 126, 234, 0.7)',
  'rgba(118, 75, 162, 0.7)',
  'rgba(255, 99, 132, 0.7)',
  'rgba(75, 192, 192, 0.7)',
  'rgba(255, 206, 86, 0.7)',
]

const ROW_COLORS = [
  'var(--edp-card)',
  'var(--edp-bg)',
  'var(--edp-amber-soft)',
  'var(--edp-green-soft)',
  'var(--edp-blue-soft)',
  'var(--edp-red-soft)',
  'var(--edp-primary-soft)',
  'var(--edp-blue-soft)',
]

function formatScore(score?: number | null): string {
  return score == null ? '—' : score.toFixed(2)
}

function formatDeltaScore(score?: number | null): string {
  if (score == null) return '—'
  return `${score > 0 ? '+' : ''}${score.toFixed(2)}`
}

function formatPercent(value?: number | null): string {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`
}

function compactSummaryText(text?: string | null): string {
  if (!text) return ''
  return text
    .replace(/[#*_`>-]/g, '')
    .split('\n')
    .map(line => line.trim())
    .filter(Boolean)
    .slice(0, 4)
    .join('\n')
}

export function ResultsPage({ taskId, partialMode = false, onOpenDetail }: Props) {
  const [loading, setLoading] = useState(true)
  const [isPartialView, setIsPartialView] = useState(partialMode)
  const [partialPreview, setPartialPreview] = useState<PartialQualityPreview | null>(null)
  const [radarData, setRadarData] = useState<any>(null)
  const [runtimeSummary, setRuntimeSummary] = useState<Record<string, RuntimeSummaryEntry>>({})
  const [runtimeDetails, setRuntimeDetails] = useState<RuntimeDetail[]>([])
  const [inferenceModelSummary, setInferenceModelSummary] = useState<InferenceModelSummary | null>(null)
  const [qualityScore, setQualityScore] = useState<QualityScore | null>(null)
  const [details, setDetails] = useState<ResultDetail[]>([])
  const [summary, setSummary] = useState<string | null>(null)
  const [selectedCot, setSelectedCot] = useState<{key: string, cot: string, metric: MetricDetail} | null>(null)
  const [detailTaskId, setDetailTaskId] = useState<string | null>(null)
  const [historicalResults, setHistoricalResults] = useState<HistoricalResult[]>([])
  const [taskStatus, setTaskStatus] = useState<string>('pending')
  const [taskProgress, setTaskProgress] = useState<string>('0/0')
  const [isOfflineEvaluation, setIsOfflineEvaluation] = useState(false)
  const [judgeModelName, setJudgeModelName] = useState<string | null>(null)
  const [dimensionVerdicts, setDimensionVerdicts] = useState<DimensionVerdictsPayload | null>(null)
  const [detailMainTab, setDetailMainTab] = useState<DimensionTabId>('quality')
  const [detailPerfDeepLink, setDetailPerfDeepLink] = useState<PerfDeepLink | null>(null)
  const [scenarioMatrix, setScenarioMatrix] = useState<any>(null)
  // 指标 key→中文名 映射（来源 /api/metrics，覆盖内置/deepeval/自定义指标，与评测指标管理页一致）
  const [metricLabelMap, setMetricLabelMap] = useState<Record<string, string>>({})

  // 评估结果页的「查看详情」统一改为新窗口跳转到评估历史详情页（?detail=&section=&qid=&metric=&step=）

  useEffect(() => {
    fetchResults()
  }, [taskId, partialMode])

  useEffect(() => {
    if (!isPartialView || taskStatus === 'completed' || taskStatus === 'failed') {
      return
    }
    const timer = setInterval(() => fetchResults(), 3000)
    return () => clearInterval(timer)
  }, [isPartialView, taskStatus, taskId])

  // 拉取指标目录，构建 key→中文名 映射，用于雷达图/对比表/明细表等指标名展示
  useEffect(() => {
    let cancelled = false
    fetch(`${API_BASE}/metrics`)
      .then(res => res.json())
      .then(d => {
        if (cancelled) return
        const map: Record<string, string> = {}
        ;(d.builtin || []).forEach((m: { key: string; label: string }) => { if (m.key) map[m.key] = METRIC_LABELS[m.key] || m.label })
        ;(d.deepeval || []).forEach((m: { key: string; label: string }) => { if (m.key) map[m.key] = m.label })
        ;(d.custom || []).forEach((m: { key: string; name: string }) => { if (m.key) map[m.key] = m.name })
        setMetricLabelMap(map)
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [])

  // 获取历史评估结果
  const fetchHistoricalResults = async (datasetId: string) => {
    try {
      const res = await fetch(`${API_BASE}/dataset/${datasetId}/history`)
      const data = await res.json()
      setHistoricalResults(data.history)
    } catch (err) {
      console.error('获取历史结果失败:', err)
    }
  }

  // 指标中文名：优先查 /api/metrics 映射，其次内置常量，最后回退原 key
  const metricLabel = (name: string) => metricLabelMap[name] || METRIC_LABELS[name] || name

  const fetchResults = async () => {
    try {
      const usePartial = partialMode || isPartialView
      const url = usePartial
        ? `${API_BASE}/result/${taskId}?partial=1`
        : `${API_BASE}/result/${taskId}`
      const res = await fetch(url)
      const data = await res.json()
      
      // 保存任务状态和进度
      setTaskStatus(data.status)
      setIsOfflineEvaluation(
        data.evaluation_mode === 'offline' ||
        (Array.isArray(data.capabilities) && !data.capabilities.includes('runtime'))
      )
      if (data.progress) {
        setTaskProgress(data.progress)
      }

      if (data.partial) {
        setIsPartialView(true)
        setPartialPreview(data.partial_quality_score || null)
        setDetails(data.details || [])
        setJudgeModelName(data.judge_model_name || null)
        setRadarData(null)
        setRuntimeSummary({})
        setRuntimeDetails([])
        setQualityScore(null)
        setDimensionVerdicts(null)
        setSummary(null)
        return
      }
      
      if (data.status === 'completed') {
        setIsPartialView(false)
        setPartialPreview(null)
        setRadarData(data.summary.radar_data || {})
        setRuntimeSummary(data.summary.runtime_summary || {})
        setRuntimeDetails(data.runtime_details || [])
        setInferenceModelSummary(data.summary.inference_model_summary || null)
        setQualityScore(data.summary.quality_score || data.quality_score || null)
        setDimensionVerdicts(
          data.summary?.dimension_verdicts || data.dimension_verdicts || null,
        )
        setJudgeModelName(data.summary?.judge_model_name || null)
        setDetails(data.details)
        setSummary(data.summary.text || null)
        // 直接从返回数据获取 dataset_id，省去额外串行请求
        if (data.dataset_id) {
          fetchHistoricalResults(data.dataset_id)
        }
        fetchScenarioMatrix(taskId)
      } else if (data.status === 'failed') {
        setSummary(data.error || '评估任务失败')
      }
    } catch (err) {
      console.error('获取结果失败:', err)
    } finally {
      setLoading(false)
    }
  }

  const fetchScenarioMatrix = async (tid: string) => {
    try {
      const res = await fetch(`${API_BASE}/scenario-eval/tasks/${tid}/matrix`)
      if (res.ok) {
        const data = await res.json()
        setScenarioMatrix(data)
      }
    } catch (err) {
      console.error('获取场景交付矩阵失败:', err)
    }
  }

  const buildChartData = () => {
    if (!radarData) return null
    
    const models = Object.keys(radarData)
    const metrics = [
      ...new Set(
        models.flatMap(m => Object.keys(radarData[m])).filter(m => !RUNTIME_METRICS.has(m)),
      ),
    ]
    if (metrics.length === 0) return null
    
    return {
      labels: metrics.map(m => metricLabel(m)),
      datasets: models.map((model, idx) => ({
        label: model,
        data: metrics.map(m => radarData[model][m] || 0),
        backgroundColor: COLORS[idx % COLORS.length].replace('0.7', '0.2'),
        borderColor: COLORS[idx % COLORS.length],
        borderWidth: 2,
        pointBackgroundColor: COLORS[idx % COLORS.length],
      }))
    }
  }

  const chartOptions = {
    maintainAspectRatio: false,
    scales: {
      r: {
        beginAtZero: true,
        max: 1,
        ticks: { stepSize: 0.2 }
      }
    },
    plugins: {
      legend: { display: false }
    }
  }

  // 计算结果对比
  const calculateComparison = () => {
    if (!radarData || historicalResults.length === 0) return null
    
    const currentModel = Object.keys(radarData)[0]
    if (!currentModel) return null
    
    const currentMetrics = radarData[currentModel]
    
    // 找到最近的历史结果
    const recentHistory = historicalResults.find(r => r.task_id !== taskId)
    if (!recentHistory) return null
    
    const comparison: any = {
      current: { ...currentMetrics },
      previous: { ...recentHistory.metrics },
      differences: {},
      overall: {}
    }
    
    // 仅对质量指标做对比；运行时效率指标不参与质量总分，不在此处评分。
    const allMetrics = new Set(
      [...Object.keys(currentMetrics), ...Object.keys(recentHistory.metrics)]
        .filter(metric => !RUNTIME_METRICS.has(metric)),
    )
    if (allMetrics.size === 0) return null
    
    let totalDiff = 0
    let improvedCount = 0
    let worsenedCount = 0
    let unchangedCount = 0
    
    allMetrics.forEach(metric => {
      const current = currentMetrics[metric] || 0
      const previous = recentHistory.metrics[metric] || 0
      const diff = current - previous
      
      comparison.differences[metric] = {
        current,
        previous,
        diff,
        percentage: previous > 0 ? (diff / previous * 100) : (current > 0 ? 100 : 0),
        status: diff > 0.01 ? 'improved' : diff < -0.01 ? 'worsened' : 'unchanged'
      }
      
      if (diff > 0.01) {
        improvedCount++
        totalDiff += diff
      } else if (diff < -0.01) {
        worsenedCount++
        totalDiff += diff
      } else {
        unchangedCount++
      }
    })
    
    // 计算整体变化
    const metricsCount = allMetrics.size
    comparison.overall = {
      totalDiff,
      avgDiff: totalDiff / metricsCount,
      improvedCount,
      worsenedCount,
      unchangedCount,
      status: totalDiff > 0.01 ? 'improved' : totalDiff < -0.01 ? 'worsened' : 'unchanged'
    }
    
    return comparison
  }

  if (loading) {
    return <div className="card">加载中...</div>
  }

  const chartData = buildChartData()
  const comparison = calculateComparison()
  const qualityDetails = details
    .map(d => ({
      ...d,
      metrics: d.metrics.filter(m => !RUNTIME_METRICS.has(m.name)),
    }))
    .filter(d => d.metrics.length > 0)

  const formatTokens = (value: number | null | undefined) =>
    value == null ? '未上报' : value.toLocaleString()

  // 计算进度百分比
  const progressParts = taskProgress.split('/')
  const current = parseInt(progressParts[0]) || 0
  const total = parseInt(progressParts[1]) || 0
  const progressPercent = total > 0 ? (current / total) * 100 : 0
  const compactSummary = compactSummaryText(summary)

  // 任务状态映射
  const statusMap: Record<string, string> = {
    'pending': '等待中',
    'processing': '处理中',
    'completed': '已完成',
    'failed': '失败',
    'paused': '已暂停',
    'cancelled': '已取消'
  }

  // 任务状态颜色映射
  const statusColorMap: Record<string, string> = {
    'pending': 'var(--edp-amber)',
    'processing': 'var(--edp-blue)',
    'completed': 'var(--edp-green)',
    'failed': 'var(--edp-red)',
    'paused': 'var(--edp-text2)',
    'cancelled': 'var(--edp-text2)'
  }

  const progressFillClass =
    taskStatus === 'processing'
      ? 'results-progress__fill results-progress__fill--processing'
      : taskStatus === 'pending'
        ? 'results-progress__fill results-progress__fill--pending'
        : taskStatus === 'failed'
          ? 'results-progress__fill results-progress__fill--failed'
          : 'results-progress__fill'

  const showFinalCharts = taskStatus === 'completed' && !isPartialView

  return (
    <div className="results-page">
      {isPartialView && (
        <div className="results-partial-banner card" role="status">
          <strong>进行中 · 部分结果预览</strong>
          <span>
            仅展示已落库指标；雷达图、运行效能汇总与终态总分将在任务完成后生成。
            {partialPreview?.metric_result_count != null && (
              <> 已记录 {partialPreview.metric_result_count} 条指标结果
                {partialPreview.completed_question_count != null && (
                  <>，覆盖 {partialPreview.completed_question_count} 题</>
                )}
                。</>
            )}
          </span>
        </div>
      )}
      {/* 任务状态和进度显示 */}
      <div className="card">
        <PageHeader
          className="results-task-status-header"
          title="任务状态"
          icon={
            <svg
              className="page-title-icon"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              aria-hidden
            >
              <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/>
              <line x1="8" y1="21" x2="16" y2="21"/>
              <line x1="12" y1="17" x2="12" y2="21"/>
            </svg>
          }
        />
        <div className="results-task-meta">
          <span
            className="results-status-badge"
            style={{ background: statusColorMap[taskStatus] || 'var(--edp-text2)' }}
          >
            {statusMap[taskStatus] || taskStatus}
          </span>
          <span>
            进度：<strong>{current}</strong> / <strong>{total}</strong>
            {total > 0 && (
              <span className="results-task-meta__label" style={{ marginLeft: 6 }}>
                ({progressPercent.toFixed(1)}%)
              </span>
            )}
          </span>
          <span
            className="results-task-meta__label"
            title={inferenceModelSummary?.labels?.join('\n') || undefined}
          >
            {isOfflineEvaluation ? (
              <>
                Judge 模型：<strong style={{ color: 'var(--edp-text)' }}>{judgeModelName || '未记录'}</strong>
                <span style={{ marginLeft: 10 }}>答案来源：评测集</span>
              </>
            ) : (
              <>
                被测 Agent 推理模型：<strong style={{ color: 'var(--edp-text)' }}>{inferenceModelSummary?.display || '未记录'}</strong>
              </>
            )}
          </span>
        </div>

        <div className="results-progress">
          <div className={progressFillClass} style={{ width: `${progressPercent}%` }} />
        </div>

        {taskStatus !== 'completed' && !isPartialView && (
          <div className="results-notice results-notice--warn">
            <h4>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                <circle cx="12" cy="12" r="10"/>
                <line x1="12" y1="16" x2="12" y2="12"/>
                <line x1="12" y1="8" x2="12.01" y2="8"/>
              </svg>
              任务进行中
            </h4>
            <p>
              评估任务正在进行中，请耐心等待。返回任务监控可查看实时日志，或在本页使用「进行中」模式查看已落库结果。
            </p>
          </div>
        )}

        {isPartialView && partialPreview?.avg_score != null && (
          <div className="results-partial-preview card" style={{ marginTop: 'var(--edp-space-3)' }}>
            <span>预览均分 <strong>{partialPreview.avg_score.toFixed(2)}</strong></span>
            {partialPreview.pass_rate != null && (
              <span style={{ marginLeft: 'var(--edp-space-4)' }}>
                预览通过率 <strong>{(partialPreview.pass_rate * 100).toFixed(1)}%</strong>
              </span>
            )}
            {partialPreview.skipped_count != null && partialPreview.skipped_count > 0 && (
              <span style={{ marginLeft: 'var(--edp-space-4)', color: 'var(--edp-text2)' }}>
                已跳过 <strong>{partialPreview.skipped_count}</strong> 项
              </span>
            )}
          </div>
        )}

        {taskStatus === 'failed' && summary && (
          <div className="results-notice results-notice--error">
            <h4>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                <circle cx="12" cy="12" r="10"/>
                <line x1="15" y1="9" x2="9" y2="15"/>
                <line x1="9" y1="9" x2="15" y2="15"/>
              </svg>
              任务失败
            </h4>
            <p>{summary}</p>
          </div>
        )}
      </div>

      {showFinalCharts && (chartData || qualityScore || dimensionVerdicts) && (
        <div className="card">
          <ResultsSectionTitle>
            <svg className="page-title-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
              <line x1="18" y1="20" x2="18" y2="10"/>
              <line x1="12" y1="20" x2="12" y2="4"/>
              <line x1="6" y1="20" x2="6" y2="14"/>
            </svg>
            评估核心看板
          </ResultsSectionTitle>
          <p className="results-section-desc">
            {isOfflineEvaluation
              ? '展示质量指标分布与结果得分（离线评估不含运行效能与过程分）。'
              : '展示质量指标分布；质量总分与四维评估结论见上方。'}
          </p>
          {(qualityScore || dimensionVerdicts) && (
            <div className="edp-verdict-bar">
              {qualityScore?.total_score != null && (
                <div className="edp-score-pill">
                  <span className="edp-num" style={{ color: scoreColor(qualityScore.total_score) }}>
                    {formatScore(qualityScore.total_score)}
                  </span>
                  <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1 }}>
                    <span className="edp-lbl">质量总分</span>
                    {qualityScore.result_score != null && (
                      <span
                        className="edp-lbl"
                        style={{ color: 'var(--edp-text3)', fontSize: 10 }}
                        title={buildQualityWeightTooltip({
                          result_weight: qualityScore.result_weight,
                          process_weight: qualityScore.process_weight,
                          stability_weight: qualityScore.stability_weight,
                          isOffline: isOfflineEvaluation,
                        })}
                      >
                        结果{formatScore(qualityScore.result_score)}
                        {!isOfflineEvaluation && qualityScore.process_score != null
                          ? `·过程${formatScore(qualityScore.process_score)}`
                          : ''}
                        {!isOfflineEvaluation && qualityScore.stability_score != null
                          ? `·稳定${formatScore(qualityScore.stability_score)}`
                          : ''}
                      </span>
                    )}
                  </div>
                </div>
              )}
              {dimensionVerdicts && (
                <div className="edp-verdict-chips">
                  {([
                    { nav: 'quality', label: '评测指标', data: dimensionVerdicts.quality },
                    ...(isOfflineEvaluation ? [] : dimensionVerdicts.process_consistency
                      ? [{ nav: 'consistency', label: '过程一致性', data: dimensionVerdicts.process_consistency }]
                      : []),
                    ...(isOfflineEvaluation ? [] : dimensionVerdicts.runtime_stability
                      ? [{ nav: 'perf', label: '运行稳定性', data: dimensionVerdicts.runtime_stability }]
                      : []),
                    ...(dimensionVerdicts.scenario_delivery
                      ? [{ nav: 'scenario', label: '场景交付', data: dimensionVerdicts.scenario_delivery }]
                      : []),
                  ] as const).map(v => {
                    const verdict = resolveVerdictBadge(v.nav, v.data)
                    const perfView = v.nav === 'perf'
                      ? (dimensionVerdicts.inference_rounds > 1 ? 'stability' : 'task')
                      : undefined
                    return (
                      <span
                        key={v.nav}
                        className="edp-vchip"
                        style={{ cursor: 'pointer' }}
                        onClick={() => openDetailWindow(taskId, { section: v.nav, perfView })}
                      >
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
          {(chartData || compactSummary) && (
            <div style={{
              display: 'grid',
              gridTemplateColumns: chartData && compactSummary ? 'minmax(320px, 1.3fr) minmax(280px, 1fr)' : '1fr',
              gap: 'var(--edp-space-4)',
              alignItems: 'stretch',
            }}>
              {chartData && (
                <div style={{
                  border: '1px solid var(--edp-border)',
                  borderRadius: 'var(--edp-radius)',
                  padding: 'var(--edp-space-4)',
                  background: 'var(--edp-card)',
                  display: 'flex',
                  flexDirection: 'column',
                }}>
                  <h4 style={{ margin: '0 0 8px 0' }}>质量指标雷达图</h4>
                  <div style={{ height: 280, position: 'relative' }}>
                    <Radar data={chartData} options={chartOptions} />
                  </div>
                </div>
              )}
              {compactSummary && (
                <div style={{
                  border: '1px solid var(--edp-border)',
                  borderRadius: 'var(--edp-radius)',
                  padding: 'var(--edp-space-4)',
                  background: 'var(--edp-card)',
                  display: 'flex',
                  flexDirection: 'column',
                }}>
                  <h4 style={{ margin: '0 0 8px 0' }}>评测摘要</h4>
                  <div style={{ whiteSpace: 'pre-line', color: 'var(--edp-text)', lineHeight: 1.6, fontSize: 'var(--edp-font-md)', flex: 1 }}>
                    {compactSummary}
                  </div>
                  <button
                    type="button"
                    onClick={() => openDetailWindow(taskId)}
                    style={{
                      border: '1px solid var(--edp-primary)',
                      background: 'var(--edp-card)',
                      color: 'var(--edp-primary)',
                      borderRadius: 'var(--edp-radius-sm)',
                      padding: '6px 12px',
                      cursor: 'pointer',
                      whiteSpace: 'nowrap',
                      alignSelf: 'flex-start',
                      marginTop: 'var(--edp-space-3)',
                    }}
                  >
                    查看完整明细
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* 结果对比卡片 */}
      {taskStatus === 'completed' && comparison && (
        <div className="card">
          <ResultsSectionTitle>
            <svg className="page-title-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
              <path d="M3 3v18h18"/>
              <path d="M18.7 8l-5.1 5.2-2.8-2.7L7 14.3"/>
            </svg>
            评估结果对比
          </ResultsSectionTitle>
          <p className="results-section-desc">
            与最近一次评估结果的对比（同一评测集，仅对比质量指标）
          </p>
          
          <div style={{ marginBottom: 'var(--edp-space-5)' }}>
            <div style={{ 
              display: 'flex', 
              justifyContent: 'space-between', 
              alignItems: 'center',
              padding: '12px 20px',
              borderRadius: 'var(--edp-radius-sm)',
              background: comparison.overall.status === 'improved' ? 'rgba(40, 167, 69, 0.1)' : 
                         comparison.overall.status === 'worsened' ? 'rgba(220, 53, 69, 0.1)' : 
                         'rgba(255, 193, 7, 0.1)',
              border: `1px solid ${comparison.overall.status === 'improved' ? 'var(--edp-green)' : 
                                  comparison.overall.status === 'worsened' ? 'var(--edp-red)' : 
                                  'var(--edp-amber)'}`
            }}>
              <div>
                <strong>整体变化：</strong>
                <span style={{ 
                  marginLeft: 'var(--edp-space-2)',
                  fontWeight: 'bold',
                  color: comparison.overall.status === 'improved' ? 'var(--edp-green)' : 
                         comparison.overall.status === 'worsened' ? 'var(--edp-red)' : 
                         'var(--edp-amber)'
                }}>
                  {comparison.overall.status === 'improved' ? (
                    <>
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--edp-green)" strokeWidth="2">
                        <line x1="7" y1="17" x2="17" y2="7"/>
                        <polyline points="7 7 17 7 17 17"/>
                      </svg>
                      变好了
                    </>
                  ) : comparison.overall.status === 'worsened' ? (
                    <>
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--edp-red)" strokeWidth="2">
                        <line x1="7" y1="7" x2="17" y2="17"/>
                        <polyline points="17 7 17 17 7 17"/>
                      </svg>
                      变差了
                    </>
                  ) : (
                    <>
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--edp-text2)" strokeWidth="2">
                        <line x1="5" y1="12" x2="19" y2="12"/>
                      </svg>
                      无明显变化
                    </>
                  )}
                </span>
              </div>
              <div>
                <span style={{ 
                  fontWeight: 'bold',
                  color: comparison.overall.totalDiff > 0 ? 'var(--edp-green)' : 
                         comparison.overall.totalDiff < 0 ? 'var(--edp-red)' : 'var(--edp-text2)'
                }}>
                  {formatDeltaScore(comparison.overall.totalDiff)}
                </span>
              </div>
            </div>
          </div>
          
          <div className="results-table-wrap">
            <table className="results-table">
              <thead>
                <tr>
                  <th style={{ textAlign: 'left' }}>指标</th>
                  <th style={{ textAlign: 'center' }}>本次得分</th>
                  <th style={{ textAlign: 'center' }}>上次得分</th>
                  <th style={{ textAlign: 'center' }}>变化值</th>
                  <th style={{ textAlign: 'center' }}>变化率</th>
                  <th style={{ textAlign: 'center' }}>状态</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(comparison.differences as Record<string, {
                  current: number
                  previous: number
                  diff: number
                  percentage: number
                  status: 'improved' | 'worsened' | 'unchanged'
                }>).map(([metric, diff]) => (
                  <tr key={metric} style={{ borderBottom: '1px solid var(--edp-border)' }}>
                    <td style={{ padding: '10px' }}>{metricLabel(metric)}</td>
                    <td style={{ padding: '10px', textAlign: 'center', fontWeight: 'bold' }}>
                      {formatScore(diff.current)}
                    </td>
                    <td style={{ padding: '10px', textAlign: 'center' }}>
                      {formatScore(diff.previous)}
                    </td>
                    <td style={{ 
                      padding: '10px', 
                      textAlign: 'center', 
                      fontWeight: 'bold',
                      color: diff.diff > 0 ? 'var(--edp-green)' : diff.diff < 0 ? 'var(--edp-red)' : 'var(--edp-text2)'
                    }}>
                      {formatDeltaScore(diff.diff)}
                    </td>
                    <td style={{ 
                      padding: '10px', 
                      textAlign: 'center',
                      fontWeight: 'bold',
                      color: diff.diff > 0 ? 'var(--edp-green)' : diff.diff < 0 ? 'var(--edp-red)' : 'var(--edp-text2)'
                    }}>
                      {diff.diff > 0 ? '+' : ''}{diff.percentage.toFixed(1)}%
                    </td>
                    <td style={{ padding: '10px', textAlign: 'center' }}>
                      <span style={{ 
                        padding: '4px 8px', 
                        borderRadius: 'var(--edp-radius-sm)',
                        fontSize: 'var(--edp-font-base)',
                        fontWeight: 'bold',
                        backgroundColor: diff.status === 'improved' ? 'rgba(40, 167, 69, 0.2)' : 
                                         diff.status === 'worsened' ? 'rgba(220, 53, 69, 0.2)' : 
                                         'rgba(108, 117, 125, 0.2)',
                        color: diff.status === 'improved' ? 'var(--edp-green)' : 
                               diff.status === 'worsened' ? 'var(--edp-red)' : 
                               'var(--edp-text2)'
                      }}>
                        {diff.status === 'improved' ? '✓ 提升' : 
                         diff.status === 'worsened' ? '✗ 下降' : 
                         '→ 持平'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          
          <div style={{ marginTop: 'var(--edp-space-4)', padding: '12px 14px', background: 'var(--edp-bg)', borderRadius: 'var(--edp-radius-sm)', fontSize: 'var(--edp-font-md)' }}>
            <h4 style={{ margin: '0 0 8px', fontSize: 'var(--edp-font-md)', fontWeight: 600, display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)' }}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
                <line x1="16" y1="13" x2="8" y2="13"/>
              </svg>
              对比分析
            </h4>
            <ul style={{ margin: 0, paddingLeft: 20, fontSize: 'var(--edp-font-md)', lineHeight: 1.6 }}>
              <li>本次质量对比共涉及 <strong>{Object.keys(comparison.differences).length}</strong> 个指标</li>
              <li>其中 <strong>{comparison.overall.improvedCount}</strong> 个指标提升，<strong>{comparison.overall.worsenedCount}</strong> 个指标下降，<strong>{comparison.overall.unchangedCount}</strong> 个指标持平</li>
              <li>质量指标整体平均变化：<strong>{formatDeltaScore(comparison.overall.avgDiff)}</strong></li>
            </ul>
            
            <div style={{ marginTop: 'var(--edp-space-3)', fontSize: 'var(--edp-font-md)' }}>
              <strong>说明：</strong>
              <p style={{ margin: '6px 0 0', color: 'var(--edp-text2)' }}>
                该对比仅包含正确性、相关性、忠诚度等质量指标；推理时延和 Tokens 消耗请在下方运行时明细查看。
              </p>
            </div>
          </div>
        </div>
      )}

      {!isOfflineEvaluation && taskStatus === 'completed' && runtimeDetails.length > 0 && (
        <div className="card">
          <ResultsSectionTitle>
            <svg className="page-title-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
              <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>
            </svg>
            运行时效率明细
          </ResultsSectionTitle>
          <p className="results-section-desc">
            展示被测 Agent 的运行事实；Tokens 细分依赖 Agent/Trace 上报，未采集时显示“未上报”。
          </p>
          <div className="results-table-wrap">
          <table className="results-table" style={{ textAlign: 'center' }}>
            <thead>
              <tr>
                <th style={{ textAlign: 'center' }}>问题</th>
                <th style={{ textAlign: 'center' }}>推理时延</th>
                <th style={{ textAlign: 'center' }}>Tokens 总消耗</th>
                <th style={{ textAlign: 'center' }}>LLM Tokens</th>
                <th style={{ textAlign: 'center' }}>工具 Tokens</th>
                <th style={{ textAlign: 'center' }}>Trace 状态</th>
              </tr>
            </thead>
            <tbody>
              {runtimeDetails.map(item => (
                <tr key={item.question_id} style={{ backgroundColor: ROW_COLORS[item.question_id % ROW_COLORS.length] }}>
                  <td style={{ textAlign: 'center' }}>Q{item.question_id + 1}</td>
                  <td style={{ textAlign: 'center' }}>
                    {item.inference_duration_ms != null ? formatDuration(item.inference_duration_ms) : '未上报'}
                  </td>
                  <td style={{ textAlign: 'center' }}>{formatTokens(item.tokens_total)}</td>
                  <td style={{ textAlign: 'center' }}>{formatTokens(item.tokens_llm)}</td>
                  <td style={{ textAlign: 'center' }}>{formatTokens(item.tokens_tool)}</td>
                  <td style={{ textAlign: 'center' }}>{item.trace_sync_status || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </div>
      )}

      {(showFinalCharts || isPartialView) && (qualityDetails.length > 0 || scenarioMatrix) && (
        <div style={{ display: 'flex', gap: 'var(--edp-space-4)', flexWrap: 'wrap' }}>
        {/* 质量指标明细 */}
        {qualityDetails.length > 0 && (
        <div className="card" style={{ flex: '1 1 48%', minWidth: 400 }}>
          <ResultsSectionTitle>
            <svg className="page-title-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
              <line x1="16" y1="13" x2="8" y2="13"/>
              <line x1="16" y1="17" x2="8" y2="17"/>
            </svg>
            质量指标明细（结果评估）
          </ResultsSectionTitle>
        <p className="results-section-desc">
          仅展示正确性、相关性、忠诚度等质量指标；推理时延与 Tokens 请至评估历史「查看详情」→「运行效能」Tab。
        </p>
        <div className="results-table-wrap">
        <table className="results-table" style={{ textAlign: 'center' }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'center' }}>问题</th>
              <th style={{ textAlign: 'center' }}>模型</th>
              <th style={{ textAlign: 'center' }}>指标</th>
              <th style={{ textAlign: 'center' }}>得分</th>
              <th style={{ textAlign: 'center' }}>阈值</th>
              <th style={{ textAlign: 'center' }}>状态</th>
              <th style={{ textAlign: 'center' }}>耗时</th>
              <th style={{ textAlign: 'center' }}>评估过程</th>
            </tr>
          </thead>
          <tbody>
            {qualityDetails.map((d, idx) => (
              d.metrics.map((m, mIdx) => {
                const key = `${idx}-${mIdx}`
                const rowBgColor = ROW_COLORS[d.question_id % ROW_COLORS.length]
                return (
                  <tr key={key} style={{ backgroundColor: rowBgColor }}>
                    <td style={{ textAlign: 'center' }}>Q{d.question_id + 1}</td>
                    <td style={{ textAlign: 'center' }}>{d.model}</td>
                    <td style={{ textAlign: 'center' }}>{metricLabel(m.name)}</td>
                    <td style={{ textAlign: 'center' }}>{formatScore(m.score)}</td>
                    <td style={{ textAlign: 'center' }}>{m.threshold}</td>
                    <td style={{ textAlign: 'center' }}>
                      {m.score == null ? (
                        <span
                          className="score-badge"
                          style={{
                            background: 'var(--edp-border-soft)',
                            color: 'var(--edp-text2)',
                            cursor: 'pointer',
                            textDecoration: 'underline',
                          }}
                          onClick={() => setSelectedCot(
                            selectedCot?.key === key ? null : { key, cot: m.cot || '', metric: m }
                          )}
                          title="点击查看跳过原因"
                        >
                          已跳过
                        </span>
                      ) : m.passed ? (
                        <span className="score-badge score-pass">通过</span>
                      ) : (
                        <span 
                          className="score-badge score-fail"
                          style={{ cursor: 'pointer', textDecoration: 'underline' }}
                          onClick={() => setSelectedCot(
                            selectedCot?.key === key ? null : { key, cot: m.cot || '', metric: m }
                          )}
                          title="点击查看失败原因"
                        >
                          未通过 ❓
                        </span>
                      )}
                    </td>
                    <td style={{ textAlign: 'center' }}>{formatDuration(m.duration_ms)}</td>
                    <td style={{ textAlign: 'center' }}>
                      <span
                        style={{ color: 'var(--edp-primary)', cursor: 'pointer', textDecoration: 'underline' }}
                        title="在评估历史详情中定位该题该指标"
                        onClick={() => openDetailWindow(taskId, { section: 'quality', qid: d.question_id, metric: m.name })}
                      >
                        查看详情
                      </span>
                    </td>
                  </tr>
                )
              })
            ))}
          </tbody>
        </table>
        </div>
      </div>
        )}

        {/* 场景交付明细 */}
        {scenarioMatrix && scenarioMatrix.questions && scenarioMatrix.questions.length > 0 && (
        <div className="card" style={{ flex: '1 1 48%', minWidth: 400 }}>
          <ResultsSectionTitle>
            <svg className="page-title-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
              <polyline points="22 4 12 14.01 9 11.01"/>
            </svg>
            场景交付明细（过程评估）
          </ResultsSectionTitle>
        <p className="results-section-desc">
          展示每个场景子步骤的评估结果与分数，包括基于Prompt评测和Ground_Truth评测。
        </p>
        <div className="results-table-wrap">
        <table className="results-table" style={{ textAlign: 'center' }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'center' }}>问题</th>
              <th style={{ textAlign: 'center' }}>场景步骤</th>
              <th style={{ textAlign: 'center' }}>得分</th>
              <th style={{ textAlign: 'center' }}>权重</th>
              <th style={{ textAlign: 'center' }}>状态</th>
              <th style={{ textAlign: 'center' }}>评估方式</th>
              <th style={{ textAlign: 'center' }}>评估分析</th>
            </tr>
          </thead>
          <tbody>
            {scenarioMatrix.questions.flatMap((q: any, qIdx: number) => {
              const cells = Object.values(q.cells || {}).filter((c: any) => c !== null) as any[]
              const cell = cells[0]
              if (!cell || !cell.steps) return []
              return cell.steps.map((step: any, sIdx: number) => {
                const key = `${qIdx}-${sIdx}`
                const evidence = step.evidence || {}
                const reason = evidence.reason || evidence.llm_judge_reason || ''
                return (
                  <tr key={key} style={{ backgroundColor: ROW_COLORS[qIdx % ROW_COLORS.length] }}>
                    <td style={{ textAlign: 'center' }}>Q{(q.question_id ?? qIdx) + 1}</td>
                    <td style={{ textAlign: 'center' }}>{step.name || step.step_key}</td>
                    <td style={{ textAlign: 'center' }}>
                      <strong style={{ color: step.passed ? 'var(--edp-green)' : 'var(--edp-red)' }}>
                        {typeof step.score === 'number' ? step.score.toFixed(2) : '—'}
                      </strong>
                    </td>
                    <td style={{ textAlign: 'center' }}>{step.weight ?? '—'}</td>
                    <td style={{ textAlign: 'center' }}>
                      {step.passed ? (
                        <span className="score-badge score-pass">通过</span>
                      ) : (
                        <span className="score-badge score-fail">未通过</span>
                      )}
                    </td>
                    <td style={{ textAlign: 'center' }}>
                      {evidence.score_source || evidence.eval_mode || 'keyword'}
                    </td>
                    <td style={{ textAlign: 'center' }}>
                      <span
                        style={{ color: 'var(--edp-primary)', cursor: 'pointer', textDecoration: 'underline' }}
                        title="在评估历史详情中定位该子步骤"
                        onClick={() => openDetailWindow(taskId, { section: 'scenario', qid: q.question_key ?? q.question_id, step: step.step_key })}
                      >
                        查看详情
                      </span>
                      {reason && (
                        <>
                          <span style={{ margin: '0 4px', color: 'var(--edp-border)' }}>·</span>
                          <span
                            style={{ color: 'var(--edp-primary)', cursor: 'pointer', textDecoration: 'underline' }}
                            title={reason}
                            onClick={() => {
                              const full = `【${step.name || step.step_key}】${reason}`
                              setSelectedCot(selectedCot?.key === key ? null : { key, cot: full, metric: { name: step.name || step.step_key, score: step.score, threshold: 0, passed: step.passed, duration_ms: 0 } as any })
                            }}
                          >
                            查看分析
                          </span>
                        </>
                      )}
                    </td>
                  </tr>
                )
              })
            })}
          </tbody>
        </table>
        </div>
      </div>
        )}
      </div>
      )}

      {selectedCot && (
        <div className="card" style={{ borderLeft: '4px solid var(--edp-red)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h3 style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--edp-red)" strokeWidth="2">
                <circle cx="12" cy="12" r="10"/>
                <line x1="15" y1="9" x2="9" y2="15"/>
                <line x1="9" y1="9" x2="15" y2="15"/>
              </svg>
              失败原因
            </h3>
            <button 
              className="btn"
              style={{ padding: '4px 12px' }}
              onClick={() => setSelectedCot(null)}
            >
              ✕ 关闭
            </button>
          </div>
          <div style={{ marginTop: 10, padding: 10, background: 'var(--edp-red-soft)', borderRadius: 'var(--edp-radius-sm)' }}>
            <p><strong>指标：</strong>{metricLabel(selectedCot.metric.name)}</p>
            <p><strong>得分：</strong>{formatScore(selectedCot.metric.score)}（阈值：{formatScore(selectedCot.metric.threshold)}）</p>
            <p><strong>差距：</strong>{formatDeltaScore(selectedCot.metric.score != null ? selectedCot.metric.score - selectedCot.metric.threshold : null)}</p>
          </div>
          <div className="cot-panel" style={{ marginTop: 10 }}>
            <strong>分析：</strong>
            <p style={{ marginTop: 5, whiteSpace: 'pre-wrap' }}>{selectedCot.cot || '暂无详细原因'}</p>
          </div>
        </div>
      )}

      {/* Detail Modal — 仅在未提供 tab 回调时作为回退弹窗使用 */}
      {!onOpenDetail && detailTaskId && (
        <EvalDetailModal
          taskId={detailTaskId}
          initialMainTab={detailMainTab}
          initialPerfDeepLink={detailPerfDeepLink}
          onClose={() => {
            setDetailTaskId(null)
            setDetailMainTab('quality')
            setDetailPerfDeepLink(null)
          }}
        />
      )}
    </div>
  )
}
