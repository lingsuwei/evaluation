import { useState, useRef, useEffect, useCallback, type CSSProperties } from 'react'

export interface ColumnConfigItem {
  key: string
  label: string
  defaultVisible: boolean
}

interface ColumnConfigProps {
  columns: ColumnConfigItem[]
  visibleKeys: string[]
  onToggle: (key: string) => void
  onReset: () => void
}

/**
 * 列配置下拉组件：勾选需要展示的列，选择持久化到 localStorage。
 * 复用与 ColumnFilter 一致的 position:fixed + JS 定位策略，避免被表格 overflow 裁剪。
 */
export function ColumnConfig({
  columns,
  visibleKeys,
  onToggle,
  onReset,
}: ColumnConfigProps) {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const [panelStyle, setPanelStyle] = useState<CSSProperties>({ visibility: 'hidden' })

  const visibleSet = new Set(visibleKeys)

  const updatePanelPosition = useCallback(() => {
    if (!triggerRef.current) return
    const rect = triggerRef.current.getBoundingClientRect()
    const panelWidth = 200
    const panelHeight = panelRef.current?.offsetHeight || 200
    let left = rect.left
    let top = rect.bottom + 4
    if (left + panelWidth > window.innerWidth - 8) {
      left = window.innerWidth - panelWidth - 8
    }
    if (left < 8) left = 8
    if (top + panelHeight > window.innerHeight - 8 && rect.top - panelHeight - 4 > 8) {
      top = rect.top - panelHeight - 4
    }
    setPanelStyle({ position: 'fixed', left, top, zIndex: 1000, visibility: 'visible' })
  }, [])

  useEffect(() => {
    if (open) {
      requestAnimationFrame(() => {
        updatePanelPosition()
      })
    }
  }, [open, updatePanelPosition])

  // 点击外部关闭；滚动/缩放时仅重新定位面板，不关闭。
  // 关键：不使用捕获阶段（第三参数不为 true），否则面板内部列表的滚动条、
  // 表格容器的横向滚动等任何子元素的 scroll 事件都会误触发关闭，
  // 导致无法拖动滚动条选择首屏之外的列、勾选列后面板被关闭。
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      if (triggerRef.current?.contains(target)) return
      if (panelRef.current?.contains(target)) return
      setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    window.addEventListener('scroll', updatePanelPosition)
    window.addEventListener('resize', updatePanelPosition)
    return () => {
      document.removeEventListener('mousedown', handler)
      window.removeEventListener('scroll', updatePanelPosition)
      window.removeEventListener('resize', updatePanelPosition)
    }
  }, [open, updatePanelPosition])

  const handleReset = () => {
    onReset()
  }

  return (
    <span className="col-config">
      <button
        ref={triggerRef}
        type="button"
        className="edp-btn col-config__trigger"
        onClick={() => setOpen(o => !o)}
        title="列配置：勾选需要展示的字段"
        aria-label="列配置"
        aria-expanded={open}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="M12 2v20M2 7l4-4 4 4M22 17l-4 4-4-4" />
          <path d="M5 7v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7" />
        </svg>
        <span className="col-config__label">列配置</span>
      </button>
      {open && (
        <div ref={panelRef} className="col-config__panel" style={panelStyle}>
          <div className="col-config__head">
            <span>列配置</span>
            <button type="button" className="col-config__reset" onClick={handleReset}>
              恢复默认
            </button>
          </div>
          <ul className="col-config__list">
            {columns.map(col => (
              <li key={col.key}>
                <label className={`col-config__opt${visibleSet.has(col.key) ? ' is-on' : ''}`}>
                  <input
                    type="checkbox"
                    checked={visibleSet.has(col.key)}
                    onChange={() => onToggle(col.key)}
                  />
                  <span>{col.label}</span>
                </label>
              </li>
            ))}
          </ul>
          <div className="col-config__foot">
            <span className="col-config__count">已选 {visibleKeys.length} / {columns.length} 列</span>
            <button type="button" className="col-config__done" onClick={() => setOpen(false)}>
              完成
            </button>
          </div>
        </div>
      )}
    </span>
  )
}
