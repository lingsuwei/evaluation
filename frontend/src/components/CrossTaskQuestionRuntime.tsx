import { useMemo, useState } from 'react'
import { formatDuration } from '../utils/dateUtils'
import { useLangfuseHost } from '../utils/useLangfuseHost'

export interface QuestionRuntimeCell {
  missing: boolean
  run_count: number
  latency_ms?: number | null
  tokens_total?: number | null
  llm_call_count?: number | null
  tool_call_count?: number | null
  trace_id?: string | null
  agent_run_id?: string | null
  vs_baseline?: {
    latency_delta_ms?: number | null
    tokens_delta?: number | null
    llm_delta?: number | null
    tool_delta?: number | null
  } | null
}

export interface QuestionRuntimeRow {
  question_key: string
  question_id: number
  label: string
  cells: Record<string, QuestionRuntimeCell>
}

export interface QuestionRuntimePayload {
  baseline_task_id: string
  member_ids: string[]
  rows: QuestionRuntimeRow[]
  has_runtime: boolean
}

interface MemberMeta {
  member_id: string
  label?: string
}

interface Props {
  summary: QuestionRuntimePayload
  members: MemberMeta[]
}

function fmtNum(v: number | null | undefined, round = false): string {
  if (v == null || Number.isNaN(v)) return '—'
  return round ? String(Math.round(v)) : String(v)
}

function fmtDelta(v: number | null | undefined, suffix = ''): string {
  if (v == null || Number.isNaN(v)) return ''
  if (v === 0) return `±0${suffix}`
  const sign = v > 0 ? '+' : ''
  const num = Number.isInteger(v) ? v : v.toFixed(2)
  return `${sign}${num}${suffix}`
}

function deltaColor(v: number | null | undefined): string {
  if (v == null || v === 0) return '#999'
  return v > 0 ? '#c62828' : '#2e7d32'
}

export function CrossTaskQuestionRuntime({ summary, members }: Props) {
  const [expanded, setExpanded] = useState(false)
  const langfuseHost = useLangfuseHost()

  const labelById = useMemo(() => {
    const m: Record<string, string> = {}
    for (const it of members) m[it.member_id] = it.label || it.member_id
    return m
  }, [members])

  if (!summary?.rows?.length) return null

  const traceUrl = (traceId?: string | null) =>
    traceId && langfuseHost ? `${langfuseHost}/trace/${traceId}` : null

  const baselineId = summary.baseline_task_id
  const orderedMembers = summary.member_ids

  const metricDefs: {
    key: keyof QuestionRuntimeCell
    deltaKey: keyof NonNullable<QuestionRuntimeCell['vs_baseline']>
    label: string
    round?: boolean
    suffix?: string
  }[] = [
    { key: 'latency_ms', deltaKey: 'latency_delta_ms', label: '时延', round: true },
    { key: 'tokens_total', deltaKey: 'tokens_delta', label: 'Tokens', round: true },
    { key: 'llm_call_count', deltaKey: 'llm_delta', label: 'LLM' },
    { key: 'tool_call_count', deltaKey: 'tool_delta', label: '工具' },
  ]

  return (
    <div
      style={{
        marginBottom: 20,
        padding: '12px 16px',
        background: '#f3f8ff',
        borderRadius: 8,
        border: '1px solid #cfe2ff',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
          flexWrap: 'wrap',
        }}
      >
        <h4 style={{ margin: 0, fontSize: 14, fontWeight: 600, color: '#1565c0' }}>
          单题运行效能对比（同题 · 多版本）
        </h4>
        <button
          type="button"
          onClick={() => setExpanded(v => !v)}
          style={{
            border: '1px solid #90caf9',
            background: '#fff',
            color: '#1565c0',
            borderRadius: 6,
            padding: '4px 12px',
            fontSize: 12,
            cursor: 'pointer',
          }}
        >
          {expanded ? '收起明细' : `展开明细（${summary.rows.length} 题）`}
        </button>
      </div>
      <p style={{ margin: '6px 0 0 0', fontSize: 12, color: '#666' }}>
        逐题对齐各版本的时延 / Tokens / LLM / 工具调用（多轮取均值）。Δ 为相对基线版本（
        {labelById[baselineId] || baselineId.slice(0, 8)}）。绿降红升，仅供效能参考，不计入质量总分。
        {!summary.has_runtime && ' 当前所选任务暂无运行时指标。'}
      </p>

      {expanded && (
        <div style={{ overflowX: 'auto', marginTop: 12 }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
            <thead>
              <tr style={{ background: '#e3f0ff', textAlign: 'left' }}>
                <th style={{ padding: 8, minWidth: 160 }}>题目</th>
                <th style={{ padding: 8, minWidth: 150 }}>版本</th>
                {metricDefs.map(m => (
                  <th key={m.key} style={{ padding: 8 }}>
                    {m.label}
                  </th>
                ))}
                <th style={{ padding: 8 }}>Trace</th>
              </tr>
            </thead>
            <tbody>
              {summary.rows.map(row =>
                orderedMembers.map((mid, idx) => {
                  const cell = row.cells[mid]
                  const isBaseline = mid === baselineId
                  const url = traceUrl(cell?.trace_id)
                  return (
                    <tr key={`${row.question_key}-${mid}`}>
                      {idx === 0 && (
                        <td
                          rowSpan={orderedMembers.length}
                          style={{
                            padding: 8,
                            borderTop: '1px solid #cfe2ff',
                            verticalAlign: 'top',
                            fontWeight: 600,
                            color: '#333',
                          }}
                        >
                          {row.label}
                        </td>
                      )}
                      <td
                        style={{
                          padding: 8,
                          borderTop: idx === 0 ? '1px solid #cfe2ff' : '1px solid #eef3fb',
                        }}
                      >
                        {labelById[mid] || mid}
                        {isBaseline && (
                          <span style={{ marginLeft: 6, fontSize: 11, color: '#1565c0', fontWeight: 600 }}>
                            基线
                          </span>
                        )}
                      </td>
                      {metricDefs.map(m => {
                        const value = cell?.[m.key] as number | null | undefined
                        const delta = cell?.vs_baseline?.[m.deltaKey]
                        return (
                          <td
                            key={m.key}
                            style={{
                              padding: 8,
                              borderTop: idx === 0 ? '1px solid #cfe2ff' : '1px solid #eef3fb',
                            }}
                          >
                            {cell?.missing ? (
                              <span style={{ color: '#bbb' }}>未跑</span>
                            ) : (
                              <>
                                <span>{m.key === 'latency_ms' ? formatDuration(value) : fmtNum(value, m.round)}</span>
                                {!isBaseline && delta != null && (
                                  <span style={{ marginLeft: 6, fontSize: 11, color: deltaColor(delta) }}>
                                    {m.key === 'latency_ms' ? (delta != null ? `${delta > 0 ? '+' : ''}${formatDuration(delta)}` : '') : fmtDelta(delta)}
                                  </span>
                                )}
                              </>
                            )}
                          </td>
                        )
                      })}
                      <td
                        style={{
                          padding: 8,
                          borderTop: idx === 0 ? '1px solid #cfe2ff' : '1px solid #eef3fb',
                        }}
                      >
                        {url ? (
                          <a href={url} target="_blank" rel="noreferrer" style={{ color: '#1565c0', fontSize: 12 }}>
                            打开 Trace
                          </a>
                        ) : (
                          <span style={{ color: '#bbb' }}>—</span>
                        )}
                      </td>
                    </tr>
                  )
                }),
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
