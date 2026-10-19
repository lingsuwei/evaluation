import { useCallback, useEffect, useMemo, useState } from 'react'
import { CheckpointRunCompare, type CheckpointRunRow } from './CheckpointRunCompare'
import { SingleRunChainView } from './SingleRunChainView'
import { DimensionVerdictHeadline } from './DimensionVerdictHeadline'
import type { VerdictStatus } from '../types/dimensionVerdict'

const API_BASE = '/api'

interface RunSummary {
  agent_run_id: string
  run_index: number
  agent_name?: string | null
  required_total: number
  required_passed: number
  required_all_passed: boolean
  optional_total: number
  optional_hit: number
  items: Array<{
    checkpoint_id: string
    name: string
    required?: boolean | null
    passed: boolean
    hit?: boolean | null
    order_ok?: boolean | null
    outcome?: string | null
  }>
}

interface QuestionConsistency {
  question_id: number
  run_count: number
  is_multi_run?: boolean
  required_status: 'green' | 'yellow' | 'red'
  chain_status: string
  chain_pass_rate?: number | null
  rounds_all_ok?: number
  round_count?: number
  all_rounds_ok?: boolean
  runs: RunSummary[]
}

interface ConsistencySummary {
  total_runs: number
  multi_run_questions: number
  single_run_questions?: number
  green: number
  yellow: number
  red: number
  chain_pass_rate?: number | null
  questions_all_rounds_ok?: number
  question_count?: number
  weakest_chain?: { name?: string; pass_rate?: string } | null
}

interface ConsistencyData {
  questions: QuestionConsistency[]
  summary: ConsistencySummary
}

interface CheckpointReportRun {
  agent_run_id: string
  run_index?: number
  question_id?: number | null
  agent_name?: string | null
  required_total?: number
  required_passed?: number
  optional_total?: number
  optional_hit?: number
  items: RunSummary['items']
}

interface Props {
  taskId: string
  taskAgentId?: string | null
  inferenceRounds?: number
  langfuseTraceReady?: boolean
  checkpointReport?: {
    runs: CheckpointReportRun[]
    summary?: { run_count?: number }
  } | null
  onOpenSpanCompare?: (questionId: number, leftRunId: string, rightRunId: string) => void
  processVerdict?: {
    status: VerdictStatus
    score?: number | null
    headline: string
  } | null
}

function requiredStatusFromRuns(runs: Array<{
  required_total?: number
  required_passed?: number
  optional_total?: number
  optional_hit?: number
  items?: RunSummary['items']
}>): 'green' | 'yellow' | 'red' {
  const summaries = runs.map(r => {
    const items = r.items ?? []
    const requiredItems = items.filter(i => i.required)
    // items 为空（Run 存在但未产出过程点结果）时视为未通过：
    // 典型场景是任务被取消，过程点评估未执行，不能计为"必达全通过"。
    const required_all_passed =
      items.length === 0 ? false
        : requiredItems.length === 0 ? true
        : requiredItems.every(i => i.passed)
    return {
      required_all_passed,
      optional_total: r.optional_total ?? 0,
      optional_hit: r.optional_hit ?? 0,
    }
  })
  if (!summaries.every(s => s.required_all_passed)) return 'red'
  if (summaries.some(s => s.optional_total > 0 && s.optional_hit < s.optional_total)) {
    return 'yellow'
  }
  return 'green'
}

function chainOkForRun(items: RunSummary['items']): boolean {
  // items 为空（Run 存在但未产出过程点结果）时返回 false：
  // 无过程点证据不能视为链条全通过。典型场景：任务被取消，过程点评估未执行。
  if (items.length === 0) return false
  for (const item of items) {
    if (!item.required) continue
    if (!item.passed) return false
    if (item.order_ok === false) return false
  }
  return true
}

function questionChainFields(
  runSummaries: RunSummary[],
  isMultiRun: boolean,
): Pick<
  QuestionConsistency,
  'chain_status' | 'chain_pass_rate' | 'rounds_all_ok' | 'round_count' | 'all_rounds_ok'
> {
  if (!isMultiRun) {
    return {
      chain_status: '单轮',
      chain_pass_rate: null,
      rounds_all_ok: 0,
      round_count: runSummaries.length,
      all_rounds_ok: false,
    }
  }
  const okCount = runSummaries.filter(r => chainOkForRun(r.items)).length
  const roundCount = runSummaries.length
  const allOk = okCount === roundCount && roundCount > 0
  return {
    chain_status: allOk ? '全轮通过' : '有轮未过',
    chain_pass_rate: roundCount ? okCount / roundCount : null,
    rounds_all_ok: okCount,
    round_count: roundCount,
    all_rounds_ok: allOk,
  }
}

