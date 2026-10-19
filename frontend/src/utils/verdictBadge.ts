// frontend/src/utils/verdictBadge.ts
import type { DimensionVerdictsPayload } from '../types/dimensionVerdict'

/** 分数着色：null→灰；≥0.8→绿；≥0.6→琥珀；<0.6→红 */
export function scoreColor(v: number | null): string {
  if (v == null) return 'var(--edp-text3)'
  return v >= 0.8 ? 'var(--edp-green)' : v >= 0.6 ? 'var(--edp-amber)' : 'var(--edp-red)'
}

/** 四维结论 status → 文字/颜色统一映射（达标绿·需关注琥珀·未达标红） */
export const VERDICT_BADGE: Record<string, { label: string; badgeCls: string; dotCls: string }> = {
  pass: { label: '达标', badgeCls: 'edp-b-pass', dotCls: 'edp-dot-pass' },
  warn: { label: '需关注', badgeCls: 'edp-b-warn', dotCls: 'edp-dot-warn' },
  fail: { label: '未达标', badgeCls: 'edp-b-fail', dotCls: 'edp-dot-fail' },
  na: { label: '—', badgeCls: 'edp-b-na', dotCls: 'edp-dot-na' },
  disabled: { label: '未启用', badgeCls: 'edp-b-na', dotCls: 'edp-dot-na' },
  descriptive: { label: '描述性', badgeCls: 'edp-b-na', dotCls: 'edp-dot-descriptive' },
}

/** 运行稳定性 performance.status → verdict status 映射 */
const PERF_TO_VERDICT: Record<string, string> = {
  within_target: 'pass',
  elevated: 'warn',
  severe: 'fail',
  na: 'na',
  disabled: 'disabled',
}

/**
 * 解析维度 data 对象，返回 badge。
 * 运行稳定性维度（nav === 'perf'）走 performance.status 映射，其它维度直接用 data.status。
 */
export function resolveVerdictBadge(
  nav: string,
  data: unknown,
): { label: string; badgeCls: string; dotCls: string } {
  if (nav === 'perf') {
    const rsData = data as DimensionVerdictsPayload['runtime_stability'] | undefined
    const perfStatus = (rsData?.performance?.status || 'na') as string
    const mapped = PERF_TO_VERDICT[perfStatus]
    if (mapped) return VERDICT_BADGE[mapped] || VERDICT_BADGE.na
  }
  const status = ((data as { status?: string } | null | undefined)?.status || 'na') as string
  return VERDICT_BADGE[status] || VERDICT_BADGE.na
}