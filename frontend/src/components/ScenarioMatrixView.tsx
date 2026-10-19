import { useMemo, useState } from 'react'
import { RunComparePanel } from './RunComparePanel'
import { createRunComparison } from '../utils/runCompare'
import { formatDuration } from '../utils/dateUtils'
import { alertLangfuseTraceBlocked } from '../utils/langfuseReadiness'
import { LangfuseTraceBlockedPanel } from './LangfuseObservabilityNotice'
import { formatScenarioStepLabel } from '../utils/scenarioStepDisplay'

export interface ScenarioMatrixMember {
  member_id: string
  label: string
  is_baseline?: boolean
}

export interface ScenarioStepCell {
  step_key: string
  name?: string
  step_order?: number
  score: number
  passed: boolean
  round_stats?: {
    round_count?: number
    pass_count?: number
    pass_rate?: string
    mean_score?: number
    min_score?: number
    std_score?: number
  }
  gt_criteria?: string[]
  evidence?: {
    matched_keywords?: string[]
    match_ratio?: number
    langfuse_observation_url?: string | null
    langfuse_trace_url?: string | null
    eval_mode?: string
    aggregate_prompt?: string | null
    reason?: string | null
    llm_judge_reason?: string | null
    matched_cmd_ids?: string[]
    missing_cmd_ids?: string[]
    score_source?: string
    score_group?: string
    note?: string | null
  }
  trace_binding?: {
    observation_url?: string | null
    trace_url?: string | null
    observation_id?: string | null
  }
}

export interface ScenarioMatrixCell {
  agent_run_id: string
  composite_score: number
  steps: ScenarioStepCell[]
  round_count?: number
  stability?: { mean?: number; std?: number; min?: number; round_count?: number }
  chain_pass_rate?: number | null
  chain_all_rounds_ok?: boolean | null
  runs?: { agent_run_id: string; run_index?: number; composite_score?: number }[]
  langfuse?: {
    trace_url?: string | null
    observation_url?: string | null
    trace_id?: string | null
  }
}

export interface ScenarioDiffHighlight {
  comparison_id?: string
  question_id?: number
  left_run_id?: string
  right_run_id?: string
  outcome?: { delta?: number; left?: number; right?: number }
  cost?: { tokens_delta?: number; latency_delta_ms?: number }
  semantic?: {
    tier?: string
    role_mismatch_count?: number
    summary_mismatch_count?: number
    judge_summary?: string
  }
  structural?: {
    same_topology?: boolean
    only_left_count?: number
    only_right_count?: number
    summary?: string
  }
}

export interface ScenarioMatrixQuestion {
  question_key: string
  label: string
  question_id?: number
  scenario_key?: string
  step_columns?: ScenarioMatrixPayload['step_columns']
  cells: Record<string, ScenarioMatrixCell | null | undefined>
  winner_label?: string | null
  diff_highlight?: ScenarioDiffHighlight | null
}

export interface ScenarioMatrixPayload {
  comparison_mode: string
  scenario_key: string
  scenario_mode?: 'auto' | 'fixed' | string
  dataset_name?: string
  template?: { name?: string; ground_truth_ref?: string }
  warnings?: string[]
  empty_reason?: string | null
  members: ScenarioMatrixMember[]
  member_ids: string[]
  baseline_member_id?: string
  step_columns: {
    step_key: string
    name: string
    weight: number
    required: boolean
  }[]
  questions: ScenarioMatrixQuestion[]
  comparison_count?: number
  step_performance_summary?: {
    step_key: string
    name: string
    avg_latency_ms?: number | null
    avg_tokens?: number | null
    sample_count?: number
  }[]
  inference_rounds?: number
  multi_round_mode?: boolean
  chain_stability?: {
    chain_pass_rate?: number | null
    questions_all_rounds_ok?: number
    question_count?: number
    max_rounds_per_question?: number
    weakest_chain?: { name?: string; pass_rate?: string } | null
  }
}

