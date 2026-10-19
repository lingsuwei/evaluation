import { useEffect, useState } from 'react'
import { formatDuration } from '../utils/dateUtils'

export interface StepItem {
  index?: number
  step_key?: string
  kind?: string
  label?: string
  latency_ms?: number | null
  tokens?: number | null
}

export interface StepSideMeta {
  run_index?: number
  trace_id?: string | null
  llm_call_count?: number
  tool_call_count?: number
  step_count?: number
  steps?: StepItem[]
}

export interface AlignedStepRow {
  index?: number
  step_key: string
  status: string
  left_index?: number | null
  right_index?: number | null
  left?: StepItem | null
  right?: StepItem | null
  delta_latency_ms?: number | null
  delta_tokens?: number | null
}

const DIFF_CHIP: Record<string, { bg: string; color: string; label: string }> = {
  removed: { bg: '#f8d7da', color: '#721c24', label: '省略' },
  added: { bg: '#d4edda', color: '#155724', label: '新增' },
  changed: { bg: '#fff3cd', color: '#856404', label: '变化' },
  order: { bg: '#ffe0b2', color: '#e65100', label: '顺序变' },
  same: { bg: '#e8eaf0', color: '#666', label: '一致' },
}

function fmtStepMeta(step: StepItem) {
  const kind = step.kind === 'tool' ? 'tool' : step.kind === 'llm' ? 'llm' : step.kind ?? ''
  const parts = [kind, step.label].filter(Boolean) as string[]
  if (step.latency_ms != null) parts.push(formatDuration(step.latency_ms))
  if (step.tokens != null) parts.push(`${step.tokens.toLocaleString()} tok`)
  return parts.join(' · ')
}

function fmtDeltaMs(v?: number | null) {
  if (v == null) return '—'
  return `${v > 0 ? '+' : ''}${v}ms`
}

function fmtDeltaTokens(v?: number | null) {
  if (v == null) return '—'
  return `${v > 0 ? '+' : ''}${v.toLocaleString()}`
}

