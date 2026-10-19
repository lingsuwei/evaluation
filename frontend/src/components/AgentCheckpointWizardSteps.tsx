import { type ReactNode } from 'react'

const stepBadge = (done: boolean) => ({
  width: 26,
  height: 26,
  borderRadius: '50%',
  display: 'inline-flex' as const,
  alignItems: 'center' as const,
  justifyContent: 'center' as const,
  fontSize: 13,
  fontWeight: 600 as const,
  flexShrink: 0,
  background: done ? '#e8f5e9' : '#eef1ff',
  color: done ? '#2e7d32' : '#5c6bc0',
  border: done ? '1px solid #a5d6a7' : '1px solid #c5cae9',
})

export function WizardStep({
  step,
  title,
  done,
  hint,
  disabled,
  children,
}: {
  step: number
  title: string
  done?: boolean
  hint?: string
  disabled?: boolean
  children?: ReactNode
}) {
  const inactive = Boolean(disabled)
  return (
    <section
      style={{
        marginTop: step === 1 ? 0 : 14,
        padding: '14px 16px 16px',
        borderRadius: 8,
        border: '1px solid #e4e7f0',
        borderLeft: `4px solid ${done ? '#66bb6a' : inactive ? '#ddd' : '#7986cb'}`,
        background: inactive ? '#fafafa' : '#fff',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: hint || children ? 10 : 0 }}>
        <span style={stepBadge(Boolean(done))}>{done ? '✓' : step}</span>
        <h4 style={{ margin: 0, fontSize: 15, fontWeight: 600, color: inactive ? '#888' : '#333' }}>
          {title}
        </h4>
        {done && (
          <span style={{ fontSize: 12, color: '#2e7d32', marginLeft: 'auto' }}>已完成</span>
        )}
      </div>
      {hint && (
        <p style={{ margin: '0 0 12px', fontSize: 13, color: '#666', lineHeight: 1.65 }}>{hint}</p>
      )}
      {!inactive && children}
      {inactive && (
        <p style={{ margin: 0, fontSize: 13, color: '#999' }}>请先完成上一步。</p>
      )}
    </section>
  )
}

export function WizardStepper({
  steps,
}: {
  steps: { label: string; done: boolean }[]
}) {
  return (
    <div
      style={{
        display: 'flex',
        flexWrap: 'wrap',
        gap: 6,
        marginTop: 14,
        padding: '10px 12px',
        borderRadius: 8,
        background: '#f8f9fe',
        border: '1px solid #e8eaf5',
      }}
    >
      {steps.map((s, idx) => (
        <div
          key={s.label}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            fontSize: 12,
            color: s.done ? '#2e7d32' : '#666',
          }}
        >
          <span style={stepBadge(s.done)}>{s.done ? '✓' : idx + 1}</span>
          <span>{s.label}</span>
          {idx < steps.length - 1 && <span style={{ color: '#ccc', margin: '0 2px' }}>›</span>}
        </div>
      ))}
    </div>
  )
}
