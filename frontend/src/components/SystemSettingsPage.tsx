import { useState, useEffect } from 'react'
import axios from 'axios'

const API_BASE = '/api'

interface LLMModel {
  id: string
  name: string
  provider: string | null
  base_url: string
  model: string
  api_key: string | null
  is_default: boolean
  max_tokens: number | null
  temperature: number | null
  timeout: number | null
  created_at: string | null
  updated_at: string | null
}

export function SystemSettingsPage({ initialTab }: { initialTab?: 'llm' | 'langfuse' }) {
  if (initialTab === 'langfuse') {
    return <LangfuseProjectConfigTab />
  }
  return <LLMConfigTab />
}

function LLMConfigTab() {
  const [models, setModels] = useState<LLMModel[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [editingModel, setEditingModel] = useState<LLMModel | null>(null)
  const [testing, setTesting] = useState<string | null>(null)
  const [testResult, setTestResult] = useState<{ id: string; success: boolean; message: string } | null>(null)
  const [showApiKey, setShowApiKey] = useState(false)
  // 模型卡片中各模型的 API Key 明文缓存
  const [revealedKeys, setRevealedKeys] = useState<Record<string, string>>({})
  const [revealingKey, setRevealingKey] = useState<string | null>(null)

  // 表单状态
  const [formData, setFormData] = useState({
    id: '',
    name: '',
    provider: '',
    base_url: '',
    model: '',
    api_key: '',
    is_default: false,
    max_tokens: 4096 as number | null,
    temperature: 0.0 as number | null,
    timeout: 180.0 as number | null,
  })

  const fetchModels = async () => {
    try {
      const res = await axios.get(`${API_BASE}/llm-models`)
      setModels(res.data)
    } catch (err) {
      console.error('获取LLM模型列表失败:', err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchModels()
  }, [])

  const resetForm = () => {
    setFormData({ id: '', name: '', provider: '', base_url: '', model: '', api_key: '', is_default: false, max_tokens: 4096, temperature: 0.0, timeout: 180.0 })
    setEditingModel(null)
    setShowForm(false)
  }

  const handleEdit = async (model: LLMModel) => {
    setEditingModel(model)
    setShowApiKey(false)
    // 从后端获取明文 API Key
    let plainKey = ''
    try {
      const res = await axios.get(`${API_BASE}/llm-models/${model.id}/api-key`)
      plainKey = res.data.api_key || ''
    } catch {
      plainKey = ''
    }
    setFormData({
      id: model.id,
      name: model.name,
      provider: model.provider || '',
      base_url: model.base_url,
      model: model.model,
      api_key: plainKey,
      is_default: model.is_default,
      max_tokens: model.max_tokens ?? 4096,
      temperature: model.temperature ?? 0.0,
      timeout: model.timeout ?? 180.0,
    })
    setShowForm(true)
  }

  const handleCreate = () => {
    resetForm()
    setShowForm(true)
  }

  const handleSubmit = async () => {
    try {
      if (editingModel) {
        // 更新
        const updateData: any = {
          name: formData.name,
          provider: formData.provider || null,
          base_url: formData.base_url,
          model: formData.model,
          api_key: formData.api_key || null,
          is_default: formData.is_default,
          max_tokens: formData.max_tokens,
          temperature: formData.temperature,
          timeout: formData.timeout,
        }
        await axios.put(`${API_BASE}/llm-models/${editingModel.id}`, updateData)
      } else {
        // 创建
        await axios.post(`${API_BASE}/llm-models`, {
          id: formData.id,
          name: formData.name,
          provider: formData.provider || null,
          base_url: formData.base_url,
          model: formData.model,
          api_key: formData.api_key || null,
          is_default: formData.is_default,
          max_tokens: formData.max_tokens,
          temperature: formData.temperature,
          timeout: formData.timeout,
        })
      }
      resetForm()
      fetchModels()
    } catch (err: any) {
      alert(err.response?.data?.detail || '操作失败')
    }
  }

  const handleDelete = async (modelId: string) => {
    if (!confirm(`确定要删除模型 '${modelId}' 吗？`)) return
    try {
      await axios.delete(`${API_BASE}/llm-models/${modelId}`)
      fetchModels()
    } catch (err: any) {
      alert(err.response?.data?.detail || '删除失败')
    }
  }

  const handleTest = async (modelId: string) => {
    setTesting(modelId)
    setTestResult(null)
    try {
      const res = await axios.post(`${API_BASE}/llm-models/${modelId}/test`)
      setTestResult({ id: modelId, success: res.data.success, message: res.data.message })
    } catch (err: any) {
      setTestResult({ id: modelId, success: false, message: err.response?.data?.detail || '测试失败' })
    } finally {
      setTesting(null)
    }
  }

  const handleSetDefault = async (modelId: string) => {
    try {
      await axios.put(`${API_BASE}/llm-models/${modelId}`, { is_default: true })
      fetchModels()
    } catch (err: any) {
      alert(err.response?.data?.detail || '设置默认模型失败')
    }
  }

  // 常见厂商预设
  const providerPresets: Record<string, { base_url: string; model: string }> = {
    '智谱AI': { base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4' },
    'OpenAI': { base_url: 'https://api.openai.com/v1', model: 'gpt-4' },
    'DeepSeek': { base_url: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
    '月之暗面': { base_url: 'https://api.moonshot.cn/v1', model: 'moonshot-v1-8k' },
    '百度文心': { base_url: 'https://aip.baidubce.com/rpc/2.0/ai_custom/v1/wenxinworkshop', model: 'ernie-bot-4' },
    '阿里通义': { base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-turbo' },
  }

  const handleProviderPreset = (provider: string) => {
    const preset = providerPresets[provider]
    if (preset) {
      setFormData(prev => ({
        ...prev,
        provider,
        base_url: preset.base_url,
        model: preset.model
      }))
    }
  }

  if (loading) {
    return <div className="card">加载中...</div>
  }

  return (
    <div className="card">
      {/* 头部 */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--edp-space-5)' }}>
        <h2 style={{ display: 'flex', alignItems: 'center', gap: '8px', margin: 0 }}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="3"/>
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
          </svg>
          模型配置
        </h2>
        <button
          onClick={handleCreate}
          style={{
            padding: '8px 16px',
            background: 'var(--edp-primary)',
            color: 'var(--edp-card)',
            border: 'none',
            borderRadius: 'var(--edp-radius-sm)',
            cursor: 'pointer',
            fontSize: 'var(--edp-font-md)',
            fontWeight: 500,
            display: 'flex',
            alignItems: 'center',
            gap: 'var(--edp-space-1)'
          }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          新增模型
        </button>
      </div>

      {/* 新增/编辑表单 */}
      {showForm && (
        <div style={{
          background: 'var(--edp-bg)',
          border: '1px solid var(--edp-border)',
          borderRadius: 'var(--edp-radius-sm)',
          padding: 'var(--edp-space-5)',
          marginBottom: 'var(--edp-space-5)'
        }}>
          <h4 style={{ margin: '0 0 16px 0', color: 'var(--edp-text)' }}>
            {editingModel ? '编辑模型配置' : '新增模型配置'}
          </h4>
          
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--edp-space-4)' }}>
            {/* 模型标识 */}
            {!editingModel && (
              <div>
                <label style={{ display: 'block', fontSize: 'var(--edp-font-md)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-1)' }}>
                  模型标识 <span style={{ color: 'var(--edp-red)' }}>*</span>
                </label>
                <input
                  type="text"
                  value={formData.id}
                  onChange={e => setFormData(prev => ({ ...prev, id: e.target.value }))}
                  placeholder="如: glm, gpt4, deepseek"
                  style={inputStyle}
                />
              </div>
            )}

            {/* 显示名称 */}
            <div>
              <label style={{ display: 'block', fontSize: 'var(--edp-font-md)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-1)' }}>
                显示名称 <span style={{ color: 'var(--edp-red)' }}>*</span>
              </label>
              <input
                type="text"
                value={formData.name}
                onChange={e => setFormData(prev => ({ ...prev, name: e.target.value }))}
                placeholder="如: 智谱GLM-4"
                style={inputStyle}
              />
            </div>

            {/* 模型厂商 - 带快捷选择 */}
            <div>
              <label style={{ display: 'block', fontSize: 'var(--edp-font-md)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-1)' }}>
                模型厂商
              </label>
              <input
                type="text"
                value={formData.provider}
                onChange={e => setFormData(prev => ({ ...prev, provider: e.target.value }))}
                placeholder="如: 智谱AI"
                style={inputStyle}
              />
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--edp-space-1)', marginTop: 'var(--edp-space-1)' }}>
                {Object.keys(providerPresets).map(p => (
                  <button
                    key={p}
                    onClick={() => handleProviderPreset(p)}
                    style={{
                      padding: '2px 8px',
                      fontSize: 'var(--edp-font-sm)',
                      border: '1px solid var(--edp-border)',
                      borderRadius: 'var(--edp-radius-sm)',
                      background: formData.provider === p ? 'var(--edp-primary)' : 'var(--edp-card)',
                      color: formData.provider === p ? 'var(--edp-card)' : 'var(--edp-text2)',
                      cursor: 'pointer'
                    }}
                  >
                    {p}
                  </button>
                ))}
              </div>
            </div>

            {/* 模型名称 */}
            <div>
              <label style={{ display: 'block', fontSize: 'var(--edp-font-md)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-1)' }}>
                模型名称 <span style={{ color: 'var(--edp-red)' }}>*</span>
              </label>
              <input
                type="text"
                value={formData.model}
                onChange={e => setFormData(prev => ({ ...prev, model: e.target.value }))}
                placeholder="如: glm-4, gpt-4, deepseek-chat"
                style={inputStyle}
              />
            </div>

            {/* 基础URL */}
            <div style={{ gridColumn: '1 / -1' }}>
              <label style={{ display: 'block', fontSize: 'var(--edp-font-md)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-1)' }}>
                基础URL (Base URL) <span style={{ color: 'var(--edp-red)' }}>*</span>
              </label>
              <input
                type="text"
                value={formData.base_url}
                onChange={e => setFormData(prev => ({ ...prev, base_url: e.target.value }))}
                placeholder="如: https://open.bigmodel.cn/api/paas/v4"
                style={{ ...inputStyle, width: '100%' }}
              />
            </div>

            {/* API Key */}
            <div style={{ gridColumn: '1 / -1' }}>
              <label style={{ display: 'block', fontSize: 'var(--edp-font-md)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-1)' }}>
                API Key
              </label>
              <div style={{ position: 'relative' }}>
                <input
                  type={showApiKey ? 'text' : 'password'}
                  value={formData.api_key}
                  onChange={e => setFormData(prev => ({ ...prev, api_key: e.target.value }))}
                  placeholder="输入API Key"
                  style={{ ...inputStyle, width: '100%', paddingRight: 36 }}
                />
                <button
                  type="button"
                  onClick={() => setShowApiKey(!showApiKey)}
                  style={{
                    position: 'absolute',
                    right: 8,
                    top: '50%',
                    transform: 'translateY(-50%)',
                    background: 'none',
                    border: 'none',
                    cursor: 'pointer',
                    padding: 2,
                    color: 'var(--edp-text3)',
                    display: 'flex',
                    alignItems: 'center'
                  }}
                  title={showApiKey ? '隐藏' : '显示'}
                >
                  {showApiKey ? (
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
                  ) : (
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                  )}
                </button>
              </div>
            </div>

            {/* 默认模型 */}
            <div style={{ gridColumn: '1 / -1' }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)', cursor: 'pointer' }}>
                <input
                  type="checkbox"
                  checked={formData.is_default}
                  onChange={e => setFormData(prev => ({ ...prev, is_default: e.target.checked }))}
                />
                <span style={{ fontSize: 'var(--edp-font-md)', color: 'var(--edp-text2)' }}>设为默认评估模型</span>
              </label>
            </div>

            {/* 高级参数：max_tokens / temperature / timeout */}
            <div style={{ gridColumn: '1 / -1', marginTop: 'var(--edp-space-2)', paddingTop: 12, borderTop: '1px solid var(--edp-border)' }}>
              <label style={{ display: 'block', fontSize: 'var(--edp-font-md)', fontWeight: 600, color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-2)' }}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ marginRight: 'var(--edp-space-1)', verticalAlign: 'middle' }}>
                  <path d="M12 15l-2-2h4l-2 2z"/><path d="M12 3v4m0 4v4m0 4v4"/>
                </svg>
                LLM 调用参数
              </label>
              <div style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text3)', marginBottom: 'var(--edp-space-2)' }}>
                控制评测 LLM 的输出行为；调整后可在评测时临时覆盖。
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 'var(--edp-space-3)' }}>
                <div>
                  <label style={{ display: 'block', fontSize: 'var(--edp-font-base)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 3 }}>
                    max_tokens <span style={{ color: 'var(--edp-text3)', fontWeight: 400 }}>(最大输出tokens)</span>
                  </label>
                  <input
                    type="number"
                    value={formData.max_tokens ?? 4096}
                    onChange={e => {
                      const v = parseInt(e.target.value)
                      setFormData(prev => ({ ...prev, max_tokens: v >= 1 ? v : prev.max_tokens }))
                    }}
                    min={1}
                    step={256}
                    placeholder="4096"
                    style={inputStyle}
                  />
                  <div style={{ fontSize: 'var(--edp-font-sm)', color: 'var(--edp-text3)', marginTop: 2 }}>最小值 1，默认 4096</div>
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: 'var(--edp-font-base)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 3 }}>
                    temperature <span style={{ color: 'var(--edp-text3)', fontWeight: 400 }}>(温度)</span>
                  </label>
                  <input
                    type="number"
                    value={formData.temperature ?? 0.0}
                    onChange={e => {
                      const v = parseFloat(e.target.value)
                      setFormData(prev => ({ ...prev, temperature: v >= 0 && v <= 1 ? v : prev.temperature }))
                    }}
                    min={0}
                    max={1}
                    step={0.1}
                    placeholder="0"
                    style={inputStyle}
                  />
                  <div style={{ fontSize: 'var(--edp-font-sm)', color: 'var(--edp-text3)', marginTop: 2 }}>范围 0~1，默认 0（确定性输出）</div>
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: 'var(--edp-font-base)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 3 }}>
                    timeout <span style={{ color: 'var(--edp-text3)', fontWeight: 400 }}>(超时/秒)</span>
                  </label>
                  <input
                    type="number"
                    value={formData.timeout ?? 180}
                    onChange={e => {
                      const v = parseInt(e.target.value)
                      setFormData(prev => ({ ...prev, timeout: v >= 30 && v <= 600 ? v : prev.timeout }))
                    }}
                    min={30}
                    max={600}
                    step={30}
                    placeholder="180"
                    style={inputStyle}
                  />
                  <div style={{ fontSize: 'var(--edp-font-sm)', color: 'var(--edp-text3)', marginTop: 2 }}>范围 30~600秒，默认 180</div>
                </div>
              </div>
            </div>
          </div>

          <div style={{ display: 'flex', gap: 'var(--edp-space-2)', marginTop: 'var(--edp-space-4)', justifyContent: 'flex-end' }}>
            <button onClick={resetForm} style={cancelBtnStyle}>取消</button>
            <button
              onClick={handleSubmit}
              disabled={!formData.id && !editingModel || !formData.name || !formData.base_url || !formData.model}
              style={{
                ...submitBtnStyle,
                opacity: (!formData.id && !editingModel) || !formData.name || !formData.base_url || !formData.model ? 0.5 : 1
              }}
            >
              {editingModel ? '保存修改' : '创建'}
            </button>
          </div>
        </div>
      )}

      {/* 模型列表 */}
      {models.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '40px 0', color: 'var(--edp-text3)' }}>
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--edp-border)" strokeWidth="1.5" style={{ marginBottom: 'var(--edp-space-3)' }}>
            <circle cx="12" cy="12" r="10"/>
            <line x1="12" y1="8" x2="12" y2="12"/>
            <line x1="12" y1="16" x2="12.01" y2="16"/>
          </svg>
          <p>暂无LLM模型配置</p>
          <p style={{ fontSize: 'var(--edp-font-base)' }}>点击"新增模型"按钮添加模型配置</p>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--edp-space-3)' }}>
          {models.map(m => (
            <div key={m.id} style={{
              border: '1px solid var(--edp-border-soft)',
              borderRadius: 'var(--edp-radius-sm)',
              padding: 'var(--edp-space-4)',
              position: 'relative',
              borderColor: m.is_default ? 'var(--edp-primary)' : 'var(--edp-border-soft)',
              background: m.is_default ? 'var(--edp-bg)' : 'var(--edp-card)'
            }}>
              {m.is_default && (
                <span style={{
                  position: 'absolute',
                  top: -8,
                  right: 12,
                  background: 'var(--edp-primary)',
                  color: 'var(--edp-card)',
                  padding: '2px 8px',
                  borderRadius: 'var(--edp-radius-sm)',
                  fontSize: 'var(--edp-font-sm)',
                  fontWeight: 500
                }}>默认</span>
              )}
              
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                <div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)', marginBottom: 'var(--edp-space-2)' }}>
                    <span style={{ fontSize: 16, fontWeight: 600, color: 'var(--edp-text)' }}>{m.name}</span>
                    <code style={{
                      background: 'var(--edp-border)',
                      padding: '2px 6px',
                      borderRadius: 'var(--edp-radius-sm)',
                      fontSize: 'var(--edp-font-base)',
                      color: 'var(--edp-text2)'
                    }}>{m.id}</code>
                    {m.provider && (
                      <span style={{
                        background: 'var(--edp-blue-soft)',
                        color: 'var(--edp-blue)',
                        padding: '2px 8px',
                        borderRadius: 'var(--edp-radius-sm)',
                        fontSize: 'var(--edp-font-sm)',
                        fontWeight: 500
                      }}>{m.provider}</span>
                    )}
                  </div>
                  <div style={{ fontSize: 'var(--edp-font-md)', color: 'var(--edp-text2)', lineHeight: 1.8 }}>
                    <div><span style={{ color: 'var(--edp-text3)', width: 70, display: 'inline-block' }}>基础URL:</span> {m.base_url}</div>
                    <div><span style={{ color: 'var(--edp-text3)', width: 70, display: 'inline-block' }}>模型名称:</span> {m.model}</div>
                    <div><span style={{ color: 'var(--edp-text3)', width: 70, display: 'inline-block' }}>max_tokens:</span> {m.max_tokens ?? 4096}</div>
                    <div><span style={{ color: 'var(--edp-text3)', width: 70, display: 'inline-block' }}>temperature:</span> {m.temperature ?? 0}</div>
                    <div><span style={{ color: 'var(--edp-text3)', width: 70, display: 'inline-block' }}>timeout:</span> {m.timeout ?? 180}s</div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-1)' }}>
                      <span style={{ color: 'var(--edp-text3)', width: 70, display: 'inline-block' }}>API Key:</span>
                      <span style={{ fontFamily: 'monospace' }}>
                        {revealedKeys[m.id] !== undefined
                          ? (revealedKeys[m.id] || '未配置')
                          : (m.api_key || '未配置')}
                      </span>
                      <button
                        onClick={async () => {
                          if (revealedKeys[m.id] !== undefined) {
                            // 已显示，点击隐藏
                            setRevealedKeys(prev => { const next = { ...prev }; delete next[m.id]; return next })
                          } else {
                            // 未显示，点击获取明文
                            setRevealingKey(m.id)
                            try {
                              const res = await axios.get(`${API_BASE}/llm-models/${m.id}/api-key`)
                              setRevealedKeys(prev => ({ ...prev, [m.id]: res.data.api_key || '' }))
                            } catch {
                              setRevealedKeys(prev => ({ ...prev, [m.id]: m.api_key || '' }))
                            } finally {
                              setRevealingKey(null)
                            }
                          }
                        }}
                        disabled={revealingKey === m.id}
                        style={{
                          background: 'none',
                          border: 'none',
                          cursor: revealingKey === m.id ? 'wait' : 'pointer',
                          padding: 2,
                          color: 'var(--edp-text3)',
                          display: 'flex',
                          alignItems: 'center'
                        }}
                        title={revealedKeys[m.id] !== undefined ? '隐藏' : '显示明文'}
                      >
                        {revealingKey === m.id ? (
                          <span style={{ fontSize: 'var(--edp-font-sm)', color: 'var(--edp-text3)' }}>...</span>
                        ) : revealedKeys[m.id] !== undefined ? (
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
                        ) : (
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                        )}
                      </button>
                    </div>
                  </div>
                </div>
                
                <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  {!m.is_default && (
                    <button
                      onClick={() => handleSetDefault(m.id)}
                      style={smallBtnStyle}
                      title="设为默认"
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
                    </button>
                  )}
                  <button
                    onClick={() => handleTest(m.id)}
                    disabled={testing === m.id}
                    style={{
                      ...smallBtnStyle,
                      opacity: testing === m.id ? 0.5 : 1,
                      color: 'var(--edp-blue)',
                      borderColor: 'var(--edp-blue)'
                    }}
                    title="测试连接"
                  >
                    {testing === m.id ? '...' : (
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
                    )}
                  </button>
                  <button onClick={() => handleEdit(m)} style={smallBtnStyle} title="编辑">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                  </button>
                  <button onClick={() => handleDelete(m.id)} style={{ ...smallBtnStyle, color: 'var(--edp-red)', borderColor: 'var(--edp-red)' }} title="删除">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                  </button>
                </div>
              </div>

              {/* 测试结果 */}
              {testResult && testResult.id === m.id && (
                <div style={{
                  marginTop: 10,
                  padding: '8px 12px',
                  borderRadius: 'var(--edp-radius-sm)',
                  fontSize: 'var(--edp-font-md)',
                  background: testResult.success ? 'var(--edp-green-soft)' : 'var(--edp-amber-soft)',
                  color: testResult.success ? 'var(--edp-green)' : 'var(--edp-red)',
                  border: `1px solid ${testResult.success ? 'var(--edp-green-soft)' : 'var(--edp-red-soft)'}`
                }}>
                  {testResult.message}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}


interface LangfuseProject {
  id: string
  name: string
  host: string | null
  organization: string | null
  langfuse_project_id: string | null
  public_key: string
}

function LangfuseProjectConfigTab() {
  const [projects, setProjects] = useState<LangfuseProject[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [showSecretKey, setShowSecretKey] = useState(false)
  const [configFilePath, setConfigFilePath] = useState('')

  const [formData, setFormData] = useState({
    id: '',
    name: '',
    host: '',
    organization: '',
    langfuse_project_id: '',
    public_key: '',
    secret_key: '',
  })

  const fetchProjects = async () => {
    try {
      const res = await axios.get(`${API_BASE}/langfuse/projects`)
      setProjects(res.data.projects || [])
    } catch (err) {
      console.error('获取 Langfuse 项目列表失败:', err)
    } finally {
      setLoading(false)
    }
  }

  const fetchConfigPath = async () => {
    try {
      const res = await axios.get(`${API_BASE}/langfuse/projects/config-file-path`)
      setConfigFilePath(res.data.path || '')
    } catch {
      setConfigFilePath('')
    }
  }

  useEffect(() => {
    fetchProjects()
    fetchConfigPath()
  }, [])

  const resetForm = () => {
    setFormData({ id: '', name: '', host: '', organization: '', langfuse_project_id: '', public_key: '', secret_key: '' })
    setEditingId(null)
    setShowForm(false)
    setShowSecretKey(false)
  }

  const handleEdit = (project: LangfuseProject) => {
    setEditingId(project.id)
    setShowSecretKey(false)
    setFormData({
      id: project.id,
      name: project.name,
      host: project.host || '',
      organization: project.organization || '',
      langfuse_project_id: project.langfuse_project_id || '',
      // public_key 按 Langfuse 设计本就公开，回填原值便于核对/编辑
      public_key: project.public_key || '',
      secret_key: '',
    })
    setShowForm(true)
  }

  const generateId = (org: string, projectId: string) => {
    const base = `${org}-${projectId}`.toLowerCase()
    return base.replace(/[^a-z0-9_-]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '') || 'project'
  }

  const handleCreate = () => {
    resetForm()
    setShowForm(true)
  }

  const handleSubmit = async () => {
    try {
      if (!editingId) {
        let id = formData.id.trim()
        if (!id) {
          id = generateId(formData.organization || 'default', formData.langfuse_project_id || 'project')
        }
        if (id.includes('/')) {
          alert('项目标识不能包含 "/" 等特殊字符')
          return
        }
        if (!formData.public_key || !formData.secret_key) {
          alert('Public Key 和 Secret Key 不能为空')
          return
        }
        await axios.post(`${API_BASE}/langfuse/projects`, {
          id,
          name: formData.name,
          host: formData.host || null,
          organization: formData.organization || null,
          langfuse_project_id: formData.langfuse_project_id || null,
          public_key: formData.public_key,
          secret_key: formData.secret_key,
        })
      } else {
        const updateData: any = { name: formData.name, host: formData.host || null, organization: formData.organization || null, langfuse_project_id: formData.langfuse_project_id || null }
        if (formData.public_key) updateData.public_key = formData.public_key
        if (formData.secret_key) updateData.secret_key = formData.secret_key
        await axios.put(`${API_BASE}/langfuse/projects/${editingId}`, updateData)
      }
      resetForm()
      fetchProjects()
    } catch (err: any) {
      alert(err.response?.data?.detail || '操作失败')
    }
  }

  const handleDelete = async (projectId: string) => {
    if (!confirm(`确定要删除项目 '${projectId}' 吗？`)) return
    try {
      await axios.delete(`${API_BASE}/langfuse/projects/${projectId}`)
      fetchProjects()
    } catch (err: any) {
      alert(err.response?.data?.detail || '删除失败')
    }
  }

  const groupedProjects = projects.reduce((acc, p) => {
    const org = p.organization || '未分组'
    if (!acc[org]) acc[org] = []
    acc[org].push(p)
    return acc
  }, {} as Record<string, LangfuseProject[]>)

  if (loading) {
    return <div className="card">加载中...</div>
  }

  return (
    <div className="card">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--edp-space-5)' }}>
        <h2 style={{ display: 'flex', alignItems: 'center', gap: '8px', margin: 0 }}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <rect x="3" y="3" width="7" height="7"/>
            <rect x="14" y="3" width="7" height="7"/>
            <rect x="14" y="14" width="7" height="7"/>
            <rect x="3" y="14" width="7" height="7"/>
          </svg>
          Langfuse 项目配置
        </h2>
        <button
          onClick={handleCreate}
          style={{
            padding: '8px 16px',
            background: 'var(--edp-primary)',
            color: 'var(--edp-card)',
            border: 'none',
            borderRadius: 'var(--edp-radius-sm)',
            cursor: 'pointer',
            fontSize: 'var(--edp-font-md)',
            fontWeight: 500,
            display: 'flex',
            alignItems: 'center',
            gap: 'var(--edp-space-1)'
          }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          新增项目
        </button>
      </div>

      {configFilePath && (
        <div style={{ marginBottom: 'var(--edp-space-4)', padding: '8px 12px', background: 'var(--edp-primary-soft)', borderRadius: 'var(--edp-radius-sm)', fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)' }}>
          配置文件: <code style={{ background: 'var(--edp-border)', padding: '2px 6px', borderRadius: 3 }}>{configFilePath}</code>
        </div>
      )}

      {showForm && (
        <div style={{
          background: 'var(--edp-bg)',
          border: '1px solid var(--edp-border)',
          borderRadius: 'var(--edp-radius-sm)',
          padding: 'var(--edp-space-5)',
          marginBottom: 'var(--edp-space-5)'
        }}>
          <h4 style={{ margin: '0 0 16px 0', color: 'var(--edp-text)' }}>
            {editingId ? '编辑项目配置' : '新增项目配置'}
          </h4>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--edp-space-4)' }}>
            <div>
              <label style={{ display: 'block', fontSize: 'var(--edp-font-md)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-1)' }}>
                显示名称 <span style={{ color: 'var(--edp-red)' }}>*</span>
              </label>
              <input
                type="text"
                value={formData.name}
                onChange={e => setFormData(prev => ({ ...prev, name: e.target.value }))}
                placeholder="如: CC消费者-性能评测"
                style={inputStyle}
              />
            </div>

            <div>
              <label style={{ display: 'block', fontSize: 'var(--edp-font-md)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-1)' }}>
                Langfuse 服务器地址
              </label>
              <input
                type="text"
                value={formData.host}
                onChange={e => setFormData(prev => ({ ...prev, host: e.target.value }))}
                placeholder="如: http://175.178.171.155:13001"
                style={inputStyle}
              />
            </div>

            <div>
              <label style={{ display: 'block', fontSize: 'var(--edp-font-md)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-1)' }}>
                Langfuse 组织
              </label>
              <input
                type="text"
                value={formData.organization}
                onChange={e => setFormData(prev => ({ ...prev, organization: e.target.value }))}
                placeholder="如: cc-consumer-2.0"
                style={inputStyle}
              />
            </div>

            <div>
              <label style={{ display: 'block', fontSize: 'var(--edp-font-md)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-1)' }}>
                Langfuse 项目 ID
              </label>
              <input
                type="text"
                value={formData.langfuse_project_id}
                onChange={e => setFormData(prev => ({ ...prev, langfuse_project_id: e.target.value }))}
                placeholder="如: perf"
                style={inputStyle}
              />
            </div>

            <div style={{ gridColumn: '1 / -1' }}>
              <label style={{ display: 'block', fontSize: 'var(--edp-font-md)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-1)' }}>
                Public Key <span style={{ color: 'var(--edp-red)' }}>*</span>
              </label>
              <input
                type="text"
                value={formData.public_key}
                onChange={e => setFormData(prev => ({ ...prev, public_key: e.target.value }))}
                placeholder={editingId ? '留空则不修改' : 'pk-lf-...'}
                style={{ ...inputStyle, width: '100%' }}
              />
            </div>

            <div style={{ gridColumn: '1 / -1' }}>
              <label style={{ display: 'block', fontSize: 'var(--edp-font-md)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-1)' }}>
                Secret Key <span style={{ color: 'var(--edp-red)' }}>*</span>
              </label>
              <div style={{ position: 'relative' }}>
                <input
                  type={showSecretKey ? 'text' : 'password'}
                  value={formData.secret_key}
                  onChange={e => setFormData(prev => ({ ...prev, secret_key: e.target.value }))}
                  placeholder={editingId ? '留空则不修改' : 'sk-lf-...'}
                  style={{ ...inputStyle, width: '100%', paddingRight: 36 }}
                />
                <button
                  type="button"
                  onClick={() => setShowSecretKey(!showSecretKey)}
                  style={{
                    position: 'absolute',
                    right: 8,
                    top: '50%',
                    transform: 'translateY(-50%)',
                    background: 'none',
                    border: 'none',
                    cursor: 'pointer',
                    padding: 2,
                    color: 'var(--edp-text3)',
                    display: 'flex',
                    alignItems: 'center'
                  }}
                  title={showSecretKey ? '隐藏' : '显示'}
                >
                  {showSecretKey ? (
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
                  ) : (
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                  )}
                </button>
              </div>
            </div>
          </div>

          <div style={{ display: 'flex', gap: 'var(--edp-space-2)', marginTop: 'var(--edp-space-4)', justifyContent: 'flex-end' }}>
            <button onClick={resetForm} style={cancelBtnStyle}>取消</button>
            <button
              onClick={handleSubmit}
              disabled={!formData.name || (!editingId && (!formData.public_key || !formData.secret_key))}
              style={{
                ...submitBtnStyle,
                opacity: (!formData.name || (!editingId && (!formData.public_key || !formData.secret_key))) ? 0.5 : 1
              }}
            >
              {editingId ? '保存修改' : '创建'}
            </button>
          </div>
        </div>
      )}

      {projects.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '40px 0', color: 'var(--edp-text3)' }}>
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--edp-border)" strokeWidth="1.5" style={{ marginBottom: 'var(--edp-space-3)' }}>
            <circle cx="12" cy="12" r="10"/>
            <line x1="12" y1="8" x2="12" y2="12"/>
            <line x1="12" y1="16" x2="12.01" y2="16"/>
          </svg>
          <p>暂无 Langfuse 项目配置</p>
          <p style={{ fontSize: 'var(--edp-font-base)' }}>点击"新增项目"按钮添加 Langfuse 项目</p>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--edp-space-4)' }}>
          {Object.entries(groupedProjects).map(([org, orgProjects]) => (
            <div key={org}>
              <div style={{
                fontSize: 'var(--edp-font-md)',
                fontWeight: 600,
                color: 'var(--edp-primary)',
                marginBottom: 'var(--edp-space-2)',
                padding: '4px 8px',
                background: 'var(--edp-primary-soft)',
                borderRadius: 'var(--edp-radius-sm)',
                display: 'inline-block'
              }}>
                组织: {org}
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--edp-space-2)' }}>
                {orgProjects.map(p => (
                  <div key={p.id} style={{
                    border: '1px solid var(--edp-border-soft)',
                    borderRadius: 'var(--edp-radius-sm)',
                    padding: '12px 16px',
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    background: 'var(--edp-card)'
                  }}>
                    <div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)', marginBottom: 'var(--edp-space-1)' }}>
                        <span style={{ fontSize: 15, fontWeight: 600, color: 'var(--edp-text)' }}>{p.name}</span>
                        <code style={{
                          background: 'var(--edp-border)',
                          padding: '2px 6px',
                          borderRadius: 'var(--edp-radius-sm)',
                          fontSize: 'var(--edp-font-base)',
                          color: 'var(--edp-text2)'
                        }}>{p.id}</code>
                      </div>
                      <div style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text3)' }}>
                        {p.host && (
                          <span style={{ marginRight: 'var(--edp-space-3)' }}>
                            服务器: <code style={{ background: 'var(--edp-bg)', padding: '1px 4px', borderRadius: 2 }}>{p.host}</code>
                          </span>
                        )}
                        {p.langfuse_project_id && (
                          <span style={{ marginRight: 'var(--edp-space-3)' }}>
                            Langfuse 项目: <code style={{ background: 'var(--edp-bg)', padding: '1px 4px', borderRadius: 2 }}>{p.langfuse_project_id}</code>
                          </span>
                        )}
                        {p.public_key && (
                          <span style={{ marginRight: 'var(--edp-space-3)', display: 'inline-flex', alignItems: 'center', gap: 'var(--edp-space-1)' }}>
                            Public Key:
                            <code
                              style={{ background: 'var(--edp-bg)', padding: '1px 4px', borderRadius: 2, wordBreak: 'break-all' }}
                              title={p.public_key}
                            >{p.public_key}</code>
                          </span>
                        )}
                      </div>
                    </div>
                    <div style={{ display: 'flex', gap: 'var(--edp-space-2)' }}>
                      <button
                        onClick={() => handleEdit(p)}
                        style={smallBtnStyle}
                        title="编辑"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                      </button>
                      <button
                        onClick={() => handleDelete(p.id)}
                        style={{ ...smallBtnStyle, color: 'var(--edp-red)' }}
                        title="删除"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}


const inputStyle: React.CSSProperties = {
  width: '100%',
  padding: '8px 12px',
  border: '1px solid var(--edp-border)',
  borderRadius: 'var(--edp-radius-sm)',
  fontSize: 'var(--edp-font-md)',
  outline: 'none',
  boxSizing: 'border-box'
}

const smallBtnStyle: React.CSSProperties = {
  padding: '6px',
  border: '1px solid var(--edp-border)',
  borderRadius: 'var(--edp-radius-sm)',
  background: 'var(--edp-card)',
  cursor: 'pointer',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  color: 'var(--edp-text2)'
}

const cancelBtnStyle: React.CSSProperties = {
  padding: '8px 20px',
  border: '1px solid var(--edp-border)',
  borderRadius: 'var(--edp-radius-sm)',
  background: 'var(--edp-card)',
  cursor: 'pointer',
  fontSize: 'var(--edp-font-md)',
  color: 'var(--edp-text2)'
}

const submitBtnStyle: React.CSSProperties = {
  padding: '8px 20px',
  border: 'none',
  borderRadius: 'var(--edp-radius-sm)',
  background: 'var(--edp-primary)',
  cursor: 'pointer',
  fontSize: 'var(--edp-font-md)',
  color: 'var(--edp-card)',
  fontWeight: 500
}