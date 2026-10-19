import { useState, useRef, useEffect } from 'react'
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
  onUploadSuccess: (datasetId: string) => void
  onEvaluationStart: (taskId: string) => void
  datasetId: string | null
  datasets: Dataset[]
  loadingDatasets: boolean
  onDatasetSelect: (datasetId: string) => void
  agentId: string | null
}

export function UploadPage({ onUploadSuccess, onEvaluationStart, datasetId, datasets, loadingDatasets, onDatasetSelect, agentId }: Props) {
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<any[] | null>(null)
  const [uploading, setUploading] = useState(false)
  // 防止重复提交评测（页面卡顿或双击导致多次发起）
  const [startingEvaluation, setStartingEvaluation] = useState(false)
  const startingRef = useRef(false)
  const [error, setError] = useState<string | null>(null)

  const [datasetDetail, setDatasetDetail] = useState<DatasetDetail | null>(null)
  const [loadingDataset, setLoadingDataset] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const fetchDatasetDetail = async () => {
      if (!datasetId) {
        setDatasetDetail(null)
        return
      }

      setLoadingDataset(true)
      try {
        const res = await axios.get(`${API_BASE}/datasets/${datasetId}`)
        setDatasetDetail(res.data)
      } catch (err: any) {
        console.error('获取数据集详情失败:', err)
        setError(err.response?.data?.detail || '获取数据集详情失败')
      } finally {
        setLoadingDataset(false)
      }
    }

    fetchDatasetDetail()
  }, [datasetId])

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]
    if (!f) return

    setFile(f)
    setError(null)

    if (f.name.toLowerCase().endsWith('.csv')) {
      setPreview(null)
      return
    }
    try {
      const text = await f.text()
      const data = JSON.parse(text)
      setPreview(data.slice(0, 3))
    } catch {
      setError('无效的 JSON 文件')
      setPreview(null)
    }
  }

  const handleUpload = async () => {
    if (!file) return

    setUploading(true)
    setError(null)

    try {
      const formData = new FormData()
      formData.append('file', file)

      const res = await axios.post(`${API_BASE}/upload`, formData)
      onUploadSuccess(res.data.dataset_id)
    } catch (err: any) {
      setError(err.response?.data?.error || '上传失败')
    } finally {
      setUploading(false)
    }
  }

  const handleStartEvaluation = async () => {
    if (!datasetId) return

    // 防止重复提交：使用 ref 作为同步锁，避免 setState 异步窗口内双击穿透
    if (startingRef.current) return
    startingRef.current = true
    setStartingEvaluation(true)

    try {
      const res = await axios.post(`${API_BASE}/evaluate`, {
        dataset_id: datasetId,
        agent_id: agentId
      })
      onEvaluationStart(res.data.task_uuid)
    } catch (err: any) {
      setError(err.response?.data?.detail || '启动评估失败')
    } finally {
      startingRef.current = false
      setStartingEvaluation(false)
    }
  }

  return (
    <div>
      <div className="card">
        <h2 style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)' }}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="17 8 12 3 7 8"/>
            <line x1="12" y1="3" x2="12" y2="15"/>
          </svg>
          上传数据集
        </h2>
        <p className="ucd-muted" style={{ marginBottom: 'var(--edp-space-4)' }}>
          上传包含问题和模型回答的 JSON 或 CSV 文件
        </p>

        <input
          ref={fileRef}
          type="file"
          accept=".json,.csv"
          onChange={handleFileChange}
          className="file-input"
        />

        {error && (
          <div style={{ color: 'var(--edp-red)', margin: 'var(--edp-space-3) 0', display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)' }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10"/>
              <line x1="15" y1="9" x2="9" y2="15"/>
              <line x1="9" y1="9" x2="15" y2="15"/>
            </svg>
            {error}
          </div>
        )}

        {preview && (
          <div style={{ margin: 'var(--edp-space-4) 0' }}>
            <h4>预览（显示前 {preview.length} 条）：</h4>
            <pre style={{
              background: 'var(--edp-bg)',
              padding: 'var(--edp-space-3)',
              borderRadius: 'var(--edp-radius-sm)',
              overflow: 'auto',
              maxHeight: 200,
              fontSize: 'var(--edp-font-base)'
            }}>
              {JSON.stringify(preview, null, 2)}
            </pre>
          </div>
        )}

        <button
          className="btn btn-primary"
          onClick={handleUpload}
          disabled={!file || uploading}
        >
          {uploading ? (
                <>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite' }}>
                    <circle cx="12" cy="12" r="10"/>
                    <path d="M12 6v6l4 2"/>
                  </svg>
                  上传中...
                </>
              ) : (
                <>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                    <polyline points="17 8 12 3 7 8"/>
                    <line x1="12" y1="3" x2="12" y2="15"/>
                  </svg>
                  上传数据集
                </>
              )}
        </button>
      </div>

      <div className="card">
        <h2 style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)' }}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
                <line x1="16" y1="13" x2="8" y2="13"/>
                <line x1="16" y1="17" x2="8" y2="17"/>
              </svg>
              选择已有数据集
            </h2>
        <p className="ucd-muted" style={{ marginBottom: 'var(--edp-space-4)' }}>
          从已上传的数据集中选择一个进行二次评估
        </p>

        {loadingDatasets ? (
          <div className="ucd-muted" style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)' }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite' }}>
              <circle cx="12" cy="12" r="10"/>
              <path d="M12 6v6l4 2"/>
            </svg>
            加载数据集中...
          </div>
        ) : datasets.length === 0 ? (
          <div className="ucd-muted">暂无已上传的数据集</div>
        ) : (
          <div style={{ marginBottom: 'var(--edp-space-4)' }}>
            <select
              value={datasetId || ''}
              onChange={(e) => onDatasetSelect(e.target.value)}
              style={{
                padding: 'var(--edp-space-2) var(--edp-space-3)',
                border: '1px solid var(--edp-border)',
                borderRadius: 'var(--edp-radius-sm)',
                width: '100%',
                maxWidth: 400
              }}
            >
              <option value="">-- 选择数据集 --</option>
              {datasets.map(dataset => (
                <option key={dataset.id} value={dataset.id}>
                  {dataset.id} - {dataset.item_count} 条数据 - {formatDate(dataset.created_at)}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      {datasetId && (
        <div className="card">
          <h2 style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)' }}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
              </svg>
              开始评估
            </h2>
          <p className="ucd-muted" style={{ marginBottom: 'var(--edp-space-4)' }}>
            数据集 ID：<code>{datasetId}</code>
          </p>

          {agentId && (
            <div style={{ marginBottom: 'var(--edp-space-4)', padding: 'var(--edp-space-3)', background: 'var(--edp-primary-soft)', borderRadius: 'var(--edp-radius-sm)' }}>
              <p style={{ margin: 0 }}>
                <strong>当前选择的 Agent：</strong>
                <code style={{ marginLeft: 'var(--edp-space-3)' }}>{agentId}</code>
              </p>
            </div>
          )}

          <button
            className="btn btn-primary"
            onClick={handleStartEvaluation}
            disabled={startingEvaluation}
          >
            {startingEvaluation ? (
              <>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite' }}>
                  <circle cx="12" cy="12" r="10"/>
                  <path d="M12 6v6l4 2"/>
                </svg>
                启动中...
              </>
            ) : (
              <>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <polygon points="5 3 19 12 5 21 5 3"/>
                </svg>
                开始评估
              </>
            )}
          </button>
        </div>
      )}

      {datasetId && datasetDetail && (
        <div className="card">
          <h2 style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)' }}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
                <line x1="16" y1="13" x2="8" y2="13"/>
                <line x1="16" y1="17" x2="8" y2="17"/>
              </svg>
              数据集内容
            </h2>
          <p className="ucd-muted" style={{ marginBottom: 'var(--edp-space-4)' }}>
            共 {datasetDetail.item_count} 条数据
          </p>

          {loadingDataset ? (
            <div className="ucd-muted" style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)' }}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite' }}>
                <circle cx="12" cy="12" r="10"/>
                <path d="M12 6v6l4 2"/>
              </svg>
              加载数据集内容中...
            </div>
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table className="results-table">
                <thead>
                  <tr>
                    <th style={{ width: '60px' }}>序号</th>
                    <th style={{ width: '300px' }}>问题</th>
                    <th style={{ width: '300px' }}>模型答案</th>
                    <th style={{ width: '300px' }}>上下文</th>
                    <th style={{ width: '300px' }}>真实答案</th>
                  </tr>
                </thead>
                <tbody>
                  {datasetDetail.data.map((item, index) => (
                    <tr key={index}>
                      <td>{index + 1}</td>
                      <td>
                        <div style={{ maxWidth: '300px', wordWrap: 'break-word' }}>
                          {item.question || item.q || '-'}
                        </div>
                      </td>
                      <td>
                        <div style={{ maxWidth: '300px', wordWrap: 'break-word' }}>
                          {item.answer || item.a || item.response || item.model_answer || '-'}
                        </div>
                      </td>
                      <td>
                        <div style={{ maxWidth: '300px', wordWrap: 'break-word' }}>
                          {item.context || '-'}
                        </div>
                      </td>
                      <td>
                        <div style={{ maxWidth: '300px', wordWrap: 'break-word' }}>
                          {item.ground_truth || item.gt || item.true_answer || item.reference || '-'}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
