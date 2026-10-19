import type { CSSProperties } from 'react'
import {
  LANGFUSE_AGENT_FORM_NOTICE,
  LANGFUSE_TRACE_FEATURES,
  type LangfuseReadinessStatus,
} from '../utils/langfuseReadiness'

const badgeStyle = (color: string): CSSProperties => ({
  display: 'inline-flex',
  alignItems: 'center',
  flexShrink: 0,
  padding: '2px 8px',
  borderRadius: 10,
  fontSize: 11,
  fontWeight: 600,
  color: '#fff',
  background: color,
  lineHeight: 1.4,
  whiteSpace: 'nowrap',
})

export function LangfuseReadinessBadge({
  status,
  label,
}: {
  status: LangfuseReadinessStatus
  label?: string
}) {
  const ready = status === 'ready'
  return (
    <span
      className="langfuse-readiness-badge"
      style={badgeStyle(ready ? '#389e0d' : '#d48806')}
      title={ready ? 'Langfuse 已绑定' : 'Langfuse 未绑定，Trace 能力不可用'}
    >
      {label || (ready ? '观测就绪' : '观测未就绪')}
    </span>
  )
}

/** L0：创建/编辑 Agent 表单说明条 */
export function LangfuseAgentFormNotice({ variant = 'info' }: { variant?: 'info' | 'warn' }) {
  const isWarn = variant === 'warn'
  return (
    <div
      style={{
        marginTop: 12,
        marginBottom: 4,
        padding: '10px 12px',
        borderRadius: 6,
        fontSize: 12,
        lineHeight: 1.55,
        background: isWarn ? '#fff7e6' : '#f0f7ff',
        border: `1px solid ${isWarn ? '#ffd591' : '#bae0ff'}`,
        color: '#434343',
      }}
    >
      <strong style={{ display: 'block', marginBottom: 4 }}>
        {isWarn ? 'Langfuse 观测未就绪' : 'Langfuse 与 Trace 能力说明'}
      </strong>
      <span>{LANGFUSE_AGENT_FORM_NOTICE}</span>
      <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
        {LANGFUSE_TRACE_FEATURES.map((f) => (
          <li key={f}>{f}</li>
        ))}
      </ul>
    </div>
  )
}

/** L2：Trace 对比 / 下钻空态 */
export function LangfuseTraceBlockedPanel({
  title = 'Trace 对比不可用',
  compact = false,
}: {
  title?: string
  compact?: boolean
}) {
  return (
    <div
      style={{
        padding: compact ? '10px 12px' : '16px',
        borderRadius: 6,
        background: '#fff7e6',
        border: '1px solid #ffd591',
        fontSize: compact ? 12 : 13,
        color: '#614700',
        lineHeight: 1.55,
      }}
    >
      <strong style={{ display: 'block', marginBottom: compact ? 4 : 8 }}>{title}</strong>
      <p style={{ margin: 0 }}>
        需要 Agent 在 onto-agent 启用 Langfuse，且与 eval 注册表匹配（Agent 管理 → 从 Agent 同步 → 保存）。
      </p>
      {!compact && (
        <ul style={{ margin: '8px 0 0', paddingLeft: 18 }}>
          {LANGFUSE_TRACE_FEATURES.map((f) => (
            <li key={f}>{f}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
