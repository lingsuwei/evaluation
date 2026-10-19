import { useCallback, useEffect, useMemo, useState } from 'react'
import axios from 'axios'

const API_BASE = '/api'

interface ReferenceTemplate {
  name: string
  description: string | null
  order: number | null
  required: boolean
  score_weight: number
  match_mode: string
  enabled: boolean
  enforce_order: boolean
  match_rules: { mode: string; rules: unknown[] }
}

interface ReferenceCatalog {
  id: string
  description: string | null
  source: string | null
  template_set_name: string
  count: number
  templates: ReferenceTemplate[]
}

interface Props {
  agentName: string
  configuredNames: string[]
  onSeeded: () => void | Promise<void>
  formatApiError: (err: unknown, fallback: string) => string
  embedded?: boolean
}

function formatApiError(err: unknown, fallback: string): string {
  const ax = err as { response?: { data?: { detail?: string } } }
  return ax.response?.data?.detail || fallback
}

export function AgentCheckpointReferencePanel({
  agentName,
  configuredNames,
  onSeeded,
  formatApiError: formatErr = formatApiError,
  embedded = false,
}: Props) {
  const [catalog, setCatalog] = useState<ReferenceCatalog | null>(null)
  const [loading, setLoading] = useState(true)
  const [seeding, setSeeding] = useState(false)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState(false)
  const [expandedRules, setExpandedRules] = useState<string | null>(null)

  const configuredSet = useMemo(
    () => new Set(configuredNames.map((n) => n.trim()).filter(Boolean)),
    [configuredNames],
  )

  const rows = useMemo(() => {
    const templates = catalog?.templates ?? []
    return [...templates].sort((a, b) => {
      const ao = a.order ?? Number.MAX_SAFE_INTEGER
      const bo = b.order ?? Number.MAX_SAFE_INTEGER
      if (ao !== bo) return ao - bo
      return a.name.localeCompare(b.name, 'zh-CN')
    })
  }, [catalog])

  const missingNames = useMemo(
    () => rows.filter((t) => !configuredSet.has(t.name)).map((t) => t.name),
    [rows, configuredSet],
  )

  const fetchCatalog = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await axios.get<ReferenceCatalog>(
        `${API_BASE}/checkpoints/reference-templates/onto-agent`,
      )
      setCatalog(res.data)
    } catch (err: unknown) {
      setCatalog(null)
      setError(formatErr(err, '加载参考模板失败'))
    } finally {
      setLoading(false)
    }
  }, [formatErr])

  useEffect(() => {
    void fetchCatalog()
  }, [fetchCatalog])

  const handleSeedMissing = async () => {
    if (!agentName.trim() || missingNames.length === 0) return
    if (
      !window.confirm(
        `将为 Agent「${agentName}」初始化 ${missingNames.length} 条缺失的 onto-agent 参考过程点？\n已有同名过程点不会被覆盖。`,
      )
    ) {
      return
    }
    setSeeding(true)
    setError('')
    try {
      await axios.post(`${API_BASE}/checkpoints/templates/seed-from-reference`, {
        agent_name: agentName.trim(),
        template_names: missingNames,
      })
      await onSeeded()
    } catch (err: unknown) {
      setError(formatErr(err, '初始化失败'))
    } finally {
      setSeeding(false)
    }
  }

  if (loading) {
    return (
      <p style={{ margin: 0, color: '#666', fontSize: 13 }}>加载 onto-agent 参考过程点…</p>
    )
  }

  if (!catalog) {
    return (
      <div className={embedded ? undefined : 'config-page-panel alert-error'} style={{ marginBottom: embedded ? 0 : 20 }}>
        <p style={{ margin: '0 0 8px' }}>
          {error ||
            '无法加载 onto-agent 参考模板。请确认 backend 已重启并包含 reference-templates 接口。'}
        </p>
        <button type="button" className="btn" onClick={() => void fetchCatalog()}>
          重试
        </button>
      </div>
    )
  }

  const configuredCount = rows.length - missingNames.length

  const inner = (
    <>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          flexWrap: 'wrap',
          gap: 12,
        }}
      >
        <div>
          {!embedded && <h3 style={{ margin: '0 0 6px', fontSize: 16 }}>onto-agent 默认识别参考</h3>}
          <p style={{ margin: 0, fontSize: 13, color: '#555', lineHeight: 1.6 }}>
            {embedded ? 'M2 参考模板' : 'onto-agent 默认识别参考'} · 共 {catalog.count} 条
            {agentName.trim() ? (
              <>
                {' '}
                · 已配置 <strong>{configuredCount}</strong>/{catalog.count}
                {missingNames.length > 0 && (
                  <>
                    {' '}
                    · 缺失 <strong style={{ color: '#e65100' }}>{missingNames.length}</strong> 条
                  </>
                )}
              </>
            ) : null}
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <button type="button" className="btn" onClick={() => setExpanded((v) => !v)}>
            {expanded ? '收起参考' : '展开参考'}
          </button>
          {agentName.trim() && missingNames.length > 0 ? (
            <button
              type="button"
              className="btn btn-primary"
              disabled={seeding}
              onClick={() => void handleSeedMissing()}
            >
              {seeding ? '初始化中…' : `初始化缺失项 (${missingNames.length})`}
            </button>
          ) : agentName.trim() ? (
            <button type="button" className="btn" disabled title="参考模板已全部配置">
              已全部配置
            </button>
          ) : null}
        </div>
      </div>

      {error && <p style={{ margin: '10px 0 0', fontSize: 13, color: '#c0392b' }}>{error}</p>}

      {expanded && (
        <div className="checkpoint-table-wrap" style={{ marginTop: 14 }}>
          <table className="results-table checkpoint-table">
            <thead>
              <tr>
                <th style={{ textAlign: 'center', width: 56 }}>顺序</th>
                <th style={{ textAlign: 'center' }}>过程点</th>
                <th style={{ textAlign: 'center' }}>说明</th>
                <th style={{ textAlign: 'center', width: 56 }}>必达</th>
                <th style={{ textAlign: 'center', width: 72 }}>权重</th>
                <th style={{ textAlign: 'center', width: 88 }}>状态</th>
                <th style={{ textAlign: 'center', width: 88 }}>规则</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => {
                const configured = configuredSet.has(t.name)
                return (
                  <tr key={t.name}>
                    <td style={{ textAlign: 'center' }}>{t.order ?? '—'}</td>
                    <td style={{ textAlign: 'center' }}>{t.name}</td>
                    <td style={{ textAlign: 'left', maxWidth: 280 }}>{t.description || '—'}</td>
                    <td style={{ textAlign: 'center' }}>{t.required ? '是' : '否'}</td>
                    <td style={{ textAlign: 'center' }}>{t.score_weight}</td>
                    <td style={{ textAlign: 'center', color: configured ? '#2e7d32' : '#e65100' }}>
                      {configured ? '已配置' : '待初始化'}
                    </td>
                    <td className="cell-actions">
                      <button
                        type="button"
                        className="btn"
                        onClick={() =>
                          setExpandedRules((prev) => (prev === t.name ? null : t.name))
                        }
                      >
                        {expandedRules === t.name ? '收起' : '查看'}
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {expandedRules && (
            <pre
              style={{
                marginTop: 10,
                padding: 12,
                background: '#1e1e1e',
                color: '#d4d4d4',
                borderRadius: 6,
                fontSize: 12,
                overflow: 'auto',
                maxHeight: 200,
              }}
            >
              {JSON.stringify(
                rows.find((r) => r.name === expandedRules)?.match_rules ?? {},
                null,
                2,
              )}
            </pre>
          )}
        </div>
      )}
    </>
  )

  if (embedded) {
    return <div style={{ padding: '10px 12px', background: '#f8f9fe', borderRadius: 6, border: '1px solid #e8eaf5' }}>{inner}</div>
  }

  return (
    <div
      className="card"
      style={{
        marginBottom: 20,
        border: '1px solid #c5cae9',
        background: '#fafbff',
      }}
    >
      {inner}
    </div>
  )
}
