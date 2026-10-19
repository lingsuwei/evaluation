import { useEffect, useMemo, useState } from 'react'
import { formatDuration } from '../utils/dateUtils'
import { SpanNode, SpanTreeColumn } from './SpanTreeView'
import { useLangfuseHost } from '../utils/useLangfuseHost'

const API_BASE = '/api'

interface TimelineStep {
  id: string
  name: string
  aggregate_key?: string | null
  latency_ms?: number | null
  mock_response_payload?: unknown
  is_deterministic_io?: boolean
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

interface DualTreeResponse {
  comparison_id: string
  left_run_id: string
  right_run_id: string
  left: { tree: SpanNode[]; node_count: number }
  right: { tree: SpanNode[]; node_count: number }
  align_aggregate_keys: string[]
  only_left_aggregate_keys: string[]
  only_right_aggregate_keys: string[]
  left_timeline?: TimelineStep[]
  right_timeline?: TimelineStep[]
  left_checkpoints?: CheckpointReport | null
  right_checkpoints?: CheckpointReport | null
  left_trace_id?: string | null
  right_trace_id?: string | null
}

export interface RunCompareContentProps {
  comparisonId: string
  initialAggregateKey?: string | null
  leftColumnTitle?: string
  rightColumnTitle?: string
  embedded?: boolean
}

export function RunCompareContent({
  comparisonId,
  initialAggregateKey,
  leftColumnTitle,
  rightColumnTitle,
  embedded = false,
}: RunCompareContentProps) {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [data, setData] = useState<DualTreeResponse | null>(null)
  const [selectedKey, setSelectedKey] = useState<string | null>(initialAggregateKey ?? null)

  useEffect(() => {
    setSelectedKey(initialAggregateKey ?? null)
  }, [comparisonId, initialAggregateKey])

  useEffect(() => {
    const load = async () => {
      try {
        setLoading(true)
        setError(null)
        setData(null)
        const treeRes = await fetch(`${API_BASE}/comparisons/${comparisonId}/span-trees`)
        if (!treeRes.ok) throw new Error('加载 Span 树失败')
        const json: DualTreeResponse = await treeRes.json()
        setData(json)
        if (!initialAggregateKey && json.left_timeline?.length) {
          const diffKey = json.only_right_aggregate_keys[0] || json.only_left_aggregate_keys[0]
          setSelectedKey(diffKey || json.left_timeline[0].aggregate_key || null)
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : '加载失败')
      } finally {
        setLoading(false)
      }
    }
    void load()
  }, [comparisonId, initialAggregateKey])

  const langfuseHost = useLangfuseHost()

  const alignSet = useMemo(() => new Set(data?.align_aggregate_keys || []), [data])
  const onlyLeft = useMemo(() => new Set(data?.only_left_aggregate_keys || []), [data])
  const onlyRight = useMemo(() => new Set(data?.only_right_aggregate_keys || []), [data])

  const leftStep = data?.left_timeline?.find(t => t.aggregate_key === selectedKey)
  const rightStep = data?.right_timeline?.find(t => t.aggregate_key === selectedKey)

  const matchCheckpoint = (side: 'left' | 'right', name: string) => {
    const rep = side === 'left' ? data?.left_checkpoints : data?.right_checkpoints
    return (rep?.items || []).find(i => i.name === name || (selectedKey && i.name?.includes(selectedKey)))
  }

  const traceUrl = (traceId?: string | null) => {
    if (!traceId || !langfuseHost) return null
    return `${langfuseHost}/trace/${traceId}`
  }

  const leftTitle =
    leftColumnTitle ?? (data ? `左 Run (${data.left.node_count})` : '左 Run')
  const rightTitle =
    rightColumnTitle ?? (data ? `右 Run (${data.right.node_count})` : '右 Run')

  if (loading) {
    return <p style={{ fontSize: 13, color: '#666', margin: 0 }}>加载 Span 树对比…</p>
  }
  if (error) {
    return <p style={{ color: '#c62828', fontSize: 13, margin: 0 }}>{error}</p>
  }
  if (!data) {
    return null
  }

  return (
    <>
      {!embedded && (
        <h4 style={{ margin: '0 0 12px', fontSize: 14 }}>推理链 / Span 树对比</h4>
      )}
      <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
        <SpanTreeColumn
          title={leftTitle}
          tree={data.left.tree}
          highlightKeys={alignSet}
          onlyKeys={onlyLeft}
          selectedKey={selectedKey}
          onSelect={(key, _node) => setSelectedKey(key)}
        />
        <div style={{ width: 200, flexShrink: 0, fontSize: 12, color: '#666', paddingTop: 28 }}>
          <strong>时间轴</strong>
          {(data.left_timeline || []).map((s, i) => (
            <div
              key={s.id}
              role="button"
              tabIndex={0}
              onClick={() => setSelectedKey(s.aggregate_key || null)}
              onKeyDown={e => e.key === 'Enter' && setSelectedKey(s.aggregate_key || null)}
              style={{
                marginTop: 6,
                padding: '4px 6px',
                borderLeft: selectedKey === s.aggregate_key ? '2px solid #667eea' : '2px solid #ddd',
                cursor: 'pointer',
                background: selectedKey === s.aggregate_key ? '#f0f4ff' : undefined,
              }}
            >
              {i + 1}. {s.name}
              <div style={{ fontSize: 11 }}>{s.aggregate_key}</div>
            </div>
          ))}
        </div>
        <SpanTreeColumn
          title={rightTitle}
          tree={data.right.tree}
          highlightKeys={alignSet}
          onlyKeys={onlyRight}
          selectedKey={selectedKey}
          onSelect={(key, _node) => setSelectedKey(key)}
        />
      </div>
      {selectedKey && (
        <div style={{ marginTop: 16, padding: 12, background: '#f9f9f9', borderRadius: 6, fontSize: 12 }}>
          <strong>下钻 · {selectedKey}</strong>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 8 }}>
            <div>
              <div>
                左：{leftStep?.name ?? '—'} ·{' '}
                {leftStep?.latency_ms != null ? formatDuration(leftStep.latency_ms) : '—'}
              </div>
              <div style={{ color: '#666', marginTop: 4 }}>
                mock:{' '}
                {leftStep?.mock_response_payload
                  ? JSON.stringify(leftStep.mock_response_payload).slice(0, 120)
                  : '—'}
              </div>
              {matchCheckpoint('left', leftStep?.name || '') && (
                <div style={{ marginTop: 4 }}>
                  过程点：{matchCheckpoint('left', leftStep?.name || '')?.passed ? '通过' : '未通过'}
                </div>
              )}
            </div>
            <div>
              <div>
                右：{rightStep?.name ?? '—'} ·{' '}
                {rightStep?.latency_ms != null ? formatDuration(rightStep.latency_ms) : '—'}
              </div>
              <div style={{ color: '#666', marginTop: 4 }}>
                mock:{' '}
                {rightStep?.mock_response_payload
                  ? JSON.stringify(rightStep.mock_response_payload).slice(0, 120)
                  : '—'}
              </div>
              {matchCheckpoint('right', rightStep?.name || '') && (
                <div style={{ marginTop: 4 }}>
                  过程点：{matchCheckpoint('right', rightStep?.name || '')?.passed ? '通过' : '未通过'}
                </div>
              )}
            </div>
          </div>
          <div style={{ marginTop: 8, display: 'flex', gap: 12 }}>
            {traceUrl(data.left_trace_id) && (
              <a href={traceUrl(data.left_trace_id)!} target="_blank" rel="noreferrer" style={{ color: '#667eea' }}>
                左 Trace
              </a>
            )}
            {traceUrl(data.right_trace_id) && (
              <a href={traceUrl(data.right_trace_id)!} target="_blank" rel="noreferrer" style={{ color: '#667eea' }}>
                右 Trace
              </a>
            )}
            {!langfuseHost && (
              <span style={{ color: '#888' }}>Langfuse 未配置时可从 Trace 探索页下钻</span>
            )}
          </div>
        </div>
      )}
    </>
  )
}

interface ModalProps {
  comparisonId: string
  onClose: () => void
  initialAggregateKey?: string | null
}

export function RunComparePanel({ comparisonId, onClose, initialAggregateKey }: ModalProps) {
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
          width: 'min(1200px, 96vw)',
          maxHeight: '92vh',
          overflow: 'auto',
          padding: 20,
        }}
        onClick={e => e.stopPropagation()}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <h3 style={{ margin: 0 }}>推理链 / Span 树对比</h3>
          <button
            type="button"
            onClick={onClose}
            style={{ border: 'none', background: '#eee', padding: '6px 12px', borderRadius: 4, cursor: 'pointer' }}
          >
            关闭
          </button>
        </div>
        <RunCompareContent comparisonId={comparisonId} initialAggregateKey={initialAggregateKey} embedded />
      </div>
    </div>
  )
}
