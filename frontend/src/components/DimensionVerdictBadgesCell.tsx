import {
  type DimensionVerdictBadgesPayload,
  type PerformanceStabilityStatus,
  type VerdictStatus,
  performanceStatusColor,
  performanceStatusLabel,
  verdictStatusColor,
  verdictStatusLabel,
} from '../types/dimensionVerdict'

/**
 * 四维评估结论徽章（历史列表极简密度）。
 *
 * 数据来自后端 compact_dimension_verdict_badges：
 *   { quality, process_consistency, scenario_delivery, runtime_performance }
 *
 * 与详情页 DimensionVerdictBar 同源同阈值，颜色口径一致。
 * 未勾选维度显示 disabled（灰）；未完成任务（payload 为 null）显示 —。
 */

interface BadgeSpec {
  /** 维度简称（1 字，用于极简密度） */
  short: string
  /** 维度全称（tooltip 用） */
  full: string
  /** 状态值 */
  status: string
  /** 状态色（verdictStatusColor / performanceStatusColor 返回值） */
  color: string
  /** 状态中文（verdictStatusLabel / performanceStatusLabel 返回值） */
  label: string
  /** 跳详情 Tab 的 tabId */
  tabId: 'quality' | 'consistency' | 'scenario' | 'perf'
  /** 是否额外标注「独立维度，不计入质量总分」（仅场景） */
  independent?: boolean
}

interface Props {
  badges?: DimensionVerdictBadgesPayload | null
  /** 点击徽章跳转详情对应 Tab */
  onNavigate?: (tabId: 'quality' | 'consistency' | 'scenario' | 'perf') => void
}

function isPerformanceStatus(s: string): s is PerformanceStabilityStatus {
  return ['within_target', 'elevated', 'severe', 'na', 'disabled'].includes(s)
}

function isVerdictStatus(s: string): s is VerdictStatus {
  return ['pass', 'warn', 'fail', 'na', 'disabled'].includes(s)
}

export function DimensionVerdictBadgesCell({ badges, onNavigate }: Props) {
  if (!badges) {
    return <span className="ucd-muted">—</span>
  }

  const specs: BadgeSpec[] = [
    {
      short: '质',
      full: '质量指标',
      status: badges.quality,
      color: isVerdictStatus(badges.quality) ? verdictStatusColor(badges.quality) : '#888',
      label: isVerdictStatus(badges.quality) ? verdictStatusLabel(badges.quality) : badges.quality,
      tabId: 'quality',
    },
    {
      short: '过',
      full: '过程一致性',
      status: badges.process_consistency,
      color: isVerdictStatus(badges.process_consistency)
        ? verdictStatusColor(badges.process_consistency)
        : '#888',
      label: isVerdictStatus(badges.process_consistency)
        ? verdictStatusLabel(badges.process_consistency)
        : badges.process_consistency,
      tabId: 'consistency',
    },
    {
      short: '稳',
      full: '运行稳定性',
      status: badges.runtime_performance,
      color: isPerformanceStatus(badges.runtime_performance)
        ? performanceStatusColor(badges.runtime_performance)
        : '#888',
      label: isPerformanceStatus(badges.runtime_performance)
        ? performanceStatusLabel(badges.runtime_performance)
        : badges.runtime_performance,
      tabId: 'perf',
    },
    {
      short: '场',
      full: '场景交付',
      status: badges.scenario_delivery,
      color: isVerdictStatus(badges.scenario_delivery)
        ? verdictStatusColor(badges.scenario_delivery)
        : '#888',
      label: isVerdictStatus(badges.scenario_delivery)
        ? verdictStatusLabel(badges.scenario_delivery)
        : badges.scenario_delivery,
      tabId: 'scenario',
      independent: true,
    },
  ]

  const tooltipText = specs
    .map(s => {
      const base = `${s.full}：${s.label}`
      return s.independent ? `${base}（独立维度，不计入质量总分）` : base
    })
    .join('\n')

  return (
    <span
      className="dv-badges"
      title={tooltipText}
      style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--edp-space-1)' }}
    >
      {specs.map(spec => {
        const clickable = Boolean(onNavigate) && spec.status !== 'disabled' && spec.status !== 'na'
        return (
          <button
            key={spec.short}
            type="button"
            className={`dv-badge${clickable ? ' dv-badge--clickable' : ''}`}
            onClick={() => clickable && onNavigate?.(spec.tabId)}
            disabled={!clickable}
            title={`${spec.full}：${spec.label}${spec.independent ? '（独立维度，不计入质量总分）' : ''}`}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 3,
              padding: '2px 5px',
              borderRadius: 'var(--edp-radius-sm)',
              border: 'none',
              background: 'transparent',
              cursor: clickable ? 'pointer' : 'default',
              fontSize: 'var(--edp-font-sm)',
              color: 'var(--edp-text2)',
            }}
          >
            <span
              className="dv-badge__dot"
              style={{
                display: 'inline-block',
                width: 8,
                height: 8,
                borderRadius: '50%',
                background: spec.color,
                flexShrink: 0,
              }}
            />
            <span className="dv-badge__label">{spec.short}</span>
          </button>
        )
      })}
    </span>
  )
}
