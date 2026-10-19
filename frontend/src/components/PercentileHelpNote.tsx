/** P75 / P90 百分位指标的大白话说明（运行效能、过程点配置等共用） */
import { HISTORICAL_PERCENTILE, TASK_PERCENTILE } from './helpCopy'

export function PercentileHelpNote({
  compact = false,
  scope = 'task',
}: {
  compact?: boolean
  scope?: 'task' | 'historical'
}) {
  const copy = scope === 'historical' ? HISTORICAL_PERCENTILE : TASK_PERCENTILE
  const subjectShort = scope === 'historical' ? '历史 Run' : '本任务 Run'

  if (compact) {
    return (
      <p style={{ margin: '0 0 8px', fontSize: 11, color: '#888', lineHeight: 1.55 }}>
        <strong style={{ color: '#666', fontWeight: 600 }}>{copy.title}</strong>
        {' '}
        {copy.intro}{' '}
        <strong style={{ color: '#666', fontWeight: 500 }}>P75</strong>
        {' '}
        {copy.p75.replace(/^P75\s*/, '')}{' '}
        <strong style={{ color: '#666', fontWeight: 500 }}>P90</strong>
        {' '}
        {copy.p90.replace(/^P90\s*/, '')}
      </p>
    )
  }

  return (
    <div
      style={{
        marginBottom: 10,
        padding: '8px 10px',
        background: '#fff',
        border: '1px dashed #d0d7e6',
        borderRadius: 6,
        fontSize: 12,
        color: '#555',
        lineHeight: 1.6,
      }}
    >
      <div style={{ fontWeight: 600, color: '#444', marginBottom: 4 }}>{copy.title}</div>
      <div>
        {scope === 'historical' ? (
          <>
            基于同 Agent、同评测集的<strong>历史 Run</strong>算出的参考线（不含本次任务），用来判断「这次跑得比过去快还是慢」。
          </>
        ) : (
          <>
            基于<strong>本任务</strong>内各题 Run 的分布（通常每题 1 条；同题多轮时每轮各 1 条），用来看题目间谁偏慢/偏费。
          </>
        )}
      </div>
      <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
        <li>
          <strong>P75</strong>：约 <strong>75%</strong> 的 {subjectShort} 时延/Tokens ≤ 这个值
        </li>
        <li>
          <strong>P90</strong>：约 <strong>90%</strong> 的 {subjectShort} ≤ 这个值，可理解为「本批大多数 Run 的上限参考」
        </li>
      </ul>
      <div style={{ marginTop: 6, fontSize: 11, color: '#888' }}>
        样本数较少时，P75/P90 波动较大，仅供参考；业界 APM / LLM 观测平台普遍使用这一命名。
      </div>
    </div>
  )
}
