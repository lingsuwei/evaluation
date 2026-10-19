import { useCallback, useEffect, useMemo, useState } from 'react'
import axios from 'axios'
import { formatScenarioStepLabel } from '../utils/scenarioStepDisplay'

const API_BASE = '/api'

interface AgentOption {
  logical_agent_id: string
  name: string
}

interface SampleRun {
  agent_run_id: string
  trace_id: string
  question_index: number | null
  question_preview: string
  started_at: string
  status: string
  trace_sync_status?: string | null
  has_span_tree: boolean
}

export interface StepBatchResultItem {
  step_id?: string
  step_key: string
  step_name: string
  step_order?: number
  enabled: boolean
  required: boolean
  span_bound: boolean
  perf_ready: boolean
  bind_source: string
  matched_observation_name?: string | null
  suggestion?: string | null
  eval_mode?: string | null
  aggregated_answer_preview?: string | null
  aggregate_keyword_hits?: string[]
}

interface BatchPreviewResponse {
  trace_id: string
  observation_count: number
  observation_source: string
  observation_names: string[]
  enabled_total: number
  bound_count: number
  perf_ready_count: number
  all_enabled_bound: boolean
  any_perf_ready: boolean
  results: StepBatchResultItem[]
}

interface Props {
  datasetId: string
  datasetItemId: string
  stepCount: number
  agentOptions: AgentOption[]
  verifyAgentName: string
  onVerifyAgentChange: (name: string) => void
  formatApiError: (err: unknown, fallback: string) => string
  onNavigateOnlineEval?: () => void
}

function formatRunLabel(run: SampleRun): string {
  const q =
    run.question_index != null
      ? `Q${run.question_index + 1}`
      : run.question_preview.slice(0, 24) || 'Run'
  const time = run.started_at ? run.started_at.replace('T', ' ').slice(0, 16) : ''
  const spanHint = run.has_span_tree ? '已物化' : '待同步'
  return `${q} · ${time} · ${spanHint}`
}

