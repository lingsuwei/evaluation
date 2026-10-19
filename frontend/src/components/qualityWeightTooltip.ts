/** 质量总分权重 tooltip 生成器：根据场景动态展示实际权重构成及退化原因。 */

export interface QualityWeightContext {
  result_weight?: number
  process_weight?: number
  stability_weight?: number
  /** 是否离线评估 */
  isOffline?: boolean
  /** 推理轮次 */
  inference_rounds?: number
  /** 过程一致性维度是否启用 */
  processConsistencyEnabled?: boolean
  /** 稳定性维度是否启用 */
  stabilityEnabled?: boolean
  /** 安全类别分（pass_rate 均分，不进 total） */
  safetyScore?: number | null
  /** 已触发的门禁列表（取首条最严的展示） */
  gates?: Array<{ type: string; applied?: boolean; cap?: number; reason?: string }>
}

/**
 * 生成质量总分权重 tooltip 文案。
 *
 * 场景化规则（按优先级叠加）：
 * - 基础：列出 >0 的权重项 → "结果60% · 过程20% · 稳定20%"
 * - 单轮（inference_rounds<=1）且稳定性权重为0：追加"单轮推理，稳定性权重并入结果分"
 * - 过程一致性未启用：追加"过程一致性未启用"
 * - 稳定性维度未启用：追加"稳定性维度未启用"
 * - 离线且无过程分：追加"离线评估"
 */
export function buildQualityWeightTooltip(ctx: QualityWeightContext): string {
  const rw = ctx.result_weight ?? 0
  const pw = ctx.process_weight ?? 0
  const sw = ctx.stability_weight ?? 0

  const parts: string[] = []
  if (rw > 0) parts.push(`结果${Math.round(rw * 100)}%`)
  if (pw > 0) parts.push(`过程${Math.round(pw * 100)}%`)
  if (sw > 0) parts.push(`稳定${Math.round(sw * 100)}%`)

  const weightStr = parts.length > 0 ? parts.join(' · ') : '—'

  const notes: string[] = []
  const isMultiRound = (ctx.inference_rounds ?? 1) > 1
  const processEnabled = ctx.processConsistencyEnabled ?? true
  const stabilityEnabled = ctx.stabilityEnabled ?? true

  if (ctx.isOffline && pw === 0) {
    notes.push('离线评估')
  }
  if (!isMultiRound && sw === 0 && stabilityEnabled) {
    notes.push('单轮推理，稳定性权重并入结果分')
  }
  if (!processEnabled) {
    notes.push('过程一致性未启用')
  }
  if (!stabilityEnabled) {
    notes.push('稳定性维度未启用')
  }
  if (ctx.safetyScore != null) {
    notes.push(`安全${ctx.safetyScore.toFixed(2)}`)
  }

  const appliedGates = (ctx.gates ?? []).filter(g => g.applied !== false)
  if (appliedGates.length > 0) {
    const g = appliedGates[0]
    const gateDesc = g.reason || `${g.type} 封顶 ${g.cap}`
    notes.push(`已触发门禁：${gateDesc}`)
  }

  const noteStr = notes.length > 0 ? `（${notes.join('；')}）` : ''
  return `${weightStr}${noteStr}`
}
