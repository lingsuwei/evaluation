import type { CSSProperties } from 'react'

export const OFFLINE_NA_LABEL = '不涉及'

export const OFFLINE_NA_TOOLTIP = '离线评估使用评测集已有答案，不调用 Agent 推理'

export function isOfflineEvaluationTask(task: {
  evaluation_mode?: string | null
  agent_id?: string | null
}): boolean {
  return task.evaluation_mode === 'offline' || !task.agent_id
}

export const offlineNaTextStyle: CSSProperties = {
  color: '#999',
  fontStyle: 'italic',
}
