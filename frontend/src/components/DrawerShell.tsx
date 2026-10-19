// frontend/src/components/DrawerShell.tsx
import { ReactNode } from 'react'

export interface DrawerShellProps {
  title: ReactNode
  onClose: () => void
  children: ReactNode
  width?: number
}

export function DrawerShell({ title, onClose, children, width = 440 }: DrawerShellProps) {
  return (
    <>
      <div className="edp-drawer-mask show" onClick={onClose} />
      <div className="edp-drawer show" style={width !== 440 ? { width } : undefined}>
        <div className="edp-dh">
          <h3>{title}</h3>
          <span className="edp-x" onClick={onClose}>×</span>
        </div>
        <div className="edp-db">{children}</div>
      </div>
    </>
  )
}