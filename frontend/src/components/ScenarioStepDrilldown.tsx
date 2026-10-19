import { useEffect, useMemo, useState } from 'react'
import { formatScenarioStepLabel } from '../utils/scenarioStepDisplay'
import { LangfuseTraceBlock } from './LangfuseTraceBlock'

const API_BASE = '/api'

export interface LangfuseLinks {
  langfuse_host?: string | null
  trace_id?: string | null
  trace_url?: string | null
  observation_url?: string | null
  langfuse_project_id?: string | null
  langfuse_project_key?: string | null
}

export interface ScenarioStepTraceBinding extends LangfuseLinks {
  label?: string
  observation_source?: string
  input_preview?: string
  output_preview?: string
  observation_id?: string | null
  observation_name?: string | null
  observation_type?: string | null
}

export interface ScenarioStepDetail {
  step_key: string
  name?: string
  step_order?: number
  weight?: number
  required?: boolean
  gt_criteria?: string[] | string
  score: number
  passed: boolean
  evidence?: {
    matched_keywords?: string[]
    bind_source?: string
    observation_name?: string
    note?: string
    score_source?: string
    match_ratio?: number
    reason?: string
    llm_judge_reason?: string
    eval_mode?: string
    eval_source?: string
    score_group?: string
    aggregate_prompt?: string
    langfuse_trace_url?: string | null
    langfuse_observation_url?: string | null
  }
  trace_binding?: ScenarioStepTraceBinding
}

export interface ScenarioSpanRef {
  step_id: string
  step_key: string
  step_name?: string
  observation_id?: string | null
  span_node_id?: string | null
  bind_source?: string | null
}

export interface ScenarioStepsDetailPayload {
  agent_run_id: string
  scenario_key: string
  composite_score: number
  trace_id?: string | null
  langfuse_host?: string | null
  langfuse?: LangfuseLinks
  observation_source?: string
  template?: { name?: string; ground_truth_ref?: string }
  steps: ScenarioStepDetail[]
  span_refs?: ScenarioSpanRef[]
}

interface Props {
  agentRunId: string
  questionLabel?: string
  scenarioKey?: string
}

interface SpanTreeNode {
  id: string
  name: string
  observation_id?: string | null
  children?: SpanTreeNode[]
}

function ScenarioRunSpanTree({
  agentRunId,
  highlightObservationId,
  highlightObservationIds,
}: {
  agentRunId: string
  highlightObservationId?: string | null
  highlightObservationIds?: string[]
}) {
  const highlightSet = useMemo(() => {
    const set = new Set<string>()
    for (const id of highlightObservationIds || []) {
      if (id) set.add(id)
    }
    if (highlightObservationId) set.add(highlightObservationId)
    return set
  }, [highlightObservationId, highlightObservationIds])
  const [tree, setTree] = useState<SpanTreeNode[]>([])
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    const load = async () => {
      try {
        setLoading(true)
        setErr(null)
        const res = await fetch(
          `${API_BASE}/runs/${encodeURIComponent(agentRunId)}/span-tree`,
        )
        if (!res.ok) {
          setTree([])
          return
        }
        const body = await res.json()
        setTree(body.tree || [])
      } catch (e) {
        setErr(e instanceof Error ? e.message : '加载 Span 树失败')
      } finally {
        setLoading(false)
      }
    }
    void load()
  }, [agentRunId])

  const renderNode = (node: SpanTreeNode, depth = 0) => {
    const obsId = node.observation_id || ''
    const highlighted = Boolean(obsId && highlightSet.has(obsId))
    return (
      <div key={node.id} style={{ marginLeft: depth * 14, marginBottom: 4 }}>
        <div
          style={{
            fontSize: 11,
            padding: '2px 6px',
            borderRadius: 3,
            background: highlighted ? 'var(--edp-amber-soft)' : 'transparent',
            border: highlighted ? '1px solid var(--edp-amber)' : '1px solid transparent',
          }}
        >
          {node.name}
          {obsId ? (
            <span style={{ color: 'var(--edp-text3)', marginLeft: 6 }}>{obsId.slice(0, 10)}…</span>
          ) : null}
        </div>
        {(node.children || []).map(c => renderNode(c, depth + 1))}
      </div>
    )
  }

  if (loading) {
    return <p style={{ fontSize: 11, color: 'var(--edp-text3)' }}>加载平台 Span 树…</p>
  }
  if (err) {
    return <p style={{ fontSize: 11, color: 'var(--edp-red)' }}>{err}</p>
  }
  if (!tree.length) {
    return (
      <p style={{ fontSize: 11, color: 'var(--edp-text3)' }}>
        暂无物化 Span（需 Trace 同步完成后显示）
      </p>
    )
  }
  return (
    <div
      style={{
        maxHeight: 220,
        overflow: 'auto',
        padding: 8,
        background: 'var(--edp-bg)',
        border: '1px solid var(--edp-border-soft)',
        borderRadius: 4,
      }}
    >
      {tree.map(n => renderNode(n))}
    </div>
  )
}

