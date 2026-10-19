import { useEffect, useMemo, useState } from 'react'
import { formatDuration } from '../utils/dateUtils'
import { SpanNode, SpanTreeColumn } from './SpanTreeView'
import { LangfuseTraceBlock } from './LangfuseTraceBlock'

const API_BASE = '/api'

interface TimelineStep {
  id: string
  name: string
  aggregate_key?: string | null
  latency_ms?: number | null
  tokens?: number | null
  mock_response_payload?: unknown
  is_deterministic_io?: boolean
  node_type?: string | null
  observation_id?: string | null
}

interface FlatNode extends TimelineStep {
  step_index?: number | null
  sibling_order?: number | null
}

interface SpanTreeResponse {
  agent_run_id: string
  trace_id?: string | null
  langfuse_project_id?: string | null
  langfuse_host?: string | null
  question_id?: number | null
  node_count: number
  tree: SpanNode[]
  nodes: FlatNode[]
}

interface CheckpointReport {
  items?: Array<{
    name: string
    passed: boolean
    hit?: boolean | null
    order_ok?: boolean | null
    evidence?: Record<string, unknown>
  }>
}

const SYNC_HINT: Record<string, string> = {
  pending_sync:
    '当前 Run 处于「待同步」：Agent 推理已结束，Trace Sync Worker 将自动拉取 Langfuse trace 并物化。请稍后刷新。',
  pending: 'Trace 同步任务排队中，请稍后刷新或执行 backfill trace sync。',
  failed: 'Trace 同步失败，可在 Trace 同步队列查看错误并重试。',
  skipped: '本 Run 无 trace_id，无法物化 Span（Agent 未上报 Trace）。',
  synced:
    'Trace 已标记为 synced，但 observations 为空——Langfuse 上该 Trace 可能没有可解析的 Span/Generation 节点。',
}

function buildTimeline(nodes: FlatNode[]): TimelineStep[] {
  const ordered = [...nodes].sort(
    (a, b) =>
      (a.step_index ?? 0) - (b.step_index ?? 0) ||
      (a.sibling_order ?? 0) - (b.sibling_order ?? 0),
  )
  return ordered.map(n => ({
    id: n.id,
    name: n.name,
    aggregate_key: n.aggregate_key,
    latency_ms: n.latency_ms,
    tokens: n.tokens,
    mock_response_payload: n.mock_response_payload,
    is_deterministic_io: n.is_deterministic_io,
    node_type: n.node_type,
    observation_id: n.observation_id,
  }))
}

export interface RunSpanTreeContentProps {
  runId: string
  questionId?: number | null
  runIndex?: number
  traceSyncStatus?: string | null
  embedded?: boolean
}