interface Props {
  data: ScenarioMatrixPayload
  subtitle: string
  enableSpanCompare?: boolean
  langfuseTraceReady?: boolean
  /** 点击 L2 子步骤单元格时回调（用于 Trace 下钻 Demo） */
  onStepCellSelect?: (
    questionKey: string,
    questionLabel: string,
    memberId: string,
    agentRunId: string | undefined,
  ) => void
}

function scoreColor(score: number | null | undefined): string {
  if (score == null) return '#eee'
  if (score >= 0.8) return '#d4edda'
  if (score >= 0.5) return '#fff3cd'
  return '#f8d7da'
}

function formatDelta(
  baseline: number | null | undefined,
  other: number | null | undefined,
): { text: string; color: string } | null {
  if (baseline == null || other == null) return null
  const d = other - baseline
  const sign = d > 0 ? '+' : ''
  return {
    text: `${sign}${d.toFixed(2)}`,
    color: d >= 0 ? '#155724' : '#721c24',
  }
}

function baselineMemberLabel(data: ScenarioMatrixPayload, baselineId: string): string {
  return data.members.find(m => m.member_id === baselineId)?.label ?? baselineId
}

function compareMemberLabel(data: ScenarioMatrixPayload, memberId: string): string {
  return data.members.find(m => m.member_id === memberId)?.label ?? memberId
}

function DiffSummaryPanel({ highlight }: { highlight: ScenarioDiffHighlight }) {
  const { semantic, outcome, cost } = highlight
  if (!semantic && !outcome && !cost) return null
  return (
    <div
      style={{
        marginTop: 12,
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
        gap: 10,
      }}
    >
      {semantic && (
        <div
          style={{
            background: '#fff',
            border: '1px solid #e0e0e0',
            borderRadius: 4,
            padding: 10,
            fontSize: 12,
          }}
        >
          <strong style={{ color: '#6f42c1' }}>Semantic diff</strong>
          <p style={{ margin: '6px 0 0', color: '#555' }}>
            {semantic.judge_summary ??
              `角色不一致 ${semantic.role_mismatch_count ?? 0} · 摘要不一致 ${semantic.summary_mismatch_count ?? 0}`}
          </p>
          {semantic.tier && (
            <span style={{ fontSize: 11, color: '#888' }}>tier: {semantic.tier}</span>
          )}
        </div>
      )}
      {outcome?.delta != null && (
        <div
          style={{
            background: '#fff',
            border: '1px solid #e0e0e0',
            borderRadius: 4,
            padding: 10,
            fontSize: 12,
          }}
        >
          <strong>Outcome diff</strong>
          <p style={{ margin: '6px 0 0', color: '#555' }}>
            Δ {outcome.delta > 0 ? '+' : ''}
            {outcome.delta?.toFixed(3)}（{outcome.left?.toFixed(2)} → {outcome.right?.toFixed(2)}）
          </p>
        </div>
      )}
      {cost && (cost.tokens_delta != null || cost.latency_delta_ms != null) && (
        <div
          style={{
            background: '#fff',
            border: '1px solid #e0e0e0',
            borderRadius: 4,
            padding: 10,
            fontSize: 12,
          }}
        >
          <strong>Cost diff</strong>
          <p style={{ margin: '6px 0 0', color: '#555' }}>
            {cost.latency_delta_ms != null &&
              `延迟 Δ ${cost.latency_delta_ms > 0 ? '+' : cost.latency_delta_ms < 0 ? '-' : ''}${formatDuration(Math.abs(cost.latency_delta_ms))}`}
            {cost.tokens_delta != null && ` · Tokens Δ ${cost.tokens_delta}`}
          </p>
        </div>
      )}
    </div>
  )
}

