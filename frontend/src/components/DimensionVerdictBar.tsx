import {
  type DimensionTabId,
  type DimensionVerdictsPayload,
  performanceStatusColor,
  performanceStatusLabel,
  truncateHeadline,
  verdictStatusColor,
  verdictStatusLabel,
} from '../types/dimensionVerdict'

export type DimensionNavigateOptions = {
  perfView?: 'stability' | 'task' | 'run'
}

interface CardSpec {
  key: string
  title: string
  tabId: DimensionTabId
  badge: string
  badgeColor: string
  subtitle: string
  scoreLine?: string | null
  disabled?: boolean
  navigateOptions?: DimensionNavigateOptions
}

interface Props {
  data: DimensionVerdictsPayload
  onNavigateTab?: (tabId: DimensionTabId, options?: DimensionNavigateOptions) => void
  /** 隐藏不可跳转的维度（如无过程一致性 Tab） */
  availableTabs?: Set<DimensionTabId>
}

function formatScore(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—'
  return value.toFixed(2)
}

function buildCards(data: DimensionVerdictsPayload): CardSpec[] {
  const { quality, process_consistency, scenario_delivery, runtime_stability } = data
  const rs = runtime_stability

  const runtimeBadge = performanceStatusLabel(rs.performance.status)

  const runtimeColor = performanceStatusColor(rs.performance.status)

  return [
    {
      key: 'quality',
      title: '质量指标',
      tabId: 'quality',
      badge: verdictStatusLabel(quality.status),
      badgeColor: verdictStatusColor(quality.status),
      subtitle: truncateHeadline(quality.headline),
      scoreLine: quality.score != null ? `结果分 ${formatScore(quality.score)}` : null,
    },
    {
      key: 'consistency',
      title: '过程一致性',
      tabId: 'consistency',
      badge: verdictStatusLabel(process_consistency.status),
      badgeColor: verdictStatusColor(process_consistency.status),
      subtitle: truncateHeadline(process_consistency.headline),
      scoreLine:
        process_consistency.score != null
          ? `过程分 ${formatScore(process_consistency.score)}`
          : null,
    },
    {
      key: 'runtime',
      title: '运行稳定性',
      tabId: 'perf',
      badge: runtimeBadge,
      badgeColor: runtimeColor,
      subtitle: truncateHeadline(rs.headline, 72),
      scoreLine: (() => {
        const qs = rs.quality_stability
        if (!qs || qs.status === 'na') return null
        const pr = qs.metrics?.pass_rate
        if (pr == null) return null
        const pt = qs.metrics?.pass_total ?? 0
        const jt = qs.metrics?.judge_total ?? 0
        return `多轮正确率 ${(pr * 100).toFixed(0)}%（${pt}/${jt}）`
      })(),
      navigateOptions:
        data.inference_rounds > 1 ? { perfView: 'stability' } : { perfView: 'task' },
    },
    {
      key: 'scenario',
      title: '场景交付',
      tabId: 'scenario',
      badge: verdictStatusLabel(scenario_delivery.status),
      badgeColor: verdictStatusColor(scenario_delivery.status),
      subtitle: truncateHeadline(scenario_delivery.headline),
      scoreLine:
        scenario_delivery.score != null
          ? `综合 ${formatScore(scenario_delivery.score)}`
          : null,
    },
  ]
}

export function DimensionVerdictBar({ data, onNavigateTab, availableTabs }: Props) {
  const cards = buildCards(data).map(card => ({
    ...card,
    disabled: availableTabs ? !availableTabs.has(card.tabId) : false,
  }))

  return (
    <div style={{ marginBottom: 16 }}>
      <h3 style={{ margin: '0 0 4px 0', fontSize: 15, fontWeight: 600 }}>评测维度结论</h3>
      <p style={{ margin: '0 0 12px', fontSize: 12, color: '#888' }}>
        各维度一句话结论；点击卡片跳转对应 Tab 下钻明细
      </p>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
          gap: 12,
        }}
      >
        {cards.map(card => {
          const clickable = Boolean(onNavigateTab) && !card.disabled
          return (
            <button
              key={card.key}
              type="button"
              disabled={!clickable}
              onClick={() => {
                if (clickable && onNavigateTab) {
                  onNavigateTab(card.tabId, card.navigateOptions)
                }
              }}
              style={{
                textAlign: 'left',
                padding: '12px 14px',
                borderRadius: 10,
                border: '1px solid #e0e4ef',
                background: card.disabled ? '#f5f5f5' : '#fff',
                cursor: clickable ? 'pointer' : 'default',
                opacity: card.disabled ? 0.72 : 1,
                boxShadow: clickable ? '0 1px 2px rgba(0,0,0,0.04)' : undefined,
              }}
            >
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'flex-start',
                  gap: 8,
                  marginBottom: 6,
                }}
              >
                <span style={{ fontSize: 13, fontWeight: 600, color: '#333' }}>{card.title}</span>
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: 600,
                    padding: '2px 8px',
                    borderRadius: 4,
                    color: '#fff',
                    background: card.badgeColor,
                    whiteSpace: 'nowrap',
                    flexShrink: 0,
                  }}
                >
                  {card.badge}
                </span>
              </div>
              {card.scoreLine && (
                <div style={{ fontSize: 12, fontWeight: 600, color: '#667eea', marginBottom: 4 }}>
                  {card.scoreLine}
                </div>
              )}
              <div style={{ fontSize: 12, color: '#555', lineHeight: 1.45 }}>{card.subtitle}</div>
              {clickable && (
                <div style={{ marginTop: 8, fontSize: 11, color: '#667eea' }}>查看详情 →</div>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}
