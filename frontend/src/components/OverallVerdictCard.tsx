import type { ScenarioMatrixMember } from './ScenarioMatrixView'

export interface DimensionVerdictMember {
  member_id: string
  label: string
  value?: number | null
  normalized_score?: number | null
  status: 'better' | 'worse' | 'tie' | 'na'
  delta_vs_baseline?: number | null
  significant?: boolean
}

export interface DimensionVerdict {
  dimension: string
  display_name: string
  per_member: DimensionVerdictMember[]
  winner_member_id?: string | null
  winner_label?: string | null
  tie_threshold?: number
  applicable?: boolean
  weight?: number
}

export interface RankingRow {
  member_id: string
  label: string
  total_score?: number | null
  rank: number
  dimension_status: Record<string, string>
}

export interface OverallWinner {
  member_id: string
  label: string
  total_score?: number | null
  rank: number
  margin?: number | null
  is_significant?: boolean
}

export interface OverallVerdictPayload {
  overall_winner?: OverallWinner | null
  ranking: RankingRow[]
  dimension_verdicts: DimensionVerdict[]
  recommendation_text: string
  caveats: string[]
  weights?: Record<string, number>
  has_multi_round?: boolean
}

export interface StabilitySummaryRow {
  member_id: string
  task_id: string
  label: string
  is_baseline?: boolean
  chain_pass_rate?: number | null
  questions_all_rounds_ok?: number
  question_count?: number
  max_rounds_per_question?: number
  weakest_chain?: { name?: string; pass_rate?: string } | null
  has_multi_round?: boolean
}

export interface StabilitySummariesPayload {
  baseline_task_id: string
  rows: StabilitySummaryRow[]
}

interface Props {
  verdict: OverallVerdictPayload
  members?: ScenarioMatrixMember[]
}

const STATUS_LABEL: Record<string, string> = {
  better: '领先',
  worse: '落后',
  tie: '持平',
  na: '不适用',
}

const STATUS_COLOR: Record<string, string> = {
  better: '#28a745',
  worse: '#dc3545',
  tie: '#6c757d',
  na: '#adb5bd',
}

function fmtVal(v: number | null | undefined, digits = 3): string {
  if (v == null || Number.isNaN(v)) return '—'
  return Number.isInteger(v) ? String(v) : v.toFixed(digits)
}