export function ScenarioMatrixView({
  data,
  subtitle,
  enableSpanCompare = true,
  langfuseTraceReady = true,
  onStepCellSelect,
}: Props) {
  const memberIds = data.member_ids
  const baselineId =
    data.baseline_member_id ??
    data.members.find(m => m.is_baseline)?.member_id ??
    memberIds[0]

  const [selectedQk, setSelectedQk] = useState<string | null>(() => {
    const first = data.questions.find(q =>
      Object.values(q.cells).some(c => c != null),
    )
    return first?.question_key ?? data.questions[0]?.question_key ?? null
  })
  const [selectedStepKey, setSelectedStepKey] = useState<string | null>(null)
  const [selectedMemberForEvidence, setSelectedMemberForEvidence] = useState<string | null>(
    null,
  )
  const [rightMemberId, setRightMemberId] = useState<string>(() => {
    const other = memberIds.find(id => id !== baselineId)
    return other ?? memberIds[0] ?? ''
  })
  const [spanCompareId, setSpanCompareId] = useState<string | null>(null)
  const [compareBusy, setCompareBusy] = useState(false)
  const [compareError, setCompareError] = useState<string | null>(null)

  const selectedRow = data.questions.find(q => q.question_key === selectedQk)

  const l2StepColumns = useMemo(() => {
    if (selectedRow?.step_columns?.length) {
      return selectedRow.step_columns
    }
    return data.step_columns
  }, [selectedRow, data.step_columns])

  const nonBaselineMembers = useMemo(
    () => memberIds.filter(id => id !== baselineId),
    [memberIds, baselineId],
  )

  const canSpanCompare =
    enableSpanCompare &&
    memberIds.length >= 2 &&
    selectedRow != null &&
    selectedRow.cells[baselineId]?.agent_run_id &&
    selectedRow.cells[rightMemberId]?.agent_run_id &&
    baselineId !== rightMemberId

  const openSpanCompare = async () => {
    if (!langfuseTraceReady) {
      alertLangfuseTraceBlocked('推理链 / Span 树对比')
      return
    }
    if (!selectedRow || !canSpanCompare) return
    const leftRunId = selectedRow.cells[baselineId]?.agent_run_id
    const rightRunId = selectedRow.cells[rightMemberId]?.agent_run_id
    if (!leftRunId || !rightRunId) return
    setCompareBusy(true)
    setCompareError(null)
    try {
      const id = await createRunComparison(
        leftRunId,
        rightRunId,
        'scenario_matrix',
      )
      setSpanCompareId(id)
    } catch (e) {
      setCompareError(e instanceof Error ? e.message : '对比失败')
    } finally {
      setCompareBusy(false)
    }
  }

  const evidenceCell =
    selectedRow && selectedMemberForEvidence
      ? selectedRow.cells[selectedMemberForEvidence]
      : null

  const evidenceStep = useMemo(() => {
    if (!selectedRow || !selectedStepKey || !selectedMemberForEvidence) return null
    const cell = selectedRow.cells[selectedMemberForEvidence]
    return cell?.steps?.find(s => s.step_key === selectedStepKey) ?? null
  }, [selectedRow, selectedStepKey, selectedMemberForEvidence])

  const showDiffPanel = useMemo(() => {
    if (!selectedRow?.diff_highlight || !evidenceStep) return false
    const h = selectedRow.diff_highlight
    const hasDiff = Boolean(
      h.semantic ||
        h.outcome?.delta != null ||
        h.cost?.tokens_delta != null ||
        h.cost?.latency_delta_ms != null,
    )
    if (!hasDiff) return false
    return !evidenceStep.passed || Boolean(h.semantic)
  }, [selectedRow, evidenceStep])

  const deltaColumnLabel = useMemo(() => {
    const base = baselineMemberLabel(data, baselineId)
    const right = compareMemberLabel(data, rightMemberId)
    return `Δ(${right}−${base})`
  }, [data, baselineId, rightMemberId])

  return (
    <div style={{ padding: '12px 0', fontSize: 13 }}>
      <div style={{ marginBottom: 12 }}>
        <strong>{data.template?.name ?? '场景子步骤版本对比'}</strong>
        <span style={{ marginLeft: 8, color: '#666', fontSize: 12 }}>{subtitle}</span>
        {(data.comparison_count ?? 0) > 0 && (
          <span style={{ marginLeft: 8, fontSize: 11, color: '#888' }}>
            · {data.comparison_count} 组 Run 对比
          </span>
        )}
      </div>
      {(data.warnings?.length ?? 0) > 0 && (
        <div
          style={{
            marginBottom: 12,
            padding: '8px 12px',
            background: '#fff3cd',
            borderRadius: 4,
            color: '#856404',
            fontSize: 12,
          }}
        >
          {data.warnings!.map((w, i) => (
            <div key={i}>{w}</div>
          ))}
        </div>
      )}

      {(data.step_performance_summary?.length ?? 0) > 0 && (
        <div
          style={{
            marginBottom: 16,
            padding: '10px 14px',
            background: '#fff8f0',
            border: '1px solid #ffe0b2',
            borderRadius: 8,
          }}
        >
          <h4 style={{ margin: '0 0 8px', fontSize: 13, color: '#e65100', textAlign: 'center' }}>
            子步骤运行效能参考（按绑定 Span 统计）
          </h4>
          <p style={{ margin: '0 0 8px', fontSize: 11, color: '#888', textAlign: 'center' }}>
            按子步骤绑定的 Span 节点统计均时延与 Tokens；未绑定 Span 的步骤显示 —
          </p>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12, tableLayout: 'fixed' }}>
            <colgroup>
              <col style={{ width: '40%' }} />
              <col style={{ width: '20%' }} />
              <col style={{ width: '20%' }} />
              <col style={{ width: '20%' }} />
            </colgroup>
            <thead>
              <tr style={{ color: '#666' }}>
                <th style={{ padding: '6px 8px', textAlign: 'left' }}>步骤</th>
                <th style={{ padding: '6px 8px', textAlign: 'center' }}>均时延</th>
                <th style={{ padding: '6px 8px', textAlign: 'center' }}>均 Tokens</th>
                <th style={{ padding: '6px 8px', textAlign: 'center' }}>样本</th>
              </tr>
            </thead>
            <tbody>
              {data.step_performance_summary!.map(row => (
                <tr key={row.step_key}>
                  <td style={{ padding: '6px 8px', borderTop: '1px solid #ffe0b2', textAlign: 'left' }}>
                    {row.name || row.step_key}
                  </td>
                  <td style={{ padding: '6px 8px', borderTop: '1px solid #ffe0b2', textAlign: 'center' }}>
                    {row.avg_latency_ms != null
                      ? formatDuration(Math.round(row.avg_latency_ms))
                      : '—'}
                  </td>
                  <td style={{ padding: '6px 8px', borderTop: '1px solid #ffe0b2', textAlign: 'center' }}>
                    {row.avg_tokens != null ? Math.round(row.avg_tokens) : '—'}
                  </td>
                  <td style={{ padding: '6px 8px', borderTop: '1px solid #ffe0b2', textAlign: 'center' }}>
                    {row.sample_count ?? '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(data.multi_round_mode || (data.inference_rounds ?? 0) > 1) && data.chain_stability && (
        <div
          style={{
            marginBottom: 12,
            padding: '10px 14px',
            background: '#e3f2fd',
            border: '1px solid #90caf9',
            borderRadius: 8,
            fontSize: 12,
          }}
        >
          <strong>多轮稳定性</strong>
          <span style={{ marginLeft: 10 }}>
            每题 {data.inference_rounds ?? data.chain_stability.max_rounds_per_question ?? '—'} 轮
          </span>
          {data.chain_stability.chain_pass_rate != null && (
            <span style={{ marginLeft: 10 }}>
              关键链条全轮通过率{' '}
              {(data.chain_stability.chain_pass_rate * 100).toFixed(0)}%
              （{data.chain_stability.questions_all_rounds_ok ?? 0}/
              {data.chain_stability.question_count ?? 0} 题）
            </span>
          )}
          {data.chain_stability.weakest_chain && (
            <span style={{ marginLeft: 10, color: '#856404' }}>
              最弱：{data.chain_stability.weakest_chain.name}{' '}
              {data.chain_stability.weakest_chain.pass_rate}
            </span>
          )}
        </div>
      )}

      <h3 style={{ fontSize: 14, margin: '16px 0 8px' }}>L1 · 按评测题（场景）综合分</h3>
      <div style={{ overflowX: 'auto', marginBottom: 20 }}>
        <table className="results-table" style={{ minWidth: 600 }}>
          <thead>
            <tr>
              <th style={{ minWidth: 160 }}>评测题</th>
              {data.members.map(m => (
                <th key={m.member_id} style={{ minWidth: 100, fontSize: 12 }}>
                  {m.label}
                  {m.is_baseline ? ' (基线)' : ''}
                </th>
              ))}
              <th>胜者</th>
            </tr>
          </thead>
          <tbody>
            {data.questions.map(row => (
              <tr
                key={row.question_key}
                onClick={() => {
                  setSelectedQk(row.question_key)
                  setSelectedStepKey(null)
                  setSelectedMemberForEvidence(null)
                }}
                style={{
                  cursor: 'pointer',
                  background: selectedQk === row.question_key ? '#f0f4ff' : undefined,
                }}
              >
                <td style={{ fontSize: 12 }}>{row.label}</td>
                {memberIds.map(mid => {
                  const cell = row.cells[mid]
                  const score = cell?.composite_score
                  const multi = (cell?.round_count ?? 0) > 1
                  const unstable = multi && (cell?.stability?.std ?? 0) > 0.05
                  return (
                    <td
                      key={mid}
                      style={{
                        textAlign: 'center',
                        background: scoreColor(score),
                        boxShadow: unstable ? 'inset 0 0 0 2px #ffb74d' : undefined,
                      }}
                    >
                      {score != null ? score.toFixed(2) : '—'}
                      {multi && cell?.stability && (
                        <div style={{ fontSize: 10, color: '#666', marginTop: 2 }}>
                          σ={cell.stability.std?.toFixed(2) ?? '—'}
                          {cell.chain_pass_rate != null && (
                            <> · 链 {Math.round(cell.chain_pass_rate * 100)}%</>
                          )}
                        </div>
                      )}
                    </td>
                  )
                })}
                <td style={{ fontSize: 11, color: '#666' }}>
                  {row.winner_label ?? '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selectedRow && (
        <>
          <div
            style={{
              display: 'flex',
              flexWrap: 'wrap',
              gap: 12,
              alignItems: 'center',
              marginBottom: 8,
            }}
          >
            <h3 style={{ fontSize: 14, margin: 0 }}>
              L2 · 子步骤矩阵 — {selectedRow.label}
              {selectedRow.scenario_key ? (
                <span style={{ marginLeft: 8, color: '#888', fontWeight: 400, fontSize: 11 }}>
                  {selectedRow.scenario_key}
                </span>
              ) : null}
            </h3>
            {enableSpanCompare && nonBaselineMembers.length > 0 && (
              <>
                {!langfuseTraceReady && (
                  <div style={{ marginBottom: 8, flexBasis: '100%' }}>
                    <LangfuseTraceBlockedPanel compact title="推理链 / Span 树对比不可用" />
                  </div>
                )}
                <label style={{ fontSize: 12, color: '#666' }}>
                  对比列
                  <select
                    value={rightMemberId}
                    onChange={e => setRightMemberId(e.target.value)}
                    style={{ marginLeft: 6, fontSize: 12 }}
                  >
                    {nonBaselineMembers.map(mid => {
                      const m = data.members.find(x => x.member_id === mid)
                      return (
                        <option key={mid} value={mid}>
                          {m?.label ?? mid}
                        </option>
                      )
                    })}
                  </select>
                </label>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={!canSpanCompare || compareBusy || !langfuseTraceReady}
                  onClick={() => void openSpanCompare()}
                  style={{ padding: '4px 10px', fontSize: 12 }}
                  title={!langfuseTraceReady ? '需要 Agent 绑定 Langfuse' : undefined}
                >
                  {compareBusy ? '准备中…' : '推理链 / Span 树对比'}
                </button>
              </>
            )}
          </div>
          {compareError && (
            <p style={{ color: '#dc3545', fontSize: 12, marginBottom: 8 }}>{compareError}</p>
          )}
          <div style={{ overflowX: 'auto' }}>
            <table className="results-table" style={{ minWidth: 700 }}>
              <thead>
                <tr>
                  <th>步骤</th>
                  {data.members.map(m => (
                    <th key={m.member_id} style={{ fontSize: 12 }}>
                      {m.label}
                    </th>
                  ))}
                  {memberIds.length >= 2 && (
                    <th style={{ fontSize: 11, minWidth: 72 }}>{deltaColumnLabel}</th>
                  )}
                </tr>
              </thead>
              <tbody>
                {l2StepColumns.map((step, stepIdx) => {
                  const baselineCell = selectedRow.cells[baselineId]
                  const rightCell = selectedRow.cells[rightMemberId]
                  const baselineStep = baselineCell?.steps?.find(
                    s => s.step_key === step.step_key,
                  )
                  const rightStep = rightCell?.steps?.find(
                    s => s.step_key === step.step_key,
                  )
                  const delta = formatDelta(baselineStep?.score, rightStep?.score)
                  const stepLabel = formatScenarioStepLabel(
                    {
                      name: step.name,
                      step_order: baselineStep?.step_order ?? rightStep?.step_order,
                      step_key: step.step_key,
                    },
                    stepIdx + 1,
                  )
                  return (
                  <tr key={step.step_key}>
                    <td>
                      {stepLabel}
                      <span style={{ color: '#999', marginLeft: 6 }}>
                        w={step.weight}
                      </span>
                    </td>
                    {memberIds.map(mid => {
                      const cell = selectedRow.cells[mid]
                      const st = cell?.steps?.find(s => s.step_key === step.step_key)
                      const score = st?.score
                      const active =
                        selectedStepKey === step.step_key &&
                        selectedMemberForEvidence === mid
                      return (
                        <td
                          key={mid}
                          role="button"
                          tabIndex={0}
                          onClick={() => {
                            setSelectedStepKey(step.step_key)
                            setSelectedMemberForEvidence(mid)
                            onStepCellSelect?.(
                              selectedRow.question_key,
                              selectedRow.label,
                              mid,
                              cell?.agent_run_id,
                            )
                          }}
                          onKeyDown={e => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              setSelectedStepKey(step.step_key)
                              setSelectedMemberForEvidence(mid)
                            }
                          }}
                          style={{
                            textAlign: 'center',
                            background: active ? '#e3e8ff' : scoreColor(score),
                            cursor: st ? 'pointer' : 'default',
                          }}
                        >
                          {score != null ? (
                            <>
                              {score.toFixed(2)}
                              {st?.passed ? ' ✓' : ''}
                              {st?.round_stats?.pass_rate && (
                                <div style={{ fontSize: 10, color: '#666' }}>
                                  {st.round_stats.pass_rate} 轮
                                </div>
                              )}
                            </>
                          ) : (
                            '—'
                          )}
                        </td>
                      )
                    })}
                    {memberIds.length >= 2 && (
                      <td
                        style={{
                          textAlign: 'center',
                          fontSize: 12,
                          fontWeight: 600,
                          color: delta?.color ?? '#666',
                        }}
                      >
                        {delta?.text ?? '—'}
                      </td>
                    )}
                  </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          {evidenceStep && (
            <div
              style={{
                marginTop: 12,
                background: '#f9f9f9',
                padding: 12,
                borderRadius: 4,
                fontSize: 12,
              }}
            >
              {(() => {
                const isLlm = evidenceStep.evidence?.eval_mode === 'llm_judge'
                const titleLabel = isLlm ? '评测提示词' : '判分细则'
                return (
                  <>
                    <strong>
                      {titleLabel} ·{' '}
                      {formatScenarioStepLabel({
                        name: evidenceStep.name,
                        step_order: evidenceStep.step_order,
                        step_key: evidenceStep.step_key,
                      })}
                    </strong>
                    {isLlm ? (
                      evidenceStep.evidence?.aggregate_prompt ? (
                        <div style={{ margin: '8px 0 0', whiteSpace: 'pre-wrap' }}>
                          {String(evidenceStep.evidence.aggregate_prompt)}
                        </div>
                      ) : (
                        <p style={{ margin: '8px 0 0', color: '#999' }}>（未配置评测提示词）</p>
                      )
                    ) : (
                      <ul style={{ margin: '8px 0 0 18px' }}>
                        {(evidenceStep.gt_criteria || []).map(c => (
                          <li key={c}>{c}</li>
                        ))}
                      </ul>
                    )}
                  </>
                )
              })()}
              {evidenceStep.evidence?.eval_mode !== 'llm_judge' && (
                <p style={{ marginTop: 8, color: '#555' }}>
                  证据：命中{' '}
                  {(evidenceStep.evidence?.matched_cmd_ids as string[] | undefined)?.length
                    ? `指令 ID ${(evidenceStep.evidence?.matched_cmd_ids as string[]).join(', ')}`
                    : `关键词 ${(evidenceStep.evidence?.matched_keywords || []).join(', ') || '无'}`}
                  {evidenceStep.evidence?.match_ratio != null
                    ? `（${(evidenceStep.evidence.match_ratio * 100).toFixed(0)}%）`
                    : ''}
                </p>
              )}
              {evidenceStep.evidence?.eval_mode === 'llm_judge' && (evidenceStep.evidence?.reason || evidenceStep.evidence?.llm_judge_reason) && (
                <div style={{ marginTop: 8 }}>
                  <div style={{ color: '#666', marginBottom: 4 }}>评估分析</div>
                  <div style={{ padding: 8, background: '#fff', borderRadius: 4, border: '1px solid #e8e8e8', whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>
                    {evidenceStep.evidence?.reason || evidenceStep.evidence?.llm_judge_reason}
                  </div>
                </div>
              )}
              {(evidenceStep.evidence?.missing_cmd_ids as string[] | undefined)?.length ? (
                <p style={{ marginTop: 4, color: '#856404', fontSize: 12 }}>
                  缺失指令 ID：{(evidenceStep.evidence?.missing_cmd_ids as string[]).join(', ')}
                </p>
              ) : null}
              {(evidenceStep.trace_binding?.observation_url ||
                evidenceStep.evidence?.langfuse_observation_url ||
                evidenceCell?.langfuse?.trace_url) && (
                <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                  {(evidenceStep.trace_binding?.observation_url ||
                    evidenceStep.evidence?.langfuse_observation_url) && (
                    <a
                      href={
                        evidenceStep.trace_binding?.observation_url ||
                        evidenceStep.evidence?.langfuse_observation_url ||
                        '#'
                      }
                      target="_blank"
                      rel="noreferrer"
                      className="btn btn-secondary"
                      style={{ padding: '4px 10px', fontSize: 12 }}
                    >
                      Langfuse · 本子步骤
                    </a>
                  )}
                  {(evidenceStep.trace_binding?.trace_url ||
                    evidenceCell?.langfuse?.trace_url) && (
                    <a
                      href={
                        evidenceStep.trace_binding?.trace_url ||
                        evidenceCell?.langfuse?.trace_url ||
                        '#'
                      }
                      target="_blank"
                      rel="noreferrer"
                      className="btn btn-secondary"
                      style={{ padding: '4px 10px', fontSize: 12 }}
                    >
                      Langfuse · 整题 Trace
                    </a>
                  )}
                </div>
              )}
              {showDiffPanel && selectedRow.diff_highlight && (
                <DiffSummaryPanel highlight={selectedRow.diff_highlight} />
              )}
            </div>
          )}
        </>
      )}

      {spanCompareId && (
        <RunComparePanel
          comparisonId={spanCompareId}
          onClose={() => setSpanCompareId(null)}
        />
      )}
    </div>
  )
}
