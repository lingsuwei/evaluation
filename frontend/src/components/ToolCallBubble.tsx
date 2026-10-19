import { useState } from 'react'

/**
 * 工具调用气泡：渲染 chat_tool_call / chat_tool_result 事件。
 * 可展开查看参数和返回结果。
 */

export interface ToolCallLine {
  id: number
  type: string
  toolName?: string
  toolArgs?: Record<string, any>
  toolResult?: any
  round?: number
}

interface Props {
  line: ToolCallLine
}

const TOOL_LABELS: Record<string, string> = {
  list_datasets: '查询数据集',
  list_agents: '查询 Agent',
  list_llm_models: '查询 LLM 模型',
  list_metrics: '查询评测指标',
  get_dataset_preview: '预览数据集',
  recommend_metrics: '推荐指标',
  build_eval_request: '组装配置草稿',
}

export function ToolCallBubble({ line }: Props) {
  const [expanded, setExpanded] = useState(false)
  const label = TOOL_LABELS[line.toolName || ''] || line.toolName || '工具调用'

  return (
    <div className="chat-bubble chat-bubble-tool">
      <div className="chat-bubble-header" onClick={() => setExpanded(e => !e)}>
        <span className="chat-tool-icon">🔧</span>
        <span className="chat-tool-name">{label}</span>
        {line.round != null && <span className="chat-tool-round">R{line.round}</span>}
        <span className="chat-bubble-toggle">{expanded ? '收起' : '展开'}</span>
      </div>
      {expanded && (
        <div className="chat-bubble-detail">
          {line.toolArgs && Object.keys(line.toolArgs).length > 0 && (
            <div className="chat-tool-section">
              <div className="chat-tool-section-label">参数</div>
              <pre className="chat-tool-json">{JSON.stringify(
                Object.fromEntries(
                  Object.entries(line.toolArgs).filter(([k]) => !k.startsWith('_'))
                ),
                null, 2
              )}</pre>
            </div>
          )}
          {line.toolResult != null && (
            <div className="chat-tool-section">
              <div className="chat-tool-section-label">结果</div>
              <pre className="chat-tool-json">{JSON.stringify(line.toolResult, null, 2)}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
