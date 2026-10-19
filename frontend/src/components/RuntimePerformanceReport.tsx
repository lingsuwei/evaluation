import { useCallback, useEffect, useMemo, useState } from 'react'
import { formatDuration } from '../utils/dateUtils'
import { HelpTip, TaskPercentileTipBody } from './HelpTip'
import { RunSpanTreePanel } from './RunSpanTreePanel'
import { RuntimeStabilityHeadline } from './DimensionVerdictHeadline'
import { IterationLatencyChart } from './IterationLatencyChart'
import { MultiRoundSpanTreePanel } from './MultiRoundSpanTreePanel'
import type { DimensionVerdictsPayload } from '../types/dimensionVerdict'

const API_BASE = '/api'

interface RunRow {
  agent_run_id: string
  question_id?: number | null
  run_index?: number
  llm_call_count?: number
  tool_call_count?: number
  latency_ms?: number | null
  tokens_total?: number | null
  trace_sync_status?: string | null
  step_summary?: string | null
  step_count?: number
  correctness_score?: number | null
  correctness_passed?: boolean | null
}

interface RunDistribution {
  source?: string
  sample_count?: number
  latency_sample_count?: number
  tokens_sample_count?: number
  reason?: string
  inference_latency_ms?: { p75?: number | null; p90?: number | null; sample_count?: number }
  tokens_total?: { p75?: number | null; p90?: number | null; sample_count?: number }
}

interface CheckpointAttributionRow {
  checkpoint_id: string
  name: string
  order?: number
  required?: boolean
  hit_rate?: number
  pass_rate?: number
  avg_latency_ms?: number | null
  avg_tokens?: number | null
  evaluated_runs?: number
}

interface PerformanceReport {
  summary: {
    run_count?: number
    question_count?: number
    inference_rounds?: number
    multi_round_mode?: boolean
    llm_call_count?: number
    tool_call_count?: number
    avg_llm_per_question?: number | null
    avg_tool_per_question?: number | null
    tokens_total?: number | null
    avg_tokens_per_question?: number | null
    avg_latency_ms?: number | null
    tokens_llm_total?: number | null
    tokens_tool_total?: number | null
    checkpoint_compliance_rate?: number | null
    checkpoint_optional_hit_rate?: number | null
    avg_loop_iterations?: number | null
  }
  checkpoint_summary?: {
    avg_required_compliance_rate?: number | null
    avg_compliance_rate?: number | null
    avg_optional_hit_rate?: number | null
  } | null
  target_comparison?: {
    inference_latency_ms?: {
      current_ms?: number | null
      target_ms?: number
      target_source?: string
      status?: string | null
      task_p75?: number | null
      task_p90?: number | null
    }
    tokens_total?: {
      current_avg?: number | null
      target?: number
      target_source?: string
      status?: string | null
      task_p75?: number | null
      task_p90?: number | null
    }
  }
  tool_breakdown?: { tool: string; count: number; share: number }[]
  run_rows?: RunRow[]
  focus_questions?: {
    slowest?: RunRow[]
    costliest?: RunRow[]
  }
  materialization?: { status?: string; unsynced_runs?: number }
  run_distribution?: RunDistribution | null
  checkpoint_cost_attribution?: {
    logical_agent_id?: string
    template_count?: number
    rows?: CheckpointAttributionRow[]
  } | null
  chain_stability?: {
    chain_pass_rate?: number | null
    questions_all_rounds_ok?: number
    question_count?: number
    max_rounds_per_question?: number
    weakest_chain?: { name?: string; pass_rate?: string } | null
    order_sensitive_checkpoints?: {
      name?: string
      pass_rate?: string
      order_ok_rate?: string | null
      enforce_order?: boolean
    }[]
  } | null
  stability_questions?: StabilityQuestion[]
  stability_focus?: { question_id?: number; reason?: string } | null
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
  headline?: string | null
  is_focus?: boolean
  correctness_mean?: number | null
  correctness_min?: number | null
  correctness_max?: number | null
  correctness_std?: number | null
  correctness_pass_count?: number
  correctness_pass_rate?: number | null
}

export interface PerfDeepLink {
  view?: 'task' | 'stability' | 'run'
  questionId?: number
  agentRunId?: string
}

interface Props {
  taskId: string
  deepLink?: PerfDeepLink | null
  onDeepLinkConsumed?: () => void
  onOpenRuntimeTargets?: () => void
  onOpenCheckpointCompliance?: () => void
  onOpenQualityForQuestion?: (questionId: number) => void
  runtimeStabilityVerdict?: DimensionVerdictsPayload['runtime_stability'] | null
}

function fmtPct(v?: number | null) {
  if (v == null) return '—'
  return `${(v * 100).toFixed(1)}%`
}

function fmtTokens(v?: number | null) {
  if (v == null) return '—'
  return v.toLocaleString()
}

