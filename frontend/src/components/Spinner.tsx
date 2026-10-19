/** 轻量加载旋转动画组件 — 内联 SVG，零外部依赖，符合 UCD 规范。 */
interface SpinnerProps {
  size?: 16 | 20 | 28
  label?: string
  color?: string
}

export function Spinner({ size = 20, label, color = 'var(--edp-primary)' }: SpinnerProps) {
  const svg = (
    <svg
      className="edp-spinner"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      role="img"
      aria-label={label || '加载中'}
    >
      <circle className="edp-spinner-track" cx="12" cy="12" r="10" strokeWidth="3" />
      <path
        className="edp-spinner-arc"
        d="M12 2a10 10 0 0 1 10 10"
        strokeWidth="3"
        strokeLinecap="round"
        stroke={color}
      />
    </svg>
  )
  if (!label) return svg
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 'var(--edp-space-2)',
        color: 'var(--edp-text3)',
        fontSize: 'var(--edp-font-base)',
      }}
    >
      {svg}
      <span>{label}</span>
    </span>
  )
}