function scoreBg(score: number): string {
  if (score >= 0.8) return 'var(--edp-green-soft)'
  if (score >= 0.5) return 'var(--edp-amber-soft)'
  return 'var(--edp-red-soft)'
}

/**
 * 格式化 input/output 预览内容，提升可读性。
 * - JSON 字符串（对象/数组）→ 缩进 2 空格 pretty-print
 * - 其它（纯文本 / 已抽出的 LLM 文本）→ 原样返回
 * 返回 { text, isJson } 便于 UI 区分展示。
 */
export function formatPreviewContent(raw: string | null | undefined): {
  text: string
  isJson: boolean
} {
  const trimmed = (raw ?? '').trim()
  if (!trimmed) return { text: '', isJson: false }
  // 仅尝试以 { 或 [ 开头的内容，避免对普通文本做无谓解析
  if (trimmed[0] === '{' || trimmed[0] === '[') {
    try {
      const parsed = JSON.parse(trimmed)
      const text = JSON.stringify(parsed, null, 2)
      return { text, isJson: true }
    } catch {
      // 不是合法 JSON（可能被截断或本就是 Python dict repr），原样返回
    }
  }
  return { text: trimmed, isJson: false }
}

/**
 * 渲染 input/output 预览块。参考质量指标页逐步对比的样式：
 * 等宽字体 + pre-wrap + 行高 1.6 + 顶部小标签区分 JSON/文本。
 */
export function PreviewBlock({
  label,
  raw,
  emptyHint,
  maxHeight = 240,
}: {
  label: string
  raw: string | null | undefined
  emptyHint: string
  maxHeight?: number
}) {
  const { text, isJson } = formatPreviewContent(raw)
  if (!text) {
    return (
      <div style={{ marginBottom: 8 }}>
        <div style={{ color: 'var(--edp-text3)', fontSize: 12, marginBottom: 4 }}>{label}</div>
        <div
          style={{
            padding: 10,
            background: 'var(--edp-bg)',
            borderRadius: 4,
            border: '1px dashed var(--edp-border)',
            color: 'var(--edp-text3)',
            fontSize: 12,
            lineHeight: 1.6,
          }}
        >
          {emptyHint}
        </div>
      </div>
    )
  }
  return (
    <div style={{ marginBottom: 8 }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          marginBottom: 4,
          color: 'var(--edp-text3)',
          fontSize: 12,
        }}
      >
        <span>{label}</span>
        <span
          style={{
            fontSize: 10,
            padding: '1px 6px',
            borderRadius: 8,
            background: isJson ? 'var(--edp-blue-soft)' : 'var(--edp-bg)',
            color: isJson ? 'var(--edp-blue)' : 'var(--edp-text3)',
            fontWeight: 600,
          }}
        >
          {isJson ? 'JSON' : 'Text'}
        </span>
      </div>
      <pre
        style={{
          margin: 0,
          padding: 10,
          background: 'var(--edp-bg)',
          borderRadius: 4,
          border: '1px solid var(--edp-border)',
          maxHeight,
          overflow: 'auto',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          fontFamily:
            'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
          fontSize: 12.5,
          lineHeight: 1.6,
          color: 'var(--edp-text)',
        }}
      >
        {text}
      </pre>
    </div>
  )
}

