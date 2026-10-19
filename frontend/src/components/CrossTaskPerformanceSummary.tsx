import { formatDuration } from '../utils/dateUtils'

export interface PerformanceSummaryRow {
  member_id: string
  task_id: string
  label: string
  is_baseline?: boolean
  llm_call_count?: number
  tool_call_count?: number
  avg_llm_per_question?: number | null
  avg_tool_per_question?: number | null
  avg_tokens_total?: number | null
  avg_inference_latency_ms?: number | null
  checkpoint_compliance_rate?: number | null
  vs_baseline?: {
    llm_delta?: number | null
    tool_delta?: number | null
    avg_llm_delta?: number | null
    avg_tool_delta?: number | null
    tokens_delta?: number | null
    latency_delta_ms?: number | null
    compliance_delta?: number | null
  } | null
}

export interface PerformanceSummariesPayload {
  baseline_task_id: string
  rows: PerformanceSummaryRow[]
}

interface Props {
  summaries: PerformanceSummariesPayload
}

function fmtDelta(v: number | null | undefined, suffix = ''): string {
  if (v == null || Number.isNaN(v)) return '—'
  const sign = v > 0 ? '+' : ''
  return `${sign}${Number.isInteger(v) ? v : v.toFixed(2)}${suffix}`
}

export function CrossTaskPerformanceSummary({ summaries }: Props) {
  if (!summaries?.rows?.length) return null

  return (
    <div
      style={{
        marginBottom: 20,
        padding: '12px 16px',
        background: '#fff8f0',
        borderRadius: 8,
        border: '1px solid #ffe0b2',
      }}
    >
      <h4 style={{ margin: '0 0 8px 0', fontSize: 14, fontWeight: 600, color: '#e65100' }}>
        跨任务运行效能结构对比
      </h4>
      <p style={{ margin: '0 0 12px 0', fontSize: 12, color: '#666' }}>
        LLM/工具调用次数与均 Tokens、时延；合规率为必达过程点合规（不含可选未执行）。Δ 为相对基线任务（
        {summaries.baseline_task_id.slice(0, 8)}…）。不参与质量总分。
      </p>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ background: '#fff3e0', textAlign: 'left' }}>
              <th style={{ padding: 8 }}>任务 / 版本</th>
              <th style={{ padding: 8 }}>LLM 合计</th>
              <th style={{ padding: 8 }}>工具合计</th>
              <th style={{ padding: 8 }}>均 LLM/题</th>
              <th style={{ padding: 8 }}>均工具/题</th>
              <th style={{ padding: 8 }}>均 Tokens</th>
              <th style={{ padding: 8 }}>均时延</th>
              <th style={{ padding: 8 }}>必达合规率</th>
              <th style={{ padding: 8 }}>Δ LLM</th>
              <th style={{ padding: 8 }}>Δ 工具</th>
              <th style={{ padding: 8 }}>Δ 必达合规</th>
            </tr>
          </thead>
          <tbody>
            {summaries.rows.map(row => (
              <tr key={row.member_id}>
                <td style={{ padding: 8, borderTop: '1px solid #ffe0b2' }}>
                  {row.label}
                  {row.is_baseline && (
                    <span
                      style={{
                        marginLeft: 6,
                        fontSize: 11,
                        color: '#e65100',
                        fontWeight: 600,
                      }}
                    >
                      基线
                    </span>
                  )}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #ffe0b2' }}>
                  {row.llm_call_count ?? '—'}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #ffe0b2' }}>
                  {row.tool_call_count ?? '—'}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #ffe0b2' }}>
                  {row.avg_llm_per_question ?? '—'}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #ffe0b2' }}>
                  {row.avg_tool_per_question ?? '—'}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #ffe0b2' }}>
                  {row.avg_tokens_total != null ? Math.round(row.avg_tokens_total) : '—'}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #ffe0b2' }}>
                  {row.avg_inference_latency_ms != null
                    ? formatDuration(row.avg_inference_latency_ms)
                    : '—'}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #ffe0b2' }}>
                  {row.checkpoint_compliance_rate != null
                    ? `${(row.checkpoint_compliance_rate * 100).toFixed(1)}%`
                    : '—'}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #ffe0b2' }}>
                  {row.is_baseline ? '—' : fmtDelta(row.vs_baseline?.llm_delta)}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #ffe0b2' }}>
                  {row.is_baseline ? '—' : fmtDelta(row.vs_baseline?.tool_delta)}
                </td>
                <td style={{ padding: 8, borderTop: '1px solid #ffe0b2' }}>
                  {row.is_baseline
                    ? '—'
                    : row.vs_baseline?.compliance_delta != null
                      ? fmtDelta(row.vs_baseline.compliance_delta * 100, 'pp')
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