export function RuntimePerformanceReport({
  taskId,
  deepLink,
  onDeepLinkConsumed,
  onOpenRuntimeTargets,
  onOpenCheckpointCompliance,
  onOpenQualityForQuestion,
  runtimeStabilityVerdict,
}: Props) {
  const [data, setData] = useState<PerformanceReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  type ViewMode = 'task' | 'stability' | 'run'
  const [view, setView] = useState<ViewMode>('task')
  const [viewInitialized, setViewInitialized] = useState(false)
  const [drillRunId, setDrillRunId] = useState<string | null>(null)
  const [compareQ, setCompareQ] = useState<number | null>(null)

  const load = useCallback(async () => {
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
  }, [taskId])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    setViewInitialized(false)
    setView('task')
    setCompareQ(null)
    setDrillRunId(null)
  }, [taskId])

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

  const hasMultiRound = useMemo(
    () => [...runsByQuestion.values()].some(list => list.length > 1),
    [runsByQuestion],
  )

  const stabilityMode = useMemo(
    () => Boolean(data?.summary?.multi_round_mode) || hasMultiRound,
    [data?.summary?.multi_round_mode, hasMultiRound],
  )

  const stabilityQuestions = useMemo(
    () => data?.stability_questions ?? [],
    [data?.stability_questions],
  )

  const initCompareForQuestion = useCallback((q: number) => {
    const list = runsByQuestion.get(q) ?? []
    if (list.length < 2) return
    setCompareQ(q)
  }, [runsByQuestion])

  const applyDeepLink = useCallback(
    (link: PerfDeepLink): boolean => {
      if (link.view === 'stability' && link.questionId != null) {
        setView('stability')
        setDrillRunId(null)
        initCompareForQuestion(link.questionId)
        return true
      }

      if (link.view === 'task') {
        setView('task')
        setDrillRunId(null)
        return true
      }

      if (link.view === 'run' || link.agentRunId != null || link.questionId != null) {
        setView('run')
        if (link.agentRunId) {
          setDrillRunId(link.agentRunId)
          return true
        }
        if (link.questionId != null) {
          const row = (data?.run_rows ?? []).find(r => r.question_id === link.questionId)
          if (row) {
            setDrillRunId(row.agent_run_id)
            return true
          }
        }
      }

      // deepLink 未完整匹配任何分支（如 view=stability 但无 questionId）
      // 至少切换视图，由调用方继续默认初始化逻辑选择题目
      if (link.view === 'stability') {
        setView('stability')
      }
      return false
    },
    [data?.run_rows, initCompareForQuestion, runsByQuestion],
  )

  useEffect(() => {
    if (!data) return

    if (deepLink) {
      const applied = applyDeepLink(deepLink)
      onDeepLinkConsumed?.()
      if (applied) {
        setViewInitialized(true)
        return
      }
      // deepLink 未完整应用（如 view=stability 但无 questionId），
      // 继续执行默认初始化以设置 compareQ
    }

    if (viewInitialized) return

    if (stabilityMode) {
      setView('stability')
      const focusQ =
        data.stability_focus?.question_id ??
        stabilityQuestions.find(sq => sq.is_focus)?.question_id ??
        stabilityQuestions[0]?.question_id ??
        [...runsByQuestion.entries()].find(([, list]) => list.length > 1)?.[0]
      if (focusQ != null) {
        initCompareForQuestion(focusQ)
      }
    }
    setViewInitialized(true)
  }, [
    data,
    deepLink,
    viewInitialized,
    stabilityMode,
    stabilityQuestions,
    runsByQuestion,
    initCompareForQuestion,
    applyDeepLink,
    onDeepLinkConsumed,
  ])

  const runById = useMemo(() => {
    const map = new Map<string, RunRow>()
    for (const r of data?.run_rows ?? []) {
      map.set(r.agent_run_id, r)
    }
    return map
  }, [data?.run_rows])

  const drillRun = drillRunId ? runById.get(drillRunId) : null

  const openDrill = (runId: string) => {
    setView('run')
    setDrillRunId(runId)
  }

  const openStabilityCompare = (q: number) => {
    const list = runsByQuestion.get(q) ?? []
    if (list.length < 2) return
    setView('stability')
    setDrillRunId(null)
    initCompareForQuestion(q)
  }

  if (loading) {
    return <p style={{ padding: 'var(--edp-space-6)', fontSize: 'var(--edp-font-md)', color: 'var(--edp-text2)' }}>加载运行效能报告…</p>
  }
  if (error) {
    return <p style={{ padding: 'var(--edp-space-6)', color: 'var(--edp-red)', fontSize: 'var(--edp-font-md)' }}>{error}</p>
  }
  if (!data) return null

  const s = data.summary
  const dist = data.run_distribution
  const tokLlm = s.tokens_llm_total ?? 0
  const tokTool = s.tokens_tool_total ?? 0
  const tokParts = tokLlm + tokTool
  const cpAttr = data.checkpoint_cost_attribution?.rows ?? []
  const tc = data.target_comparison

  return (
    <div style={{ padding: '16px 24px 28px' }}>
      <p style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)', margin: '0 0 16px', lineHeight: 1.6 }}>
        本 Tab 只看时延、Token、调用次数波动，不判断推理步骤是否正确。过程一致性请见「质量指标 → 过程一致性总览 / 推理链对比」。
        {stabilityMode
          ? ' 本任务为稳定性评测（每题多轮），默认进入「多轮稳定性」查看时延与 Token 波动。'
          : ' 任务汇总看整次评测；单题 Run 明细按题目下钻推理链。'}
      </p>

      <div style={{ display: 'inline-flex', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', overflow: 'hidden', marginBottom: 'var(--edp-space-4)' }}>
        {(
          stabilityMode
            ? (['task', 'stability', 'run'] as const)
            : (['task', 'run'] as const)
        ).map(v => (
          <button
            key={v}
            type="button"
            onClick={() => setView(v)}
            style={{
              padding: '8px 16px',
              border: 'none',
              background: view === v ? (v === 'stability' ? 'var(--edp-blue)' : 'var(--edp-primary)') : 'var(--edp-card)',
              color: view === v ? 'var(--edp-card)' : 'var(--edp-text2)',
              fontWeight: view === v ? 600 : 400,
              fontSize: 'var(--edp-font-md)',
              cursor: 'pointer',
            }}
          >
            {v === 'task' ? '任务汇总' : v === 'stability' ? '多轮稳定性' : '单题 RUN'}
          </button>
        ))}
      </div>

      {view === 'stability' && stabilityMode && (
        <>
          {runtimeStabilityVerdict &&
            runtimeStabilityVerdict.performance.status !== 'na' && (
              <RuntimeStabilityHeadline
                mergedHeadline={runtimeStabilityVerdict.headline}
                performance={runtimeStabilityVerdict.performance}
              />
            )}

          {runtimeStabilityVerdict?.quality_stability &&
            runtimeStabilityVerdict.quality_stability.status !== 'na' && (
              <div style={{ marginBottom: 'var(--edp-space-4)', padding: 14, background: 'var(--edp-green-soft)', border: '1px solid var(--edp-green-soft)', borderRadius: 'var(--edp-radius-sm)' }}>
                <h4 style={{ margin: '0 0 8px', fontSize: 'var(--edp-font-lg)', color: 'var(--edp-green)' }}>
                  结果正确性稳定性
                  <span
                    style={{
                      marginLeft: 'var(--edp-space-2)',
                      fontSize: 'var(--edp-font-sm)',
                      fontWeight: 600,
                      padding: '2px 8px',
                      borderRadius: 'var(--edp-radius-sm)',
                      background:
                        runtimeStabilityVerdict.quality_stability.status === 'pass'
                          ? 'var(--edp-green-soft)'
                          : runtimeStabilityVerdict.quality_stability.status === 'warn'
                            ? 'var(--edp-amber-soft)'
                            : 'var(--edp-red-soft)',
                      color:
                        runtimeStabilityVerdict.quality_stability.status === 'pass'
                          ? 'var(--edp-green)'
                          : runtimeStabilityVerdict.quality_stability.status === 'warn'
                            ? 'var(--edp-amber)'
                            : 'var(--edp-red)',
                    }}
                  >
                    {runtimeStabilityVerdict.quality_stability.status === 'pass'
                      ? '达标'
                      : runtimeStabilityVerdict.quality_stability.status === 'warn'
                        ? '需关注'
                        : '未达标'}
                  </span>
                </h4>
                <p style={{ margin: 0, fontSize: 'var(--edp-font-md)', color: 'var(--edp-text)' }}>
                  {runtimeStabilityVerdict.quality_stability.headline}
                </p>
                {runtimeStabilityVerdict.quality_stability.metrics.pass_rate != null && (
                  <p style={{ margin: '8px 0 0', fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)' }}>
                    多轮正确率{' '}
                    <strong>{(runtimeStabilityVerdict.quality_stability.metrics.pass_rate * 100).toFixed(0)}%</strong>
                    （{runtimeStabilityVerdict.quality_stability.metrics.pass_total ?? 0}/
                    {runtimeStabilityVerdict.quality_stability.metrics.judge_total ?? 0} 轮通过）
                    {runtimeStabilityVerdict.quality_stability.metrics.avg_std != null && (
                      <> · 质量波动 std {runtimeStabilityVerdict.quality_stability.metrics.avg_std.toFixed(4)}</>
                    )}
                  </p>
                )}
              </div>
            )}

          {data.chain_stability && (
            <div style={{ marginBottom: 'var(--edp-space-4)', display: 'grid', gap: 'var(--edp-space-3)' }}>
              <div style={{ padding: 14, background: 'var(--edp-primary-soft)', border: '1px solid var(--edp-primary-soft)', borderRadius: 'var(--edp-radius-sm)' }}>
                  <h4 style={{ margin: '0 0 8px', fontSize: 'var(--edp-font-lg)', color: 'var(--edp-primary)' }}>
                    过程链条参考
                    {onOpenCheckpointCompliance && (
                      <button
                        type="button"
                        onClick={onOpenCheckpointCompliance}
                        style={{
                          marginLeft: 'var(--edp-space-2)',
                          fontSize: 'var(--edp-font-sm)',
                          fontWeight: 500,
                          color: 'var(--edp-primary)',
                          background: 'none',
                          border: 'none',
                          cursor: 'pointer',
                          textDecoration: 'underline',
                          padding: 0,
                        }}
                      >
                        详见过程一致性 Tab →
                      </button>
                    )}
                  </h4>
                  <p style={{ margin: 0, fontSize: 'var(--edp-font-md)' }}>
                    {data.chain_stability.chain_pass_rate != null && (
                      <>
                        全轮通过率{' '}
                        <strong>{(data.chain_stability.chain_pass_rate * 100).toFixed(0)}%</strong>
                        （{data.chain_stability.questions_all_rounds_ok ?? 0}/
                        {data.chain_stability.question_count ?? 0} 题）
                      </>
                    )}
                    {data.chain_stability.weakest_chain && (
                      <span style={{ marginLeft: 'var(--edp-space-2)', color: 'var(--edp-amber)' }}>
                        最弱链条：{data.chain_stability.weakest_chain.name}{' '}
                        {data.chain_stability.weakest_chain.pass_rate}
                      </span>
                    )}
                  </p>
                </div>
            </div>
          )}

          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--edp-space-3)', alignItems: 'center', marginBottom: 'var(--edp-space-4)' }}>
            <label style={{ fontSize: 'var(--edp-font-md)', fontWeight: 500 }}>选择题目</label>
            <select
              value={compareQ ?? ''}
              onChange={e => {
                const q = Number(e.target.value)
                if (!Number.isNaN(q)) openStabilityCompare(q)
              }}
              style={{ padding: '6px 10px', borderRadius: 'var(--edp-radius-sm)', border: '1px solid var(--edp-border)', fontSize: 'var(--edp-font-md)', minWidth: 200 }}
            >
              {(stabilityQuestions.length > 0
                ? stabilityQuestions
                : [...runsByQuestion.entries()]
                    .filter(([, list]) => list.length > 1)
                    .map(([qid, list]) => ({
                      question_id: qid,
                      round_count: list.length,
                      is_focus: qid === data.stability_focus?.question_id,
                    }))
              ).map(sq => (
                <option key={sq.question_id} value={sq.question_id}>
                  #{sq.question_id} · {sq.round_count ?? '—'} 轮
                  {sq.is_focus ? ' · 波动最大' : ''}
                </option>
              ))}
            </select>
            {compareQ != null && (
              <span style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)' }}>
                {(runsByQuestion.get(compareQ) ?? []).length} 轮 Run · 查看各轮时延与 Token 波动
              </span>
            )}
          </div>

          {compareQ != null && (() => {
            const sq = stabilityQuestions.find(x => x.question_id === compareQ)
            if (!sq) return null
            return (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 10, marginBottom: 'var(--edp-space-4)' }}>
                {[
                  {
                    label: '时延范围',
                    value:
                      sq.latency_min_ms != null && sq.latency_max_ms != null
                        ? `${formatDuration(Math.round(sq.latency_min_ms))} – ${formatDuration(Math.round(sq.latency_max_ms))}`
                        : '—',
                  },
                  {
                    label: 'Tokens 范围',
                    value:
                      sq.tokens_min != null && sq.tokens_max != null
                        ? `${fmtTokens(sq.tokens_min)} – ${fmtTokens(sq.tokens_max)}`
                        : '—',
                  },
                  {
                    label: 'LLM 调用',
                    value:
                      sq.llm_call_min != null && sq.llm_call_max != null
                        ? `${sq.llm_call_min} – ${sq.llm_call_max}`
                        : '—',
                  },
                  {
                    label: '工具调用',
                    value:
                      sq.tool_call_min != null && sq.tool_call_max != null
                        ? `${sq.tool_call_min} – ${sq.tool_call_max}`
                        : '—',
                  },
                  {
                    label: '正确性范围',
                    value:
                      sq.correctness_min != null && sq.correctness_max != null
                        ? `${sq.correctness_min.toFixed(2)} – ${sq.correctness_max.toFixed(2)}`
                        : '—',
                    highlight:
                      sq.correctness_pass_rate != null && sq.correctness_pass_rate < 0.6
                        ? 'warn'
                        : undefined,
                    sub:
                      sq.correctness_pass_count != null && sq.round_count != null
                        ? `通过 ${sq.correctness_pass_count}/${sq.round_count}`
                        : undefined,
                  },
                ].map(card => (
                  <div key={card.label} style={{
                    border: card.highlight === 'warn' ? '1px solid var(--edp-red-soft)' : '1px solid var(--edp-blue-soft)',
                    background: card.highlight === 'warn' ? 'var(--edp-red-soft)' : 'var(--edp-blue-soft)',
                    borderRadius: 'var(--edp-radius-sm)',
                    padding: 'var(--edp-space-3)',
                  }}>
                    <div style={{ fontSize: 'var(--edp-font-sm)', color: card.highlight === 'warn' ? 'var(--edp-red)' : 'var(--edp-blue)' }}>{card.label}</div>
                    <div style={{ fontSize: 15, fontWeight: 600, marginTop: 'var(--edp-space-1)', color: card.highlight === 'warn' ? 'var(--edp-red)' : undefined }}>
                      {card.value}
                    </div>
                    {card.sub && (
                      <div style={{ fontSize: 'var(--edp-font-sm)', color: 'var(--edp-text3)', marginTop: 2 }}>{card.sub}</div>
                    )}
                  </div>
                ))}
              </div>
            )
          })()}

          {compareQ != null &&
            (runsByQuestion.get(compareQ) ?? []).length > 0 && (
              <IterationLatencyChart
                runs={(runsByQuestion.get(compareQ) ?? []).map(r => ({
                  agent_run_id: r.agent_run_id,
                  run_index: r.run_index,
                  latency_ms: r.latency_ms,
                  tokens_total: r.tokens_total,
                  correctness_score: r.correctness_score,
                  correctness_passed: r.correctness_passed,
                }))}
                questionId={compareQ}
              />
            )}

          {compareQ != null &&
            (runsByQuestion.get(compareQ) ?? []).length > 1 && (
              <MultiRoundSpanTreePanel
                runs={(runsByQuestion.get(compareQ) ?? []).map(r => ({
                  agent_run_id: r.agent_run_id,
                  run_index: r.run_index,
                }))}
                questionId={compareQ}
              />
            )}
        </>
      )}

      {view === 'task' && (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 'var(--edp-space-3)', marginBottom: 'var(--edp-space-5)' }}>
            {[
              { label: 'LLM 调用', value: s.llm_call_count, sub: `本任务合计 · 均 ${s.avg_llm_per_question ?? '—'}/题` },
              { label: '工具调用', value: s.tool_call_count, sub: `本任务合计 · 均 ${s.avg_tool_per_question ?? '—'}/题` },
              { label: '总 Tokens', value: s.tokens_total != null ? fmtTokens(s.tokens_total) : '—', sub: `均 ${fmtTokens(s.avg_tokens_per_question != null ? Math.round(s.avg_tokens_per_question) : null)}/题` },
              { label: '平均时延', value: s.avg_latency_ms != null ? formatDuration(Math.round(s.avg_latency_ms)) : '—', sub: `${s.run_count ?? 0} 个 Run` },
              ...(s.avg_loop_iterations != null
                ? [{ label: '主循环轮次', value: s.avg_loop_iterations, sub: 'agent.loop.iteration 均' }]
                : []),
              ...(s.checkpoint_compliance_rate != null
                ? [{
                    label: '必达合规率',
                    value: fmtPct(s.checkpoint_compliance_rate),
                    sub: onOpenCheckpointCompliance
                      ? `仅统计必达过程点${s.checkpoint_optional_hit_rate != null ? ` · 可选命中 ${fmtPct(s.checkpoint_optional_hit_rate)}` : ''} · 质量 Tab 明细`
                      : s.checkpoint_optional_hit_rate != null
                        ? `可选命中 ${fmtPct(s.checkpoint_optional_hit_rate)}`
                        : '仅统计必达过程点',
                    onClick: onOpenCheckpointCompliance,
                  }]
                : []),
            ].map(k => (
              <div
                key={k.label}
                style={{
                  border: '1px solid var(--edp-amber-soft)',
                  background: 'var(--edp-amber-soft)',
                  borderRadius: 'var(--edp-radius-sm)',
                  padding: 14,
                  cursor: k.onClick ? 'pointer' : undefined,
                }}
                onClick={k.onClick}
                role={k.onClick ? 'button' : undefined}
              >
                <div style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)' }}>{k.label}</div>
                <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--edp-amber)' }}>{k.value ?? '—'}</div>
                <div style={{ fontSize: 'var(--edp-font-sm)', color: 'var(--edp-text3)', marginTop: 'var(--edp-space-1)' }}>{k.sub}</div>
              </div>
            ))}
          </div>

          {hasMultiRound && data.chain_stability && (
            <div style={{ marginBottom: 'var(--edp-space-5)', display: 'grid', gap: 'var(--edp-space-3)' }}>
              <div style={{ padding: 14, background: 'var(--edp-blue-soft)', border: '1px solid var(--edp-blue-soft)', borderRadius: 'var(--edp-radius-sm)' }}>
                  <h4 style={{ margin: '0 0 8px', fontSize: 'var(--edp-font-lg)', color: 'var(--edp-blue)' }}>关键链条稳定性</h4>
                  <p style={{ margin: 0, fontSize: 'var(--edp-font-md)' }}>
                    {data.chain_stability.chain_pass_rate != null && (
                      <>
                        全轮通过率{' '}
                        <strong>{(data.chain_stability.chain_pass_rate * 100).toFixed(0)}%</strong>
                        （{data.chain_stability.questions_all_rounds_ok ?? 0}/
                        {data.chain_stability.question_count ?? 0} 题 · 每题最多{' '}
                        {data.chain_stability.max_rounds_per_question ?? s.inference_rounds ?? '—'} 轮）
                      </>
                    )}
                    {data.chain_stability.weakest_chain && (
                      <span style={{ marginLeft: 'var(--edp-space-2)', color: 'var(--edp-amber)' }}>
                        最弱链条：{data.chain_stability.weakest_chain.name}{' '}
                        {data.chain_stability.weakest_chain.pass_rate}
                      </span>
                    )}
                  </p>
                  {(data.chain_stability.order_sensitive_checkpoints?.length ?? 0) > 0 && (
                    <ul style={{ margin: '8px 0 0', paddingLeft: 18, fontSize: 'var(--edp-font-base)' }}>
                      {data.chain_stability.order_sensitive_checkpoints!
                        .filter(c => c.enforce_order)
                        .slice(0, 5)
                        .map(c => (
                          <li key={c.name}>
                            {c.name}：通过 {c.pass_rate}
                            {c.order_ok_rate ? ` · 顺序 ${c.order_ok_rate}` : ''}
                          </li>
                        ))}
                    </ul>
                  )}
                </div>
            </div>
          )}

          {tc && (tc.inference_latency_ms || tc.tokens_total) && (
            <div style={{ marginBottom: 'var(--edp-space-5)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
                <h4 style={{ margin: 0, fontSize: 'var(--edp-font-lg)' }}>
                  <HelpTip
                    label="与目标 / 本任务分布对比"
                    tip={<TaskPercentileTipBody />}
                    align="left"
                  />
                </h4>
                {onOpenRuntimeTargets && (
                  <button
                    type="button"
                    onClick={onOpenRuntimeTargets}
                    style={{
                      fontSize: 'var(--edp-font-base)',
                      padding: '6px 12px',
                      border: '1px solid var(--edp-amber-soft)',
                      borderRadius: 'var(--edp-radius-sm)',
                      background: 'var(--edp-amber-soft)',
                      color: 'var(--edp-amber)',
                      cursor: 'pointer',
                    }}
                  >
                    调整本任务目标
                  </button>
                )}
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--edp-space-3)' }}>
                {tc.inference_latency_ms?.target_ms != null && (
                  <TargetCard
                    title="推理时延"
                    value={
                      tc.inference_latency_ms.current_ms != null
                        ? formatDuration(Math.round(tc.inference_latency_ms.current_ms))
                        : '—'
                    }
                    status={tc.inference_latency_ms.status}
                    sub={`目标 ≤ ${formatDuration(tc.inference_latency_ms.target_ms)} · 本任务 P75 ${tc.inference_latency_ms.task_p75 != null ? formatDuration(tc.inference_latency_ms.task_p75) : '—'} · P90 ${tc.inference_latency_ms.task_p90 != null ? formatDuration(tc.inference_latency_ms.task_p90) : '—'}`}
                    ratio={
                      tc.inference_latency_ms.current_ms != null && tc.inference_latency_ms.target_ms
                        ? Math.min(100, (tc.inference_latency_ms.current_ms / tc.inference_latency_ms.target_ms) * 100)
                        : 0
                    }
                  />
                )}
                {tc.tokens_total?.target != null && (
                  <TargetCard
                    title="Tokens 总消耗"
                    value={
                      tc.tokens_total.current_avg != null
                        ? `${fmtTokens(Math.round(tc.tokens_total.current_avg))}/题`
                        : '—'
                    }
                    status={tc.tokens_total.status}
                    sub={`目标 ≤ ${fmtTokens(tc.tokens_total.target)}/题 · 本任务 P75 ${tc.tokens_total.task_p75 != null ? fmtTokens(tc.tokens_total.task_p75) : '—'} · P90 ${tc.tokens_total.task_p90 != null ? fmtTokens(tc.tokens_total.task_p90) : '—'}`}
                    ratio={
                      tc.tokens_total.current_avg != null && tc.tokens_total.target
                        ? Math.min(100, (tc.tokens_total.current_avg / tc.tokens_total.target) * 100)
                        : 0
                    }
                  />
                )}
              </div>
            </div>
          )}

          {(dist?.inference_latency_ms?.p75 != null || dist?.tokens_total?.p75 != null) && (
            <div style={{ marginBottom: 'var(--edp-space-5)', padding: 14, background: 'var(--edp-bg)', border: '1px solid var(--edp-border-soft)', borderRadius: 'var(--edp-radius-sm)' }}>
              <h4 style={{ margin: '0 0 8px', fontSize: 'var(--edp-font-lg)' }}>
                <HelpTip
                  label="本任务 Run 分布（P75 / P90）"
                  tip={<TaskPercentileTipBody />}
                  align="left"
                />
              </h4>
              <p className="results-section-desc" style={{ marginBottom: 10 }}>
                统计本任务内各题 Run。每题通常 1 条样本；同题多轮时每轮各算 1 条。
                {dist?.sample_count ? ` 有效样本 ${dist.sample_count} 条` : ''}
                {dist?.latency_sample_count != null && dist?.tokens_sample_count != null &&
                dist.latency_sample_count !== dist.tokens_sample_count ? (
                  <span>
                    {' '}
                    （时延 {dist.latency_sample_count} · Tokens {dist.tokens_sample_count}）
                  </span>
                ) : null}
                。
              </p>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--edp-space-3)', fontSize: 'var(--edp-font-md)' }}>
                {dist?.inference_latency_ms?.sample_count ? (
                  <div>
                    <strong>推理时延</strong>
                    <div style={{ color: 'var(--edp-text2)', marginTop: 'var(--edp-space-1)' }}>
                      P75: {formatDuration(dist.inference_latency_ms.p75 ?? 0)} · P90:{' '}
                      {formatDuration(dist.inference_latency_ms.p90 ?? 0)}
                    </div>
                    {s.avg_latency_ms != null && dist.inference_latency_ms.p90 != null && (
                      <div style={{ fontSize: 'var(--edp-font-base)', color: s.avg_latency_ms > dist.inference_latency_ms.p90 ? 'var(--edp-red)' : 'var(--edp-green)', marginTop: 'var(--edp-space-1)' }}>
                        本任务均时延 {formatDuration(Math.round(s.avg_latency_ms))}
                        {s.avg_latency_ms > dist.inference_latency_ms.p90 ? ' · 高于本任务 P90' : ' · 在本任务 P90 内'}
                      </div>
                    )}
                  </div>
                ) : null}
                {dist?.tokens_total?.sample_count ? (
                  <div>
                    <strong>Tokens（单 Run）</strong>
                    <div style={{ color: 'var(--edp-text2)', marginTop: 'var(--edp-space-1)' }}>
                      P75: {fmtTokens(dist.tokens_total.p75)} · P90: {fmtTokens(dist.tokens_total.p90)}
                    </div>
                    {s.avg_tokens_per_question != null && dist.tokens_total.p90 != null && (
                      <div style={{ fontSize: 'var(--edp-font-base)', color: s.avg_tokens_per_question > dist.tokens_total.p90 ? 'var(--edp-red)' : 'var(--edp-green)', marginTop: 'var(--edp-space-1)' }}>
                        本任务均 Tokens {fmtTokens(Math.round(s.avg_tokens_per_question))}
                        {s.avg_tokens_per_question > dist.tokens_total.p90 ? ' · 高于本任务 P90' : ' · 在本任务 P90 内'}
                      </div>
                    )}
                  </div>
                ) : null}
              </div>
            </div>
          )}

          {tokParts > 0 && (
            <div style={{ marginBottom: 'var(--edp-space-5)' }}>
              <h4 style={{ margin: '0 0 10px', fontSize: 'var(--edp-font-lg)' }}>Token 构成（本任务合计）</h4>
              <div style={{ display: 'grid', gridTemplateColumns: '100px 1fr 80px', gap: 10, alignItems: 'center', marginBottom: 'var(--edp-space-2)', fontSize: 'var(--edp-font-md)' }}>
                <span>LLM</span>
                <div style={{ height: 20, background: 'var(--edp-primary-soft)', borderRadius: 'var(--edp-radius-sm)', overflow: 'hidden' }}>
                  <div style={{ height: '100%', width: `${Math.round((tokLlm / tokParts) * 100)}%`, background: 'linear-gradient(90deg,var(--edp-primary),var(--edp-primary))' }} />
                </div>
                <span style={{ textAlign: 'right' }}>{fmtTokens(tokLlm)}</span>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '100px 1fr 80px', gap: 10, alignItems: 'center', fontSize: 'var(--edp-font-md)' }}>
                <span>工具</span>
                <div style={{ height: 20, background: 'var(--edp-primary-soft)', borderRadius: 'var(--edp-radius-sm)', overflow: 'hidden' }}>
                  <div style={{ height: '100%', width: `${Math.round((tokTool / tokParts) * 100)}%`, background: 'linear-gradient(90deg,var(--edp-amber-soft),var(--edp-amber))' }} />
                </div>
                <span style={{ textAlign: 'right' }}>{fmtTokens(tokTool)}</span>
              </div>
            </div>
          )}

          {(data.tool_breakdown?.length ?? 0) > 0 && (
            <div style={{ marginBottom: 'var(--edp-space-5)' }}>
              <h4 style={{ margin: '0 0 10px', fontSize: 'var(--edp-font-lg)' }}>工具调用分布（本任务合计）</h4>
              {data.tool_breakdown!.map(t => (
                <div key={t.tool} style={{ display: 'grid', gridTemplateColumns: '120px 1fr 48px', gap: 10, alignItems: 'center', marginBottom: 'var(--edp-space-2)', fontSize: 'var(--edp-font-md)' }}>
                  <code>{t.tool}</code>
                  <div style={{ height: 20, background: 'var(--edp-primary-soft)', borderRadius: 'var(--edp-radius-sm)', overflow: 'hidden' }}>
                    <div style={{ height: '100%', width: `${Math.round(t.share * 100)}%`, background: 'linear-gradient(90deg,var(--edp-primary),var(--edp-primary))' }} />
                  </div>
                  <span style={{ textAlign: 'right' }}>{t.count}次</span>
                </div>
              ))}
            </div>
          )}

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--edp-space-4)', marginBottom: cpAttr.length ? 20 : 0 }}>
            <FocusTable
              title="耗时最长"
              rows={data.focus_questions?.slowest ?? []}
              field="latency_ms"
              onDrill={openDrill}
              onOpenQuality={onOpenQualityForQuestion}
            />
            <FocusTable
              title="Token 消耗最多"
              rows={data.focus_questions?.costliest ?? []}
              field="tokens_total"
              onDrill={openDrill}
              onOpenQuality={onOpenQualityForQuestion}
            />
          </div>

          {cpAttr.length > 0 && (
            <div style={{ border: '1px dashed var(--edp-primary-soft)', borderRadius: 'var(--edp-radius-sm)', padding: 'var(--edp-space-4)', background: 'var(--edp-bg)' }}>
              <h4 style={{ margin: '0 0 8px', fontSize: 'var(--edp-font-lg)' }}>过程点效能归因</h4>
              <p style={{ margin: '0 0 12px', fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)', lineHeight: 1.6 }}>
                通过 checkpoint_results 命中 Span 关联耗时/Tokens，不重跑 match_rules。
                {s.checkpoint_compliance_rate != null && (
                  <> 必达合规率 {fmtPct(s.checkpoint_compliance_rate)}</>
                )}
                {s.checkpoint_optional_hit_rate != null && (
                  <> · 可选命中率 {fmtPct(s.checkpoint_optional_hit_rate)}</>
                )}
                。
              </p>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 'var(--edp-font-md)' }}>
                <thead>
                  <tr style={{ background: 'var(--edp-primary-soft)', textAlign: 'left' }}>
                    <th style={{ padding: 'var(--edp-space-2)' }}>过程点</th>
                    <th style={{ padding: 'var(--edp-space-2)' }}>命中率</th>
                    <th style={{ padding: 'var(--edp-space-2)' }}>通过率</th>
                    <th style={{ padding: 'var(--edp-space-2)' }}>均耗时</th>
                    <th style={{ padding: 'var(--edp-space-2)' }}>均 Tokens</th>
                    <th style={{ padding: 'var(--edp-space-2)' }}>评估 Run 数</th>
                    <th style={{ padding: 'var(--edp-space-2)' }}></th>
                  </tr>
                </thead>
                <tbody>
                  {cpAttr.map(row => (
                    <tr key={row.checkpoint_id}>
                      <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)' }}>
                        {row.name}
                        {row.required && (
                          <span style={{ marginLeft: 'var(--edp-space-1)', fontSize: 'var(--edp-font-xs)', color: 'var(--edp-red)' }}>必填</span>
                        )}
                      </td>
                      <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)' }}>{fmtPct(row.hit_rate)}</td>
                      <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)' }}>{fmtPct(row.pass_rate)}</td>
                      <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)' }}>
                        {row.avg_latency_ms != null ? formatDuration(Math.round(row.avg_latency_ms)) : '—'}
                      </td>
                      <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)' }}>
                        {row.avg_tokens != null ? fmtTokens(Math.round(row.avg_tokens)) : '—'}
                      </td>
                      <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)' }}>{row.evaluated_runs ?? '—'}</td>
                      <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)' }}>
                        {onOpenCheckpointCompliance && (
                          <button
                            type="button"
                            style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-primary)', background: 'none', border: 'none', cursor: 'pointer' }}
                            onClick={onOpenCheckpointCompliance}
                          >
                            过程合规
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {view === 'run' && (
        <>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 'var(--edp-font-md)', marginBottom: 'var(--edp-space-4)' }}>
            <thead>
              <tr style={{ background: 'var(--edp-primary-soft)', textAlign: 'left' }}>
                <th style={{ padding: 'var(--edp-space-2)' }}>题号</th>
                {hasMultiRound && <th style={{ padding: 'var(--edp-space-2)' }}>轮次</th>}
                <th style={{ padding: 'var(--edp-space-2)', textAlign: 'right' }}>LLM</th>
                <th style={{ padding: 'var(--edp-space-2)', textAlign: 'right' }}>工具</th>
                <th style={{ padding: 'var(--edp-space-2)', textAlign: 'right' }}>时延</th>
                <th style={{ padding: 'var(--edp-space-2)', textAlign: 'right' }}>Tokens</th>
                <th style={{ padding: 'var(--edp-space-2)', textAlign: 'right' }}>正确性</th>
                <th style={{ padding: 'var(--edp-space-2)' }}>推理顺序摘要</th>
                <th style={{ padding: 'var(--edp-space-2)' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {(data.run_rows ?? []).map(r => {
                const multi = (runsByQuestion.get(r.question_id ?? 0)?.length ?? 0) > 1
                const selected = drillRunId === r.agent_run_id
                return (
                  <tr
                    key={r.agent_run_id}
                    style={{ background: selected ? 'var(--edp-primary-soft)' : undefined }}
                  >
                    <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)' }}>#{r.question_id}</td>
                    {hasMultiRound && (
                      <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)' }}>
                        第 {(r.run_index ?? 0) + 1} 轮
                        {multi && (
                          <span style={{ marginLeft: 'var(--edp-space-1)', fontSize: 'var(--edp-font-xs)', padding: '1px 6px', background: 'var(--edp-blue-soft)', color: 'var(--edp-blue)', borderRadius: 3 }}>多轮</span>
                        )}
                      </td>
                    )}
                    <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)', textAlign: 'right' }}>{r.llm_call_count ?? '—'}</td>
                    <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)', textAlign: 'right' }}>{r.tool_call_count ?? '—'}</td>
                    <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)', textAlign: 'right' }}>
                      {r.latency_ms != null ? formatDuration(r.latency_ms) : '—'}
                    </td>
                    <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)', textAlign: 'right' }}>{fmtTokens(r.tokens_total)}</td>
                    <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)', textAlign: 'right' }}>
                      {r.correctness_score != null ? (
                        <span style={{
                          fontWeight: 600,
                          color: r.correctness_passed === false ? 'var(--edp-red)' : 'var(--edp-green)',
                        }}>
                          {r.correctness_score.toFixed(2)}
                          {r.correctness_passed === false ? ' ✘' : ' ✓'}
                        </span>
                      ) : (
                        <span style={{ color: 'var(--edp-text3)' }}>—</span>
                      )}
                    </td>
                    <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)', fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)' }}>{r.step_summary ?? '—'}</td>
                    <td style={{ padding: 'var(--edp-space-2)', borderTop: '1px solid var(--edp-border-soft)', whiteSpace: 'nowrap' }}>
                      <button
                        type="button"
                        style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-primary)', background: 'none', border: 'none', cursor: 'pointer', marginRight: 'var(--edp-space-2)' }}
                        onClick={() => openDrill(r.agent_run_id)}
                      >
                        推理链
                      </button>
                      {multi && (
                        <button
                          type="button"
                          style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-blue)', background: 'none', border: 'none', cursor: 'pointer', marginRight: 'var(--edp-space-2)' }}
                          onClick={() => openStabilityCompare(r.question_id ?? 0)}
                        >
                          多轮稳定性
                        </button>
                      )}
                      {onOpenQualityForQuestion && r.question_id != null && (
                        <button
                          type="button"
                          style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-primary)', background: 'none', border: 'none', cursor: 'pointer' }}
                          onClick={() => onOpenQualityForQuestion(r.question_id!)}
                        >
                          质量判分
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>

        </>
      )}

      {drillRun && (
        <RunSpanTreePanel
          runId={drillRun.agent_run_id}
          questionId={drillRun.question_id}
          runIndex={drillRun.run_index}
          traceSyncStatus={drillRun.trace_sync_status}
          onClose={() => setDrillRunId(null)}
        />
      )}
    </div>
  )
}

