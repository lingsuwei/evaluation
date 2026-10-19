/** 场景子步骤业务展示（隐藏内部 step_key，优先顺序 + 名称）。 */

export interface ScenarioStepDisplayFields {
  step_order?: number | null
  name?: string | null
  step_key?: string | null
}

/** 列表/矩阵主文案，如「3. 生成答案」。 */
export function formatScenarioStepLabel(
  step: ScenarioStepDisplayFields,
  displayOrder?: number,
): string {
  const name = (step.name || '').trim()
  const order =
    displayOrder ??
    (step.step_order != null && step.step_order > 0 ? step.step_order : null)
  if (order != null) {
    return name ? `${order}. ${name}` : `第 ${order} 步`
  }
  return name || step.step_key || '—'
}

/** 自动生成未使用的 S{n} step_key（仅创建时写库，不在界面展示）。 */
export function nextAutoStepKey(
  existing: Array<{ step_key: string }>,
): string {
  const used = new Set(existing.map((s) => s.step_key.trim()).filter(Boolean))
  let maxN = 0
  for (const s of existing) {
    const m = /^S(\d+)$/.exec(s.step_key.trim())
    if (m) maxN = Math.max(maxN, parseInt(m[1], 10))
  }
  let n = maxN + 1
  while (used.has(`S${n}`)) n += 1
  return `S${n}`
}
