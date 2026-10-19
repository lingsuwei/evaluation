import { useState, useRef, useEffect, useCallback, type CSSProperties, type KeyboardEvent } from 'react'

type FilterType = 'text' | 'number-min' | 'enum'

interface EnumOption {
  label: string
  value: string
}

interface ColumnFilterProps {
  type: FilterType
  label: string
  value: string
  onChange: (value: string) => void
  min?: number
  max?: number
  step?: number
  options?: EnumOption[]
}

/**
 * 表头列筛选下拉组件（antd/Excel 风）
 * 支持 text / number-min / enum 三种筛选类型。
 * 下拉面板使用 position:fixed + JS 定位，避免被表格 overflow 容器裁剪。
 */
export function ColumnFilter({
  type,
  label,
  value,
  onChange,
  min,
  max,
  step,
  options,
}: ColumnFilterProps) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(value)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const [panelStyle, setPanelStyle] = useState<CSSProperties>({ visibility: 'hidden' })

  const isActive = value !== '' && value != null

  // 同步 draft + 计算面板位置
  const updatePanelPosition = useCallback(() => {
    if (!triggerRef.current) return
    const rect = triggerRef.current.getBoundingClientRect()
    const panelWidth = 220
    const panelHeight = panelRef.current?.offsetHeight || 200
    let left = rect.left
    let top = rect.bottom + 4
    // 防止右侧溢出
    if (left + panelWidth > window.innerWidth - 8) {
      left = window.innerWidth - panelWidth - 8
    }
    if (left < 8) left = 8
    // 如果下方空间不足且上方足够，向上弹出
    if (top + panelHeight > window.innerHeight - 8 && rect.top - panelHeight - 4 > 8) {
      top = rect.top - panelHeight - 4
    }
    setPanelStyle({ position: 'fixed', left, top, zIndex: 1000, visibility: 'visible' })
  }, [])

  useEffect(() => {
    if (open) {
      setDraft(value)
      // 等待面板渲染后计算位置
      requestAnimationFrame(() => {
        updatePanelPosition()
      })
    }
  }, [open, value, updatePanelPosition])

  // 点击外部关闭
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      if (triggerRef.current?.contains(target)) return
      if (panelRef.current?.contains(target)) return
      setOpen(false)
    }
    // capture 阶段监听 scroll：页面或表格容器滚动时关闭面板（fixed 定位会与表格错位）。
    // 但排除输入框内部滚动——粘贴内容时浏览器会自动滚动输入框以显示光标，
    // 这不应关闭面板，否则用户无法在面板内粘贴长文本（如 taskId）。
    const onScroll = (e: Event) => {
      const target = e.target as HTMLElement | null
      if (
        target &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.tagName === 'SELECT')
      ) {
        return
      }
      setOpen(false)
    }
    const close = () => setOpen(false)
    document.addEventListener('mousedown', handler)
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', close)
    return () => {
      document.removeEventListener('mousedown', handler)
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', close)
    }
  }, [open])

  const handleReset = () => {
    setDraft('')
    onChange('')
    setOpen(false)
  }

  const handleOK = () => {
    onChange(draft)
    setOpen(false)
  }

  const handleEnumSelect = (optValue: string) => {
    const next = draft === optValue ? '' : optValue
    setDraft(next)
    onChange(next)
    setOpen(false)
  }

  const handleKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      handleOK()
    } else if (e.key === 'Escape') {
      setOpen(false)
    }
  }

  return (
    <span className="col-filter">
      <button
        ref={triggerRef}
        type="button"
        className={`col-filter__trigger${isActive ? ' is-active' : ''}`}
        onClick={() => setOpen(o => !o)}
        title={`筛选：${label}`}
        aria-label={`筛选：${label}`}
        aria-expanded={open}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
        </svg>
        {isActive && <span className="col-filter__dot" />}
      </button>
      {open && (
        <div ref={panelRef} className="col-filter__panel" style={panelStyle}>
          <div className="col-filter__head">{label}</div>
          <div className="col-filter__body">
            {type === 'text' && (
              <input
                className="col-filter__input"
                type="text"
                placeholder={`搜索${label}`}
                value={draft}
                onChange={e => setDraft(e.target.value)}
                onKeyDown={handleKeyDown}
                autoFocus
              />
            )}
            {type === 'number-min' && (
              <div className="col-filter__num">
                <span style={{ fontSize: 12, color: '#999', flexShrink: 0 }}>≥</span>
                <input
                  className="col-filter__input"
                  type="number"
                  min={min}
                  max={max}
                  step={step}
                  placeholder="最小值"
                  value={draft}
                  onChange={e => setDraft(e.target.value)}
                  onKeyDown={handleKeyDown}
                  autoFocus
                />
              </div>
            )}
            {type === 'enum' && (
              <ul className="col-filter__list">
                {options?.map(opt => (
                  <li key={opt.value}>
                    <button
                      type="button"
                      className={`col-filter__opt${draft === opt.value ? ' is-on' : ''}`}
                      onClick={() => handleEnumSelect(opt.value)}
                    >
                      {opt.label}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="col-filter__foot">
            <button type="button" className="col-filter__reset" onClick={handleReset}>
              重置
            </button>
            <button type="button" className="col-filter__ok" onClick={handleOK}>
              确定
            </button>
          </div>
        </div>
      )}
    </span>
  )
}
