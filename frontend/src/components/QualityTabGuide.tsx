import { useState } from 'react'

interface Props {
  inferenceRounds?: number
}

export function QualityTabGuide({ inferenceRounds }: Props) {
  const [collapsed, setCollapsed] = useState(false)

  return (
    <div
      style={{
        margin: '0 24px 0',
        padding: collapsed ? '8px 12px' : '12px 16px',
        background: '#e8f0fe',
        borderRadius: 6,
        border: '1px solid #c6dafc',
        fontSize: 13,
        lineHeight: 1.8,
        color: '#1a3a6b',
      }}
    >
      <div
        role="button"
        tabIndex={0}
        onClick={() => setCollapsed(!collapsed)}
        onKeyDown={e => {
          if (e.key === 'Enter' || e.key === ' ') setCollapsed(!collapsed)
        }}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          cursor: 'pointer',
          fontWeight: 600,
          fontSize: 14,
          marginBottom: collapsed ? 0 : 8,
        }}
      >
        <span style={{ fontSize: 16 }}>{collapsed ? '▶' : '▼'}</span>
        质量 Tab 怎么看？
      </div>
      {!collapsed && (
        <>
          <div style={{ marginBottom: 4 }}>
            ① 答案好不好 → 下方「质量指标」与「单题判分明细」（正确性 / 相关性 / 忠诚度等）
          </div>
          <div style={{ marginBottom: 4 }}>
            ② 过程对不对 → 请切「过程一致性」Tab（Agent 必达过程点与推理链条是否稳定）
          </div>
          <div style={{ marginBottom: 4 }}>
            ③ 场景交付好不好 → 请切「场景子步骤」Tab（按题 S1–S8 加权打分，独立维度，不计入质量总分）
          </div>
          <div style={{ marginBottom: 4, color: '#555' }}>
            需要看慢多少、Token 差多少 → 请切「运行效能」Tab
          </div>
          {(inferenceRounds ?? 1) > 1 && (
            <div style={{ marginTop: 8, padding: '6px 10px', background: '#fff3cd', borderRadius: 4, color: '#856404' }}>
              本任务每题 {inferenceRounds} 轮：请先看「过程一致性」Tab 确认各轮必达是否全绿，再按需做 Span 对比。
            </div>
          )}
        </>
      )}
    </div>
  )
}
