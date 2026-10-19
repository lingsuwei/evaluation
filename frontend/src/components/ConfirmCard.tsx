import { useState } from 'react'

/**
 * 确认卡片：渲染 chat_confirm_required 事件。
 * AI 调用写操作工具后弹此卡片，用户确认/取消后才执行。
 */

export interface PendingConfirm {
  toolName: string
  toolArgs: Record<string, any>
  round?: number
}

interface Props {
  pending: PendingConfirm
  sessionId: string
  onConfirmed: (result: any) => void
  onCancelled: () => void
  onError: (msg: string) => void
}

const TOOL_LABELS: Record<string, string> = {
  create_llm_model: '创建 LLM 模型',
  update_llm_model: '更新 LLM 模型',
  delete_llm_model: '删除 LLM 模型',
  create_agent: '注册 Agent',
  update_agent: '更新 Agent',
  delete_agent: '删除 Agent',
  create_eval_script: '创建评估脚本',
  delete_eval_script: '删除评估脚本',
  launch_eval: '启动评估',
}

// 关键参数优先展示顺序（提升可读性）
const PRIORITY_FIELDS: Record<string, string[]> = {
  create_llm_model: ['id', 'name', 'provider', 'base_url', 'model'],
  update_llm_model: ['id', 'name', 'provider', 'base_url', 'model'],
  delete_llm_model: ['id'],
  create_agent: ['name', 'url', 'endpoint', 'provider', 'model'],
  update_agent: ['name', 'url', 'endpoint'],
  delete_agent: ['agent_id'],
  create_eval_script: ['name', 'description'],
  delete_eval_script: ['script_id'],
}

export function ConfirmCard({ pending, sessionId, onConfirmed, onCancelled, onError }: Props) {
  const [submitting, setSubmitting] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const label = TOOL_LABELS[pending.toolName] || pending.toolName
  const priority = PRIORITY_FIELDS[pending.toolName] || []
  const args = pending.toolArgs || {}

  const renderArg = (key: string) => {
    const val = args[key]
    if (val == null) return null
    const display = typeof val === 'object' ? JSON.stringify(val, null, 2) : String(val)
    return (
      <div key={key} className="confirm-arg-row">
        <span className="confirm-arg-key">{key}</span>
        <span className="confirm-arg-val">{display}</span>
      </div>
    )
  }

  const shownKeys = priority.filter(k => args[k] != null)
  const otherKeys = Object.keys(args).filter(k => !priority.includes(k) && args[k] != null)

  const handleConfirm = async () => {
    setSubmitting(true)
    try {
      const res = await fetch(`/api/chat/sessions/${sessionId}/confirm`, { method: 'POST' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        onError(data.detail || `确认失败 (${res.status})`)
        return
      }
      onConfirmed(data)
    } catch (e: any) {
      onError(`确认请求失败: ${e.message || e}`)
    } finally {
      setSubmitting(false)
    }
  }

  const handleCancel = async () => {
    setSubmitting(true)
    try {
      const res = await fetch(`/api/chat/sessions/${sessionId}/cancel`, { method: 'POST' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        onError(data.detail || `取消失败 (${res.status})`)
        return
      }
      onCancelled()
    } catch (e: any) {
      onError(`取消请求失败: ${e.message || e}`)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="confirm-card">
      <div className="confirm-card-header">
        <span className="confirm-card-icon">⚠️</span>
        <span className="confirm-card-title">{label}</span>
        {pending.round != null && <span className="confirm-card-round">R{pending.round}</span>}
      </div>
      <div className="confirm-card-desc">AI 发起了写操作，请确认后执行：</div>
      <div className="confirm-card-args">
        {shownKeys.map(renderArg)}
        {otherKeys.length > 0 && (
          <div className="confirm-args-toggle" onClick={() => setExpanded(e => !e)}>
            {expanded ? '收起其他参数' : `查看其他 ${otherKeys.length} 个参数`}
          </div>
        )}
        {expanded && otherKeys.map(renderArg)}
      </div>
      <div className="confirm-card-actions">
        <button
          className="chat-btn chat-btn-primary"
          onClick={handleConfirm}
          disabled={submitting}
        >
          {submitting ? '处理中…' : '确认执行'}
        </button>
        <button
          className="chat-btn chat-btn-secondary"
          onClick={handleCancel}
          disabled={submitting}
        >
          取消
        </button>
      </div>
    </div>
  )
}