function TargetCard({
  title,
  value,
  status,
  sub,
  ratio,
}: {
  title: string
  value: string
  status?: string | null
  sub: string
  ratio: number
}) {
  const ok = status === 'ok'
  return (
    <div style={{ border: '1px solid var(--edp-amber-soft)', background: 'var(--edp-amber-soft)', borderRadius: 'var(--edp-radius-sm)', padding: 14 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontWeight: 600, fontSize: 'var(--edp-font-md)' }}>{title}</span>
        {status && (
          <span
            style={{
              fontSize: 'var(--edp-font-sm)',
              fontWeight: 600,
              padding: '2px 8px',
              borderRadius: 'var(--edp-radius-sm)',
              background: ok ? 'var(--edp-green-soft)' : 'var(--edp-amber-soft)',
              color: ok ? 'var(--edp-green)' : 'var(--edp-amber)',
            }}
          >
            {ok ? '达标' : '偏高'}
          </span>
        )}
      </div>
      <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--edp-amber)', margin: '8px 0' }}>{value}</div>
      <div style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-2)' }}>{sub}</div>
      <div style={{ height: 8, background: 'var(--edp-amber-soft)', borderRadius: 'var(--edp-radius-sm)', overflow: 'hidden' }}>
        <div
          style={{
            height: '100%',
            width: `${Math.round(ratio)}%`,
            background: ok ? 'var(--edp-green)' : 'var(--edp-amber)',
          }}
        />
      </div>
    </div>
  )
}

