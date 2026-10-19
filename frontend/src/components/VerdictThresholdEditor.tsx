import { useEffect, useState } from 'react'
import type { DimensionVerdictsPayload, VerdictThresholdMeta } from '../types/dimensionVerdict'

const API_BASE = '/api'

type ThresholdKey = keyof VerdictThresholdMeta['effective']

interface FieldSpec {
  key: ThresholdKey
  label: string
  hint: string
  step: string
  min: number
  max: number
  scale: 'fraction' | 'percent'
  group: string
}

const THRESHOLD_FIELDS: FieldSpec[] = [
  {
    key: 'quality_pass_rate_pass',
    label: '质量指标通过率 · 达标',
    hint: '各质量因子通过率均 ≥ 此值视为达标',
    step: '0.01',
    min: 0,
    max: 1,
    scale: 'fraction',
    group: '质量指标',
  },
  {
    key: 'quality_pass_rate_warn',
    label: '质量指标通过率 · 预警',
    hint: '低于此值为未达标',
    step: '0.01',
    min: 0,
    max: 1,
    scale: 'fraction',
    group: '质量指标',
  },
  {
    key: 'quality_total_pass',
    label: '质量总分 · 达标',
    hint: '总分 ≥ 此值且各因子达标 → pass',
    step: '0.01',
    min: 0,
    max: 1,
    scale: 'fraction',
    group: '质量指标',
  },
  {
    key: 'quality_total_warn',
    label: '质量总分 · 预警',
    hint: '总分低于此值为 fail',
    step: '0.01',
    min: 0,
    max: 1,
    scale: 'fraction',
    group: '质量指标',
  },
  {
    key: 'process_chain_pass_warn',
    label: '过程链条全轮通过率 · 预警',
    hint: '低于此值触发需关注（0–1）',
    step: '0.01',
    min: 0,
    max: 1,
    scale: 'fraction',
    group: '过程一致性',
  },
  {
    key: 'scenario_process_pass',
    label: '场景综合分 · 达标',
    step: '0.01',
    min: 0,
    max: 1,
    scale: 'fraction',
    hint: 'process_score 达标线',
    group: '场景交付',
  },
  {
    key: 'scenario_process_warn',
    label: '场景综合分 · 预警',
    step: '0.01',
    min: 0,
    max: 1,
    scale: 'fraction',
    hint: '低于此值为 fail',
    group: '场景交付',
  },
  {
    key: 'scenario_required_pass',
    label: '必做步骤通过率 · 达标',
    step: '0.01',
    min: 0,
    max: 1,
    scale: 'fraction',
    hint: 'required_pass_rate 达标线',
    group: '场景交付',
  },
  {
    key: 'scenario_required_warn',
    label: '必做步骤通过率 · 预警',
    step: '0.01',
    min: 0,
    max: 1,
    scale: 'fraction',
    hint: '低于此值为 fail',
    group: '场景交付',
  },
  {
    key: 'scenario_score_std_volatile',
    label: '多轮交付波动 σ 阈值',
    step: '0.01',
    min: 0,
    max: 1,
    scale: 'fraction',
    hint: '题目多轮综合分标准差超过此值计为波动',
    group: '场景交付',
  },
  {
    key: 'perf_pass_rate_within',
    label: 'SLO 达标率 · 达标',
    step: '1',
    min: 0,
    max: 100,
    scale: 'percent',
    hint: '时延/Token 达标率均 ≥ 此值（%）',
    group: '运行稳定性 · 性能',
  },
  {
    key: 'perf_pass_rate_elevated',
    label: 'SLO 达标率 · 偏高',
    step: '1',
    min: 0,
    max: 100,
    scale: 'percent',
    hint: '低于达标线但 ≥ 此值为性能偏高',
    group: '运行稳定性 · 性能',
  },
]

interface Props {
  taskId: string
  open: boolean
  onClose: () => void
  onSaved: (payload: {
    thresholds: VerdictThresholdMeta
    dimension_verdicts: DimensionVerdictsPayload
  }) => void
}