function formatChainCell(q: QuestionConsistency): string {
  if (!q.is_multi_run && q.run_count < 2) return '单轮'
  if (q.all_rounds_ok) return '全轮通过'
  if (q.rounds_all_ok != null && q.round_count != null && q.round_count > 0) {
    return `${q.rounds_all_ok}/${q.round_count} 轮通过`
  }
  return q.chain_status || '—'
}

function buildFromCheckpointReport(report: NonNullable<Props['checkpointReport']>): ConsistencyData {
  const byQ = new Map<number, CheckpointReportRun[]>()
  for (const run of report.runs ?? []) {
    const qid = run.question_id ?? 0
    if (!byQ.has(qid)) byQ.set(qid, [])
    byQ.get(qid)!.push(run)
  }

  let green = 0
  let yellow = 0
  let red = 0
  let multi = 0
  let single = 0
  let questionsAllRoundsOk = 0

  const questions: QuestionConsistency[] = [...byQ.entries()]
    .sort(([a], [b]) => a - b)
    .map(([question_id, qRuns]) => {
      const sorted = [...qRuns].sort((a, b) => (a.run_index ?? 0) - (b.run_index ?? 0))
      const is_multi_run = sorted.length >= 2
      if (is_multi_run) multi += 1
      else single += 1

      const runs: RunSummary[] = sorted.map(r => ({
        agent_run_id: r.agent_run_id,
        run_index: r.run_index ?? 0,
        agent_name: r.agent_name,
        required_total: r.required_total ?? 0,
        required_passed: r.required_passed ?? 0,
        required_all_passed:
          (r.items?.length ?? 0) > 0 &&
          ((r.required_total ?? 0) === 0 ||
           (r.required_passed ?? 0) >= (r.required_total ?? 0)),
        optional_total: r.optional_total ?? 0,
        optional_hit: r.optional_hit ?? 0,
        items: r.items ?? [],
      }))

      const required_status = requiredStatusFromRuns(runs)
      const chainFields = questionChainFields(runs, is_multi_run)

      if (required_status === 'red') {
        red += 1
      } else if (required_status === 'green' && (!is_multi_run || chainFields.all_rounds_ok)) {
        green += 1
      } else {
        yellow += 1
      }

      if (chainFields.all_rounds_ok) {
        questionsAllRoundsOk += 1
      }

      return {
        question_id,
        run_count: sorted.length,
        is_multi_run,
        required_status,
        ...chainFields,
        runs,
      }
    })

  const questionCount = questions.length
  return {
    questions,
    summary: {
      total_runs: report.runs?.length ?? 0,
      multi_run_questions: multi,
      single_run_questions: single,
      green,
      yellow,
      red,
      chain_pass_rate: questionCount ? questionsAllRoundsOk / questionCount : null,
      questions_all_rounds_ok: questionsAllRoundsOk,
      question_count: questionCount,
      weakest_chain: null,
    },
  }
}

const STATUS_COLORS: Record<string, string> = {
  green: '#28a745',
  yellow: '#e6a817',
  red: '#dc3545',
}

const STATUS_LABELS: Record<string, string> = {
  green: '全绿',
  yellow: '有差异',
  red: '有必达未过',
}

