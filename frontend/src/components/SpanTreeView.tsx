import { formatDuration } from '../utils/dateUtils'

export interface SpanNode {
  id: string
  name: string
  aggregate_key?: string | null
  latency_ms?: number | null
  tokens?: number | null
  mock_response_payload?: unknown
  is_deterministic_io?: boolean
  node_type?: string | null
  observation_id?: string | null
  iteration?: number | null
  children?: SpanNode[]
}

export function SpanTreeColumn({
  title,
  tree,
  highlightKeys,
  onlyKeys,
  selectedKey,
  onSelect,
}: {
  title: string
  tree: SpanNode[]
  highlightKeys?: Set<string>
  onlyKeys?: Set<string>
  selectedKey: string | null
  onSelect: (key: string | null, node: SpanNode) => void
}) {
  const highlights = highlightKeys ?? new Set<string>()
  const only = onlyKeys ?? new Set<string>()

  const renderNode = (node: SpanNode, depth = 0) => {
    const key = node.aggregate_key || ''
    const isAlign = highlights.has(key)
    const isOnly = only.has(key)
    const isSel = selectedKey && key === selectedKey
    return (
      <div key={node.id} style={{ marginLeft: depth * 16, marginBottom: 6 }}>
        <div
          role="button"
          tabIndex={0}
          onClick={() => onSelect(key || null, node)}
          onKeyDown={e => e.key === 'Enter' && onSelect(key || null, node)}
          style={{
            padding: '6px 8px',
            borderRadius: 4,
            fontSize: 12,
            cursor: 'pointer',
            background: isSel ? '#e3f2fd' : isOnly ? '#fff3e0' : isAlign ? '#e8f5e9' : '#f5f5f5',
            border: isSel ? '1px solid #667eea' : isOnly ? '1px solid #ffb74d' : isAlign ? '1px solid #81c784' : '1px solid #e0e0e0',
          }}
        >
          <div style={{ fontWeight: 500 }}>
            {node.name}
            {node.iteration != null && (
              <span
                style={{
                  marginLeft: 6,
                  fontSize: 10,
                  padding: '1px 6px',
                  background: '#ede7f6',
                  color: '#5e35b1',
                  borderRadius: 3,
                  fontWeight: 500,
                }}
              >
                iter {node.iteration}
              </span>
            )}
          </div>
          {key ? <div style={{ color: '#666', marginTop: 2 }}>{key}</div> : null}
          <div style={{ color: '#888', marginTop: 2 }}>
            {node.latency_ms != null ? <span>{formatDuration(node.latency_ms)} </span> : null}
            {node.tokens != null ? <span>· {node.tokens} tok</span> : null}
            {node.node_type ? <span> · {node.node_type}</span> : null}
          </div>
        </div>
        {(node.children || []).map(child => renderNode(child, depth + 1))}
      </div>
    )
  }

  return (
    <div style={{ flex: 1, minWidth: 0 }}>
      <h4 style={{ margin: '0 0 8px 0', fontSize: 14 }}>{title}</h4>
      {tree.length === 0 ? (
        <p style={{ fontSize: 12, color: '#888' }}>无 Span 节点</p>
      ) : (
        tree.map(n => renderNode(n))
      )}
    </div>
  )
}