export function VerdictThresholdEditor({ taskId, open, onClose, onSaved }: Props) {
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [meta, setMeta] = useState<VerdictThresholdMeta | null>(null)
  const [inputs, setInputs] = useState<Record<string, string>>({})

  useEffect(() => {
    if (!open) return
    let cancelled = false
    const load = async () => {
      setLoading(true)
      setNotice(null)
      try {
        const res = await fetch(`${API_BASE}/tasks/${taskId}/verdict-thresholds`)
        if (!res.ok) throw new Error('加载结论规则失败')
        const data = await res.json()
        if (cancelled) return
        const thresholdMeta: VerdictThresholdMeta = {
          effective: data.effective,
          defaults: data.defaults,
          source: data.source,
          custom_keys: data.custom_keys ?? [],
        }
        setMeta(thresholdMeta)
        const next: Record<string, string> = {}
        for (const field of THRESHOLD_FIELDS) {
          const val = thresholdMeta.effective[field.key]
          next[field.key] = val != null ? String(val) : ''
        }
        setInputs(next)
      } catch (err) {
        if (!cancelled) {
          setNotice(err instanceof Error ? err.message : '加载失败')
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [open, taskId])

  const handleResetDefaults = () => {
    if (!meta) return
    const next: Record<string, string> = {}
    for (const field of THRESHOLD_FIELDS) {
      next[field.key] = String(meta.defaults[field.key])
    }
    setInputs(next)
    setNotice('已恢复为系统默认值（保存后生效）')
  }

  const handleSave = async () => {
    if (!meta) return
    const patch: Record<string, number> = {}
    for (const field of THRESHOLD_FIELDS) {
      const raw = inputs[field.key]?.trim()
      if (!raw) {
        setNotice(`${field.label} 不能为空`)
        return
      }
      const num = Number(raw)
      if (!Number.isFinite(num)) {
        setNotice(`${field.label} 须为有效数字`)
        return
      }
      if (num < field.min || num > field.max) {
        setNotice(`${field.label} 须在 ${field.min}–${field.max} 之间`)
        return
      }
      patch[field.key] = num
    }

    try {
      setSaving(true)
      setNotice(null)
      const res = await fetch(`${API_BASE}/tasks/${taskId}/verdict-thresholds`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      })
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}))
        throw new Error(typeof errBody.detail === 'string' ? errBody.detail : '保存失败')
      }
      const data = await res.json()
      onSaved({
        thresholds: {
          effective: data.effective,
          defaults: data.defaults,
          source: data.source,
          custom_keys: data.custom_keys ?? [],
        },
        dimension_verdicts: data.dimension_verdicts,
      })
      onClose()
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  if (!open) return null

  const groups = Array.from(new Set(THRESHOLD_FIELDS.map(f => f.group)))

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-content eval-target-dialog"
        style={{ maxWidth: 560, maxHeight: '90vh', overflow: 'auto' }}
        onClick={e => e.stopPropagation()}
      >
        <h3 className="eval-target-dialog__title">结论规则</h3>
        <p className="eval-target-dialog__desc">
          调整本任务四维结论判定阈值。保存后仅重算结论展示，不会重跑 Agent 或 Judge。
          {meta && (
            <span style={{ display: 'block', marginTop: 6 }}>
              当前：
              <strong style={{ marginLeft: 4 }}>
                {meta.source === 'task_custom' ? '任务自定义' : '系统默认'}
              </strong>
              {meta.custom_keys.length > 0 && (
                <span style={{ color: '#666', marginLeft: 6 }}>
                  （已覆盖 {meta.custom_keys.length} 项）
                </span>
              )}
            </span>
          )}
        </p>

        {loading ? (
          <p style={{ color: '#666' }}>加载中…</p>
        ) : (
          <>
            {groups.map(group => (
              <div key={group} style={{ marginBottom: 16 }}>
                <div
                  style={{
                    fontSize: 12,
                    fontWeight: 600,
                    color: '#667eea',
                    marginBottom: 8,
                  }}
                >
                  {group}
                </div>
                <div className="eval-target-dialog__fields">
                  {THRESHOLD_FIELDS.filter(f => f.group === group).map(field => {
                    const isCustom = meta?.custom_keys.includes(field.key)
                    return (
                      <label key={field.key}>
                        <span className="eval-target-dialog__field-label">
                          {field.label}
                          {field.scale === 'fraction' && ' (0–1)'}
                          {field.scale === 'percent' && ' (%)'}
                          {isCustom && (
                            <span style={{ color: '#e6a817', marginLeft: 4, fontWeight: 400 }}>
                              已自定义
                            </span>
                          )}
                        </span>
                        <input
                          type="number"
                          min={field.min}
                          max={field.max}
                          step={field.step}
                          className="eval-target-dialog__input"
                          value={inputs[field.key] ?? ''}
                          onChange={e =>
                            setInputs(prev => ({ ...prev, [field.key]: e.target.value }))
                          }
                        />
                        <span style={{ fontSize: 11, color: '#888' }}>{field.hint}</span>
                      </label>
                    )
                  })}
                </div>
              </div>
            ))}
          </>
        )}

        {notice && <div className="eval-target-dialog__notice">{notice}</div>}

        <div className="eval-target-dialog__footer">
          <button
            type="button"
            className="eval-target-dialog__btn eval-target-dialog__btn--secondary"
            onClick={handleResetDefaults}
            disabled={loading || saving}
          >
            恢复默认
          </button>
          <button
            type="button"
            className="eval-target-dialog__btn eval-target-dialog__btn--secondary"
            onClick={onClose}
            disabled={saving}
          >
            取消
          </button>
          <button
            type="button"
            className="eval-target-dialog__btn eval-target-dialog__btn--primary"
            disabled={loading || saving}
            onClick={() => void handleSave()}
          >
            {saving ? '保存中…' : '保存并重算结论'}
          </button>
        </div>
      </div>
    </div>
  )
}