function fmtPct(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(1)}%`
}

function fmtDelta(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  const sign = v > 0 ? '+' : ''
  return `${sign}${Number.isInteger(v) ? v : v.toFixed(3)}`
}

/**
 * 根据维度名推断该维度 value 的展示格式（百分率 vs 分数 vs 整数）。
 */
function formatDimValue(dim: string, v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  if (dim === 'quality' || dim === 'scenario') return fmtVal(v, 3)
  if (dim === 'process' || dim === 'stability') return fmtPct(v)
  if (dim === 'cost') return fmtVal(v, 0) // tokens 整数
  return fmtVal(v)
}

export function OverallVerdictCard({ verdict }: Props) {
  if (!verdict || !verdict.ranking?.length) return null

  const { overall_winner, ranking, dimension_verdicts, recommendation_text, caveats } =
    verdict

  const winnerLabel = overall_winner?.label
  const winnerScore = overall_winner?.total_score
  const isSignificant = overall_winner?.is_significant

  return (
    <div
      style={{
        marginBottom: 20,
        background: 'linear-gradient(135deg, #6b5ce7 0%, #8a7df0 100%)',
        borderRadius: 10,
        color: '#fff',
        overflow: 'hidden',
        boxShadow: '0 4px 14px rgba(107, 92, 231, 0.25)',
      }}
    >
      {/* 结论头部 */}
      <div style={{ padding: '16px 20px 14px' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            fontSize: 12,
            opacity: 0.9,
            marginBottom: 8,
          }}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M12 2L2 7l10 5 10-5-10-5z" />
            <path d="M2 17l10 5 10-5M2 12l10 5 10-5" />
          </svg>
          综合结论 · 多版本对比
        </div>
        {overall_winner ? (
          <div style={{ fontSize: 15, fontWeight: 600, lineHeight: 1.6 }}>
            {isSignificant ? '推荐版本' : '综合最优（差异不显著）'}：
            <span style={{ fontSize: 18, margin: '0 4px' }}>{winnerLabel}</span>
            {winnerScore != null && (
              <span style={{ opacity: 0.85, fontSize: 13 }}>
                综合得分 {fmtVal(winnerScore)}
              </span>
            )}
            {!isSignificant && (
              <span style={{ opacity: 0.75, fontSize: 12, marginLeft: 6 }}>
                （与基线差距不显著）
              </span>
            )}
          </div>
        ) : (
          <div style={{ fontSize: 15, fontWeight: 600 }}>暂无法判定综合最优版本</div>
        )}
        {recommendation_text && (
          <div
            style={{
              marginTop: 8,
              fontSize: 13,
              lineHeight: 1.6,
              opacity: 0.95,
              background: 'rgba(255,255,255,0.12)',
              padding: '8px 12px',
              borderRadius: 6,
            }}
          >
            {recommendation_text}
          </div>
        )}
      </div>

      {/* 排名表 */}
      <div style={{ background: 'rgba(255,255,255,0.95)', color: '#333' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr style={{ background: '#f1eefb', color: '#5a4fcf' }}>
              <th style={cellHead}>排名</th>
              <th style={cellHead}>版本</th>
              <th style={cellHead}>综合得分</th>
              {dimension_verdicts.map((d) => (
                <th key={d.dimension} style={cellHead}>
                  {d.display_name}
                  {d.weight != null && d.weight > 0 && (
                    <span style={{ opacity: 0.6, fontWeight: 400 }}>
                      {' '}
                      w={d.weight.toFixed(2)}
                    </span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ranking.map((r) => (
              <tr key={r.member_id} style={{ borderBottom: '1px solid #eee' }}>
                <td style={cellBody}>
                  <span
                    style={{
                      display: 'inline-flex',
                      width: 20,
                      height: 20,
                      borderRadius: '50%',
                      background: r.rank === 1 ? '#6b5ce7' : '#e0e0e0',
                      color: r.rank === 1 ? '#fff' : '#666',
                      alignItems: 'center',
                      justifyContent: 'center',
                      fontSize: 11,
                      fontWeight: 600,
                    }}
                  >
                    {r.rank}
                  </span>
                </td>
                <td style={{ ...cellBody, fontWeight: 600 }}>{r.label}</td>
                <td style={{ ...cellBody, color: '#6b5ce7', fontWeight: 600 }}>
                  {fmtVal(r.total_score)}
                </td>
                {dimension_verdicts.map((d) => {
                  const st = r.dimension_status?.[d.dimension] || 'na'
                  return (
                    <td
                      key={d.dimension}
                      style={{ ...cellBody, color: STATUS_COLOR[st] || '#666' }}
                    >
                      {STATUS_LABEL[st] || st}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>

        {/* 维度明细：各版本 value + Δ */}
        <div style={{ borderTop: '2px solid #f1eefb' }}>
          <div
            style={{
              padding: '8px 12px 4px',
              fontSize: 12,
              color: '#5a4fcf',
              fontWeight: 600,
            }}
          >
            各维度明细（值 · Δvs基线 · 胜者）
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
              <thead>
                <tr style={{ background: '#faf9fe', color: '#888' }}>
                  <th style={cellHeadSm}>维度</th>
                  {ranking.map((r) => (
                    <th key={r.member_id} style={cellHeadSm}>
                      {r.label}
                    </th>
                  ))}
                  <th style={cellHeadSm}>维度胜者</th>
                </tr>
              </thead>
              <tbody>
                {dimension_verdicts.map((d) => (
                  <tr key={d.dimension} style={{ borderBottom: '1px solid #f0f0f5' }}>
                    <td style={{ ...cellBodySm, fontWeight: 600, color: '#555' }}>
                      {d.display_name}
                      {!d.applicable && (
                        <span style={{ color: '#adb5bd', fontSize: 10 }}> 不适用</span>
                      )}
                    </td>
                    {ranking.map((r) => {
                      const pm = d.per_member.find(
                        (p) => p.member_id === r.member_id,
                      )
                      const st = pm?.status || 'na'
                      return (
                        <td
                          key={r.member_id}
                          style={{ ...cellBodySm, color: STATUS_COLOR[st] || '#666' }}
                        >
                          <div>{formatDimValue(d.dimension, pm?.value)}</div>
                          {pm?.delta_vs_baseline != null && (
                            <div style={{ fontSize: 10, opacity: 0.85 }}>
                              Δ{fmtDelta(pm.delta_vs_baseline)}
                            </div>
                          )}
                        </td>
                      )
                    })}
                    <td style={{ ...cellBodySm, color: '#6b5ce7', fontWeight: 600 }}>
                      {d.winner_label || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* caveats */}
        {caveats?.length > 0 && (
          <div
            style={{
              padding: '8px 12px 10px',
              fontSize: 11,
              color: '#8a6d3b',
              background: '#fffbf0',
              borderTop: '1px solid #f5ebcc',
            }}
          >
            {caveats.map((c, i) => (
              <div key={i} style={{ lineHeight: 1.6 }}>
                · {c}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

const cellHead: React.CSSProperties = {
  padding: '8px 10px',
  textAlign: 'left',
  fontWeight: 600,
  fontSize: 12,
}

const cellBody: React.CSSProperties = {
  padding: '8px 10px',
  fontSize: 12,
}

const cellHeadSm: React.CSSProperties = {
  padding: '6px 8px',
  textAlign: 'left',
  fontWeight: 600,
  fontSize: 11,
}

const cellBodySm: React.CSSProperties = {
  padding: '6px 8px',
  fontSize: 11,
}
