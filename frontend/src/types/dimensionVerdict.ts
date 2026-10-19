export type VerdictStatus = 'pass' | 'warn' | 'fail' | 'na' | 'disabled'

export type StructuralStabilityStatus =
  | 'consistent'
  | 'occasional_drift'
  | 'significant_drift'
  | 'na'
  | 'disabled'

export type PerformanceStabilityStatus =
  | 'within_target'
  | 'elevated'
  | 'severe'
  | 'na'
  | 'disabled'

export type DimensionTabId = 'quality' | 'consistency' | 'scenario' | 'perf'

export interface DimensionVerdictsPayload {
  task_id: string
  evaluation_mode: 'online' | 'offline'
  inference_rounds: number
  thresholds?: VerdictThresholdMeta
  quality: {
    status: VerdictStatus
    score: number | null
    headline: string
    tab_id: 'quality'
    metrics: Record<string, unknown>
  }
  process_consistency: {
    status: VerdictStatus
    score: number | null
    headline: string
    tab_id: 'consistency'
    metrics: Record<string, unknown>
  }
  scenario_delivery: {
    status: VerdictStatus
    score: number | null
    headline: string
    tab_id: 'scenario'
    metrics: Record<string, unknown>
  }
  runtime_stability: {
    status: 'descriptive'
    headline: string
    tab_id: 'perf'
    structural: {
      status: StructuralStabilityStatus
      headline: string
      metrics: Record<string, unknown>
    }
    performance: {
      status: PerformanceStabilityStatus
      headline: string
      metrics: Record<string, unknown>
    }
    quality_stability?: {
      status: VerdictStatus
      headline: string
      metrics: {
        pass_rate?: number | null
        avg_std?: number | null
        max_std?: number | null
        min_score?: number | null
        round_count?: number
        judge_total?: number
        pass_total?: number
      }
    }
  }
}

export interface VerdictThresholdEffective {
  quality_pass_rate_pass: number
  quality_pass_rate_warn: number
  quality_total_pass: number
  quality_total_warn: number
  process_chain_pass_warn: number
  scenario_process_pass: number
  scenario_process_warn: number
  scenario_required_pass: number
  scenario_required_warn: number
  scenario_score_std_volatile: number
  struct_drift_ratio_significant: number
  perf_pass_rate_within: number
  perf_pass_rate_elevated: number
}

export interface VerdictThresholdMeta {
  effective: VerdictThresholdEffective
  defaults: VerdictThresholdEffective
  source: 'system_default' | 'task_custom'
  custom_keys: string[]
}

export interface DimensionVerdictBadgesPayload {
  quality: VerdictStatus | string
  process_consistency: VerdictStatus | string
  scenario_delivery: VerdictStatus | string
  runtime_performance: PerformanceStabilityStatus | string
}

const VERDICT_STATUS_LABEL: Record<VerdictStatus, string> = {
  pass: '达标',
  warn: '需关注',
  fail: '未达标',
  na: '不适用',
  disabled: '未启用',
}

const STRUCTURAL_LABEL: Record<StructuralStabilityStatus, string> = {
  consistent: '结构一致',
  occasional_drift: '偶发波动',
  significant_drift: '明显波动',
  na: '不适用',
  disabled: '未启用',
}

const PERFORMANCE_LABEL: Record<PerformanceStabilityStatus, string> = {
  within_target: '达标',
  elevated: '需关注',
  severe: '未达标',
  na: '—',
  disabled: '未启用',
}

export function verdictStatusColor(status: VerdictStatus): string {
  switch (status) {
    case 'pass':
      return '#22a06b'
    case 'warn':
      return '#e6a817'
    case 'fail':
      return '#dc3545'
    case 'disabled':
      return '#9099b0'
    default:
      return '#9099b0'
  }
}

export function structuralStatusColor(status: StructuralStabilityStatus): string {
  switch (status) {
    case 'consistent':
      return '#1565c0'
    case 'occasional_drift':
      return '#e6a817'
    case 'significant_drift':
      return '#e65100'
    case 'disabled':
      return '#9e9e9e'
    default:
      return '#888'
  }
}

export function performanceStatusColor(status: PerformanceStabilityStatus): string {
  switch (status) {
    case 'within_target':
      return '#22a06b'
    case 'elevated':
      return '#e6a817'
    case 'severe':
      return '#dc3545'
    case 'disabled':
      return '#9099b0'
    default:
      return '#9099b0'
  }
}

export function verdictStatusLabel(status: VerdictStatus): string {
  return VERDICT_STATUS_LABEL[status] ?? status
}

export function structuralStatusLabel(status: StructuralStabilityStatus): string {
  return STRUCTURAL_LABEL[status] ?? status
}

export function performanceStatusLabel(status: PerformanceStabilityStatus): string {
  return PERFORMANCE_LABEL[status] ?? status
}

export function truncateHeadline(text: string, maxLen = 56): string {
  const t = (text || '').trim()
  if (t.length <= maxLen) return t
  return `${t.slice(0, maxLen - 1)}…`
}
