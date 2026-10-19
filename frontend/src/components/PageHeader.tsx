import type { ReactNode } from 'react'

interface PageHeaderProps {
  title: string
  description?: ReactNode
  icon?: ReactNode
  actions?: ReactNode
  className?: string
}

/** 页面左上角标题区 — 全站 UCD 统一规范，见 .cursor/rules/onto-eval-ucd-page-header.mdc */
export function PageHeader({ title, description, icon, actions, className }: PageHeaderProps) {
  return (
    <div className={`page-header-block${className ? ` ${className}` : ''}`}>
      <div className="page-header-block__main">
        <h2 className="page-title">
          {icon}
          {title}
        </h2>
        {description != null && (
          <p className="page-desc">{description}</p>
        )}
      </div>
      {actions ? <div className="page-header-block__actions">{actions}</div> : null}
    </div>
  )
}
