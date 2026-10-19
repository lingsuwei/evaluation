import { LangfuseTraceBlockedPanel } from './LangfuseObservabilityNotice'
import { RunSpanTreeContent } from './RunSpanTreePanel'

interface Props {
  runId: string
  questionId: number
  runIndex?: number
  langfuseTraceReady?: boolean
}

export function SingleRunChainView({
  runId,
  questionId,
  runIndex = 0,
  langfuseTraceReady = true,
}: Props) {
  if (!langfuseTraceReady) {
    return <LangfuseTraceBlockedPanel compact title="推理链查看不可用" />
  }

  return (
    <div style={{ fontSize: 13 }}>
      <p style={{ margin: '0 0 12px', fontSize: 12, color: '#888' }}>
        本题第 {runIndex + 1} 轮 Run 的 Span 拓扑与过程点下钻（单轮查看，非多轮对比）
      </p>
      <div
        style={{
          maxHeight: 'min(72vh, 640px)',
          overflow: 'auto',
          border: '1px solid #e8eaf0',
          borderRadius: 8,
          padding: 14,
          background: '#fafbff',
        }}
      >
        <RunSpanTreeContent
          runId={runId}
          questionId={questionId}
          runIndex={runIndex}
          embedded
        />
      </div>
    </div>
  )
}
