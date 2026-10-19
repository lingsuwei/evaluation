import { useState, useEffect, useRef } from 'react'
import axios from 'axios'

const API_BASE = '/api'

interface OntologyPackage {
  name: string
  file_count: number
  created_at: string
  size_bytes: number
}

export function OntologyPackagePage() {
  const [packages, setPackages] = useState<OntologyPackage[]>([])
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)
  const [pkgName, setPkgName] = useState('')
  const [fileInputRef] = useState(useRef<HTMLInputElement>(null))
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [error, setError] = useState('')
  const [deleting, setDeleting] = useState<string | null>(null)

  const fetchPackages = async () => {
    setLoading(true)
    try {
      const res = await axios.get(`${API_BASE}/ontology-packages`)
      setPackages(res.data)
    } catch (err) {
      console.error('获取本体包列表失败:', err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchPackages()
  }, [])

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file && file.name.toLowerCase().endsWith('.zip')) {
      setSelectedFile(file)
      // 自动填充名称（去掉 .zip 后缀）
      if (!pkgName) {
        setPkgName(file.name.replace(/\.zip$/i, ''))
      }
    } else {
      setSelectedFile(null)
      setError('请选择 ZIP 格式的文件')
    }
  }

  const handleUpload = async () => {
    if (!selectedFile) {
      setError('请先选择 ZIP 文件')
      return
    }
    if (!pkgName.trim()) {
      setError('请输入本体包名称')
      return
    }
    setUploading(true)
    setError('')
    try {
      const formData = new FormData()
      formData.append('name', pkgName.trim())
      formData.append('file', selectedFile)
      await axios.post(`${API_BASE}/ontology-packages/upload`, formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      setPkgName('')
      setSelectedFile(null)
      if (fileInputRef.current) {
        fileInputRef.current.value = ''
      }
      fetchPackages()
    } catch (err: any) {
      const msg = err.response?.data?.detail || err.message || '上传失败'
      setError(msg)
    } finally {
      setUploading(false)
    }
  }

  const handleDelete = async (name: string) => {
    if (!confirm(`确认删除本体包「${name}」？此操作不可恢复。`)) return
    setDeleting(name)
    try {
      await axios.delete(`${API_BASE}/ontology-packages/${name}`)
      fetchPackages()
    } catch (err: any) {
      setError(err.response?.data?.detail || '删除失败')
    } finally {
      setDeleting(null)
    }
  }

  const formatSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  return (
    <div style={{ padding: 'var(--edp-space-6)', maxWidth: 900 }}>
      <h2 style={{ marginBottom: 'var(--edp-space-1)', fontSize: 'var(--edp-font-2xl)', fontWeight: 600 }}>本体包管理</h2>
      <p style={{ color: 'var(--edp-text3)', fontSize: 'var(--edp-font-md)', marginBottom: 'var(--edp-space-6)' }}>
        上传 ZIP 格式的本体包（.md 文件），解压后用于 BM25 忠实度评估
      </p>

      {/* 上传区域 */}
      <div style={{
        background: 'var(--edp-bg)',
        border: '1px solid var(--edp-border-soft)',
        borderRadius: 'var(--edp-radius-sm)',
        padding: 'var(--edp-space-5)',
        marginBottom: 'var(--edp-space-6)',
      }}>
        <div style={{ display: 'flex', gap: 'var(--edp-space-3)', alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <div style={{ flex: '1 1 200px' }}>
            <label style={{ display: 'block', fontSize: 'var(--edp-font-base)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-1)' }}>
              本体包名称
            </label>
            <input
              type="text"
              value={pkgName}
              onChange={(e) => setPkgName(e.target.value)}
              placeholder="输入名称（将作为目录名）"
              style={{
                width: '100%',
                padding: '8px 12px',
                border: '1px solid var(--edp-border)',
                borderRadius: 'var(--edp-radius-sm)',
                fontSize: 'var(--edp-font-md)',
                boxSizing: 'border-box',
              }}
            />
          </div>
          <div style={{ flex: '1 1 200px' }}>
            <label style={{ display: 'block', fontSize: 'var(--edp-font-base)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-1)' }}>
              ZIP 文件
            </label>
            <input
              ref={fileInputRef}
              type="file"
              accept=".zip"
              onChange={handleFileChange}
              style={{ fontSize: 'var(--edp-font-md)' }}
            />
          </div>
          <button
            onClick={handleUpload}
            disabled={uploading || !selectedFile || !pkgName.trim()}
            style={{
              padding: '8px 20px',
              background: uploading ? 'var(--edp-border)' : 'var(--edp-blue)',
              color: 'var(--edp-card)',
              border: 'none',
              borderRadius: 'var(--edp-radius-sm)',
              fontSize: 'var(--edp-font-md)',
              fontWeight: 500,
              cursor: uploading ? 'not-allowed' : 'pointer',
            }}
          >
            {uploading ? '上传中...' : '上传'}
          </button>
        </div>
        {error && (
          <div style={{ marginTop: 'var(--edp-space-3)', color: 'var(--edp-red)', fontSize: 'var(--edp-font-md)' }}>{error}</div>
        )}
      </div>

      {/* 列表 */}
      {loading ? (
        <div style={{ textAlign: 'center', padding: 40, color: 'var(--edp-text3)' }}>加载中...</div>
      ) : packages.length === 0 ? (
        <div style={{ textAlign: 'center', padding: 40, color: 'var(--edp-text3)' }}>
          暂无本体包，请上传 ZIP 文件
        </div>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ borderBottom: '2px solid var(--edp-border-soft)' }}>
              <th style={{ textAlign: 'left', padding: '10px 12px', fontSize: 'var(--edp-font-md)', color: 'var(--edp-text2)' }}>名称</th>
              <th style={{ textAlign: 'center', padding: '10px 12px', fontSize: 'var(--edp-font-md)', color: 'var(--edp-text2)' }}>文件数</th>
              <th style={{ textAlign: 'center', padding: '10px 12px', fontSize: 'var(--edp-font-md)', color: 'var(--edp-text2)' }}>大小</th>
              <th style={{ textAlign: 'center', padding: '10px 12px', fontSize: 'var(--edp-font-md)', color: 'var(--edp-text2)' }}>创建时间</th>
              <th style={{ textAlign: 'center', padding: '10px 12px', fontSize: 'var(--edp-font-md)', color: 'var(--edp-text2)' }}>操作</th>
            </tr>
          </thead>
          <tbody>
            {packages.map((pkg) => (
              <tr key={pkg.name} style={{ borderBottom: '1px solid var(--edp-border)' }}>
                <td style={{ padding: '10px 12px', fontSize: 'var(--edp-font-md)', fontWeight: 500 }}>{pkg.name}</td>
                <td style={{ padding: '10px 12px', fontSize: 'var(--edp-font-md)', textAlign: 'center', color: 'var(--edp-text2)' }}>{pkg.file_count}</td>
                <td style={{ padding: '10px 12px', fontSize: 'var(--edp-font-md)', textAlign: 'center', color: 'var(--edp-text2)' }}>{formatSize(pkg.size_bytes)}</td>
                <td style={{ padding: '10px 12px', fontSize: 'var(--edp-font-md)', textAlign: 'center', color: 'var(--edp-text2)' }}>{pkg.created_at}</td>
                <td style={{ padding: '10px 12px', textAlign: 'center' }}>
                  <button
                    onClick={() => handleDelete(pkg.name)}
                    disabled={deleting === pkg.name}
                    style={{
                      padding: '4px 12px',
                      background: deleting === pkg.name ? 'var(--edp-border)' : 'var(--edp-card)',
                      color: deleting === pkg.name ? 'var(--edp-text3)' : 'var(--edp-red)',
                      border: '1px solid var(--edp-red-soft)',
                      borderRadius: 'var(--edp-radius-sm)',
                      fontSize: 'var(--edp-font-base)',
                      cursor: deleting === pkg.name ? 'not-allowed' : 'pointer',
                    }}
                  >
                    {deleting === pkg.name ? '删除中...' : '删除'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
