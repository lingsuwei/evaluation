import { useState, useEffect } from 'react'
import axios from 'axios'
import { formatDate } from '../utils/dateUtils'

const API_BASE = '/api'

interface Dataset {
  id: string
  item_count: number
  created_at: string
}

interface DatasetDetail {
  id: string
  item_count: number
  created_at: string
  data: any[]
}

interface Props {
  onDatasetSelect: (datasetId: string) => void
}

export function DatasetManagePage({ onDatasetSelect }: Props) {
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedDatasets, setSelectedDatasets] = useState<string[]>([])
  const [deleting, setDeleting] = useState(false)
  const [copying, setCopying] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [editingDataset, setEditingDataset] = useState<DatasetDetail | null>(null)
  const [editData, setEditData] = useState<Array<Record<string, any>>>([])
  const [showModal, setShowModal] = useState(false)
  const [modalMessage, setModalMessage] = useState('')
  const [modalType, setModalType] = useState<'confirm' | 'success' | 'error'>('confirm')
  const [confirmAction, setConfirmAction] = useState<(() => Promise<void>) | null>(null)

  const fetchDatasets = async () => {
    setLoading(true)
    try {
      const res = await axios.get(`${API_BASE}/datasets`)
      setDatasets(res.data.datasets)
    } catch (err) {
      console.error('获取数据集列表失败:', err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchDatasets()
  }, [])

  const filteredDatasets = datasets.filter(dataset =>
    dataset.id.toLowerCase().includes(searchQuery.toLowerCase())
  )

  const toggleDatasetSelection = (datasetId: string) => {
    setSelectedDatasets(prev => {
      if (prev.includes(datasetId)) {
        return prev.filter(id => id !== datasetId)
      } else {
        return [...prev, datasetId]
      }
    })
  }

  const toggleSelectAll = () => {
    if (selectedDatasets.length === filteredDatasets.length) {
      setSelectedDatasets([])
    } else {
      setSelectedDatasets(filteredDatasets.map(d => d.id))
    }
  }

  const handleModalClose = () => {
    setShowModal(false)
    setConfirmAction(null)
  }

  const handleModalConfirm = async () => {
    if (confirmAction) {
      await confirmAction()
    }
  }

  const handleDeleteDataset = async (datasetId: string) => {
    setModalMessage(`确定要删除数据集 ${datasetId} 吗？此操作不可恢复。`)
    setModalType('confirm')
    setConfirmAction(async () => {
      setDeleting(true)
      try {
        const res = await axios.delete(`${API_BASE}/datasets/${datasetId}`)
        setModalMessage('删除成功')
        setModalType('success')
        await fetchDatasets()
      } catch (err: any) {
        if (err.response?.status === 404) {
          setModalMessage('数据集已被删除')
          setModalType('success')
          await fetchDatasets()
        } else {
          setModalMessage(`删除失败: ${err.response?.data?.detail || '未知错误'}`)
          setModalType('error')
        }
      } finally {
        setDeleting(false)
      }
    })
    setShowModal(true)
  }

  const handleCopyDataset = async (datasetId: string) => {
    setCopying(datasetId)
    try {
      const res = await axios.post(`${API_BASE}/datasets/${datasetId}/copy`)
      setModalMessage(`复制成功！新数据集ID: ${res.data.id}`)
      setModalType('success')
      setShowModal(true)
      await fetchDatasets()
    } catch (err: any) {
      setModalMessage(`复制失败: ${err.response?.data?.detail || '未知错误'}`)
      setModalType('error')
      setShowModal(true)
    } finally {
      setCopying(null)
    }
  }

  const handleBatchDelete = async () => {
    if (selectedDatasets.length === 0) {
      alert('请先选择要删除的数据集')
      return
    }

    setModalMessage(`确定要删除选中的 ${selectedDatasets.length} 个数据集吗？此操作不可恢复。`)
    setModalType('confirm')
    setConfirmAction(async () => {
      setDeleting(true)
      try {
        await axios.delete(`${API_BASE}/datasets/batch`, {
          data: { dataset_ids: selectedDatasets }
        })
        setModalMessage(`成功删除 ${selectedDatasets.length} 个数据集`)
        setModalType('success')
        setSelectedDatasets([])
        await fetchDatasets()
      } catch (err: any) {
        if (err.response?.status === 404) {
          setModalMessage('数据集已被删除')
          setModalType('success')
          setSelectedDatasets([])
          await fetchDatasets()
        } else {
          setModalMessage(`删除失败: ${err.response?.data?.detail || '未知错误'}`)
          setModalType('error')
        }
      } finally {
        setDeleting(false)
      }
    })
    setShowModal(true)
  }

  const handleEditDataset = async (datasetId: string) => {
    try {
      const res = await axios.get(`${API_BASE}/datasets/${datasetId}`)
      setEditingDataset(res.data)
      setEditData(res.data.data || [])
    } catch (err: any) {
      alert(`获取数据集详情失败: ${err.response?.data?.detail || '未知错误'}`)
    }
  }

  const handleSaveDataset = async () => {
    if (!editingDataset) return
    try {
      await axios.put(`${API_BASE}/datasets/${editingDataset.id}`, {
        data: editData
      })
      setModalMessage('保存成功')
      setModalType('success')
      setShowModal(true)
      setEditingDataset(null)
      setEditData([])
      await fetchDatasets()
    } catch (err: any) {
      setModalMessage(`保存失败: ${err.response?.data?.detail || '未知错误'}`)
      setModalType('error')
      setShowModal(true)
    }
  }

  // 使用 dateUtils 中的 formatDate 函数

  if (loading) {
    return <div className="card">加载中...</div>
  }

  return (
    <div>
      {/* Custom Modal */}
      {showModal && (
        <div className="modal-overlay">
          <div className="modal-content">
            <h3 style={{ color: modalType === 'success' ? 'var(--edp-green)' : modalType === 'error' ? 'var(--edp-red)' : 'var(--edp-primary)' }}>
              {modalType === 'success' ? '成功' : modalType === 'error' ? '失败' : '确认'}
            </h3>
            <p>{modalMessage}</p>
            {modalType === 'confirm' ? (
              <div style={{ display: 'flex', gap: 10, justifyContent: 'center' }}>
                <button onClick={handleModalClose} style={{ background: 'var(--edp-text2)' }} disabled={deleting}>取消</button>
                <button onClick={handleModalConfirm} disabled={deleting}>确定</button>
              </div>
            ) : (
              <button onClick={handleModalClose} disabled={deleting}>确定</button>
            )}
          </div>
        </div>
      )}
      
      <div className="card">
        <h2 style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
            <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
          </svg>
          评测集管理
        </h2>
        
        <div style={{ marginBottom: 15, display: 'flex', gap: 10, alignItems: 'center' }}>
          <input
            type="text"
            placeholder="搜索数据集 ID..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{
              padding: '8px 12px',
              border: '1px solid var(--edp-border)',
              borderRadius: 'var(--edp-radius-sm)',
              flex: 1,
              maxWidth: 300
            }}
          />
          <button
            className="btn btn-danger"
            onClick={handleBatchDelete}
            disabled={deleting || selectedDatasets.length === 0}
            style={{ padding: '6px 12px', fontSize: 'var(--edp-font-lg)' }}
          >
            {deleting ? (
            <>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite' }}>
                <circle cx="12" cy="12" r="10"/>
                <path d="M12 6v6l4 2"/>
              </svg>
              删除中...
            </>
          ) : (
            <>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              </svg>
              批量删除 ({selectedDatasets.length})
            </>
          )}
          </button>
        </div>

        {filteredDatasets.length === 0 ? (
          <p style={{ color: 'var(--edp-text2)' }}>暂无数据集</p>
        ) : (
          <table className="results-table dataset-table">
            <colgroup>
              <col style={{ width: '48px' }} />
              <col style={{ width: '25%' }} />
              <col style={{ width: '15%' }} />
              <col style={{ width: '22%' }} />
              <col style={{ width: '38%' }} />
            </colgroup>
            <thead>
              <tr>
                <th>
                  <input
                    type="checkbox"
                    checked={selectedDatasets.length === filteredDatasets.length && filteredDatasets.length > 0}
                    onChange={toggleSelectAll}
                    disabled={deleting}
                  />
                </th>
                <th style={{ textAlign: 'center' }}>数据集 ID</th>
                <th style={{ textAlign: 'center' }}>数据条数</th>
                <th style={{ textAlign: 'center' }}>创建时间</th>
                <th style={{ textAlign: 'center' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {filteredDatasets.map(dataset => (
                <tr key={dataset.id}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selectedDatasets.includes(dataset.id)}
                      onChange={() => toggleDatasetSelection(dataset.id)}
                      disabled={deleting}
                    />
                  </td>
                  <td className="cell-ellipsis" title={dataset.id} style={{ textAlign: 'center' }}><code>{dataset.id}</code></td>
                  <td style={{ textAlign: 'center' }}>{dataset.item_count}</td>
                  <td style={{ textAlign: 'center' }}>{formatDate(dataset.created_at)}</td>
                  <td className="cell-actions">
                    <div>
                      <button
                        className="btn btn-primary"
                        onClick={() => handleEditDataset(dataset.id)}
                        disabled={deleting}
                        style={{ padding: '6px' }}
                        title="编辑"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ verticalAlign: 'middle' }}>
                          <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                          <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                        </svg>
                      </button>
                      <button
                        className="btn btn-primary"
                        onClick={() => onDatasetSelect(dataset.id)}
                        disabled={deleting}
                        style={{ padding: '6px' }}
                        title="选择"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ verticalAlign: 'middle' }}>
                          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                          <polyline points="14 2 14 8 20 8"/>
                        </svg>
                      </button>
                      <button
                        className="btn btn-primary"
                        onClick={() => handleCopyDataset(dataset.id)}
                        disabled={deleting || copying === dataset.id}
                        style={{ padding: '6px' }}
                        title="复制"
                      >
                      {copying === dataset.id ? (
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite', verticalAlign: 'middle' }}>
                          <circle cx="12" cy="12" r="10"/>
                          <path d="M12 6v6l4 2"/>
                        </svg>
                      ) : (
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ verticalAlign: 'middle' }}>
                          <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
                          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                        </svg>
                      )}
                      </button>
                      <button
                        className="btn btn-danger"
                        onClick={() => handleDeleteDataset(dataset.id)}
                        disabled={deleting}
                        style={{ padding: '6px' }}
                        title="删除"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ verticalAlign: 'middle' }}>
                          <polyline points="3 6 5 6 21 6"/>
                          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                        </svg>
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {editingDataset && (
        <div className="card" style={{ borderLeft: '4px solid var(--edp-primary)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h3 style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
            </svg>
            编辑数据集
          </h3>
            <button
              className="btn"
              style={{ padding: '4px 12px' }}
              onClick={() => {
                setEditingDataset(null)
                setEditData('')
              }}
            >
              ✕ 关闭
            </button>
          </div>
          <div style={{ marginTop: 10, padding: 10, background: 'var(--edp-primary-soft)', borderRadius: 'var(--edp-radius-sm)' }}>
            <p><strong>数据集 ID：</strong>{editingDataset.id}</p>
            <p><strong>数据条数：</strong>{editingDataset.item_count}</p>
            <p><strong>创建时间：</strong>{formatDate(editingDataset.created_at)}</p>
          </div>
          <div style={{ marginTop: 15 }}>
            <label style={{ display: 'block', marginBottom: 5 }}>
              <strong>数据内容（表格形式）：</strong>
            </label>
            <div style={{ maxHeight: '600px', overflowY: 'auto' }}>
              <table className="results-table">
                <thead>
                  <tr>
                    <th>序号</th>
                    {editData.length > 0 && Object.keys(editData[0]).map((key) => (
                      <th key={key}>{key}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {editData.map((item, rowIndex) => (
                    <tr key={rowIndex}>
                      <td style={{ textAlign: 'center', fontWeight: 'bold' }}>{rowIndex + 1}</td>
                      {Object.entries(item).map(([key, value]) => (
                        <td key={key}>
                          <input
                            type="text"
                            value={String(value)}
                            onChange={(e) => {
                              const newData = [...editData]
                              newData[rowIndex][key] = e.target.value
                              setEditData(newData)
                            }}
                            style={{
                              width: '100%',
                              padding: '6px',
                              border: '1px solid var(--edp-border)',
                              borderRadius: 'var(--edp-radius-sm)',
                              fontSize: 'var(--edp-font-base)'
                            }}
                          />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
          <div style={{ marginTop: 15, display: 'flex', gap: 10 }}>
            <button
              className="btn btn-primary"
              onClick={handleSaveDataset}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
              <polyline points="17 21 17 13 7 13 7 21"/>
              <polyline points="7 3 7 8 15 8"/>
            </svg>
            保存修改
            </button>
            <button
              className="btn"
              onClick={() => {
                setEditingDataset(null)
                setEditData('')
              }}
            >
              取消
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
