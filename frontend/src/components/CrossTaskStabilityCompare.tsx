import { useEffect, useMemo, useState } from 'react'
import { formatDuration } from '../utils/dateUtils'
import type { QuestionRuntimePayload } from './CrossTaskQuestionRuntime'

const API_BASE = '/api'

interface MemberMeta {
  member_id: string
  label?: string
}

interface StabilitySide {
  round_count: number
  multi_round: boolean
  headline?: string | null
  sequence_jaccard_min_pair?: number | null
  step_count_min?: number | null
  step_count_max?: number | null
  latency_min_ms?: number | null
  latency_max_ms?: number | null
  tokens_min?: number | null
  tokens_max?: number | null
  llm_call_min?: number | null
  llm_call_max?: number | null
  tool_call_min?: number | null
  tool_call_max?: number | null
  unstable_step_count?: number | null
  structure_summary?: { headline?: string | null } | null
  round_stability?: {
    columns?: { run_id: string; run_index: number }[]
    rows?: RoundStabilityRow[]
  }
}

interface StabilityComparePayload {
  question_key?: string
  baseline_task_id?: string
  compare_task_id?: string
  eligible: boolean
  empty_reason?: string | null
  baseline_round_count?: number
  compare_round_count?: number
  baseline?: StabilitySide | null
  compare?: StabilitySide | null
  vs_summary?: {
    jaccard_delta?: number | null
    latency_spread_delta_ms?: number | null
    unstable_step_delta?: number | null
    more_stable_side?: 'baseline' | 'compare' | 'tie' | null
  } | null
}

interface Props {
  summary: QuestionRuntimePayload
  members: MemberMeta[]
}

function fmtRange(min?: number | null, max?: number | null, fmt?: (v: number) => string) {
  if (min == null || max == null) return '—'
  const f = fmt ?? String
  return min === max ? f(min) : `${f(min)} – ${f(max)}`
}

function fmtTokens(v: number) {
  return v.toLocaleString()
}

function MetricCards({ side, label }: { side: StabilitySide; label: string }) {
  const cards = [
    {
      title: '时延范围',
      value: fmtRange(side.latency_min_ms, side.latency_max_ms, v =>
        formatDuration(Math.round(v)),
      ),
    },
    {
      title: 'Tokens 范围',
      value: fmtRange(side.tokens_min, side.tokens_max, fmtTokens),
    },
    {
      title: 'LLM 调用',
      value: fmtRange(side.llm_call_min, side.llm_call_max),
    },
    {
      title: '工具调用',
      value: fmtRange(side.tool_call_min, side.tool_call_max),
    },
  ]

  return (
    <div style={{ flex: '1 1 280px', minWidth: 240 }}>
      <h5 style={{ margin: '0 0 8px', fontSize: 13, color: '#3949ab' }}>{label}</h5>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))',
          gap: 8,
        }}
      >
        {cards.map(c => (
          <div
            key={c.title}
            style={{
              border: '1px solid #d9def0',
              background: '#fff',
              borderRadius: 8,
              padding: 10,
            }}
          >
            <div style={{ fontSize: 11, color: '#666' }}>{c.title}</div>
            <div style={{ fontSize: 14, fontWeight: 600, marginTop: 4 }}>{c.value}</div>
          </div>
        ))}
      </div>
      {side.headline && (
        <p style={{ margin: '8px 0 0', fontSize: 12, color: '#555', lineHeight: 1.5 }}>
          {side.headline}
        </p>
      )}
    </div>
  )
}

