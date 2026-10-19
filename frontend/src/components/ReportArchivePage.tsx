import { useCallback, useEffect, useState } from 'react'
import { formatDate } from '../utils/dateUtils'

const API_BASE = '/api'

interface ReportItem {
  id: string
  report_type: string // evaluation | comparison
  title: string
  format: string // xlsx | html
  task_ids?: string[] | null
  dataset_name?: string | null
  agent_name?: string | null
  workspace_name?: string | null
  file_size?: number | null
  verdict_status?: string | null
  score?: number | null
  created_at?: string | null
}

interface ListResp {
  total: number
  page: number
  page_size: number
  items: ReportItem[]
}

const VERDICT_LABEL: Record<string, string> = {
  pass: '达标',
  warn: '需关注',
  fail: '未达标',
  na: '不适用',
}
const VERDICT_COLOR: Record<string, string> = {
  pass: 'var(--edp-green)',
  warn: 'var(--edp-amber)',
  fail: 'var(--edp-red)',
  na: 'var(--edp-text3)',
}

const PAGE_SIZE = 20

function typeLabel(t: string): string {
  return t === 'comparison' ? '对比报告' : '评测报告'
}

function formatLabel(f: string): string {
  return (f || '').toUpperCase()
}

function fmtSize(bytes?: number | null): string {
  if (!bytes) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

export function ReportArchivePage() {
  const [items, setItems] = useState<ReportItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [filter, setFilter] = useState<'all' | 'evaluation' | 'comparison'>('all')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [confirmId, setConfirmId] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams({
        page: String(page),
        page_size: String(PAGE_SIZE),
      })
      if (filter !== 'all') params.set('report_type', filter)
      const res = await fetch(`${API_BASE}/reports?${params.toString()}`)
      if (!res.ok) throw new Error(`加载失败 (${res.status})`)
      const data: ListResp = await res.json()
      setItems(data.items || [])
      setTotal(data.total || 0)
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [page, filter])

  useEffect(() => {
    void load()
  }, [load])

  const handleDownload = async (r: ReportItem) => {
    setBusyId(r.id)
    try {
      const res = await fetch(`${API_BASE}/reports/${r.id}/download`)
      if (!res.ok) throw new Error('下载失败')
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${r.title || 'report'}.${r.format === 'html' ? 'html' : 'xlsx'}`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (e) {
      setError(e instanceof Error ? e.message : '下载失败')
    } finally {
      setBusyId(null)
    }
  }

  const handlePreview = (r: ReportItem) => {
    if (r.format !== 'html') return
    window.open(`${API_BASE}/reports/${r.id}/preview`, '_blank')
  }

  const handleDelete = async (id: string) => {
    setBusyId(id)
    try {
      const res = await fetch(`${API_BASE}/reports/${id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('删除失败')
      // 若当前页删空且非第一页，回退一页
      if (items.length === 1 && page > 1) {
        setPage(page - 1)
      } else {
        void load()
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '删除失败')
    } finally {
      setBusyId(null)
      setConfirmId(null)
    }
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div className="card history-card" style={{ padding: '20px 24px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 'var(--edp-space-4)', flexWrap: 'wrap', gap: 'var(--edp-space-3)' }}>
        <h2 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>评测报告</h2>
        <div className="sub-tabs" style={{ marginBottom: 0 }}>
          {(['all', 'evaluation', 'comparison'] as const).map((f) => (
            <button
              key={f}
              type="button"
              className={`sub-tab ${filter === f ? 'active' : ''}`}
              onClick={() => {
                setFilter(f)
                setPage(1)
              }}
            >
              {f === 'all' ? '全部' : f === 'evaluation' ? '评测报告' : '对比报告'}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div style={{ padding: 'var(--edp-space-3)', color: 'var(--edp-red)', fontSize: 'var(--edp-font-md)', background: 'var(--edp-amber-soft)', border: '1px solid var(--edp-red-soft)', borderRadius: 'var(--edp-radius-sm)', marginBottom: 'var(--edp-space-3)' }}>
          {error}
        </div>
      )}

      {loading ? (
        <p style={{ padding: 'var(--edp-space-6)', textAlign: 'center', color: 'var(--edp-text3)', fontSize: 'var(--edp-font-md)' }}>加载中…</p>
      ) : items.length === 0 ? (
        <p style={{ padding: 'var(--edp-space-6)', textAlign: 'center', color: 'var(--edp-text3)', fontSize: 'var(--edp-font-md)' }}>
          暂无报告。可在「评估历史」中导出评测报告，或在「场景/版本对比」中导出对比报告，导出后会自动归档到此。
        </p>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 'var(--edp-font-md)' }}>
            <thead>
              <tr style={{ background: 'var(--edp-bg)', textAlign: 'left' }}>
                <th style={{ padding: '10px 12px' }}>标题</th>
                <th style={{ padding: '10px 12px' }}>类型</th>
                <th style={{ padding: '10px 12px' }}>格式</th>
                <th style={{ padding: '10px 12px' }}>评测集</th>
                <th style={{ padding: '10px 12px' }}>工作空间</th>
                <th style={{ padding: '10px 12px' }}>综合结论</th>
                <th style={{ padding: '10px 12px' }}>大小</th>
                <th style={{ padding: '10px 12px' }}>归档时间</th>
                <th style={{ padding: '10px 12px' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((r) => {
                const status = r.verdict_status
                const showVerdict = r.report_type === 'comparison' && status
                return (
                  <tr key={r.id}>
                    <td style={{ padding: '10px 12px', borderTop: '1px solid var(--edp-primary-soft)', maxWidth: 360 }}>
                      <div style={{ fontWeight: 500 }}>{r.title || '—'}</div>
                      {r.agent_name && (
                        <div style={{ fontSize: 'var(--edp-font-sm)', color: 'var(--edp-text3)' }}>{r.agent_name}</div>
                      )}
                    </td>
                    <td style={{ padding: '10px 12px', borderTop: '1px solid var(--edp-primary-soft)' }}>{typeLabel(r.report_type)}</td>
                    <td style={{ padding: '10px 12px', borderTop: '1px solid var(--edp-primary-soft)' }}>{formatLabel(r.format)}</td>
                    <td style={{ padding: '10px 12px', borderTop: '1px solid var(--edp-primary-soft)' }}>{r.dataset_name || '—'}</td>
                    <td style={{ padding: '10px 12px', borderTop: '1px solid var(--edp-primary-soft)' }}>{r.workspace_name || '—'}</td>
                    <td style={{ padding: '10px 12px', borderTop: '1px solid var(--edp-primary-soft)' }}>
                      {showVerdict ? (
                        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                          <span style={{ color: 'var(--edp-card)', background: VERDICT_COLOR[status] || 'var(--edp-text3)', fontSize: 'var(--edp-font-sm)', fontWeight: 600, padding: '2px 8px', borderRadius: 'var(--edp-radius-sm)' }}>
                            {VERDICT_LABEL[status] || status}
                          </span>
                          {r.score != null && <span style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-primary)', fontWeight: 600 }}>{Number(r.score).toFixed(2)}</span>}
                        </span>
                      ) : '—'}
                    </td>
                    <td style={{ padding: '10px 12px', borderTop: '1px solid var(--edp-primary-soft)' }}>{fmtSize(r.file_size)}</td>
                    <td style={{ padding: '10px 12px', borderTop: '1px solid var(--edp-primary-soft)', whiteSpace: 'nowrap' }}>{r.created_at ? formatDate(r.created_at) : '—'}</td>
                    <td style={{ padding: '10px 12px', borderTop: '1px solid var(--edp-primary-soft)', whiteSpace: 'nowrap' }}>
                      {r.format === 'html' && (
                        <button className="btn btn-secondary" style={{ padding: '4px 10px', fontSize: 'var(--edp-font-base)', marginRight: 6 }} onClick={() => handlePreview(r)} disabled={busyId === r.id}>
                          预览
                        </button>
                      )}
                      <button className="btn btn-secondary" style={{ padding: '4px 10px', fontSize: 'var(--edp-font-base)', marginRight: 6 }} onClick={() => handleDownload(r)} disabled={busyId === r.id}>
                        下载
                      </button>
                      {confirmId === r.id ? (
                        <>
                          <button className="btn btn-primary" style={{ padding: '4px 10px', fontSize: 'var(--edp-font-base)', marginRight: 'var(--edp-space-1)' }} onClick={() => handleDelete(r.id)} disabled={busyId === r.id}>
                            确认
                          </button>
                          <button className="btn btn-secondary" style={{ padding: '4px 10px', fontSize: 'var(--edp-font-base)' }} onClick={() => setConfirmId(null)}>
                            取消
                          </button>
                        </>
                      ) : (
                        <button className="btn btn-secondary" style={{ padding: '4px 10px', fontSize: 'var(--edp-font-base)', color: 'var(--edp-red)' }} onClick={() => setConfirmId(r.id)} disabled={busyId === r.id}>
                          删除
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 'var(--edp-space-3)', fontSize: 'var(--edp-font-md)', color: 'var(--edp-text3)' }}>
            <span>共 {total} 份</span>
            <div style={{ display: 'flex', gap: 'var(--edp-space-2)' }}>
              <button className="btn btn-secondary" style={{ padding: '4px 12px', fontSize: 'var(--edp-font-md)' }} onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1}>
                上一页
              </button>
              <span style={{ lineHeight: '30px' }}>{page} / {totalPages}</span>
              <button className="btn btn-secondary" style={{ padding: '4px 12px', fontSize: 'var(--edp-font-md)' }} onClick={() => setPage((p) => Math.min(totalPages, p + 1))} disabled={page >= totalPages}>
                下一页
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