function FocusTable({
  title,
  rows,
  field,
  onDrill,
  onOpenQuality,
}: {
  title: string
  rows: RunRow[]
  field: 'latency_ms' | 'tokens_total'
  onDrill: (runId: string) => void
  onOpenQuality?: (questionId: number) => void
}) {
  return (
    <div style={{ border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', overflow: 'hidden' }}>
      <h4 style={{ margin: 0, padding: '10px 14px', fontSize: 'var(--edp-font-md)', background: 'var(--edp-bg)', borderBottom: '1px solid var(--edp-border)' }}>{title}</h4>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 'var(--edp-font-md)' }}>
        <tbody>
          {rows.map(r => (
            <tr key={r.agent_run_id}>
              <td style={{ padding: 'var(--edp-space-2)' }}>#{r.question_id}</td>
              <td style={{ padding: 'var(--edp-space-2)', textAlign: 'right' }}>
                {field === 'latency_ms'
                  ? r.latency_ms != null
                    ? formatDuration(r.latency_ms)
                    : '—'
                  : fmtTokens(r.tokens_total)}
              </td>
              <td style={{ padding: 'var(--edp-space-2)', whiteSpace: 'nowrap' }}>
                <button
                  type="button"
                  style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-primary)', background: 'none', border: 'none', cursor: 'pointer', marginRight: 'var(--edp-space-2)' }}
                  onClick={() => onDrill(r.agent_run_id)}
                >
                  推理链
                </button>
                {onOpenQuality && r.question_id != null && (
                  <button
                    type="button"
                    style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-primary)', background: 'none', border: 'none', cursor: 'pointer' }}
                    onClick={() => onOpenQuality(r.question_id!)}
                  >
                    质量判分
                  </button>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={3} style={{ padding: 'var(--edp-space-3)', color: 'var(--edp-text3)', fontSize: 'var(--edp-font-base)' }}>暂无数据</td></tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
