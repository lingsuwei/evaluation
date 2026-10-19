import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import {
  CHECKPOINT_REQUIRED,
  ENFORCE_ORDER_CHECKPOINT,
  ENFORCE_ORDER_SCENARIO,
  HISTORICAL_PERCENTILE,
  TASK_PERCENTILE,
} from './helpCopy'

export function HelpTip({
  label,
  tip,
  align = 'center',
  ariaLabel,
}: {
  label: string
  tip: ReactNode
  align?: 'left' | 'center' | 'right'
  ariaLabel?: string
}) {
  const tipId = useId()
  const [visible, setVisible] = useState(false)
  const [pinned, setPinned] = useState(false)
  const [position, setPosition] = useState({ top: 0, left: 0 })
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const triggerRef = useRef<HTMLSpanElement>(null)
  const popoverRef = useRef<HTMLDivElement>(null)

  const clearDelay = useCallback(() => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current)
      timeoutRef.current = null
    }
  }, [])

  const updatePosition = useCallback(() => {
    if (!triggerRef.current || !popoverRef.current) return

    const rect = triggerRef.current.getBoundingClientRect()
    const popoverWidth = popoverRef.current.offsetWidth
    const popoverHeight = popoverRef.current.offsetHeight
    const gap = 8
    const margin = 10

    let left = rect.left + rect.width / 2 - popoverWidth / 2
    let top = rect.bottom + gap

    if (left + popoverWidth > window.innerWidth - margin) {
      left = window.innerWidth - popoverWidth - margin
    }
    if (left < margin) left = margin

    if (top + popoverHeight > window.innerHeight - margin) {
      top = rect.top - popoverHeight - gap
    }
    if (top < margin) top = margin

    setPosition({ top, left })
  }, [])

  const show = useCallback(() => {
    clearDelay()
    timeoutRef.current = setTimeout(() => setVisible(true), 200)
  }, [clearDelay])

  const hide = useCallback(() => {
    clearDelay()
    if (!pinned) setVisible(false)
  }, [clearDelay, pinned])

  const togglePin = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault()
      e.stopPropagation()
      clearDelay()
      setPinned((prev) => {
        const next = !prev
        setVisible(next)
        return next
      })
    },
    [clearDelay],
  )

  useEffect(() => {
    if (visible) updatePosition()
  }, [visible, updatePosition])

  useEffect(() => {
    if (!pinned) return
    const onDocClick = (e: MouseEvent) => {
      const target = e.target as Node
      if (triggerRef.current?.contains(target) || popoverRef.current?.contains(target)) return
      setPinned(false)
      setVisible(false)
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [pinned])

  useEffect(() => () => clearDelay(), [clearDelay])

  const justify =
    align === 'left' ? 'flex-start' : align === 'right' ? 'flex-end' : 'center'

  return (
    <span
      ref={triggerRef}
      className="help-tip"
      style={{
        justifyContent: justify,
      }}
      onMouseEnter={show}
      onMouseLeave={hide}
      aria-describedby={visible ? tipId : undefined}
    >
      <span className="help-tip__label">{label}</span>
      <button
        type="button"
        className="help-tip__icon"
        aria-label={ariaLabel || `${label}说明`}
        onClick={togglePin}
        onMouseDown={(e) => e.preventDefault()}
      >
        i
      </button>
      {visible && (
        <div
          ref={popoverRef}
          id={tipId}
          role="tooltip"
          className="ucd-help-popover"
          style={{
            position: 'fixed',
            top: position.top,
            left: position.left,
            zIndex: 10000,
          }}
          onMouseEnter={() => {
            clearDelay()
            setVisible(true)
          }}
          onMouseLeave={hide}
        >
          {tip}
        </div>
      )}
    </span>
  )
}

export function EnforceOrderTipBody({
  variant,
}: {
  variant: 'checkpoint' | 'scenario'
}) {
  const copy =
    variant === 'checkpoint' ? ENFORCE_ORDER_CHECKPOINT : ENFORCE_ORDER_SCENARIO
  return (
    <>
      <div className="ucd-tip-title">{copy.title}</div>
      <div>{copy.body}</div>
    </>
  )
}

export function CheckpointRequiredTipBody() {
  return (
    <>
      <div className="ucd-tip-title">{CHECKPOINT_REQUIRED.title}</div>
      <div>{CHECKPOINT_REQUIRED.body}</div>
    </>
  )
}

export function HistoricalPercentileTipBody({ showFootnote = false }: { showFootnote?: boolean }) {
  return (
    <>
      <div className="ucd-tip-title">
        {HISTORICAL_PERCENTILE.title}
      </div>
      <div>{HISTORICAL_PERCENTILE.intro}</div>
      <div style={{ marginTop: 4 }}>{HISTORICAL_PERCENTILE.p75}</div>
      <div>{HISTORICAL_PERCENTILE.p90}</div>
      {showFootnote && (
        <div className="ucd-tip-footnote">
          {HISTORICAL_PERCENTILE.checkpointFootnote}
        </div>
      )}
    </>
  )
}

export function TaskPercentileTipBody() {
  return (
    <>
      <div className="ucd-tip-title">
        {TASK_PERCENTILE.title}
      </div>
      <div>{TASK_PERCENTILE.intro}</div>
      <div style={{ marginTop: 4 }}>{TASK_PERCENTILE.p75}</div>
      <div>{TASK_PERCENTILE.p90}</div>
      <div className="ucd-tip-footnote">
        {TASK_PERCENTILE.footnote}
      </div>
    </>
  )
}