export function RunSpanTreeContent({
  runId,
  questionId,
  runIndex,
  traceSyncStatus,
  embedded = false,
}: RunSpanTreeContentProps) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [data, setData] = useState<SpanTreeResponse | null>(null)
  const [checkpoints, setCheckpoints] = useState<CheckpointReport | null>(null)
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [selectedNode, setSelectedNode] = useState<SpanNode | null>(null)

  useEffect(() => {
    const load = async () => {
      try {
        setLoading(true)
        setError(null)
        setData(null)
        setSelectedKey(null)
        setSelectedNode(null)
        const [treeRes, cpRes] = await Promise.all([
          fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/span-tree`),
          fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/checkpoints`).catch(() => null),
        ])
        if (!treeRes.ok) {
          const body = await treeRes.json().catch(() => ({}))
          throw new Error(typeof body.detail === 'string' ? body.detail : '加载 Span 树失败')
        }
        const json: SpanTreeResponse = await treeRes.json()
        setData(json)
        if (cpRes?.ok) {
          setCheckpoints(await cpRes.json())
        }
        const timeline = buildTimeline(json.nodes || [])
        if (timeline.length) {
          setSelectedKey(timeline[0].aggregate_key || null)
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : '加载失败')
      } finally {
        setLoading(false)
      }
    }
    void load()
  }, [runId])

  const timeline = useMemo(
    () => (data?.nodes ? buildTimeline(data.nodes) : []),
    [data?.nodes],
  )

  const selectedStep = timeline.find(t => t.aggregate_key === selectedKey)

  const matchCheckpoint = (name: string) =>
    (checkpoints?.items || []).find(
      i => i.name === name || (selectedKey && i.name?.includes(selectedKey)),
    )

  const handleSelect = (key: string | null, node: SpanNode) => {
    setSelectedKey(key)
    setSelectedNode(node)
  }

  const titleQ = questionId ?? data?.question_id
  const titleRound = runIndex != null ? ` · 第 ${runIndex + 1} 轮` : ''

  if (loading) {
    return <p style={{ fontSize: 13, color: '#666', margin: 0 }}>加载 Span 树…</p>
  }
  if (error) {
    return <p style={{ color: '#c62828', fontSize: 13, margin: 0 }}>{error}</p>
  }
  if (!data) {
    return null
  }

  if (data.node_count === 0) {
    return (
      <div style={{ margin: 0, fontSize: 13, color: '#888' }}>
        <p style={{ margin: '0 0 8px' }}>
          暂无物化 Span 节点（0 节点）。
          {traceSyncStatus ? (
            <span style={{ marginLeft: 6 }}>
              Trace 物化状态：<strong>{traceSyncStatus}</strong>
            </span>
          ) : null}
        </p>
        <p style={{ margin: 0, fontSize: 12, lineHeight: 1.5 }}>
          {traceSyncStatus && SYNC_HINT[traceSyncStatus]
            ? SYNC_HINT[traceSyncStatus]
            : '请确认 Trace 已同步并完成 Builder 物化；可在评估详情顶部查看「Trace 物化」汇总状态。'}
        </p>
      </div>
    )
  }

  return (
    <>
      {!embedded && (
        <h4 style={{ margin: '0 0 12px', fontSize: 14 }}>
          推理链 · Span 树下钻
          {titleQ != null ? ` · 题目 #${titleQ}${titleRound}` : ''}
          {` · ${data.node_count} 节点`}
        </h4>
      )}
      <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
        <SpanTreeColumn
          title="调用结构"
          tree={data.tree}
          selectedKey={selectedKey}
          onSelect={handleSelect}
        />
        <div style={{ width: 220, flexShrink: 0, fontSize: 12, color: '#666', paddingTop: 28 }}>
          <strong>扁平时间轴</strong>
          {timeline.map((s, i) => (
            <div
              key={s.id}
              role="button"
              tabIndex={0}
              onClick={() => {
                setSelectedKey(s.aggregate_key || null)
                setSelectedNode(null)
              }}
              onKeyDown={e => {
                if (e.key === 'Enter') {
                  setSelectedKey(s.aggregate_key || null)
                  setSelectedNode(null)
                }
              }}
              style={{
                marginTop: 6,
                padding: '4px 6px',
                borderLeft: selectedKey === s.aggregate_key ? '2px solid #667eea' : '2px solid #ddd',
                cursor: 'pointer',
                background: selectedKey === s.aggregate_key ? '#f0f4ff' : undefined,
              }}
            >
              {i + 1}. {s.name}
              {s.aggregate_key ? (
                <div style={{ fontSize: 11, color: '#888' }}>{s.aggregate_key}</div>
              ) : null}
              <div style={{ fontSize: 11, marginTop: 2 }}>
                {s.latency_ms != null ? formatDuration(s.latency_ms) : '—'}
              </div>
            </div>
          ))}
        </div>
      </div>

      {(selectedKey || selectedNode || selectedStep) && (
        <div style={{ marginTop: 16, padding: 12, background: '#f9f9f9', borderRadius: 6, fontSize: 12 }}>
          <strong>节点下钻</strong>
          {selectedKey ? <span style={{ marginLeft: 8, color: '#666' }}>{selectedKey}</span> : null}
          <div style={{ marginTop: 8, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <div>
              <div style={{ fontWeight: 500 }}>
                {(selectedNode?.name ?? selectedStep?.name) || '—'}
              </div>
              <div style={{ marginTop: 4, color: '#555' }}>
                耗时：{' '}
                {(selectedNode?.latency_ms ?? selectedStep?.latency_ms) != null
                  ? formatDuration(selectedNode?.latency_ms ?? selectedStep?.latency_ms!)
                  : '—'}
              </div>
              <div style={{ marginTop: 2, color: '#555' }}>
                Tokens：{(selectedNode?.tokens ?? selectedStep?.tokens) ?? '—'}
              </div>
              {(selectedNode?.node_type ?? selectedStep?.node_type) ? (
                <div style={{ marginTop: 2, color: '#555' }}>
                  类型：{selectedNode?.node_type ?? selectedStep?.node_type}
                </div>
              ) : null}
            </div>
            <div>
              <div style={{ color: '#666' }}>
                mock 响应：{' '}
                {(selectedNode?.mock_response_payload ?? selectedStep?.mock_response_payload)
                  ? JSON.stringify(
                      selectedNode?.mock_response_payload ?? selectedStep?.mock_response_payload,
                    ).slice(0, 160)
                  : '—'}
              </div>
              {matchCheckpoint(selectedNode?.name ?? selectedStep?.name ?? '') && (
                <div style={{ marginTop: 4 }}>
                  过程点：{' '}
                  {matchCheckpoint(selectedNode?.name ?? selectedStep?.name ?? '')?.passed
                    ? '通过'
                    : '未通过'}
                </div>
              )}
            </div>
          </div>
          {data.trace_id && (
            <LangfuseTraceBlock
              traceUrl={null}
              traceId={data.trace_id}
              langfuseProjectId={data.langfuse_project_id}
              langfuseHost={data.langfuse_host}
              label="在 Langfuse 打开 Trace"
            />
          )}
        </div>
      )}
    </>
  )
}

interface ModalProps {
  runId: string
  questionId?: number | null
  runIndex?: number
  traceSyncStatus?: string | null
  onClose: () => void
}

export function RunSpanTreePanel({
  runId,
  questionId,
  runIndex,
  traceSyncStatus,
  onClose,
}: ModalProps) {
  const titleQ = questionId
  const titleRound = runIndex != null ? ` · 第 ${runIndex + 1} 轮` : ''

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.45)',
        zIndex: 2000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
      }}
      onClick={onClose}
    >
      <div
        style={{
          background: '#fff',
          borderRadius: 8,
          width: 'min(960px, 96vw)',
          maxHeight: '92vh',
          overflow: 'auto',
          padding: 20,
        }}
        onClick={e => e.stopPropagation()}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <h3 style={{ margin: 0 }}>
            推理链 · Span 树下钻
            {titleQ != null ? ` · 题目 #${titleQ}${titleRound}` : ''}
          </h3>
          <button
            type="button"
            onClick={onClose}
            style={{ border: 'none', background: '#eee', padding: '6px 12px', borderRadius: 4, cursor: 'pointer' }}
          >
            关闭
          </button>
        </div>
        <RunSpanTreeContent
          runId={runId}
          questionId={questionId}
          runIndex={runIndex}
          traceSyncStatus={traceSyncStatus}
          embedded
        />
      </div>
    </div>
  )
}