export function ScenarioStepDrilldown({
  agentRunId,
  questionLabel,
  scenarioKey,
}: Props) {
  const [resolvedKey, setResolvedKey] = useState<string | null>(scenarioKey ?? null)
  const [data, setData] = useState<ScenarioStepsDetailPayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expandedKey, setExpandedKey] = useState<string | null>(null)

  useEffect(() => {
    if (scenarioKey) {
      setResolvedKey(scenarioKey)
      return
    }
    if (!agentRunId) {
      setResolvedKey(null)
      return
    }
    const resolve = async () => {
      try {
        const res = await fetch(
          `${API_BASE}/scenario-eval/runs/${encodeURIComponent(agentRunId)}/resolved-scenario`,
        )
        if (res.ok) {
          const body = await res.json()
          setResolvedKey(body.scenario_key || null)
        } else {
          setResolvedKey(null)
        }
      } catch {
        setResolvedKey(null)
      }
    }
    void resolve()
  }, [agentRunId, scenarioKey])

  useEffect(() => {
    if (!agentRunId || !resolvedKey) {
      setData(null)
      return
    }
    const load = async () => {
      try {
        setLoading(true)
        setError(null)
        const qs = resolvedKey
          ? `?scenario_key=${encodeURIComponent(resolvedKey)}`
          : ''
        const res = await fetch(
          `${API_BASE}/scenario-eval/runs/${encodeURIComponent(agentRunId)}/steps-detail${qs}`,
        )
        if (!res.ok) {
          const body = await res.json().catch(() => ({}))
          throw new Error(
            typeof body.detail === 'string' ? body.detail : '加载子步骤 Trace 失败',
          )
        }
        setData(await res.json())
        setExpandedKey(null)
      } catch (e) {
        setError(e instanceof Error ? e.message : '加载失败')
        setData(null)
      } finally {
        setLoading(false)
      }
    }
    void load()
  }, [agentRunId, resolvedKey])

  if (!agentRunId) {
    return (
      <p style={{ fontSize: 12, color: 'var(--edp-text3)', marginTop: 12 }}>
        请在上方 L2 矩阵中点击某一 Agent 列的子步骤单元格，查看该步骤的 input/output 与 Langfuse 下钻。
      </p>
    )
  }

  if (loading) {
    return <p style={{ fontSize: 12, color: 'var(--edp-text3)', marginTop: 12 }}>加载子步骤 Trace…</p>
  }
  if (error) {
    return <p style={{ fontSize: 12, color: 'var(--edp-red)', marginTop: 12 }}>{error}</p>
  }
  if (!data) return null

  const expandedStep = data.steps.find(s => s.step_key === expandedKey)
  const highlightObsId =
    expandedStep?.trace_binding?.observation_id ?? null
  const boundObsSet = new Set<string>()
  for (const ref of data.span_refs || []) {
    if (ref.observation_id) boundObsSet.add(ref.observation_id)
  }
  for (const step of data.steps || []) {
    const oid = step.trace_binding?.observation_id
    if (oid) boundObsSet.add(oid)
  }
  const allBoundObsIds = [...boundObsSet]

  return (
    <div style={{ marginTop: 16 }}>
      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: 12,
          alignItems: 'center',
          marginBottom: 10,
        }}
      >
        <h3 style={{ fontSize: 14, margin: 0 }}>
          子步骤 Trace 下钻
          {questionLabel ? ` — ${questionLabel}` : ''}
        </h3>
        <span style={{ fontSize: 12, color: 'var(--edp-text3)' }}>
          综合分 {data.composite_score.toFixed(2)}
          {resolvedKey ? ` · 场景 ${resolvedKey}` : ''}
          {data.observation_source ? ` · 观测源 ${data.observation_source}` : ''}
        </span>
      </div>

      <LangfuseTraceBlock
        traceUrl={data.langfuse?.trace_url ?? null}
        traceId={data.langfuse?.trace_id ?? null}
        langfuseProjectId={data.langfuse?.langfuse_project_id ?? null}
        langfuseHost={data.langfuse?.langfuse_host ?? null}
        observationUrl={expandedStep?.trace_binding?.observation_url ?? null}
      />

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {data.steps.map((step, stepIdx) => {
          const tb = step.trace_binding
          const expanded = expandedKey === step.step_key
          const stepObsUrl =
            tb?.observation_url && tb?.observation_id
              ? tb.observation_url
              : null
          const stepTraceUrl = tb?.trace_url ?? data.langfuse?.trace_url ?? null
          return (
            <div
              key={step.step_key}
              style={{
                border: '1px solid var(--edp-border)',
                borderRadius: 6,
                overflow: 'hidden',
                background: 'var(--edp-card)',
              }}
            >
              <button
                type="button"
                onClick={() =>
                  setExpandedKey(expanded ? null : step.step_key)
                }
                style={{
                  width: '100%',
                  textAlign: 'left',
                  padding: '10px 12px',
                  border: 'none',
                  background: scoreBg(step.score),
                  color: 'var(--edp-text)',
                  cursor: 'pointer',
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  gap: 8,
                }}
              >
                <span>
                  {formatScenarioStepLabel(step, stepIdx + 1)}
                  {step.passed ? ' ✓' : ''}
                </span>
                <span style={{ fontSize: 12 }}>
                  {step.score.toFixed(2)} · w={step.weight}
                </span>
              </button>
              {expanded && (
                <div style={{ padding: 12, fontSize: 12, borderTop: '1px solid var(--edp-border-soft)' }}>
                  {/* 评估结果 */}
                  <div style={{ marginBottom: 8, padding: '8px 12px', background: 'var(--edp-bg)', borderRadius: 4, border: '1px solid var(--edp-border)' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                      <span style={{ fontWeight: 500 }}>评估结果</span>
                      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
                        <span>得分: <strong style={{ color: step.passed ? 'var(--edp-green)' : 'var(--edp-red)' }}>{step.score.toFixed(2)}</strong></span>
                        <span style={{
                          padding: '1px 8px',
                          borderRadius: 10,
                          fontSize: 11,
                          background: step.passed ? 'var(--edp-green-soft)' : 'var(--edp-red-soft)',
                          color: step.passed ? 'var(--edp-green)' : 'var(--edp-red)',
                        }}>{step.passed ? '通过' : '未通过'}</span>
                      </div>
                    </div>
                    <div style={{ color: 'var(--edp-text3)', fontSize: 11, marginBottom: (step.evidence?.reason || step.evidence?.llm_judge_reason) ? 8 : 0 }}>
                      评估方式：{step.evidence?.eval_mode || step.evidence?.score_source || 'keyword'}
                      {step.evidence?.score_group && ` · ${step.evidence.score_group}`}
                    </div>
                    {(step.evidence?.reason || step.evidence?.llm_judge_reason) && (
                      <>
                        <div style={{ color: 'var(--edp-text3)', fontSize: 12, marginBottom: 4 }}>评估分析</div>
                        <div
                          style={{
                            padding: 8,
                            background: 'var(--edp-card)',
                            borderRadius: 4,
                            border: '1px solid var(--edp-border)',
                            whiteSpace: 'pre-wrap',
                            lineHeight: 1.6,
                          }}
                        >
                          {step.evidence?.reason || step.evidence?.llm_judge_reason}
                        </div>
                      </>
                    )}
                    {step.evidence?.aggregate_prompt && (
                      <>
                        <div style={{ color: 'var(--edp-text3)', fontSize: 12, marginBottom: 4, marginTop: 8 }}>评测提示词</div>
                        <div
                          style={{
                            padding: 8,
                            background: 'var(--edp-card)',
                            borderRadius: 4,
                            border: '1px solid var(--edp-border)',
                            whiteSpace: 'pre-wrap',
                            lineHeight: 1.6,
                            maxHeight: 300,
                            overflow: 'auto',
                          }}
                        >
                          {step.evidence.aggregate_prompt}
                        </div>
                      </>
                    )}
                  </div>
                  {(() => {
                    const criteria = step.gt_criteria
                    if (!criteria) return null
                    const items = Array.isArray(criteria) ? criteria : [criteria]
                    if (items.length === 0) return null
                    return (
                      <div style={{ marginBottom: 8 }}>
                        <strong>判分要点：</strong>
                        {items.join('；')}
                      </div>
                    )
                  })()}
                  <p style={{ margin: '0 0 8px', color: 'var(--edp-text2)' }}>
                    命中：
                    {(step.evidence?.matched_cmd_ids as string[] | undefined)?.length
                      ? (step.evidence?.matched_cmd_ids as string[]).join(', ')
                      : (step.evidence?.matched_keywords || []).join(', ') || '无'}
                    {step.evidence?.match_ratio != null
                      ? `（${(step.evidence.match_ratio * 100).toFixed(0)}%）`
                      : ''}
                    {step.evidence?.score_source
                      ? ` · 判分来源：${step.evidence.score_source}`
                      : ''}
                  </p>
                  {(step.evidence?.missing_cmd_ids as string[] | undefined)?.length ? (
                    <p style={{ margin: '0 0 8px', color: 'var(--edp-amber)', fontSize: 12 }}>
                      缺失：{(step.evidence?.missing_cmd_ids as string[]).join(', ')}
                    </p>
                  ) : null}
                  {step.evidence?.note && (
                    <p style={{ margin: '0 0 8px', color: 'var(--edp-amber)' }}>{step.evidence.note}</p>
                  )}
                  {tb?.observation_name && (
                    <p style={{ margin: '0 0 8px', color: 'var(--edp-text2)' }}>
                      绑定 Span：<code>{tb.observation_name}</code>
                      {tb.observation_id ? ` (${tb.observation_id.slice(0, 8)}…)` : ''}
                    </p>
                  )}
                  <PreviewBlock
                    label="Input"
                    raw={tb?.input_preview}
                    emptyHint="（无或未绑定到独立 observation）"
                    maxHeight={180}
                  />
                  <PreviewBlock
                    label="Output"
                    raw={tb?.output_preview}
                    emptyHint="（无）"
                    maxHeight={260}
                  />
                  <LangfuseTraceBlock
                    traceUrl={stepTraceUrl}
                    traceId={tb?.trace_id ?? data.langfuse?.trace_id ?? null}
                    langfuseProjectId={tb?.langfuse_project_id ?? data.langfuse?.langfuse_project_id ?? null}
                    langfuseHost={tb?.langfuse_host ?? data.langfuse?.langfuse_host ?? null}
                    observationUrl={stepObsUrl}
                    label={stepObsUrl ? '在 Langfuse 中查看本子步骤 ↗' : '在 Langfuse 中查看整题 Trace ↗'}
                  />
                </div>
              )}
            </div>
          )
        })}
      </div>

      <div style={{ marginTop: 16 }}>
        <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>
          平台 Span 树（高亮已绑定子步骤的 observation
          {allBoundObsIds.length ? ` · ${allBoundObsIds.length} 处` : ''}）
        </div>
        <ScenarioRunSpanTree
          agentRunId={agentRunId}
          highlightObservationId={highlightObsId}
          highlightObservationIds={allBoundObsIds}
        />
      </div>
    </div>
  )
}