export function StepSequenceDiffView({
  alignedSteps,
  leftLabel,
  rightLabel,
  leftMeta,
  rightMeta,
  langfuseHost,
  leftTraceLabel = '打开基线 Trace',
  rightTraceLabel = '打开对比 Trace',
}: {
  alignedSteps: AlignedStepRow[]
  leftLabel: string
  rightLabel: string
  leftMeta: StepSideMeta
  rightMeta: StepSideMeta
  langfuseHost: string
  leftTraceLabel?: string
  rightTraceLabel?: string
}) {
  const lSteps = leftMeta.step_count ?? leftMeta.steps?.length ?? 0
  const rSteps = rightMeta.step_count ?? rightMeta.steps?.length ?? 0
  const stepDelta = rSteps - lSteps
  const llmDelta =
    (rightMeta.llm_call_count ?? 0) - (leftMeta.llm_call_count ?? 0)
  const toolDelta =
    (rightMeta.tool_call_count ?? 0) - (leftMeta.tool_call_count ?? 0)

  const diffCount = alignedSteps.filter(r => r.status !== 'same').length
  const shouldDefaultExpand = alignedSteps.length > 0 && diffCount === 0
  const [showAll, setShowAll] = useState(shouldDefaultExpand)

  useEffect(() => {
    setShowAll(shouldDefaultExpand)
  }, [alignedSteps, shouldDefaultExpand])

  const visibleRows = showAll
    ? alignedSteps
    : alignedSteps.filter(r => r.status !== 'same')

  const summaryChips: string[] = []
  if (stepDelta !== 0 || lSteps > 0 || rSteps > 0) {
    summaryChips.push(
      stepDelta !== 0
        ? `总步数 ${lSteps} → ${rSteps}（${stepDelta > 0 ? '+' : ''}${stepDelta}）`
        : `总步数 ${lSteps}`,
    )
  }
  if (llmDelta !== 0 || toolDelta !== 0) {
    summaryChips.push(`调用次数：LLM ${llmDelta > 0 ? '+' : ''}${llmDelta}，工具 ${toolDelta > 0 ? '+' : ''}${toolDelta}`)
  }
  if (shouldDefaultExpand) {
    summaryChips.push('结构一致')
  }

  const traceUrl = (traceId?: string | null) => {
    if (!traceId || !langfuseHost) return null
    return `${langfuseHost}/trace/${traceId}`
  }
  const leftTraceUrl = traceUrl(leftMeta.trace_id)
  const rightTraceUrl = traceUrl(rightMeta.trace_id)

  return (
    <div className="step-diff">
      <div className="step-diff__head">
        <div className="step-diff__titlebar">
          <h4 className="step-diff__title">Step 序列对比</h4>
          {(leftTraceUrl || rightTraceUrl) && (
            <div className="step-diff__trace-links">
              {leftTraceUrl && (
                <a href={leftTraceUrl} target="_blank" rel="noreferrer">
                  {leftTraceLabel}
                </a>
              )}
              {rightTraceUrl && (
                <a href={rightTraceUrl} target="_blank" rel="noreferrer">
                  {rightTraceLabel}
                </a>
              )}
            </div>
          )}
        </div>
        {alignedSteps.length > 0 && (
          <button
            type="button"
            className="step-diff__toggle"
            onClick={() => setShowAll(v => !v)}
          >
            {showAll
              ? diffCount > 0
                ? '仅看差异'
                : '收起完整步骤'
              : diffCount > 0
                ? `展开全部步骤（${alignedSteps.length}）`
                : `查看全部步骤（${alignedSteps.length}）`}
          </button>
        )}
      </div>

      {summaryChips.length > 0 && (
        <div className="step-diff__summary">
          {summaryChips.map(chip => (
            <span key={chip} className="step-diff__chip">{chip}</span>
          ))}
        </div>
      )}

      {diffCount > 0 && (
        <div className="step-diff__legend">
          {(['removed', 'added', 'order', 'changed'] as const).map(key => (
            <span
              key={key}
              className={`step-diff__badge step-diff__badge--${key}`}
            >
              {DIFF_CHIP[key].label}
            </span>
          ))}
        </div>
      )}

      {alignedSteps.length === 0 ? (
        <p className="step-diff__empty-state">暂无 Step 数据，请确认 Trace 已同步。</p>
      ) : visibleRows.length === 0 ? (
        <p className="step-diff__empty-state">
          各步骤结构一致；可点击「查看全部步骤」查看完整 Step 序列。
        </p>
      ) : (
        <div className="step-diff__scroll">
          <div className="step-diff__columns">
            <div>{leftLabel}</div>
            <div />
            <div>{rightLabel}</div>
          </div>
          {visibleRows.map(row => {
            const st = DIFF_CHIP[row.status] ?? DIFF_CHIP.same
            const badgeClass =
              row.status === 'removed' ||
              row.status === 'added' ||
              row.status === 'order' ||
              row.status === 'changed'
                ? ` step-diff__badge--${row.status}`
                : ''
            const hasDelta =
              row.delta_latency_ms != null || row.delta_tokens != null
            return (
              <div
                key={`${row.index ?? 0}-${row.step_key}`}
                className={`step-diff__row step-diff__row--${row.status}`}
              >
                <div className="step-diff__side step-diff__side--left">
                  <span className="step-diff__lineno">{row.left_index ?? ''}</span>
                  <div className="step-diff__body">
                    {row.left ? (
                      <>
                        <code className="step-diff__key">{row.left.step_key ?? row.step_key}</code>
                        <span className="step-diff__meta">{fmtStepMeta(row.left)}</span>
                      </>
                    ) : (
                      <span className="step-diff__empty">—</span>
                    )}
                  </div>
                </div>
                <div className="step-diff__gutter">
                  {row.status !== 'same' && (
                    <span className={`step-diff__badge${badgeClass}`}>{st.label}</span>
                  )}
                </div>
                <div className="step-diff__side step-diff__side--right">
                  <span className="step-diff__lineno">{row.right_index ?? ''}</span>
                  <div className="step-diff__body">
                    {row.right ? (
                      <>
                        <code className="step-diff__key">{row.right.step_key ?? row.step_key}</code>
                        <span className="step-diff__meta">{fmtStepMeta(row.right)}</span>
                        {row.status === 'changed' && hasDelta && (
                          <div className="step-diff__delta">
                            {row.delta_latency_ms != null && (
                              <span>{fmtDeltaMs(row.delta_latency_ms)}</span>
                            )}
                            {row.delta_latency_ms != null && row.delta_tokens != null && ' · '}
                            {row.delta_tokens != null && (
                              <span>{fmtDeltaTokens(row.delta_tokens)} Tokens</span>
                            )}
                          </div>
                        )}
                        {row.status === 'order' &&
                          row.left_index != null &&
                          row.right_index != null && (
                          <div className="step-diff__delta">
                            基线 #{row.left_index} → 对比 #{row.right_index}
                          </div>
                        )}
                      </>
                    ) : (
                      <span className="step-diff__empty">—</span>
                    )}
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
