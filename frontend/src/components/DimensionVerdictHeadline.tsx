import {
  performanceStatusColor,
  performanceStatusLabel,
  type PerformanceStabilityStatus,
  type VerdictStatus,
  verdictStatusColor,
  verdictStatusLabel,
} from '../types/dimensionVerdict'

interface VerdictBannerProps {
  title: string
  status: VerdictStatus
  headline: string
  scoreLine?: string | null
}

export function DimensionVerdictHeadline({
  title,
  status,
  headline,
  scoreLine,
}: VerdictBannerProps) {
  return (
    <div
      style={{
        marginBottom: 14,
        padding: '12px 14px',
        borderRadius: 8,
        border: '1px solid #e0e4ef',
        background: '#f8f9fc',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          marginBottom: headline || scoreLine ? 8 : 0,
          flexWrap: 'wrap',
        }}
      >
        <strong style={{ fontSize: 13, color: '#333' }}>{title}</strong>
        <span
          style={{
            fontSize: 11,
            fontWeight: 600,
            padding: '2px 8px',
            borderRadius: 4,
            color: '#fff',
            background: verdictStatusColor(status),
          }}
        >
          {verdictStatusLabel(status)}
        </span>
        {scoreLine && (
          <span style={{ fontSize: 12, fontWeight: 600, color: '#667eea' }}>{scoreLine}</span>
        )}
      </div>
      {headline && (
        <p style={{ margin: 0, fontSize: 13, color: '#444', lineHeight: 1.55 }}>{headline}</p>
      )}
    </div>
  )
}

interface RuntimeStabilityHeadlineProps {
  mergedHeadline?: string
  performance: {
    status: PerformanceStabilityStatus
    headline: string
  }
}

export function RuntimeStabilityHeadline({
  mergedHeadline,
  performance,
}: RuntimeStabilityHeadlineProps) {
  return (
    <div
      style={{
        marginBottom: 16,
        padding: '12px 14px',
        borderRadius: 8,
        border: '1px solid #e0e4ef',
        background: '#f8f9fc',
      }}
    >
      <strong style={{ display: 'block', fontSize: 13, color: '#333', marginBottom: 10 }}>
        运行稳定性结论
      </strong>
      {mergedHeadline && (
        <p style={{ margin: '0 0 10px', fontSize: 12, color: '#555', lineHeight: 1.5 }}>
          {mergedHeadline}
        </p>
      )}
      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 13 }}>
        <span
          style={{
            flexShrink: 0,
            fontSize: 11,
            fontWeight: 600,
            padding: '2px 8px',
            borderRadius: 4,
            color: '#fff',
            background: performanceStatusColor(performance.status),
          }}
        >
          {performanceStatusLabel(performance.status)}
        </span>
        <span style={{ color: '#444', lineHeight: 1.5 }}>
          <strong style={{ color: '#444' }}>性能 · </strong>
          {performance.headline}
        </span>
      </div>
    </div>
  )
}
