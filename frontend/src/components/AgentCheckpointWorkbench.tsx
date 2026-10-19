import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import axios from 'axios'
import { WizardStep, WizardStepper } from './AgentCheckpointWizardSteps'

const API_BASE = '/api'

interface AgentOption {
  id: string
  name: string
  logical_agent_id: string
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

interface BatchResultItem {
  template_id: string
  template_name: string
  enabled: boolean
  required: boolean
  order: number | null
  hit: boolean
  matched_observation_name: string | null
  suggestion: string | null
}

interface BatchPreviewResponse {
  trace_id: string
  observation_count: number
  observation_source: string
  enabled_total: number
  hit_count: number
  required_miss_count: number
  all_required_hit: boolean
  results: BatchResultItem[]
}

interface WeightSummary {
  enabled_count: number
  total_count: number
  weight_sum: number
  normalized: boolean
}

interface Props {
  agentName: string
  agentId: string
  agentOptions: AgentOption[]
  onAgentChange: (name: string) => void
  formatAgentLabel: (opt: Pick<AgentOption, 'name' | 'logical_agent_id'>) => string
  templateCount: number
  weightSummary: WeightSummary
  onNavigateOnlineEval?: (agentId: string) => void
  onRefreshTemplates?: () => void | Promise<void>
  onNormalizeWeights?: () => void | Promise<void>
  normalizing?: boolean
  formatApiError: (err: unknown, fallback: string) => string
  initSection: ReactNode
  configSection: ReactNode
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

export function AgentCheckpointWorkbench({
  agentName,
  agentId,
  agentOptions,
  onAgentChange,
  formatAgentLabel,
  templateCount,
  weightSummary,
  onNavigateOnlineEval,
  onRefreshTemplates,
  onNormalizeWeights,
  normalizing,
  formatApiError,
  initSection,
  configSection,
}: Props) {
  const [sampleRuns, setSampleRuns] = useState<SampleRun[]>([])
  const [runsLoading, setRunsLoading] = useState(false)
  const [selectedRunId, setSelectedRunId] = useState('')
  const [manualTraceId, setManualTraceId] = useState('')
  const [showManualTrace, setShowManualTrace] = useState(false)
  const [batchLoading, setBatchLoading] = useState(false)
  const [batchError, setBatchError] = useState('')
  const [batchResult, setBatchResult] = useState<BatchPreviewResponse | null>(null)

  const hasAgent = Boolean(agentName.trim())
  const hasTemplates = templateCount > 0
  const hasRuns = sampleRuns.length > 0
  const verified = Boolean(batchResult?.all_required_hit)

  const selectedRun = useMemo(
    () => sampleRuns.find((r) => r.agent_run_id === selectedRunId) ?? null,
    [sampleRuns, selectedRunId],
  )

  const traceIdForVerify = (manualTraceId.trim() || selectedRun?.trace_id || '').trim()

  const fetchSampleRuns = useCallback(async () => {
    const name = agentName.trim()
    if (!name) {
      setSampleRuns([])
      setSelectedRunId('')
      return
    }
    setRunsLoading(true)
    try {
      const res = await axios.get<{ runs: SampleRun[] }>(`${API_BASE}/checkpoints/sample-runs`, {
        params: { agent_name: name, limit: 10 },
      })
      const runs = res.data.runs || []
      setSampleRuns(runs)
      setSelectedRunId((prev) =>
        prev && runs.some((r) => r.agent_run_id === prev) ? prev : runs[0]?.agent_run_id ?? '',
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
  }, [fetchSampleRuns])

  const stepMeta = useMemo(
    () => [
      { label: '选择 Agent', done: hasAgent },
      { label: '初始化模板', done: hasTemplates },
      { label: '过程点配置', done: hasTemplates },
      { label: 'Trace 验证', done: verified },
    ],
    [hasAgent, hasTemplates, verified],
  )

  const handleBatchVerify = async () => {
    if (!hasAgent || !traceIdForVerify) return
    setBatchLoading(true)
    setBatchError('')
    try {
      const res = await axios.post<BatchPreviewResponse>(
        `${API_BASE}/checkpoints/templates/batch-preview`,
        { agent_name: agentName.trim(), trace_id: traceIdForVerify },
      )
      setBatchResult(res.data)
    } catch (err: unknown) {
      setBatchError(formatApiError(err, '批量验证失败，请稍后重试或刷新 Run 列表后重试'))
      setBatchResult(null)
    } finally {
      setBatchLoading(false)
    }
  }

  return (
    <div
      className="card"
      style={{
        padding: '20px 22px 22px',
        border: '1px solid #c5cae9',
        background: 'linear-gradient(180deg, #fafbff 0%, #fff 8%)',
      }}
    >
      <h2 style={{ margin: '0 0 6px', fontSize: 18 }}>Agent 过程点 · 配置工作台</h2>
      <p style={{ margin: 0, fontSize: 13, color: '#555', lineHeight: 1.65 }}>
        按下方四步完成配置。过程点描述 Agent 执行路径是否合规（看 Trace），与答案对错无关；配置可随时保存，不依赖在线评测。
      </p>

      <WizardStepper steps={stepMeta} />

      <WizardStep
        step={1}
        title="选择 Agent"
        done={hasAgent}
        hint="选择要配置过程点的 Agent。此处仅做配置，不在 Agent 管理中预检推理。"
      >
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center' }}>
          <label style={{ fontSize: 14 }}>
            Agent 名称：
            <select
              value={agentName}
              onChange={(e) => onAgentChange(e.target.value)}
              style={{
                marginLeft: 8,
                padding: '8px 12px',
                border: '1px solid #ddd',
                borderRadius: 4,
                minWidth: 260,
              }}
            >
              <option value="">请选择 Agent</option>
              {agentOptions.map((opt) => (
                <option key={`${opt.name}-${opt.logical_agent_id}`} value={opt.name}>
                  {formatAgentLabel(opt)}
                </option>
              ))}
            </select>
          </label>
          {onRefreshTemplates && (
            <button type="button" className="btn" onClick={() => void onRefreshTemplates()}>
              刷新列表
            </button>
          )}
        </div>
      </WizardStep>

      <WizardStep
        step={2}
        title="初始化模板"
        done={hasTemplates}
        disabled={!hasAgent}
        hint="从 onto-agent M2 参考模板一键导入缺失项；已有同名过程点不会被覆盖。"
      >
        {initSection}
      </WizardStep>

      <WizardStep
        step={3}
        title="过程点配置"
        done={hasTemplates}
        disabled={!hasAgent}
        hint="在列表中管理启用/禁用、顺序依赖与权重；禁用行会灰化。调整权重后可在下方「权重归一化为 1.0」。"
      >
        {hasTemplates && (
          <div
            style={{
              display: 'flex',
              flexWrap: 'wrap',
              gap: 10,
              marginBottom: 12,
              alignItems: 'center',
            }}
          >
            {onNormalizeWeights && (
              <button
                type="button"
                className="btn"
                disabled={normalizing || weightSummary.enabled_count === 0}
                onClick={() => void onNormalizeWeights()}
              >
                {normalizing ? '归一化中…' : '权重归一化为 1.0'}
              </button>
            )}
            <span style={{ fontSize: 13, color: '#666' }}>
              启用 {weightSummary.enabled_count}/{weightSummary.total_count} 条 · 权重合计{' '}
              <strong>{weightSummary.weight_sum.toFixed(3)}</strong>
              {!weightSummary.normalized && weightSummary.enabled_count > 0 ? (
                <span style={{ color: '#e65100' }}>（建议归一化）</span>
              ) : weightSummary.enabled_count > 0 ? (
                <span style={{ color: '#2e7d32' }}>（已归一化）</span>
              ) : null}
            </span>
          </div>
        )}
        {configSection}
      </WizardStep>

      <WizardStep
        step={4}
        title="Trace 验证"
        done={verified}
        disabled={!hasAgent || !hasTemplates}
        hint={
          hasRuns
            ? '选择一次最近 Run，批量验证各过程点是否在 Trace 中命中。'
            : '尚无 Trace 样本：可先保存配置，跑 1 题在线评测后再回来验证；也可手动粘贴 trace_id。'
        }
      >
        {!runsLoading && !hasRuns && (
          <div
            style={{
              marginBottom: 12,
              padding: '10px 12px',
              borderRadius: 8,
              background: '#fff8e1',
              border: '1px solid #ffe082',
              fontSize: 13,
              display: 'flex',
              flexWrap: 'wrap',
              gap: 10,
              alignItems: 'center',
            }}
          >
            <span style={{ flex: 1, minWidth: 200, lineHeight: 1.6 }}>
              建议：在线评测选 1 题运行，产生 Trace 后点「刷新 Run 列表」。
            </span>
            {agentId && onNavigateOnlineEval && (
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => onNavigateOnlineEval(agentId)}
              >
                去在线评测
              </button>
            )}
            <button type="button" className="btn" onClick={() => setShowManualTrace((v) => !v)}>
              {showManualTrace ? '收起 trace_id' : '手动 trace_id'}
            </button>
          </div>
        )}

        {(hasRuns || showManualTrace) && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center' }}>
            {hasRuns && (
              <label style={{ fontSize: 13 }}>
                最近 Run：
                <select
                  value={selectedRunId}
                  onChange={(e) => {
                    setSelectedRunId(e.target.value)
                    setManualTraceId('')
                  }}
                  style={{
                    marginLeft: 8,
                    padding: '6px 10px',
                    border: '1px solid #ddd',
                    borderRadius: 4,
                    minWidth: 280,
                  }}
                >
                  {sampleRuns.map((run) => (
                    <option key={run.agent_run_id} value={run.agent_run_id}>
                      {formatRunLabel(run)}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <button
              type="button"
              className="btn btn-primary"
              disabled={batchLoading || !traceIdForVerify}
              onClick={() => void handleBatchVerify()}
            >
              {batchLoading ? '验证中…' : '批量验证命中'}
            </button>
            <button type="button" className="btn" onClick={() => void fetchSampleRuns()}>
              刷新 Run 列表
            </button>
            {hasRuns && (
              <button type="button" className="btn" onClick={() => setShowManualTrace((v) => !v)}>
                {showManualTrace ? '收起手动' : '手动 trace_id'}
              </button>
            )}
          </div>
        )}

        {selectedRun && !selectedRun.has_span_tree && !manualTraceId.trim() && (
          <div
            className="eval-notice eval-notice--warn"
            style={{ marginTop: 10, fontSize: 13, lineHeight: 1.65 }}
          >
            当前 Run 显示「待同步」：Trace Span 尚未写入评测平台，批量验证可能失败。请先「刷新 Run
            列表」等待「已物化」，或改用「手动 trace_id」。
          </div>
        )}

        {showManualTrace && (
          <div style={{ marginTop: 10 }}>
            <input
              type="text"
              value={manualTraceId}
              onChange={(e) => setManualTraceId(e.target.value)}
              placeholder="粘贴 Langfuse trace_id 或 agent_run_id"
              style={{
                width: '100%',
                maxWidth: 480,
                padding: '8px 12px',
                border: '1px solid #ddd',
                borderRadius: 4,
              }}
            />
          </div>
        )}

        {batchError && (
          <p style={{ margin: '10px 0 0', color: '#c0392b', fontSize: 13, whiteSpace: 'pre-line', lineHeight: 1.65 }}>
            {batchError}
          </p>
        )}

        {batchResult && (
          <div style={{ marginTop: 14 }}>
            <div
              style={{
                padding: '10px 12px',
                borderRadius: 8,
                marginBottom: 10,
                fontSize: 13,
                background: batchResult.all_required_hit ? '#e8f5e9' : '#fff3e0',
                border: batchResult.all_required_hit ? '1px solid #a5d6a7' : '1px solid #ffcc80',
                color: batchResult.all_required_hit ? '#1b5e20' : '#e65100',
              }}
            >
              {batchResult.all_required_hit ? (
                <>
                  ✓ 必达过程点全部命中（{batchResult.hit_count}/{batchResult.enabled_total}{' '}
                  启用项命中）
                </>
              ) : (
                <>
                  ⚠ 必达未全部命中：{batchResult.required_miss_count} 项缺失 · 启用命中{' '}
                  {batchResult.hit_count}/{batchResult.enabled_total}
                </>
              )}
            </div>
            <div className="checkpoint-table-wrap">
              <table className="results-table checkpoint-table">
                <thead>
                  <tr>
                    <th style={{ textAlign: 'center', width: 48 }}>顺序</th>
                    <th style={{ textAlign: 'center' }}>过程点</th>
                    <th style={{ textAlign: 'center', width: 56 }}>必达</th>
                    <th style={{ textAlign: 'center', width: 72 }}>命中</th>
                    <th style={{ textAlign: 'left' }}>匹配 Span</th>
                    <th style={{ textAlign: 'left' }}>建议</th>
                  </tr>
                </thead>
                <tbody>
                  {[...batchResult.results]
                    .filter((r) => r.enabled)
                    .sort((a, b) => (a.order ?? 999) - (b.order ?? 999))
                    .map((row) => (
                      <tr key={row.template_id}>
                        <td style={{ textAlign: 'center' }}>{row.order ?? '—'}</td>
                        <td style={{ textAlign: 'center' }}>{row.template_name}</td>
                        <td style={{ textAlign: 'center' }}>{row.required ? '是' : '否'}</td>
                        <td
                          style={{
                            textAlign: 'center',
                            color: row.hit ? '#2e7d32' : '#c0392b',
                            fontWeight: 600,
                          }}
                        >
                          {row.hit ? '✓' : '✗'}
                        </td>
                        <td>{row.matched_observation_name || '—'}</td>
                        <td>{row.suggestion || '—'}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </WizardStep>
    </div>
  )
}
