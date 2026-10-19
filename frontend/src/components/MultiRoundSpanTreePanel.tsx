/**
 * 多轮调用链并列/切换展示（稳定性场景，方案①）
 * - N≤3：横向并列 N 棵调用链树（复用 SpanTreeColumn）
 * - N>3：轮次下拉切换，一次一棵
 * 数据：GET /api/runs/{run_id}/span-tree（读 run_span_nodes，符合范式 P-08/P-15）
 * 不做 N 元差异着色（语义模糊），差异对比交给 RunComparePanel 双树。
 */
import { useEffect, useState } from 'react'
import { formatDuration } from '../utils/dateUtils'
import { SpanNode, SpanTreeColumn } from './SpanTreeView'

const API_BASE = '/api'

interface RunRef {
  agent_run_id: string
  run_index?: number
}

interface SpanTreePayload {
  tree: SpanNode[]
  node_count: number
  trace_sync_status?: string | null
}

interface RoundData {
  run_id: string
  run_index: number
  loading: boolean
  error: string | null
  data: SpanTreePayload | null
}

const SYNC_HINT: Record<string, string> = {
  pending_sync: '待同步：Trace Sync Worker 将自动拉取 Langfuse trace。',
  pending: 'Trace 同步排队中，请稍后刷新。',
  failed: 'Trace 同步失败，可在 Trace 同步队列查看错误并重试。',
  skipped: '本 Run 无 trace_id，无法物化 Span。',
  synced: 'Trace 已同步，但 observations 为空——Langfuse 上该 Trace 无可解析节点。',
}

const PARALLEL_THRESHOLD = 3

export interface MultiRoundSpanTreePanelProps {
  runs: RunRef[]
  questionId?: number | null
}

export function MultiRoundSpanTreePanel({ runs, questionId }: MultiRoundSpanTreePanelProps) {
  const [rounds, setRounds] = useState<RoundData[]>([])
  const [activeIdx, setActiveIdx] = useState(0)

  useEffect(() => {
    const ordered = [...runs].sort((a, b) => (a.run_index ?? 0) - (b.run_index ?? 0))
    setActiveIdx(0)
    setRounds(
      ordered.map(r => ({
        run_id: r.agent_run_id,
        run_index: r.run_index ?? 0,
        loading: true,
        error: null,
        data: null,
      })),
    )

    let cancelled = false
    ordered.forEach((r, idx) => {
      fetch(`${API_BASE}/runs/${encodeURIComponent(r.agent_run_id)}/span-tree`)
        .then(res => (res.ok ? res.json() : Promise.reject(new Error('加载 Span 树失败'))))
        .then((json: SpanTreePayload) => {
          if (cancelled) return
          setRounds(prev => {
            const next = [...prev]
            next[idx] = { ...next[idx], loading: false, data: json }
            return next
          })
        })
        .catch(e => {
          if (cancelled) return
          setRounds(prev => {
            const next = [...prev]
            next[idx] = { ...next[idx], loading: false, error: e instanceof Error ? e.message : '加载失败' }
            return next
          })
        })
    })
    return () => {
      cancelled = true
    }
  }, [runs])

  if (rounds.length === 0) return null

  const parallelMode = rounds.length <= PARALLEL_THRESHOLD

  const renderRound = (rd: RoundData, showTitle = true) => {
    const title = `第 ${rd.run_index + 1} 轮`
    if (rd.loading) {
      return (
        <div key={rd.run_id} style={{ flex: 1, minWidth: 0 }}>
          {showTitle && <h4 style={{ margin: '0 0 8px', fontSize: 13 }}>{title}</h4>}
          <p style={{ fontSize: 12, color: '#888' }}>加载调用链…</p>
        </div>
      )
    }
    if (rd.error) {
      return (
        <div key={rd.run_id} style={{ flex: 1, minWidth: 0 }}>
          {showTitle && <h4 style={{ margin: '0 0 8px', fontSize: 13 }}>{title}</h4>}
          <p style={{ fontSize: 12, color: '#c62828' }}>{rd.error}</p>
        </div>
      )
    }
    const d = rd.data
    if (!d || d.node_count === 0) {
      return (
        <div key={rd.run_id} style={{ flex: 1, minWidth: 0 }}>
          {showTitle && <h4 style={{ margin: '0 0 8px', fontSize: 13 }}>{title}</h4>}
          <p style={{ fontSize: 12, color: '#888' }}>
            暂无 Span 节点（0 节点）。
            {d?.trace_sync_status ? (
              <span style={{ marginLeft: 6 }}>
                状态：<strong>{d.trace_sync_status}</strong>
              </span>
            ) : null}
          </p>
          {d?.trace_sync_status && SYNC_HINT[d.trace_sync_status] ? (
            <p style={{ margin: '4px 0 0', fontSize: 11, color: '#aaa' }}>
              {SYNC_HINT[d.trace_sync_status]}
            </p>
          ) : null}
        </div>
      )
    }
    return (
      <div key={rd.run_id} style={{ flex: 1, minWidth: 0 }}>
        <SpanTreeColumn
          title={showTitle ? `${title} · ${d.node_count} 节点` : `${d.node_count} 节点`}
          tree={d.tree}
          selectedKey={null}
          onSelect={() => {}}
        />
      </div>
    )
  }

  return (
    <div
      style={{
        marginTop: 16,
        padding: 14,
        background: '#f8f9fc',
        border: '1px solid #c5cae9',
        borderRadius: 8,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
        <h4 style={{ margin: 0, fontSize: 14, color: '#3949ab' }}>
          多轮调用链对比
          {questionId != null ? ` · 题目 #${questionId}` : ''}
          {` · ${rounds.length} 轮`}
        </h4>
        {!parallelMode && (
          <label style={{ fontSize: 12, color: '#666', display: 'flex', alignItems: 'center', gap: 8 }}>
            选择轮次
            <select
              value={activeIdx}
              onChange={e => setActiveIdx(Number(e.target.value))}
              style={{ padding: '4px 8px', borderRadius: 6, border: '1px solid #ccc', fontSize: 12 }}
            >
              {rounds.map((rd, i) => (
                <option key={rd.run_id} value={i}>
                  第 {rd.run_index + 1} 轮
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      <p style={{ margin: '0 0 12px', fontSize: 12, color: '#666', lineHeight: 1.5 }}>
        复用单 Run Span 树渲染，按轮次并列看父子结构与迭代工具调用。
        {parallelMode ? ' 轮次较少，横向并排对比。' : ' 轮次较多，切换查看单轮。'}
        差异对比请用「推理链 / Span 树对比」双树面板。
      </p>

      {parallelMode ? (
        <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start', overflowX: 'auto' }}>
          {rounds.map(rd => renderRound(rd))}
        </div>
      ) : (
        <div>{renderRound(rounds[activeIdx] ?? rounds[0], false)}</div>
      )}
    </div>
  )
}
