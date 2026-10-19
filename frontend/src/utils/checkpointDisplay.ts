/** 过程点合规展示：区分必达合规 vs 可选命中，避免「未命中 + 通过」歧义 */

export type CheckpointOutcome =
  | 'required_hit'
  | 'required_miss'
  | 'optional_hit'
  | 'optional_skip'

export interface CheckpointItemLike {
  required?: boolean | null
  hit?: boolean | null
  passed?: boolean
  outcome?: CheckpointOutcome | null
}

export interface CheckpointRunSummary {
  required_total: number
  required_passed: number
  required_compliance_rate: number | null
  optional_total: number
  optional_hit: number
  optional_hit_rate: number | null
}

export const CHECKPOINT_COMPLIANCE_LEGEND =
  '必达过程点未命中视为违规；可选过程点未命中仅表示本次未执行，不计入必达合规。'

export function resolveCheckpointOutcome(item: CheckpointItemLike): CheckpointOutcome {
  if (item.outcome) return item.outcome
  const required = Boolean(item.required)
  const hit = Boolean(item.hit)
  const passed = Boolean(item.passed)
  if (required) return passed ? 'required_hit' : 'required_miss'
  return hit ? 'optional_hit' : 'optional_skip'
}

export function checkpointTypeLabel(required?: boolean | null): string {
  return required ? '必达' : '可选'
}

export function checkpointOutcomeLabel(outcome: CheckpointOutcome): string {
  switch (outcome) {
    case 'required_hit':
      return '必达合规'
    case 'required_miss':
      return '必达缺失'
    case 'optional_hit':
      return '可选 · 已执行'
    case 'optional_skip':
      return '可选 · 未执行'
    default:
      return '—'
  }
}

export function checkpointOutcomeColor(outcome: CheckpointOutcome): string {
  switch (outcome) {
    case 'required_hit':
      return '#155724'
    case 'required_miss':
      return '#721c24'
    case 'optional_hit':
      return '#0d47a1'
    case 'optional_skip':
      return '#6c757d'
    default:
      return '#333'
  }
}

export function checkpointRowBackground(outcome: CheckpointOutcome): string | undefined {
  if (outcome === 'optional_skip') return '#f8f9fa'
  if (outcome === 'required_miss') return '#fff5f5'
  return undefined
}

export function summarizeCheckpointItems(items: CheckpointItemLike[]): CheckpointRunSummary {
  let required_total = 0
  let required_passed = 0
  let optional_total = 0
  let optional_hit = 0
  for (const item of items) {
    if (item.required) {
      required_total += 1
      if (item.passed) required_passed += 1
    } else {
      optional_total += 1
      if (item.hit) optional_hit += 1
    }
  }
  return {
    required_total,
    required_passed,
    required_compliance_rate:
      required_total > 0 ? required_passed / required_total : null,
    optional_total,
    optional_hit,
    optional_hit_rate: optional_total > 0 ? optional_hit / optional_total : null,
  }
}

export function formatCheckpointRate(rate: number | null | undefined, digits = 0): string {
  if (rate == null || Number.isNaN(rate)) return '—'
  return `${(rate * 100).toFixed(digits)}%`
}

export function formatRequiredComplianceSummary(
  passed: number,
  total: number,
  rate?: number | null,
): string {
  const pct = formatCheckpointRate(rate ?? (total > 0 ? passed / total : null))
  return `必达合规 ${passed}/${total}（${pct}）`
}

export function formatOptionalHitSummary(hit: number, total: number): string {
  if (total <= 0) return '可选命中 —'
  return `可选命中 ${hit}/${total}`
}