export function ProcessConsistencyOverview({
  taskId,
  taskAgentId,
  inferenceRounds = 1,
  langfuseTraceReady = true,
  checkpointReport,
  onOpenSpanCompare,
  processVerdict,
}: Props) {
  const [data, setData] = useState<ConsistencyData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedQuestion, setExpandedQuestion] = useState<number | null>(null)
  const [drillTab, setDrillTab] = useState<'matrix' | 'compare'>('matrix')

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${API_BASE}/tasks/${taskId}/process-consistency`)
      if (!res.ok) throw new Error('加载过程一致性数据失败')
      const json: ConsistencyData = await res.json()
      if ((json.questions?.length ?? 0) === 0 && checkpointReport?.runs?.length) {
        setData(buildFromCheckpointReport(checkpointReport))
      } else {
        setData(json)
      }
    } catch (e: any) {
      if (checkpointReport?.runs?.length) {
        setData(buildFromCheckpointReport(checkpointReport))
        setError(null)
      } else {
        setError(e?.message || '加载失败')
      }
    } finally {
      setLoading(false)
    }
  }, [taskId, checkpointReport])

  useEffect(() => {
    void load()
  }, [load])

  const compareRunsForQuestion = useCallback(
    (questionId: number): CheckpointRunRow[] => {
      const q = data?.questions.find(x => x.question_id === questionId)
      if (!q) return []
      return q.runs.map(r => ({
        agent_run_id: r.agent_run_id,
        run_index: r.run_index,
        question_id: questionId,
        agent_name: r.agent_name,
        checkpoint_total: r.required_total + r.optional_total,
        required_passed: r.required_passed,
        required_total: r.required_total,
      }))
    },
    [data],
  )

  const expandedRuns = useMemo(() => {
    if (expandedQuestion == null || !data) return []
    const q = data.questions.find(x => x.question_id === expandedQuestion)
    return q?.runs ?? []
  }, [expandedQuestion, data])

  const allCheckpointIds = useMemo(() => {
    const ids: string[] = []
    const seen = new Set<string>()
    for (const r of expandedRuns) {
      for (const item of r.items) {
        if (!seen.has(item.checkpoint_id)) {
          seen.add(item.checkpoint_id)
          ids.push(item.checkpoint_id)
        }
      }
    }
    return ids
  }, [expandedRuns])

  const expandedQuestionMeta = useMemo(() => {
    if (expandedQuestion == null || !data) return null
    return data.questions.find(x => x.question_id === expandedQuestion) ?? null
  }, [expandedQuestion, data])

  const expandedIsMultiRun = useMemo(
    () => expandedQuestionMeta?.is_multi_run ?? expandedRuns.length >= 2,
    [expandedQuestionMeta, expandedRuns.length],
  )

  const singleRunForExpanded = expandedRuns.length === 1 ? expandedRuns[0] : null

  const toggleQuestion = (q: QuestionConsistency) => {
    if (expandedQuestion === q.question_id) {
      setExpandedQuestion(null)
      return
    }
    setExpandedQuestion(q.question_id)
    setDrillTab(q.is_multi_run || q.run_count >= 2 ? 'compare' : 'matrix')
  }

  if (loading) {
    return <p style={{ padding: '16px 24px', fontSize: 13, color: '#666' }}>加载过程一致性…</p>
  }
  if (error) {
    return <p style={{ padding: '16px 24px', color: '#dc3545', fontSize: 13 }}>{error}</p>
  }
  if (!data) {
    return null
  }

  if (data.questions.length === 0) {
    const totalRuns = data.summary.total_runs ?? 0
    return (
      <div style={{ padding: '16px 24px', borderBottom: '1px solid #e0e0e0' }}>
        <h3 style={{ margin: '0 0 4px 0', fontSize: 16 }}>过程一致性总览</h3>
        <p style={{ margin: '0 0 12px', fontSize: 12, color: '#888' }}>
          同题多 Run：Agent 必达过程点与推理链条是否稳定（与场景 Rubric 无关）
        </p>
        <div
          style={{
            padding: '14px 16px',
            background: '#f8f9fa',
            border: '1px solid #e8eaf0',
            borderRadius: 8,
            fontSize: 13,
            color: '#555',
            lineHeight: 1.7,
          }}
        >
          {totalRuns === 0 ? (
            <>
              <p style={{ margin: '0 0 8px' }}>
                未找到与本任务关联的 Agent Run 记录，无法生成过程一致性视图。
              </p>
              <p style={{ margin: 0, fontSize: 12, color: '#888' }}>
                历史任务若缺少 Run 物化，请确认 Trace 已同步；单轮任务的过程点合规请查看「质量指标」Tab。
              </p>
            </>
          ) : (
            <>
              <p style={{ margin: '0 0 8px' }}>
                本任务共 <strong>{totalRuns}</strong> 个 Run，但每题均只有 1 轮，暂无可对比的多轮过程一致性数据。
              </p>
              <p style={{ margin: 0, fontSize: 12, color: '#888' }}>
                过程一致性适用于「Agent 运行次数 ≥ 2 / 题」的稳定性评测
                {inferenceRounds > 1 ? `（本任务配置 ${inferenceRounds} 轮/题，请确认各轮 Run 已落库）` : ''}
                。单轮历史任务请查看「质量指标」Tab 中的过程点合规。
              </p>
            </>
          )}
        </div>
      </div>
    )
  }

  const s = data.summary
  const isSingleRoundTask =
    (s.single_run_questions ?? 0) > 0 && (s.multi_run_questions ?? 0) === 0
  const showChainSummary =
    !isSingleRoundTask &&
    s.chain_pass_rate != null &&
    (s.question_count ?? 0) > 0

  return (
    <div style={{ padding: '16px 24px', borderBottom: '1px solid #e0e0e0', minHeight: 120 }}>
      <h3 style={{ margin: '0 0 4px 0', fontSize: 16 }}>过程一致性总览</h3>
      <p style={{ margin: '0 0 12px', fontSize: 12, color: '#888' }}>
        {isSingleRoundTask
          ? '单轮评测：按题展示 Agent 必达过程点合规与推理链（多轮链条对比需 Agent 运行 ≥2 次/题）'
          : '同题多 Run：Agent 必达过程点与推理链条是否稳定（与场景 Rubric 无关）'}
      </p>

      {processVerdict && processVerdict.status !== 'na' && (
        <DimensionVerdictHeadline
          title="过程一致性结论"
          status={processVerdict.status}
          headline={processVerdict.headline}
          scoreLine={
            processVerdict.score != null
              ? `过程分 ${processVerdict.score.toFixed(2)}`
              : null
          }
        />
      )}

      {showChainSummary && (
        <div
          style={{
            marginBottom: 12,
            padding: '10px 14px',
            background: '#e3f2fd',
            border: '1px solid #90caf9',
            borderRadius: 8,
            fontSize: 13,
            color: '#1565c0',
          }}
        >
          推理链条全轮通过率{' '}
          <strong>{(s.chain_pass_rate! * 100).toFixed(0)}%</strong>
          （{s.questions_all_rounds_ok ?? 0}/{s.question_count ?? 0} 题）
          {s.weakest_chain && (
            <span style={{ marginLeft: 8, color: '#856404' }}>
              · 最弱链条：{s.weakest_chain.name} {s.weakest_chain.pass_rate}
            </span>
          )}
        </div>
      )}

      <div
        style={{
          display: 'flex',
          gap: 16,
          flexWrap: 'wrap',
          marginBottom: 16,
          padding: '10px 14px',
          background: '#f8f9fa',
          borderRadius: 6,
          fontSize: 13,
        }}
      >
        <span>
          已评估 <strong>{s.total_runs}</strong> 个 Run
          {isSingleRoundTask ? (
            <>
              {' '}
              · <strong>{s.single_run_questions ?? data.questions.length}</strong> 道题（单轮）
            </>
          ) : (
            <>
              {' '}
              · <strong>{s.multi_run_questions}</strong> 道题有多轮/多 Run
              {(s.single_run_questions ?? 0) > 0 && (
                <span style={{ color: '#888' }}>
                  {' '}
                  · {s.single_run_questions} 道题单轮
                </span>
              )}
            </>
          )}
        </span>
        <span style={{ color: '#888' }}>|</span>
        <span>
          过程一致：
          <strong style={{ color: STATUS_COLORS.green }}>{s.green} 题全绿</strong>
          {' · '}
          <strong style={{ color: STATUS_COLORS.yellow }}>{s.yellow} 题有差异</strong>
          {' · '}
          <strong style={{ color: STATUS_COLORS.red }}>{s.red} 题有必达未过</strong>
        </span>
      </div>

      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <thead>
          <tr style={{ background: '#f0f2f5', textAlign: 'left' }}>
            <th style={{ padding: '8px 12px', fontWeight: 600 }}>题号</th>
            <th style={{ padding: '8px 12px', fontWeight: 600 }}>Run 数</th>
            <th style={{ padding: '8px 12px', fontWeight: 600 }}>必达过程点</th>
            <th style={{ padding: '8px 12px', fontWeight: 600 }}>推理链条</th>
            <th style={{ padding: '8px 12px', fontWeight: 600 }}>操作</th>
          </tr>
        </thead>
        <tbody>
          {data.questions.map(q => (
            <tr key={q.question_id} style={{ borderTop: '1px solid #e8eaf0' }}>
              <td style={{ padding: '8px 12px' }}>#{q.question_id}</td>
              <td style={{ padding: '8px 12px' }}>{q.run_count} 轮</td>
              <td style={{ padding: '8px 12px' }}>
                <span
                  style={{
                    display: 'inline-block',
                    width: 10,
                    height: 10,
                    borderRadius: '50%',
                    background: STATUS_COLORS[q.required_status],
                    marginRight: 6,
                  }}
                />
                {STATUS_LABELS[q.required_status]}
              </td>
              <td style={{ padding: '8px 12px' }}>{formatChainCell(q)}</td>
              <td style={{ padding: '8px 12px' }}>
                <button
                  type="button"
                  onClick={() => toggleQuestion(q)}
                  style={{
                    fontSize: 12,
                    padding: '4px 10px',
                    borderRadius: 4,
                    border: '1px solid #667eea',
                    background: expandedQuestion === q.question_id ? '#667eea' : '#fff',
                    color: expandedQuestion === q.question_id ? '#fff' : '#667eea',
                    cursor: 'pointer',
                  }}
                >
                  {expandedQuestion === q.question_id ? '收起' : '查看'}
                </button>
                {q.runs.length >= 2 && onOpenSpanCompare && (
                  <button
                    type="button"
                    onClick={() => {
                      const runs = q.runs
                      const baseline = runs[0]
                      const worst = runs.reduce((prev, curr) =>
                        curr.required_passed < prev.required_passed ? curr : prev,
                      )
                      onOpenSpanCompare(q.question_id, baseline.agent_run_id, worst.agent_run_id)
                    }}
                    style={{
                      marginLeft: 8,
                      fontSize: 12,
                      padding: '4px 10px',
                      borderRadius: 4,
                      border: '1px solid #ccc',
                      background: '#fff',
                      color: '#333',
                      cursor: 'pointer',
                    }}
                  >
                    Span 对比
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {expandedQuestion != null && (
        <div
          style={{
            marginTop: 16,
            border: '1px solid #e0e0e0',
            borderRadius: 8,
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              display: 'flex',
              borderBottom: '1px solid #e0e0e0',
              background: '#f8f9fa',
            }}
          >
            {(['matrix', 'compare'] as const).map(tab => (
              <button
                key={tab}
                type="button"
                onClick={() => setDrillTab(tab)}
                style={{
                  padding: '8px 16px',
                  border: 'none',
                  borderBottom: drillTab === tab ? '2px solid #667eea' : '2px solid transparent',
                  background: 'transparent',
                  cursor: 'pointer',
                  fontSize: 13,
                  fontWeight: drillTab === tab ? 600 : 400,
                  color: drillTab === tab ? '#667eea' : '#666',
                }}
              >
                {tab === 'matrix' && '过程点矩阵'}
                {tab === 'compare' && (expandedIsMultiRun ? '推理链对比' : '推理链')}
              </button>
            ))}
          </div>

          <div style={{ padding: 12 }}>
            {drillTab === 'matrix' && (
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                <thead>
                  <tr style={{ background: '#f0f2f5', textAlign: 'left' }}>
                    <th style={{ padding: '6px 10px' }}>过程点</th>
                    <th style={{ padding: '6px 10px' }}>类型</th>
                    {expandedRuns.map(r => (
                      <th key={r.agent_run_id} style={{ padding: '6px 10px', textAlign: 'center' }}>
                        第 {r.run_index + 1} 轮
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {allCheckpointIds.map(cid => {
                    const firstItem = expandedRuns[0]?.items.find(i => i.checkpoint_id === cid)
                    return (
                      <tr key={cid} style={{ borderTop: '1px solid #e8eaf0' }}>
                        <td style={{ padding: '6px 10px' }}>{firstItem?.name || cid}</td>
                        <td style={{ padding: '6px 10px' }}>
                          <span
                            style={{
                              fontSize: 11,
                              padding: '1px 6px',
                              borderRadius: 3,
                              background: firstItem?.required ? '#fff3cd' : '#e8f5e9',
                              color: firstItem?.required ? '#856404' : '#2e7d32',
                            }}
                          >
                            {firstItem?.required ? '必达' : '可选'}
                          </span>
                        </td>
                        {expandedRuns.map(r => {
                          const item = r.items.find(i => i.checkpoint_id === cid)
                          const passed = item?.passed
                          const hit = item?.hit
                          const required = item?.required
                          let symbol = '—'
                          let color = '#ccc'
                          if (required) {
                            symbol = passed ? '✓' : '✗'
                            color = passed ? '#28a745' : '#dc3545'
                          } else {
                            symbol = hit ? '✓' : '○'
                            color = hit ? '#28a745' : '#aaa'
                          }
                          return (
                            <td
                              key={r.agent_run_id}
                              style={{
                                padding: '6px 10px',
                                textAlign: 'center',
                                color,
                                fontWeight: required && !passed ? 600 : 400,
                              }}
                            >
                              {symbol}
                            </td>
                          )
                        })}
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            )}

            {drillTab === 'compare' && (
              expandedIsMultiRun ? (
                <CheckpointRunCompare
                  runs={compareRunsForQuestion(expandedQuestion)}
                  taskAgentId={taskAgentId}
                  langfuseTraceReady={langfuseTraceReady}
                />
              ) : singleRunForExpanded ? (
                <SingleRunChainView
                  runId={singleRunForExpanded.agent_run_id}
                  questionId={expandedQuestion!}
                  runIndex={singleRunForExpanded.run_index}
                  langfuseTraceReady={langfuseTraceReady}
                />
              ) : (
                <p style={{ margin: 0, fontSize: 13, color: '#666' }}>暂无 Run 数据</p>
              )
            )}
          </div>
        </div>
      )}
    </div>
  )
}