export function CrossTaskStabilityCompare({ summary, members }: Props) {
  const labelById = useMemo(() => {
    const m: Record<string, string> = {}
    for (const it of members) m[it.member_id] = it.label || it.member_id
    return m
  }, [members])

  const questionOptions = summary?.rows ?? []
  const memberIds = summary?.member_ids ?? []

  const [questionKey, setQuestionKey] = useState('')
  const [baselineId, setBaselineId] = useState('')
  const [compareId, setCompareId] = useState('')
  const [payload, setPayload] = useState<StabilityComparePayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!questionKey && questionOptions.length) setQuestionKey(questionOptions[0].question_key)
  }, [questionOptions, questionKey])

  useEffect(() => {
    if (!baselineId && summary?.baseline_task_id) setBaselineId(summary.baseline_task_id)
  }, [summary, baselineId])

  useEffect(() => {
    if (compareId) return
    const firstOther = memberIds.find(id => id !== (summary?.baseline_task_id ?? baselineId))
    if (firstOther) setCompareId(firstOther)
  }, [memberIds, summary, baselineId, compareId])

  const localEligible = useMemo(() => {
    if (!questionKey || !baselineId || !compareId || baselineId === compareId) return false
    const row = questionOptions.find(q => q.question_key === questionKey)
    if (!row) return false
    const baseRuns = row.cells[baselineId]?.run_count ?? 0
    const cmpRuns = row.cells[compareId]?.run_count ?? 0
    return baseRuns >= 2 && cmpRuns >= 2
  }, [questionKey, baselineId, compareId, questionOptions])

  useEffect(() => {
    if (!questionKey || !baselineId || !compareId || baselineId === compareId) {
      setPayload(null)
      return
    }
    let alive = true
    setLoading(true)
    setError(null)
    const qs = new URLSearchParams({
      question_key: questionKey,
      baseline_task_id: baselineId,
      compare_task_id: compareId,
    })
    fetch(`${API_BASE}/scenario-eval/cross-task-stability-compare?${qs.toString()}`)
      .then(async r => {
        if (!r.ok) {
          const body = await r.json().catch(() => ({}))
          throw new Error(typeof body.detail === 'string' ? body.detail : '加载稳定性对比失败')
        }
        return r.json()
      })
      .then(d => {
        if (alive) setPayload(d)
      })
      .catch(e => {
        if (alive) {
          setError(e instanceof Error ? e.message : '加载失败')
          setPayload(null)
        }
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [questionKey, baselineId, compareId])

  if (!questionOptions.length || memberIds.length < 2) return null

  const selectStyle: React.CSSProperties = {
    border: '1px solid #cfd6e4',
    borderRadius: 6,
    padding: '4px 8px',
    fontSize: 12.5,
    background: '#fff',
    maxWidth: 260,
  }

  const baseLabel = labelById[baselineId] || baselineId
  const cmpLabel = labelById[compareId] || compareId

  return (
    <div
      style={{
        marginBottom: 20,
        padding: '12px 16px',
        background: '#f3f0ff',
        borderRadius: 8,
        border: '1px solid #d1c4e9',
      }}
    >
      <h4 style={{ margin: '0 0 4px 0', fontSize: 14, fontWeight: 600, color: '#5e35b1' }}>
        单题稳定性跨版本对比
      </h4>
      <p style={{ margin: '0 0 12px 0', fontSize: 12, color: '#666' }}>
        比较两版本各自的多轮波动（时延 / Tokens / 调用次数）。要求基线与对比版本对该题均 ≥2 轮。
      </p>

      <div
        style={{
          display: 'flex',
          gap: 16,
          flexWrap: 'wrap',
          alignItems: 'flex-end',
          marginBottom: 12,
        }}
      >
        <label style={{ fontSize: 12, color: '#555', display: 'flex', flexDirection: 'column', gap: 4 }}>
          题目
          <select style={selectStyle} value={questionKey} onChange={e => setQuestionKey(e.target.value)}>
            {questionOptions.map(q => (
              <option key={q.question_key} value={q.question_key}>
                {q.label}
              </option>
            ))}
          </select>
        </label>
        <label style={{ fontSize: 12, color: '#555', display: 'flex', flexDirection: 'column', gap: 4 }}>
          基线版本
          <select style={selectStyle} value={baselineId} onChange={e => setBaselineId(e.target.value)}>
            {memberIds.map(id => (
              <option key={id} value={id}>
                {labelById[id] || id}
              </option>
            ))}
          </select>
        </label>
        <label style={{ fontSize: 12, color: '#555', display: 'flex', flexDirection: 'column', gap: 4 }}>
          对比版本
          <select style={selectStyle} value={compareId} onChange={e => setCompareId(e.target.value)}>
            {memberIds.map(id => (
              <option key={id} value={id}>
                {labelById[id] || id}
              </option>
            ))}
          </select>
        </label>
      </div>

      {baselineId === compareId && (
        <p style={{ fontSize: 12, color: '#b26a00', margin: 0 }}>请选择两个不同版本进行对比。</p>
      )}

      {baselineId !== compareId && !localEligible && !loading && (
        <div
          style={{
            padding: 12,
            background: '#fff',
            border: '1px dashed #d1c4e9',
            borderRadius: 8,
            fontSize: 12.5,
            color: '#666',
            lineHeight: 1.6,
          }}
        >
          <strong style={{ color: '#5e35b1' }}>暂无法做跨版本稳定性对比</strong>
          <br />
          跨版本稳定性对比要求两版本对该题均执行过多轮评测（≥2 轮）。
          当前基线版本{' '}
          {questionOptions.find(q => q.question_key === questionKey)?.cells[baselineId]?.run_count ?? 0} 轮，
          对比版本{' '}
          {questionOptions.find(q => q.question_key === questionKey)?.cells[compareId]?.run_count ?? 0} 轮。
        </div>
      )}

      {loading && localEligible && (
        <p style={{ fontSize: 13, color: '#666', margin: 0 }}>加载稳定性对比…</p>
      )}
      {error && <p style={{ fontSize: 13, color: '#dc3545', margin: 0 }}>{error}</p>}

      {payload && !loading && payload.eligible && payload.baseline && payload.compare && (
        <>
          {payload.vs_summary && (
            <div
              style={{
                marginBottom: 12,
                padding: 10,
                background: '#ede7f6',
                borderRadius: 8,
                fontSize: 12.5,
                color: '#4527a0',
              }}
            >
              相对基线：时延波动 Δ{' '}
              {payload.vs_summary.latency_spread_delta_ms != null
                ? `${payload.vs_summary.latency_spread_delta_ms > 0 ? '+' : ''}${formatDuration(payload.vs_summary.latency_spread_delta_ms)}`
                : '—'}
              {payload.vs_summary.more_stable_side === 'baseline' && ' · 基线版本更稳定'}
              {payload.vs_summary.more_stable_side === 'compare' && ' · 对比版本更稳定'}
              {payload.vs_summary.more_stable_side === 'tie' && ' · 两版本稳定性接近'}
            </div>
          )}

          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, marginBottom: 16 }}>
            <MetricCards side={payload.baseline} label={`基线 · ${baseLabel}（${payload.baseline.round_count} 轮）`} />
            <MetricCards side={payload.compare} label={`对比 · ${cmpLabel}（${payload.compare.round_count} 轮）`} />
          </div>
        </>
      )}

      {payload && !loading && !payload.eligible && payload.empty_reason && localEligible && (
        <div
          style={{
            padding: 12,
            background: '#fff',
            border: '1px dashed #d1c4e9',
            borderRadius: 8,
            fontSize: 12.5,
            color: '#666',
          }}
        >
          {payload.empty_reason}
        </div>
      )}
    </div>
  )
}
