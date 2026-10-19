import { formatDuration } from '../utils/dateUtils'
import { ScenarioMatrixPayload } from './ScenarioMatrixView'
import type { PerformanceSummariesPayload } from './CrossTaskPerformanceSummary'
import type { QuestionRuntimePayload } from './CrossTaskQuestionRuntime'
import type { OverallVerdictPayload, StabilitySummariesPayload } from './OverallVerdictCard'

export interface OutcomeSummaryRow {
  member_id: string
  task_id: string
  label: string
  is_baseline?: boolean
  avg_correctness?: number | null
  pass_rate?: number | null
  avg_tokens_total?: number | null
  avg_inference_latency_ms?: number | null
  vs_baseline?: {
    correctness_delta?: number | null
    pass_rate_delta?: number | null
    tokens_delta?: number | null
    latency_delta_ms?: number | null
  } | null
}

export interface OutcomeSummariesPayload {
  baseline_task_id: string
  rows: OutcomeSummaryRow[]
}

interface Props {
  summaries: OutcomeSummariesPayload
}

function fmtScore(v: number | null | undefined, digits = 3): string {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toFixed(digits)
}

function fmtPct(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(1)}%`
}

function fmtDelta(v: number | null | undefined, suffix = ''): string {
  if (v == null || Number.isNaN(v)) return '—'
  const sign = v > 0 ? '+' : ''
  return `${sign}${Number.isInteger(v) ? v : v.toFixed(3)}${suffix}`
}

export function CrossTaskOutcomeSummary({ summaries }: Props) {
  if (!summaries?.rows?.length) return null

  return (
    <div
      style={{
        marginBottom: 20,
        padding: '12px 16px',
        background: '#f8f9fc',
        borderRadius: 8,
        border: '1px solid #e8eaf0',
      }}
    >
      <h4 style={{ margin: '0 0 8px 0', fontSize: 14, fontWeight: 600 }}>
        任务级 Outcome / 效率摘要
      </h4>
      <p style={{ margin: '0 0 12px 0', fontSize: 12, color: '#666' }}>
        各任务（Agent 版本）整体质量与运行效率；Δ 为相对基线任务（
        {summaries.baseline_task_id.slice(0, 8)}…）的差值。对比不同版本请在评估历史多选同评测集任务。
      </p>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ background: '#eef1f8', textAlign: 'left' }}>
              <th style={{ padding: 8 }}>任务 / 版本</th>
              <th style={{ padding: 8 }}>正确性均分</th>
              <th style={{ padding: 8 }}>通过率</th>
              <th style={{ padding: 8 }}>平均 Tokens</th>
              <th style={{ padding: 8 }}>平均时延</th>
              <th style={{ padding: 8 }}>Δ 正确性</th>
              <th style={{ padding: 8 }}>Δ Tokens</th>
              <th style={{ padding: 8 }}>Δ 时延</th>
            </tr>
          </thead>
          <tbody>
            {summaries.rows.map(row => (
              <tr key={row.member_id}>
                <td style={{ padding: 8, borderTop: '1px solid #e8eaf0' }}>
                  {row.label}
                  {row.is_baseline && (
                    <span
                      style={{
                        marginLeft: 6,
                        fontSize: 11,
                        color: '#667eea',
                        fontWeight: 600,
                      }}
                    >
                      基线
                    </span>
                  )}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #e8eaf0' }}>
                  {fmtScore(row.avg_correctness)}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #e8eaf0' }}>
                  {fmtPct(row.pass_rate)}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #e8eaf0' }}>
                  {row.avg_tokens_total != null ? Math.round(row.avg_tokens_total) : '—'}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #e8eaf0' }}>
                  {row.avg_inference_latency_ms != null
                    ? formatDuration(row.avg_inference_latency_ms)
                    : '—'}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #e8eaf0' }}>
                  {row.is_baseline
                    ? '—'
                    : fmtDelta(row.vs_baseline?.correctness_delta)}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #e8eaf0' }}>
                  {row.is_baseline ? '—' : fmtDelta(row.vs_baseline?.tokens_delta)}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #e8eaf0' }}>
                  {row.vs_baseline?.latency_delta_ms != null
                    ? `${row.vs_baseline.latency_delta_ms > 0 ? '+' : ''}${formatDuration(row.vs_baseline.latency_delta_ms)}`
                    : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export type CrossTaskMatrixPayload = ScenarioMatrixPayload & {
  outcome_summaries?: OutcomeSummariesPayload
  performance_summaries?: PerformanceSummariesPayload
  question_runtime_summary?: QuestionRuntimePayload
  stability_summaries?: StabilitySummariesPayload
  overall_verdict?: OverallVerdictPayload
}