export function ScenarioStepTraceVerifyPanel({
  datasetId,
  datasetItemId,
  stepCount,
  agentOptions,
  verifyAgentName,
  onVerifyAgentChange,
  formatApiError,
  onNavigateOnlineEval,
}: Props) {
  const [sampleRuns, setSampleRuns] = useState<SampleRun[]>([])
  const [runsLoading, setRunsLoading] = useState(false)
  const [selectedRunId, setSelectedRunId] = useState('')
  const [manualTraceId, setManualTraceId] = useState('')
  const [project, setProject] = useState('')
  const [batchLoading, setBatchLoading] = useState(false)
  const [batchError, setBatchError] = useState('')
  const [batchResult, setBatchResult] = useState<BatchPreviewResponse | null>(null)

  const agentName = verifyAgentName.trim()
  const selectedRun = useMemo(
    () => sampleRuns.find(r => r.agent_run_id === selectedRunId) ?? null,
    [sampleRuns, selectedRunId],
  )
  const traceIdForVerify = (manualTraceId.trim() || selectedRun?.trace_id || '').trim()

  const fetchSampleRuns = useCallback(async () => {
    if (!agentName) {
      setSampleRuns([])
      setSelectedRunId('')
      return
    }
    setRunsLoading(true)
    try {
      const res = await axios.get<{ runs: SampleRun[] }>(`${API_BASE}/checkpoints/sample-runs`, {
        params: { agent_name: agentName, limit: 10 },
      })
      const runs = res.data.runs || []
      setSampleRuns(runs)
      setSelectedRunId(prev =>
        prev && runs.some(r => r.agent_run_id === prev) ? prev : runs[0]?.agent_run_id ?? '',
      )
      if (runs[0]?.trace_id) setManualTraceId('')
    } catch {
      setSampleRuns([])
      setSelectedRunId('')
    } finally {
      setRunsLoading(false)
    }
  }, [agentName])

  useEffect(() => {
    setBatchResult(null)
    setBatchError('')
    void fetchSampleRuns()
  }, [fetchSampleRuns, datasetItemId])

  const handleBatchVerify = async () => {
    if (!agentName || !traceIdForVerify || !datasetItemId) return
    setBatchLoading(true)
    setBatchError('')
    try {
      const res = await axios.post<BatchPreviewResponse>(
        `${API_BASE}/scenario-eval/items/${encodeURIComponent(datasetItemId)}/steps/batch-preview-trace?dataset_id=${encodeURIComponent(datasetId)}`,
        {
          agent_name: agentName,
          trace_id: traceIdForVerify,
          project: project.trim() || null,
        },
      )
      setBatchResult(res.data)
    } catch (err: unknown) {
      setBatchError(formatApiError(err, 'Trace 绑定验证失败'))
      setBatchResult(null)
    } finally {
      setBatchLoading(false)
    }
  }

  if (stepCount <= 0) return null

  return (
    <div className="card scenario-trace-verify-panel">
      <h3 className="scenario-trace-verify-panel__title">Trace 绑定验证</h3>
      <p className="scenario-trace-verify-panel__desc">
        对齐过程点配置：选 Agent 最近 Run 或粘贴 trace_id，批量试跑本题 {stepCount} 个子步骤的 Span
        绑定。未绑定步骤在版本对比「子步骤运行效能」中均时延/Tokens 将显示 —。
      </p>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center', marginBottom: 10 }}>
        <label className="scenario-trace-field-label--inline">
          验证 Agent：
          <select
            className="scenario-trace-control scenario-trace-control--agent"
            value={verifyAgentName}
            onChange={e => onVerifyAgentChange(e.target.value)}
          >
            <option value="">— 选择 Agent —</option>
            {agentOptions.map(a => (
              <option key={a.logical_agent_id} value={a.name}>
                {a.name} ({a.logical_agent_id})
              </option>
            ))}
          </select>
        </label>
        {onNavigateOnlineEval && (
          <button type="button" className="btn btn-sm" onClick={onNavigateOnlineEval}>
            去在线评测跑 1 题
          </button>
        )}
      </div>

      {!agentName && (
        <p className="muted" style={{ margin: 0 }}>
          请选择 Agent 以加载最近 Run 样本。
        </p>
      )}

      {agentName && (
        <>
          {runsLoading ? (
            <p className="muted" style={{ margin: '0 0 8px' }}>加载最近 Run…</p>
          ) : sampleRuns.length > 0 ? (
            <div style={{ marginBottom: 10 }}>
              <label className="scenario-trace-field-label">
                最近 Run（可选）
              </label>
              <select
                className="scenario-trace-control scenario-trace-control--run"
                value={selectedRunId}
                onChange={e => {
                  const run = sampleRuns.find(r => r.agent_run_id === e.target.value)
                  setSelectedRunId(e.target.value)
                  if (run?.trace_id) setManualTraceId(run.trace_id)
                }}
              >
                {sampleRuns.map(run => (
                  <option key={run.agent_run_id} value={run.agent_run_id}>
                    {formatRunLabel(run)}
                  </option>
                ))}
              </select>
            </div>
          ) : (
            <p className="muted" style={{ margin: '0 0 8px' }}>
              尚无 Trace 样本：可先保存配置，跑 1 题在线评测后再回来验证；也可手动粘贴 trace_id。
            </p>
          )}

          <div style={{ marginBottom: 8 }}>
            <label className="scenario-trace-field-label">
              Langfuse trace_id *
            </label>
            <input
              type="text"
              className="scenario-trace-control scenario-trace-control--trace-id"
              value={manualTraceId}
              onChange={e => setManualTraceId(e.target.value)}
              placeholder="从 Trace 探索复制，或选上方最近 Run"
            />
          </div>
          <div style={{ marginBottom: 10 }}>
            <label className="scenario-trace-field-label">
              项目 id（可选）
            </label>
            <input
              type="text"
              className="scenario-trace-control scenario-trace-control--project"
              value={project}
              onChange={e => setProject(e.target.value)}
              placeholder="如 cc-consume-2.0"
            />
          </div>

          {batchError && (
            <div
              style={{
                marginBottom: 10,
                padding: '8px 12px',
                borderRadius: 6,
                background: '#fdecea',
                border: '1px solid #f5c6cb',
                color: '#c0392b',
                fontSize: 13,
              }}
            >
              {batchError}
            </div>
          )}

          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={batchLoading || !traceIdForVerify}
            onClick={() => void handleBatchVerify()}
          >
            {batchLoading ? '验证中…' : '批量验证 Span 绑定'}
          </button>

          {batchResult && (
            <div style={{ marginTop: 14 }}>
              <div
                style={{
                  padding: '10px 12px',
                  borderRadius: 6,
                  fontSize: 13,
                  marginBottom: 10,
                  background: batchResult.all_enabled_bound ? '#e8f5e9' : '#fff3e0',
                  border: batchResult.all_enabled_bound ? '1px solid #a5d6a7' : '1px solid #ffcc80',
                  color: batchResult.all_enabled_bound ? '#1b5e20' : '#e65100',
                }}
              >
                {batchResult.all_enabled_bound
                  ? `✓ 启用子步骤均已绑定 Span（${batchResult.bound_count}/${batchResult.enabled_total}）`
                  : `⚠ 仅 ${batchResult.bound_count}/${batchResult.enabled_total} 个启用子步骤绑定 Span；效能表可能为 —`}
                {batchResult.observation_count > 0 && (
                  <span style={{ marginLeft: 8, color: '#666' }}>
                    · {batchResult.observation_count} 条 observation（{batchResult.observation_source}）
                  </span>
                )}
              </div>

              {batchResult.observation_names.length > 0 && (
                <p style={{ margin: '0 0 10px', fontSize: 12, color: '#666' }}>
                  Trace 中 Span 名称：
                  <code style={{ marginLeft: 6 }}>{batchResult.observation_names.join(', ')}</code>
                  — 请在子步骤 trace_binding 的 span_name_patterns 中包含实际名称。
                </p>
              )}

              <div style={{ overflowX: 'auto' }}>
                <table className="results-table" style={{ fontSize: 12, minWidth: 640 }}>
                  <thead>
                    <tr>
                      <th>步骤</th>
                      <th>Span 绑定</th>
                      <th>效能可统计</th>
                      <th>匹配 Span</th>
                      <th>评测模式</th>
                      <th>说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {batchResult.results.map((row, rowIdx) => (
                      <tr key={row.step_key} style={{ opacity: row.enabled ? 1 : 0.55 }}>
                        <td>
                          {formatScenarioStepLabel(
                            {
                              name: row.step_name,
                              step_order: row.step_order,
                              step_key: row.step_key,
                            },
                            rowIdx + 1,
                          )}
                        </td>
                        <td>{row.span_bound ? '✓' : '✗'}</td>
                        <td>{row.perf_ready ? '✓' : '—'}</td>
                        <td>{row.matched_observation_name || '—'}</td>
                        <td>
                          {row.eval_mode === 'llm_aggregate' ? (
                            <span>
                              <span style={{ padding: '1px 6px', background: '#e3f2fd', borderRadius: 3, fontSize: 11, fontWeight: 600 }}>基于Prompt评测</span>
                              {row.aggregate_keyword_hits && row.aggregate_keyword_hits.length > 0 && (
                                <div style={{ marginTop: 2, fontSize: 11, color: '#2e7d32' }}>
                                  {row.aggregate_keyword_hits.map(k => (
                                    <span key={k} style={{ marginRight: 4 }}>{k} ✓</span>
                                  ))}
                                </div>
                              )}
                              {row.aggregated_answer_preview && (
                                <div style={{ marginTop: 2, fontSize: 10, color: '#666', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={row.aggregated_answer_preview}>
                                  {row.aggregated_answer_preview}
                                </div>
                              )}
                            </span>
                          ) : 'Ground_Truth评测'}
                        </td>
                        <td style={{ color: row.span_bound ? '#666' : '#c0392b', maxWidth: 280 }}>
                          {row.suggestion || row.bind_source}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

export interface SingleStepPreviewSummary {
  span_bound: boolean
  perf_ready: boolean
  matched_observation_name?: string | null
  bind_source: string
  observation_count: number
  observation_names: string[]
  eval_mode?: string | null
  aggregated_answer_preview?: string | null
  aggregate_keyword_hits?: string[]
}

interface SingleStepPreviewProps {
  datasetId: string
  datasetItemId: string
  agentName: string
  stepKey: string
  name: string
  keywordsText: string
  enabled: boolean
  required: boolean
  stepOrder: string | number
  formatApiError: (err: unknown, fallback: string) => string
  matchRules?: Record<string, unknown> | null
}

export function ScenarioStepSingleTracePreview({
  datasetId,
  datasetItemId,
  agentName,
  stepKey,
  name,
  keywordsText,
  enabled,
  required,
  stepOrder,
  formatApiError,
  matchRules,
}: SingleStepPreviewProps) {
  const [traceId, setTraceId] = useState('')
  const [project, setProject] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [summary, setSummary] = useState<SingleStepPreviewSummary | null>(null)

  const handlePreview = async () => {
    const tid = traceId.trim()
    if (!tid || !agentName.trim() || !name.trim()) {
      setError('请填写 trace_id、选择 Agent，并填写步骤名称')
      return
    }
    const resolvedStepKey = stepKey.trim() || 'preview'
    setLoading(true)
    setError('')
    try {
      const kws = keywordsText
        .split(/[,，\n]/)
        .map(s => s.trim())
        .filter(Boolean)
      const res = await axios.post(`${API_BASE}/scenario-eval/items/${encodeURIComponent(datasetItemId)}/steps/preview-trace?dataset_id=${encodeURIComponent(datasetId)}`, {
        trace_id: tid,
        agent_name: agentName.trim(),
        project: project.trim() || null,
        step_key: resolvedStepKey,
        name: name.trim(),
        gt_expected: { keywords: kws },
        enabled,
        required,
        step_order: stepOrder === '' ? null : Number(stepOrder),
        match_rules: matchRules || undefined,
      })
      const data = res.data as SingleStepPreviewSummary & { suggestion?: string }
      setSummary({
        span_bound: data.span_bound,
        perf_ready: data.perf_ready,
        matched_observation_name: data.matched_observation_name,
        bind_source: data.bind_source,
        observation_count: data.observation_count,
        observation_names: data.observation_names || [],
        eval_mode: data.eval_mode,
        aggregated_answer_preview: data.aggregated_answer_preview,
        aggregate_keyword_hits: data.aggregate_keyword_hits,
      })
    } catch (err: unknown) {
      setError(formatApiError(err, '试跑绑定失败'))
      setSummary(null)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div
      style={{
        marginTop: 14,
        padding: 12,
        background: '#f8f9fc',
        borderRadius: 8,
        border: '1px solid #c5cae9',
      }}
    >
      <h4 style={{ margin: '0 0 6px', fontSize: 14 }}>Trace 试跑绑定（当前表单）</h4>
      <p style={{ margin: '0 0 8px', fontSize: 12, color: '#666' }}>
        使用上方关键词与默认 trace_binding 规则试跑，无需先保存。
      </p>
      {error && (
        <p style={{ color: '#c0392b', fontSize: 13, margin: '0 0 8px' }}>{error}</p>
      )}
      <div style={{ marginBottom: 8 }}>
        <input
          type="text"
          className="scenario-trace-control scenario-trace-control--trace-id"
          value={traceId}
          onChange={e => setTraceId(e.target.value)}
          placeholder="trace_id"
        />
      </div>
      <button type="button" className="btn btn-sm" disabled={loading} onClick={() => void handlePreview()}>
        {loading ? '试跑中…' : '试跑 Span 绑定'}
      </button>
      {summary && (
        <div
          style={{
            marginTop: 10,
            padding: '8px 10px',
            borderRadius: 6,
            fontSize: 12,
            background: summary.span_bound ? '#e8f5e9' : '#fff3e0',
            border: summary.span_bound ? '1px solid #a5d6a7' : '1px solid #ffcc80',
          }}
        >
          {summary.span_bound ? '✓ 已绑定 Span' : '✗ 未绑定 Span'}
          {summary.matched_observation_name ? ` · ${summary.matched_observation_name}` : ''}
          {summary.eval_mode === 'llm_aggregate' && (
            <div style={{ marginTop: 6, padding: '6px 8px', background: '#e3f2fd', borderRadius: 4, border: '1px solid #90caf9' }}>
              <div style={{ fontWeight: 600, marginBottom: 2 }}>评测模式: 基于Prompt评测</div>
              {summary.aggregated_answer_preview && (
                <div style={{ color: '#333', marginBottom: 2 }}>
                  基于Prompt评测输出: <span style={{ fontFamily: 'monospace', fontSize: 11 }}>{summary.aggregated_answer_preview}</span>
                </div>
              )}
              {summary.aggregate_keyword_hits && summary.aggregate_keyword_hits.length > 0 && (
                <div>
                  关键词命中: {summary.aggregate_keyword_hits.map(k => (
                    <span key={k} style={{ display: 'inline-block', margin: '0 2px', padding: '1px 6px', background: '#c8e6c9', borderRadius: 3, fontSize: 11 }}>{k} ✓</span>
                  ))}
                </div>
              )}
            </div>
          )}
          {summary.observation_names.length > 0 && (
            <div style={{ marginTop: 4, color: '#666' }}>
              Trace Span 名称：{summary.observation_names.join(', ')}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
