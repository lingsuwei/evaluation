import { useState, useEffect, useRef } from 'react'
import axios from 'axios'
import { formatDate } from '../utils/dateUtils'
import { compareAgentsByLogicalAndVersion, isDuplicateAgentVersion } from '../utils/agentUtils'
import {
  getLangfuseReadinessMeta,
  isLangfuseTraceReady,
  langfuseStatusHintFromProbe,
} from '../utils/langfuseReadiness'
import {
  LangfuseAgentFormNotice,
  LangfuseReadinessBadge,
} from './LangfuseObservabilityNotice'

const API_BASE = '/api'

interface InferenceConfig {
  url: string
  endpoint: string
  headers: Record<string, string>
  auth: {
    type: string
    token?: string
    api_key?: string
    header_name?: string
    username?: string
    password?: string
  }
  timeout: number
  request_delay: number
  payload_template: Record<string, any>
  workspace_id?: string
  workspace_name?: string
  workflow?: { orchestration_pattern: string }
}

interface Agent {
  id: string
  name: string
  description: string | null
  inference_config: InferenceConfig | null
  logical_agent_id?: string | null
  version?: string | null
  version_tag?: string | null
  langfuse_project?: string | null
  created_at: string
  is_active: boolean
  agent_kind?: string
}

interface LangfuseProjectOption {
  id: string
  name: string
}

interface Props {
  onAgentSelect?: (agentId: string) => void
}

interface AgentProbeCheck {
  name: string
  ok: boolean
  message: string
  severity?: 'warning' | 'error' | string
}

interface AgentProbeResult {
  ok: boolean
  has_warnings?: boolean
  resolved_url?: string | null
  workspace_id?: string | null
  langfuse_project?: string | null
  langfuse_project_status?: string | null
  checks: AgentProbeCheck[]
  suggestions?: string[]
}

function formatOntoAgentError(err: unknown, fallback: string): string {
  const ax = err as {
    response?: { status?: number; data?: { detail?: string | unknown } }
    message?: string
  }
  const status = ax.response?.status
  const detail = ax.response?.data?.detail
  const detailText =
    typeof detail === 'string'
      ? detail
      : detail != null
        ? JSON.stringify(detail)
        : ''
  if (detailText) return status ? `[${status}] ${detailText}` : detailText
  if (status === 401) {
    return 'Onto Agent 认证失败：请先开启鉴权，填写账号密码并点击「登录」获取 Token'
  }
  if (status === 502) {
    return '无法连接到 onto-agent，请确认服务已启动；Docker 部署 eval 时 Agent 地址可填 http://host.docker.internal:5055'
  }
  if (status) return `[${status}] ${ax.message || fallback}`
  return ax.message || fallback
}

const defaultInferenceConfig: InferenceConfig = {
  url: '',
  endpoint: '',
  headers: {},
  auth: { type: 'none' },
  timeout: 1800,
  request_delay: 0.1,
  payload_template: { "request": "", "context": { "agent_mode": "thinking" } },
  workspace_id: ''
}

export function AgentManagePage({ onAgentSelect }: Props) {
  const [agents, setAgents] = useState<Agent[]>([])
  const [loading, setLoading] = useState(true)
  const [showRegister, setShowRegister] = useState(false)
  const [editingAgent, setEditingAgent] = useState<Agent | null>(null)
  const [newAgent, setNewAgent] = useState({ 
    name: '', 
    description: '',
    logical_agent_id: '',
    version: '',
    version_tag: '',
    langfuse_project: '',
    inference_config: { ...defaultInferenceConfig }
  })
  const [filterLogicalId, setFilterLogicalId] = useState('')
  const [workspaces, setWorkspaces] = useState<Array<{id: string, name: string, is_default: boolean}>>([])
  const [loadingWorkspaces, setLoadingWorkspaces] = useState(false)
  const [showInferenceConfig, setShowInferenceConfig] = useState(false)
  const [registering, setRegistering] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [payloadTemplateText, setPayloadTemplateText] = useState('')
  /** create=全新注册；newVersion=同逻辑 Agent 下注册新版本 */
  const [registerMode, setRegisterMode] = useState<'create' | 'newVersion'>('create')
  /** 新版本注册时的源 agent_id，走后端 parent_agent_id 分支 */
  const [newVersionParentId, setNewVersionParentId] = useState<string | null>(null)
  const [seedDefaultCheckpoints, setSeedDefaultCheckpoints] = useState(true)
  const [langfuseProjects, setLangfuseProjects] = useState<LangfuseProjectOption[]>([])
  const [defaultLangfuseProject, setDefaultLangfuseProject] = useState('')
  const [probing, setProbing] = useState(false)
  const [probeResult, setProbeResult] = useState<AgentProbeResult | null>(null)
  const [loggingIn, setLoggingIn] = useState(false)
  const loginInFlightRef = useRef(false)
  const registerInFlightRef = useRef(false)

  const formatLangfuseProjectLabel = (projectId: string | null | undefined) => {
    const id = (projectId || '').trim()
    if (!id) {
      return defaultLangfuseProject
        ? `默认（${defaultLangfuseProject}）`
        : '默认（保存时自动匹配或留空）'
    }
    const matched = langfuseProjects.find((p) => p.id === id)
    return matched ? `${matched.name}（${id}）` : id
  }

  const fetchLangfuseProjects = async () => {
    try {
      const res = await axios.get(`${API_BASE}/trace/projects`)
      setLangfuseProjects(res.data.projects || [])
      setDefaultLangfuseProject(res.data.default_project || '')
    } catch {
      setLangfuseProjects([])
    }
  }

  const fetchAgents = async () => {
    setLoading(true)
    try {
      const res = await axios.get(`${API_BASE}/agents`)
      setAgents(res.data)
    } catch (err) {
      console.error('获取 Agent 列表失败:', err)
    } finally {
      setLoading(false)
    }
  }

  type WorkspaceItem = { id: string; name: string; is_default: boolean }

  const workspaceOptionsForSelect = (
    list: WorkspaceItem[],
    savedId: string,
    savedName?: string,
  ): WorkspaceItem[] => {
    const id = savedId.trim()
    if (!id || list.some((ws) => ws.id === id)) return list
    const label = (savedName || '').trim() || `已失效（${id}）`
    return [{ id, name: label, is_default: false }, ...list]
  }

  const workspaceFieldsForId = (
    list: WorkspaceItem[],
    workspaceId: string,
  ): Pick<InferenceConfig, 'workspace_id' | 'workspace_name'> => {
    const id = workspaceId.trim()
    const matched = list.find((ws) => ws.id === id)
    return {
      workspace_id: id,
      workspace_name: matched?.name || undefined,
    }
  }

  // 通过后端代理获取工作空间（避免浏览器直连 Agent 地址触发 CORS / Network Error）
  const fetchWorkspaces = async (
    isEditing = false,
    configOverride?: InferenceConfig | null,
    options?: { silent?: boolean },
  ) => {
    const config =
      configOverride ?? (isEditing ? editingAgent?.inference_config : newAgent.inference_config)
    const baseUrl = config?.url?.trim()
    const token = config?.auth?.token?.trim() || ''

    if (!baseUrl) {
      if (!options?.silent) alert('请先填写 Agent 地址')
      return
    }
    if (config?.auth?.type === 'basic_auth' && !token) {
      if (!options?.silent) alert('请先开启鉴权并获取 Token')
      return
    }

    setLoadingWorkspaces(true)
    try {
      const res = await axios.get(`${API_BASE}/onto-agent/workspaces`, {
        params: {
          base_url: baseUrl.replace(/\/$/, ''),
          api_token: token,
          auth_type: config?.auth?.type || '',
          username: config?.auth?.username || '',
          password: config?.auth?.password || '',
        },
      })
      const workspaceList: WorkspaceItem[] = res.data.workspaces || []
      setWorkspaces(workspaceList)

      let savedId = (config?.workspace_id || '').trim()
      const defaultWs = workspaceList.find((ws) => ws.is_default)
      if (savedId && !workspaceList.some((ws) => ws.id === savedId)) {
        if (!options?.silent) {
          alert(`工作空间「${savedId}」在 onto-agent 中不存在，请重新选择有效工作空间。`)
        }
        savedId = ''
      }
      if (savedId) {
        const wsFields = workspaceFieldsForId(workspaceList, savedId)
        if (isEditing && editingAgent) {
          setEditingAgent({
            ...editingAgent,
            inference_config: {
              ...editingAgent.inference_config!,
              ...wsFields,
            },
          })
        } else {
          setNewAgent((prev) => ({
            ...prev,
            inference_config: {
              ...prev.inference_config,
              ...wsFields,
            },
          }))
        }
      } else if (defaultWs) {
        if (isEditing && editingAgent) {
          setEditingAgent({
            ...editingAgent,
            inference_config: {
              ...editingAgent.inference_config!,
              workspace_id: defaultWs.id,
              workspace_name: defaultWs.name,
            },
          })
        } else {
          setNewAgent((prev) => ({
            ...prev,
            inference_config: {
              ...prev.inference_config,
              workspace_id: defaultWs.id,
              workspace_name: defaultWs.name,
            },
          }))
        }
      }
    } catch (err: unknown) {
      console.error('获取工作空间失败:', err)
      if (!options?.silent) {
        alert(`获取工作空间失败: ${formatOntoAgentError(err, '未知错误')}`)
      }
    } finally {
      setLoadingWorkspaces(false)
    }
  }

  useEffect(() => {
    fetchAgents()
  }, [])

  const parsePayloadTemplateText = (text: string): Record<string, unknown> | null => {
    const trimmed = text.trim()
    if (!trimmed) return null
    try {
      const parsed = JSON.parse(trimmed)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>
      }
      alert('请求体模板必须是 JSON 对象')
      return null
    } catch {
      alert('请求体模板 JSON 格式无效，请检查后再保存')
      return null
    }
  }

  const mergeInferenceConfigWithTemplate = (
    config: InferenceConfig | null | undefined,
    templateText: string,
  ): InferenceConfig | null => {
    if (!config) return null
    const merged = { ...config }
    const parsed = parsePayloadTemplateText(templateText)
    if (parsed) {
      merged.payload_template = parsed
    }
    return merged
  }

  const validateNewAgentVersion = (): boolean => {
    const lid = (newAgent.logical_agent_id || '').trim()
    const ver = (newAgent.version || '').trim()
    if (lid && !ver) {
      alert('已填写逻辑 Agent ID 时，版本号为必填')
      return false
    }
    if (lid && ver && isDuplicateAgentVersion(agents, lid, ver)) {
      alert(`逻辑 Agent「${lid}」下已存在版本「${ver}」，请更换版本号`)
      return false
    }
    return true
  }

  const probeCheckPresentation = (check: AgentProbeCheck) => {
    if (!check.ok) return { icon: '✗', color: 'var(--edp-red)' }
    if (check.severity === 'warning') return { icon: '⚠', color: 'var(--edp-amber)' }
    return { icon: '✓', color: 'var(--edp-green)' }
  }

  const normalizeProbeFromDetail = (detail: unknown): AgentProbeResult | null => {
    if (!detail || typeof detail !== 'object') return null
    const d = detail as { probe?: AgentProbeResult; checks?: AgentProbeCheck[] }
    if (d.probe?.checks?.length) return d.probe
    if (Array.isArray(d.checks) && d.checks.length) return d as AgentProbeResult
    return null
  }

  const formatProbeResult = (probe: AgentProbeResult, message?: string): string => {
    const lines: string[] = []
    if (message) lines.push(message)
    if (probe.checks?.length) {
      for (const c of probe.checks) {
        const { icon } = probeCheckPresentation(c)
        lines.push(`${icon} ${c.name}: ${c.message}`)
      }
    }
    if (probe.suggestions?.length) {
      lines.push(`建议: ${probe.suggestions.join('；')}`)
    }
    return lines.join('\n') || '预检未通过'
  }

  const formatProbeDetail = (detail: unknown): string => {
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          if (item && typeof item === 'object' && 'msg' in item) {
            return String((item as { msg?: string }).msg || JSON.stringify(item))
          }
          return JSON.stringify(item)
        })
        .join('；')
    }
    if (!detail || typeof detail !== 'object') return String(detail ?? '')
    const d = detail as { message?: string; probe?: AgentProbeResult }
    if (d.probe) return formatProbeResult(d.probe, d.message)
    return d.message || ''
  }

  const formatApiError = (err: unknown, fallback: string): string => {
    const ax = err as {
      response?: { status?: number; data?: { detail?: unknown } }
      message?: string
    }
    const status = ax.response?.status
    const detail = ax.response?.data?.detail
    const detailText = formatProbeDetail(detail)
    if (detailText) return detailText
    if (status === 500) {
      return '服务器内部错误（500）。请确认已部署最新后端镜像并查看日志：docker logs onto-eval-app'
    }
    if (status) return `HTTP ${status}${ax.message ? `：${ax.message}` : ''}`
    return ax.message || fallback
  }

  const confirmSkipProbe = (reason: string): boolean =>
    window.confirm(`${reason}\n\n仍要保存并跳过预检？（可能导致评测时连接失败）`)

  const langfuseStatusHint = (status?: string | null, agent?: Agent | null) => {
    if (agent && isLangfuseTraceReady(agent) && status !== 'not_registered') {
      return getLangfuseReadinessMeta(agent).hint
    }
    return langfuseStatusHintFromProbe(status)
  }

  const applyProbeLangfuse = (
    result: AgentProbeResult,
    mode: 'create' | 'edit',
  ) => {
    if (!result.langfuse_project) return
    if (mode === 'create') {
      setNewAgent((prev) => ({ ...prev, langfuse_project: result.langfuse_project || '' }))
    } else {
      setEditingAgent((prev) =>
        prev ? { ...prev, langfuse_project: result.langfuse_project || '' } : prev,
      )
    }
  }

  const runInferenceProbe = async (
    config: InferenceConfig | null | undefined,
    mode: 'create' | 'edit' = 'create',
  ) => {
    if (!config?.url?.trim()) {
      alert('请先填写 Agent 地址')
      return null
    }
    setProbing(true)
    setProbeResult(null)
    try {
      const res = await axios.post<AgentProbeResult>(`${API_BASE}/onto-agent/probe-sync`, {
        inference_config: config,
      })
      setProbeResult(res.data)
      applyProbeLangfuse(res.data, mode)
      return res.data
    } catch (err: any) {
      const detail = err.response?.data?.detail
      if (detail?.probe) {
        setProbeResult(detail.probe as AgentProbeResult)
      }
      alert(`预检失败: ${formatProbeDetail(detail || err.message)}`)
      return null
    } finally {
      setProbing(false)
    }
  }

  const loginToOntoAgent = async (config: InferenceConfig, isEditing: boolean) => {
    if (loginInFlightRef.current) return
    if (!config.url) {
      alert('请先填写 Agent 地址')
      return
    }
    if (!config.auth.username || !config.auth.password) {
      alert('请填写用户名和密码')
      return
    }
    loginInFlightRef.current = true
    setLoggingIn(true)
    const loginUrl = `${config.url.replace(/\/$/, '')}/auth/login`
    try {
      const loginRes = await axios.post(loginUrl, {
        username: config.auth.username,
        password: config.auth.password,
      })
      if (loginRes.data.token) {
        if (isEditing && editingAgent) {
          setEditingAgent({
            ...editingAgent,
            inference_config: {
              ...editingAgent.inference_config!,
              auth: { ...editingAgent.inference_config!.auth, token: loginRes.data.token },
            },
          })
        } else {
          setNewAgent({
            ...newAgent,
            inference_config: {
              ...newAgent.inference_config,
              auth: { ...newAgent.inference_config.auth, token: loginRes.data.token },
            },
          })
        }
        alert('登录成功')
      }
    } catch (loginErr: unknown) {
      const ax = loginErr as { response?: { data?: { error?: string } } }
      alert(`登录失败: ${ax.response?.data?.error || '无法连接到认证服务'}`)
    } finally {
      loginInFlightRef.current = false
      setLoggingIn(false)
    }
  }

  const handleRegister = async (skipProbe = false) => {
    if (!newAgent.name) {
      alert('请填写 Agent 名称')
      return
    }
    if (!newAgent.inference_config?.url?.trim()) {
      alert('请填写 Agent 地址')
      return
    }
    if (!newAgent.inference_config?.endpoint?.trim()) {
      alert('请填写接口端点')
      return
    }
    if (!validateNewAgentVersion()) return

    if (!skipProbe && probeResult && !probeResult.ok) {
      if (!confirmSkipProbe(formatProbeResult(probeResult, 'Sync 健康预检未通过'))) {
        return
      }
      skipProbe = true
    }

    if (registerInFlightRef.current) return
    registerInFlightRef.current = true
    setRegistering(true)
    try {
      let inferenceConfig = newAgent.inference_config ? { ...newAgent.inference_config } : null

      const payload: Record<string, unknown> = {
        name: newAgent.name,
        description: newAgent.description || null,
        logical_agent_id: newAgent.logical_agent_id || null,
        version: newAgent.version || null,
        version_tag: newAgent.version_tag || null,
        langfuse_project: newAgent.langfuse_project || null,
        inference_config: inferenceConfig,
        seed_default_checkpoints: seedDefaultCheckpoints,
      }
      if (registerMode === 'newVersion' && newVersionParentId) {
        payload.parent_agent_id = newVersionParentId
      }
      if (skipProbe) {
        payload.skip_probe = true
      }
      const res = await axios.post(`${API_BASE}/agents`, payload)
      const seed = res.data?.checkpoint_seed as { created?: number; skipped?: boolean } | undefined
      const seedHint =
        seed && !seed.skipped && (seed.created ?? 0) > 0
          ? `\n已预置 ${seed.created} 条 onto-agent 默认识别过程点（过程点配置页可编辑）。`
          : ''
      alert(
        (registerMode === 'newVersion' ? '新版本注册成功' : 'Agent 新增成功') + seedHint,
      )
      setNewAgent({ name: '', description: '', logical_agent_id: '', version: '', version_tag: '', langfuse_project: '', inference_config: { ...defaultInferenceConfig } })
      setSeedDefaultCheckpoints(true)
      setRegisterMode('create')
      setNewVersionParentId(null)
      setShowRegister(false)
      setShowInferenceConfig(false)
      setPayloadTemplateText('')
      setProbeResult(null)
      await fetchAgents()
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
      const probeFromDetail = normalizeProbeFromDetail(detail)
      if (!skipProbe && probeFromDetail) {
        setProbeResult(probeFromDetail)
        if (confirmSkipProbe(formatProbeDetail(detail) || formatProbeResult(probeFromDetail))) {
          registerInFlightRef.current = false
          setRegistering(false)
          await handleRegister(true)
          return
        }
      } else {
        alert(`新增失败: ${formatApiError(err, '未知错误')}`)
      }
    } finally {
      registerInFlightRef.current = false
      setRegistering(false)
    }
  }

  const handleUpdate = async (skipProbe = false) => {
    if (!editingAgent) return

    const trimmedName = (editingAgent.name || '').trim()
    if (!trimmedName) {
      alert('请填写 Agent 名称')
      return
    }
    if (!editingAgent.inference_config?.url?.trim()) {
      alert('请填写 Agent 地址')
      return
    }
    if (!editingAgent.inference_config?.endpoint?.trim()) {
      alert('请填写接口端点')
      return
    }

    if (registerInFlightRef.current) return
    registerInFlightRef.current = true
    setRegistering(true)
    try {
      let inferenceConfig = editingAgent.inference_config ? { ...editingAgent.inference_config } : null

      const payload: Record<string, unknown> = {
        name: trimmedName,
        description: editingAgent.description || null,
        inference_config: inferenceConfig
      }
      if (skipProbe) {
        payload.skip_probe = true
      }
      await axios.put(`${API_BASE}/agents/${editingAgent.id}`, payload)
      alert('Agent 更新成功')
      setEditingAgent(null)
      setPayloadTemplateText('')
      setProbeResult(null)
      await fetchAgents()
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: unknown } } }).response?.data?.detail
      const probeFromDetail = normalizeProbeFromDetail(detail)
      if (!skipProbe && probeFromDetail) {
        setProbeResult(probeFromDetail)
        if (confirmSkipProbe(formatProbeDetail(detail) || formatProbeResult(probeFromDetail))) {
          registerInFlightRef.current = false
          setRegistering(false)
          await handleUpdate(true)
          return
        }
      } else {
        alert(`更新失败: ${formatApiError(err, '未知错误')}`)
      }
    } finally {
      registerInFlightRef.current = false
      setRegistering(false)
    }
  }

  const handleDelete = async (agentId: string) => {
    if (!confirm(`确定要删除 Agent ${agentId} 吗？此操作不可恢复。`)) {
      return
    }

    setDeleting(true)
    try {
      await axios.delete(`${API_BASE}/agents/${agentId}`)
      alert('Agent 删除成功')
      await fetchAgents()
    } catch (err: any) {
      alert(`删除失败: ${err.response?.data?.detail || '未知错误'}`)
    } finally {
      setDeleting(false)
    }
  }

  const updateAuthField = (field: string, value: string, isEditing: boolean) => {
    if (isEditing && editingAgent) {
      setEditingAgent({
        ...editingAgent,
        inference_config: {
          ...editingAgent.inference_config!,
          auth: { ...editingAgent.inference_config!.auth, [field]: value }
        }
      })
    } else {
      setNewAgent({
        ...newAgent,
        inference_config: {
          ...newAgent.inference_config,
          auth: { ...newAgent.inference_config.auth, [field]: value }
        }
      })
    }
  }

  const updateHeader = (key: string, value: string, isEditing: boolean) => {
    if (isEditing && editingAgent) {
      setEditingAgent({
        ...editingAgent,
        inference_config: {
          ...editingAgent.inference_config!,
          headers: { ...editingAgent.inference_config!.headers, [key]: value }
        }
      })
    } else {
      setNewAgent({
        ...newAgent,
        inference_config: {
          ...newAgent.inference_config,
          headers: { ...newAgent.inference_config.headers, [key]: value }
        }
      })
    }
  }

  const removeHeader = (key: string, isEditing: boolean) => {
    if (isEditing && editingAgent) {
      const newHeaders = { ...editingAgent.inference_config!.headers }
      delete newHeaders[key]
      setEditingAgent({
        ...editingAgent,
        inference_config: { ...editingAgent.inference_config!, headers: newHeaders }
      })
    } else {
      const newHeaders = { ...newAgent.inference_config.headers }
      delete newHeaders[key]
      setNewAgent({
        ...newAgent,
        inference_config: { ...newAgent.inference_config, headers: newHeaders }
      })
    }
  }

  const renderInferenceConfigForm = (
    config: InferenceConfig,
    agentDraft: Agent,
    isEditing: boolean,
    probeMode: 'create' | 'edit' = 'create',
  ) => (
    <div style={{ marginTop: 15, padding: 15, background: 'var(--edp-bg)', borderRadius: 'var(--edp-radius-sm)' }}>
      <h4 style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <circle cx="12" cy="12" r="3"/>
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
      </svg>
      Agent配置
    </h4>
      
      <div style={{ marginTop: 10 }}>
        <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>Agent地址</label>
        <input
          type="text"
          value={config.url}
          onChange={(e) => isEditing 
            ? setEditingAgent(editingAgent ? { ...editingAgent, inference_config: { ...editingAgent.inference_config!, url: e.target.value } } : null)
            : setNewAgent({ ...newAgent, inference_config: { ...newAgent.inference_config, url: e.target.value } })
          }
          placeholder="例如: http://localhost:5055（onto-agent）或 http://localhost:5002（工作流 Agent）"
          style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)' }}
        />
      </div>

      <div style={{ marginTop: 10 }}>
        <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>接口端点</label>
        <input
          type="text"
          value={config.endpoint}
          onChange={(e) => {
            const newEndpoint = e.target.value
            const isWorkflow = !newEndpoint.toLowerCase().includes('/agentic')
            if (isEditing) {
              setEditingAgent(editingAgent ? {
                ...editingAgent,
                inference_config: {
                  ...editingAgent.inference_config!,
                  endpoint: newEndpoint,
                  auth: isWorkflow ? { type: 'none' } : editingAgent.inference_config!.auth,
                  workspace_id: isWorkflow ? undefined : editingAgent.inference_config!.workspace_id,
                  workspace_name: isWorkflow ? undefined : editingAgent.inference_config!.workspace_name,
                }
              } : null)
            } else {
              setNewAgent({
                ...newAgent,
                inference_config: {
                  ...newAgent.inference_config,
                  endpoint: newEndpoint,
                  auth: isWorkflow ? { type: 'none' } : newAgent.inference_config.auth,
                  workspace_id: isWorkflow ? undefined : newAgent.inference_config.workspace_id,
                  workspace_name: isWorkflow ? undefined : newAgent.inference_config.workspace_name,
                }
              })
            }
          }}
          placeholder="onto-agent: /agentic/runs/sync | 工作流 Agent: /api/chat/sync"
          style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)' }}
        />
        <p style={{ fontSize: 'var(--edp-font-sm)', color: 'var(--edp-text3)', marginTop: 'var(--edp-space-1)', userSelect: 'text' }}>
          <code
            style={{ cursor: 'pointer', color: 'var(--edp-blue)', background: 'var(--edp-primary-soft)', padding: '1px 4px', borderRadius: 3 }}
            onClick={() => {
              const ep = '/agentic/runs/sync'
              if (isEditing) {
                setEditingAgent(editingAgent ? { ...editingAgent, inference_config: { ...editingAgent.inference_config!, endpoint: ep } } : null)
              } else {
                setNewAgent({ ...newAgent, inference_config: { ...newAgent.inference_config, endpoint: ep } })
              }
            }}
            title="点击填入"
          >/agentic/runs/sync</code>
          （onto-agent，需鉴权/工作空间）；
          <code
            style={{ cursor: 'pointer', color: 'var(--edp-blue)', background: 'var(--edp-primary-soft)', padding: '1px 4px', borderRadius: 3 }}
            onClick={() => {
              const ep = '/api/chat/sync'
              if (isEditing) {
                setEditingAgent(editingAgent ? { ...editingAgent, inference_config: { ...editingAgent.inference_config!, endpoint: ep, auth: { type: 'none' }, workspace_id: undefined, workspace_name: undefined } } : null)
              } else {
                setNewAgent({ ...newAgent, inference_config: { ...newAgent.inference_config, endpoint: ep, auth: { type: 'none' }, workspace_id: undefined, workspace_name: undefined } })
              }
            }}
            title="点击填入"
          >/api/chat/sync</code>
          （工作流 Agent，无需鉴权/工作空间）— 点击可填入
        </p>
      </div>

      <div style={{ display: 'flex', gap: 10, marginTop: 10 }}>
        <div style={{ flex: 1 }}>
          <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>超时时间（秒）</label>
          <input
            type="number"
            value={config.timeout}
            onChange={(e) => isEditing
              ? setEditingAgent(editingAgent ? { ...editingAgent, inference_config: { ...editingAgent.inference_config!, timeout: parseInt(e.target.value) || 1800 } } : null)
              : setNewAgent({ ...newAgent, inference_config: { ...newAgent.inference_config, timeout: parseInt(e.target.value) || 1800 } })
            }
            min="1"
            max="1800"
            style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)' }}
          />
        </div>
        <div style={{ flex: 1 }}>
          <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>请求间隔（秒）</label>
          <input
            type="number"
            value={config.request_delay}
            onChange={(e) => isEditing
              ? setEditingAgent(editingAgent ? { ...editingAgent, inference_config: { ...editingAgent.inference_config!, request_delay: parseFloat(e.target.value) || 0.1 } } : null)
              : setNewAgent({ ...newAgent, inference_config: { ...newAgent.inference_config, request_delay: parseFloat(e.target.value) || 0.1 } })
            }
            step="0.1"
            min="0"
            max="5"
            style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)' }}
          />
        </div>
      </div>

      {(config.endpoint || '').toLowerCase().includes('/agentic') && (
      <>
      <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <div
            onClick={() => {
              const isEnabled = config.auth.type !== 'basic_auth'
              if (isEditing && editingAgent) {
                setEditingAgent({
                  ...editingAgent,
                  inference_config: {
                    ...editingAgent.inference_config!,
                    auth: isEnabled
                      ? { type: 'basic_auth', username: '', password: '' }
                      : { type: 'none' }
                  }
                })
              } else {
                setNewAgent({
                  ...newAgent,
                  inference_config: {
                    ...newAgent.inference_config,
                    auth: isEnabled
                      ? { type: 'basic_auth', username: '', password: '' }
                      : { type: 'none' }
                  }
                })
              }
            }}
            style={{
              width: 44,
              height: 24,
              borderRadius: 'var(--edp-radius)',
              background: config.auth.type === 'basic_auth' ? 'var(--edp-primary)' : 'var(--edp-border)',
              cursor: 'pointer',
              position: 'relative',
              transition: 'background 0.3s',
              flexShrink: 0
            }}
          >
            <div
              style={{
                width: 20,
                height: 20,
                borderRadius: '50%',
                background: 'white',
                position: 'absolute',
                top: 2,
                left: config.auth.type === 'basic_auth' ? 20 : 2,
                transition: 'left 0.3s',
                boxShadow: '0 2px 4px rgba(0,0,0,0.1)'
              }}
            />
          </div>
          <label style={{ fontWeight: 500, cursor: 'pointer', whiteSpace: 'nowrap' }} onClick={() => {
            const isEnabled = config.auth.type !== 'basic_auth'
            if (isEditing && editingAgent) {
              setEditingAgent({
                ...editingAgent,
                inference_config: {
                  ...editingAgent.inference_config!,
                  auth: isEnabled
                    ? { type: 'basic_auth', username: '', password: '' }
                    : { type: 'none' }
                }
              })
            } else {
              setNewAgent({
                ...newAgent,
                inference_config: {
                  ...newAgent.inference_config,
                  auth: isEnabled
                    ? { type: 'basic_auth', username: '', password: '' }
                    : { type: 'none' }
                }
              })
            }
          }}>鉴权</label>
        </div>

        {config.auth.type === 'basic_auth' && (
          <>
            <input
              type="text"
              value={config.auth.username || ''}
              onChange={(e) => updateAuthField('username', e.target.value, isEditing)}
              placeholder="用户名"
              style={{ width: 120, padding: '6px 10px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', fontSize: 'var(--edp-font-lg)' }}
            />
            <input
              type="password"
              value={config.auth.password || ''}
              onChange={(e) => updateAuthField('password', e.target.value, isEditing)}
              placeholder="密码"
              style={{ width: 120, padding: '6px 10px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', fontSize: 'var(--edp-font-lg)' }}
            />
            <button
              type="button"
              className="btn btn-primary"
              disabled={loggingIn}
              onClick={() => loginToOntoAgent(config, isEditing)}
              style={{ padding: '6px 16px', fontSize: 'var(--edp-font-lg)' }}
            >
              {loggingIn ? '登录中...' : '登录'}
            </button>
          </>
        )}
      </div>

      {config.auth.type === 'basic_auth' && (
        <>
          <div style={{ marginTop: 15 }}>
            <p style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)', marginBottom: 10 }}>
              提示：工作空间列表从 Agent 配置中的地址获取，请确保已配置 Agent 地址并开启鉴权
            </p>
            <div style={{ display: 'flex', gap: 10, alignItems: 'flex-end' }}>
              <div style={{ flex: 1 }}>
                <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>工作空间</label>
                <select
                  value={config.workspace_id || ''}
                  onChange={(e) => {
                    const value = e.target.value
                    const wsFields = workspaceFieldsForId(workspaces, value)
                    if (isEditing && editingAgent) {
                      setEditingAgent({
                        ...editingAgent,
                        inference_config: {
                          ...editingAgent.inference_config!,
                          ...wsFields,
                        }
                      })
                    } else {
                      setNewAgent(prev => ({
                        ...prev,
                        inference_config: {
                          ...prev.inference_config,
                          ...wsFields,
                        }
                      }))
                    }
                  }}
                  style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)' }}
                >
                  <option value="">选择工作空间</option>
                  {workspaceOptionsForSelect(
                    workspaces,
                    config.workspace_id || '',
                    config.workspace_name,
                  ).map((ws) => (
                    <option key={ws.id} value={ws.id}>
                      {ws.name} {ws.is_default ? '(默认)' : ''}
                    </option>
                  ))}
                </select>
              </div>
              <button
                className="btn"
                onClick={() => fetchWorkspaces(isEditing)}
                disabled={loadingWorkspaces}
                style={{ height: 'fit-content', padding: '8px 16px' }}
              >
                {loadingWorkspaces ? '获取中...' : '获取工作空间'}
              </button>
            </div>
          </div>
        </>
      )}
      </>
      )}

      <div style={{ marginTop: 15 }}>
        <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>
          请求体模板
          <span style={{ fontWeight: 400, fontSize: 'var(--edp-font-base)', color: 'var(--edp-text3)', marginLeft: 'var(--edp-space-2)' }}>
            可选，支持变量：{'${question}'}、{'${ground_truth}'}
          </span>
        </label>
        <textarea
          value={payloadTemplateText || (config.payload_template && Object.keys(config.payload_template).length > 0 ? JSON.stringify(config.payload_template, null, 2) : '')}
          onChange={(e) => {
            const text = e.target.value
            setPayloadTemplateText(text)
            try {
              const parsed = JSON.parse(text)
              if (isEditing && editingAgent) {
                setEditingAgent({
                  ...editingAgent,
                  inference_config: {
                    ...editingAgent.inference_config!,
                    payload_template: parsed
                  }
                })
              } else {
                setNewAgent({
                  ...newAgent,
                  inference_config: {
                    ...newAgent.inference_config,
                    payload_template: parsed
                  }
                })
              }
            } catch {
              // JSON 解析失败时只更新文本，不更新 payload_template
            }
          }}
          placeholder={'{\n  "request": "${question}",\n  "context": {\n    "agent_mode": "thinking"\n  }\n}'}
          style={{
            width: '100%',
            padding: '8px 12px',
            border: '1px solid var(--edp-border)',
            borderRadius: 'var(--edp-radius-sm)',
            minHeight: 120,
            fontFamily: 'monospace',
            fontSize: 'var(--edp-font-md)',
            resize: 'vertical'
          }}
        />
        <p style={{ fontSize: 'var(--edp-font-sm)', color: 'var(--edp-text3)', marginTop: 'var(--edp-space-1)' }}>
          留空则使用默认请求体格式。JSON 格式，支持模板变量替换。
        </p>
      </div>

      <div style={{ marginTop: 'var(--edp-space-4)', padding: 'var(--edp-space-3)', border: '1px solid var(--edp-border-soft)', borderRadius: 'var(--edp-radius-sm)', background: 'var(--edp-bg)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{ fontWeight: 500 }}>Sync 健康预检</span>
          <button
            type="button"
            className="btn btn-primary"
            disabled={probing || !config.url?.trim()}
            onClick={() => runInferenceProbe(config, probeMode)}
            style={{ padding: '6px 16px', fontSize: 'var(--edp-font-lg)' }}
          >
            {probing ? '预检中...' : '连接预检'}
          </button>
          <span style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)' }}>
            保存时将自动执行 URL → 鉴权 → 工作空间 → sync ping 检查
          </span>
        </div>
        {probeResult && (
          <div style={{ marginTop: 10 }}>
            <p
              style={{
                margin: '0 0 8px',
                fontSize: 'var(--edp-font-md)',
                color: !probeResult.ok
                  ? 'var(--edp-red)'
                  : probeResult.has_warnings
                    ? 'var(--edp-amber)'
                    : 'var(--edp-green)',
                fontWeight: 500,
              }}
            >
              {!probeResult.ok
                ? '预检未通过'
                : probeResult.has_warnings
                  ? '预检通过（有警告）'
                  : '预检通过'}
              {probeResult.resolved_url ? ` · ${probeResult.resolved_url}` : ''}
            </p>
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 'var(--edp-font-base)', color: 'var(--edp-text)' }}>
              {probeResult.checks.map((c) => {
                const { icon, color } = probeCheckPresentation(c)
                return (
                  <li key={c.name} style={{ marginBottom: 'var(--edp-space-1)', color }}>
                    {icon} {c.name}: {c.message}
                  </li>
                )
              })}
            </ul>
            {probeResult.suggestions && probeResult.suggestions.length > 0 && (
              <p style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-amber)', marginTop: 'var(--edp-space-2)', marginBottom: 0 }}>
                建议: {probeResult.suggestions.join('；')}
              </p>
            )}
          </div>
        )}
      </div>

      {renderLangfuseProjectField(agentDraft.langfuse_project || '', agentDraft, {
        syncHint: Boolean(agentDraft.logical_agent_id),
        probeStatus: probeResult?.langfuse_project_status,
        afterProbe: true,
        syncDisabled: !config.url?.trim(),
        syncing: probing,
        onSync: () => runInferenceProbe(config, probeMode),
      })}

    </div>
  )

  const renderVersionFields = (
    values: { logical_agent_id?: string; version?: string; version_tag?: string },
    onChange: (field: 'logical_agent_id' | 'version' | 'version_tag', value: string) => void,
    options?: { lockLogicalId?: boolean; lockVersion?: boolean },
  ) => {
    const lockLogicalId = options?.lockLogicalId ?? false
    const lockVersion = options?.lockVersion ?? false
    const readonlyStyle = { background: 'var(--edp-bg)', color: 'var(--edp-text2)' }
    return (
    <div style={{ display: 'flex', gap: 10, marginTop: 10, flexWrap: 'wrap' }}>
      <div style={{ flex: 1, minWidth: 180 }}>
        <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>逻辑 Agent ID</label>
        <input
          type="text"
          value={values.logical_agent_id || ''}
          onChange={(e) => onChange('logical_agent_id', e.target.value)}
          placeholder="如 onto-agent-prod"
          readOnly={lockLogicalId}
          disabled={lockLogicalId}
          style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', ...(lockLogicalId ? readonlyStyle : {}) }}
        />
      </div>
      <div style={{ flex: 1, minWidth: 120 }}>
        <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>版本号</label>
        <input
          type="text"
          value={values.version || ''}
          onChange={(e) => onChange('version', e.target.value)}
          placeholder="如 v1.0.0"
          readOnly={lockVersion}
          disabled={lockVersion}
          style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', ...(lockVersion ? readonlyStyle : {}) }}
        />
      </div>
      <div style={{ flex: 1, minWidth: 120 }}>
        <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>版本标签</label>
        <input
          type="text"
          value={values.version_tag || ''}
          onChange={(e) => onChange('version_tag', e.target.value)}
          placeholder="stable / canary"
          style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)' }}
        />
      </div>
      <p style={{ width: '100%', fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)', margin: '8px 0 0' }}>
        {lockVersion
          ? '编辑时名称、版本号与逻辑 ID 不可改；发版请用列表「新版本」。'
          : lockLogicalId
            ? '逻辑 ID 已固定；请填写未使用过的版本号（与同族已有版本不可重复）。'
            : '多版本请用「注册新版本」；同一逻辑 Agent 下版本号不可重复。'}
      </p>
    </div>
    )
  }

  const renderLangfuseProjectField = (
    value: string,
    agentDraft: Agent | null,
    options?: {
      syncHint?: boolean
      probeStatus?: string | null
      onSync?: () => void
      syncDisabled?: boolean
      syncing?: boolean
      /** 紧跟 Sync 预检之后展示：弱化独立同步按钮，强调预检驱动 */
      afterProbe?: boolean
    },
  ) => {
    const display = formatLangfuseProjectLabel(value)
    const draftAgent = agentDraft ? { ...agentDraft, langfuse_project: value || agentDraft.langfuse_project } : { langfuse_project: value }
    const readiness = getLangfuseReadinessMeta(draftAgent)
    const statusText = langfuseStatusHint(options?.probeStatus, draftAgent as Agent)
    const showWarnNotice =
      options?.probeStatus === 'not_configured' ||
      options?.probeStatus === 'not_registered' ||
      (!isLangfuseTraceReady(draftAgent) && !options?.probeStatus)
    return (
      <div style={{ marginTop: options?.afterProbe ? 16 : 10 }}>
        <LangfuseAgentFormNotice variant={showWarnNotice ? 'warn' : 'info'} />
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginTop: 10 }}>
          <label style={{ fontWeight: 500, margin: 0 }}>Langfuse 项目空间</label>
          <LangfuseReadinessBadge status={readiness.status} label={readiness.badge} />
          {!options?.afterProbe && options?.onSync && (
            <button
              type="button"
              className="btn btn-secondary"
              disabled={options.syncDisabled || options.syncing}
              onClick={options.onSync}
              style={{ padding: '4px 12px', fontSize: 'var(--edp-font-md)' }}
            >
              {options.syncing ? '同步中...' : '从 Agent 同步'}
            </button>
          )}
          {options?.afterProbe && options?.onSync && (
            <button
              type="button"
              className="btn btn-secondary"
              disabled={options.syncDisabled || options.syncing}
              onClick={options.onSync}
              style={{ padding: '4px 12px', fontSize: 'var(--edp-font-md)' }}
            >
              {options.syncing ? '预检中...' : '重新预检'}
            </button>
          )}
        </div>
        <div
          style={{
            marginTop: 'var(--edp-space-2)',
            padding: '8px 12px',
            border: '1px solid var(--edp-border-soft)',
            borderRadius: 'var(--edp-radius-sm)',
            background: 'var(--edp-bg)',
            fontSize: 'var(--edp-font-lg)',
          }}
        >
          {display}
        </div>
        <p style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)', marginTop: 'var(--edp-space-1)', marginBottom: 0 }}>
          根据 onto-agent 观测配置（public_key）自动匹配 eval 注册表。
          {options?.afterProbe ? ' 绑定结果由上方「连接预检」更新，保存时亦会自动探测。' : ''}
          {options?.syncHint ? ' 保存时将同步更新同一逻辑 Agent 下的全部版本。' : ''}
          <br />
          <span
            style={{
              color:
                options?.probeStatus === 'not_registered' || showWarnNotice
                  ? 'var(--edp-red)'
                  : readiness.status === 'ready'
                    ? 'var(--edp-green)'
                    : 'var(--edp-text2)',
            }}
          >
            {statusText}
          </span>
        </p>
      </div>
    )
  }

  const startRegisterNewVersion = (source: Agent) => {
    const baseConfig = source.inference_config
      ? JSON.parse(JSON.stringify(source.inference_config))
      : { ...defaultInferenceConfig }
    const lid = (source.logical_agent_id || '').trim() || `la_${source.id}`
    setEditingAgent(null)
    setNewVersionParentId(source.id)
    setNewAgent({
      name: source.name || '',
      description: source.description || '',
      logical_agent_id: lid,
      version: '',
      version_tag: source.version_tag || '',
      langfuse_project: source.langfuse_project || '',
      inference_config: baseConfig,
    })
    setRegisterMode('newVersion')
    setPayloadTemplateText(
      JSON.stringify(baseConfig.payload_template || defaultInferenceConfig.payload_template, null, 2),
    )
    setShowRegister(true)
    setShowInferenceConfig(true)
  }

  const filterQuery = filterLogicalId.trim()
  const logicalIdOptions = Array.from(
    new Set(agents.map((a) => (a.logical_agent_id || '').trim()).filter(Boolean)),
  ).sort()
  const filteredAgents = (
    filterQuery
      ? agents.filter((a) => (a.logical_agent_id || '').includes(filterQuery))
      : [...agents]
  ).sort(compareAgentsByLogicalAndVersion)

  if (loading) {
    return <div className="card">加载中...</div>
  }

  return (
    <div>
      {/* 注册表单 */}
      {showRegister && (
        <div className="card" style={{ marginBottom: 'var(--edp-space-5)' }}>
          <h3>{registerMode === 'newVersion' ? '注册 Agent 新版本' : '新增 Agent'}</h3>
          <div style={{ marginTop: 15 }}>
            <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>Agent 名称 *</label>
            <input
              type="text"
              value={newAgent.name}
              onChange={(e) => setNewAgent({ ...newAgent, name: e.target.value })}
              placeholder="输入 Agent 名称"
              style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)' }}
            />
          </div>
          <div style={{ marginTop: 10 }}>
            <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>Agent 描述</label>
            <textarea
              value={newAgent.description}
              onChange={(e) => setNewAgent({ ...newAgent, description: e.target.value })}
              placeholder="输入 Agent 描述"
              style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', minHeight: 60 }}
            />
          </div>
          {renderVersionFields(
            newAgent,
            (field, value) => setNewAgent({ ...newAgent, [field]: value }),
            {
              lockLogicalId: registerMode === 'newVersion',
              lockVersion: false,
            },
          )}
          {registerMode === 'newVersion' && newAgent.logical_agent_id && (
            <p style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)', marginTop: 'var(--edp-space-2)' }}>
              已有版本：
              {agents
                .filter((a) => (a.logical_agent_id || '') === newAgent.logical_agent_id)
                .map((a) => a.version || '—')
                .join('、') || '无'}
            </p>
          )}

          <div style={{ marginTop: 15 }}>
            <button 
              className="btn" 
              onClick={() => setShowInferenceConfig(!showInferenceConfig)}
              style={{ marginBottom: 10 }}
            >
              {showInferenceConfig ? '▼ 隐藏Agent配置' : '▶ 配置Agent（可选）'}
            </button>
            {!showInferenceConfig && (
              <p style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)', margin: '0 0 8px' }}>
                展开后可配置连接、执行 Sync 预检并查看 Langfuse 项目绑定结果。
              </p>
            )}
            {showInferenceConfig && renderInferenceConfigForm(newAgent.inference_config, newAgent, false, 'create')}
          </div>

          <label
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              gap: 'var(--edp-space-2)',
              marginTop: 'var(--edp-space-3)',
              fontSize: 'var(--edp-font-md)',
              lineHeight: 1.5,
              cursor: 'pointer',
            }}
          >
            <input
              type="checkbox"
              checked={seedDefaultCheckpoints}
              onChange={(e) => setSeedDefaultCheckpoints(e.target.checked)}
              style={{ marginTop: 3 }}
            />
            <span>
              预置 onto-agent 默认识别过程点（5 条必达基线，写入「Agent 过程点」配置，不影响场景子步骤）
            </span>
          </label>

          <div style={{ marginTop: 15, display: 'flex', gap: 10 }}>
            <button className="btn btn-primary" onClick={handleRegister} disabled={registering} style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              {registering ? (
                <>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite' }}>
                    <circle cx="12" cy="12" r="10"/>
                    <path d="M12 6v6l4 2"/>
                  </svg>
                  保存中...
                </>
              ) : (
                <>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
                    <polyline points="17 21 17 13 7 13 7 21"/>
                    <polyline points="7 3 7 8 15 8"/>
                  </svg>
                  保存
                </>
              )}
            </button>
            <button className="btn" onClick={() => { setShowRegister(false); setShowInferenceConfig(false) }} disabled={registering}>
              取消
            </button>
          </div>
        </div>
      )}

      {/* Agent列表 */}
      <div className="card">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 15 }}>
          <h2 style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
              <circle cx="12" cy="7" r="4"/>
            </svg>
            Agent管理
          </h2>
          <button
            className="btn btn-primary"
            onClick={() => {
              if (showRegister) {
                setShowRegister(false)
                setRegisterMode('create')
                setNewVersionParentId(null)
              } else {
                setRegisterMode('create')
                setNewVersionParentId(null)
                setShowInferenceConfig(true)
                setShowRegister(true)
              }
            }}
          >
            {showRegister ? (
            <>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
              关闭
            </>
          ) : (
            <>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <line x1="12" y1="5" x2="12" y2="19"/>
                <line x1="5" y1="12" x2="19" y2="12"/>
              </svg>
              新增 Agent
            </>
          )}
          </button>
        </div>

        <div
          style={{
            marginBottom: 'var(--edp-space-3)',
            padding: '10px 14px',
            background: 'var(--edp-primary-soft)',
            borderRadius: 'var(--edp-radius-sm)',
            fontSize: 'var(--edp-font-md)',
            color: 'var(--edp-text)',
            lineHeight: 1.5,
          }}
        >
          <strong>版本模型：</strong>列表中<strong>每一行 = 一个可评测的 Agent 版本</strong>（独立
          <code>agent_id</code>）。同一逻辑 Agent 多版本会显示多行；发版请点「注册新版本」，保留旧版以便对比打分。
        </div>

        {agents.length > 0 && (
          <div style={{ marginBottom: 'var(--edp-space-3)', display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 'var(--edp-space-2)' }}>
            <label htmlFor="filter-logical-id">按逻辑 Agent ID 筛选:</label>
            <input
              id="filter-logical-id"
              type="text"
              list="agent-logical-id-options"
              value={filterLogicalId}
              onChange={(e) => setFilterLogicalId(e.target.value)}
              placeholder="输入逻辑 ID 片段"
              style={{ padding: '6px 10px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', width: 220 }}
            />
            <datalist id="agent-logical-id-options">
              {logicalIdOptions.map((id) => (
                <option key={id} value={id} />
              ))}
            </datalist>
            {filterQuery && (
              <>
                <button
                  type="button"
                  className="btn"
                  style={{ padding: '6px 12px' }}
                  onClick={() => setFilterLogicalId('')}
                >
                  清除筛选
                </button>
                <span style={{ fontSize: 'var(--edp-font-md)', color: 'var(--edp-text2)' }}>
                  共 {agents.length} 个版本，筛选后 {filteredAgents.length} 个
                </span>
              </>
            )}
          </div>
        )}

        {agents.length === 0 ? (
          <p style={{ color: 'var(--edp-text2)' }}>暂无 Agent</p>
        ) : filteredAgents.length === 0 ? (
          <p style={{ color: 'var(--edp-text2)' }}>
            无匹配的 Agent 版本，请调整筛选条件或{' '}
            <button
              type="button"
              style={{ padding: 0, border: 'none', background: 'none', color: 'var(--edp-primary)', cursor: 'pointer', textDecoration: 'underline' }}
              onClick={() => setFilterLogicalId('')}
            >
              清除筛选
            </button>
          </p>
        ) : (
          <div className="agent-table-wrap">
          <table className="results-table agent-table">
            <colgroup>
              <col style={{ width: '7%' }} />
              <col style={{ width: '9%' }} />
              <col style={{ width: '9%' }} />
              <col style={{ width: '6%' }} />
              <col style={{ width: '6%' }} />
              <col style={{ width: '11%' }} />
              <col style={{ width: '9%' }} />
              <col style={{ width: '8%' }} />
              <col style={{ width: '8%' }} />
              <col style={{ width: '150px' }} />
              <col style={{ width: '220px' }} />
            </colgroup>
            <thead>
              <tr>
                <th style={{ textAlign: 'center' }}>名称</th>
                <th style={{ textAlign: 'center' }}>逻辑 ID</th>
                <th style={{ textAlign: 'center' }}>工作空间</th>
                <th style={{ textAlign: 'center' }}>版本</th>
                <th style={{ textAlign: 'center' }}>标签</th>
                <th style={{ textAlign: 'center' }}>Langfuse 项目</th>
                <th style={{ textAlign: 'center' }}>描述</th>
                <th style={{ textAlign: 'center' }}>Agent配置</th>
                <th style={{ textAlign: 'center' }}>状态</th>
                <th style={{ textAlign: 'center' }}>创建时间</th>
                <th style={{ textAlign: 'center' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {filteredAgents.map(agent => (
                <tr key={agent.id}>
                  <td style={{ textAlign: 'center' }}>
                    <span>{agent.name}</span>
                    {agent.agent_kind === 'workflow' && (
                      <span style={{ marginLeft: 6, padding: '1px 6px', fontSize: 'var(--edp-font-sm)', background: 'var(--edp-green-soft)', color: 'var(--edp-green)', borderRadius: 'var(--edp-radius-sm)', border: '1px solid var(--edp-green-soft)' }}>
                        工作流
                      </span>
                    )}
                  </td>
                  <td style={{ textAlign: 'center' }}>
                    {agent.logical_agent_id ? (
                      <button
                        type="button"
                        style={{ padding: 0, border: 'none', background: 'none', color: 'var(--edp-primary)', cursor: 'pointer' }}
                        title="点击按此逻辑 ID 筛选"
                        onClick={() => setFilterLogicalId(agent.logical_agent_id || '')}
                      >
                        <code>{agent.logical_agent_id}</code>
                      </button>
                    ) : (
                      <code>-</code>
                    )}
                  </td>
                  <td className="cell-ellipsis" style={{ textAlign: 'center' }} title={agent.inference_config?.workspace_name || ''}>
                    {agent.inference_config?.workspace_name || '-'}
                  </td>
                  <td style={{ textAlign: 'center' }}>{agent.version || '-'}</td>
                  <td style={{ textAlign: 'center' }}>{agent.version_tag || '-'}</td>
                  <td className="cell-ellipsis" style={{ textAlign: 'center' }} title={agent.langfuse_project ? formatLangfuseProjectLabel(agent.langfuse_project) : undefined}>
                    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 'var(--edp-space-1)' }}>
                      <span>{formatLangfuseProjectLabel(agent.langfuse_project)}</span>
                      <LangfuseReadinessBadge status={getLangfuseReadinessMeta(agent).status} />
                    </div>
                  </td>
                  <td style={{ textAlign: 'center' }}>{agent.description || '-'}</td>
                  <td style={{ textAlign: 'center' }}>
                    {agent.inference_config?.url ? (
                      <span style={{ color: 'var(--edp-green)', display: 'flex', alignItems: 'center', gap: '4px', justifyContent: 'center' }}>
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--edp-green)" strokeWidth="2">
                        <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                        <polyline points="22 4 12 14.01 9 11.01"/>
                      </svg>
                      已配置
                    </span>
                    ) : (
                      <span style={{ color: 'var(--edp-text3)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>未配置</span>
                    )}
                  </td>
                  <td style={{ textAlign: 'center' }}>
                    {(() => {
                      const hasToken = agent.inference_config?.auth?.token && agent.inference_config.auth.token.length > 0
                      return (
                        <span className={`status-badge status-${hasToken ? 'completed' : 'failed'}`}>
                          {hasToken ? (
                            <>
                              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--edp-green)" strokeWidth="2">
                                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                                <polyline points="22 4 12 14.01 9 11.01"/>
                              </svg>
                              已激活
                            </>
                          ) : (
                            <>
                              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--edp-red)" strokeWidth="2">
                                <circle cx="12" cy="12" r="10"/>
                                <line x1="15" y1="9" x2="9" y2="15"/>
                                <line x1="9" y1="9" x2="15" y2="15"/>
                              </svg>
                              未激活
                            </>
                          )}
                        </span>
                      )
                    })()}
                  </td>
                  <td style={{ textAlign: 'center' }}>{formatDate(agent.created_at)}</td>
                  <td className="cell-actions">
                    <div>
                      {(() => {
                        const needsActivation = agent.inference_config?.auth?.type === 'basic_auth' && 
                          (!agent.inference_config?.auth?.token || agent.inference_config.auth.token.length === 0)
                        return needsActivation ? (
                          <button
                            className="btn"
                            style={{ padding: '6px', background: 'var(--edp-green)', color: 'white' }}
                            onClick={() => {
                              setEditingAgent({
                                ...agent,
                                inference_config: agent.inference_config 
                                  ? { ...agent.inference_config, timeout: 1800 }
                                  : { ...defaultInferenceConfig }
                              })
                              setPayloadTemplateText('')
                            }}
                            disabled={deleting}
                            title="激活"
                          >
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ verticalAlign: 'middle' }}>
                              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
                            </svg>
                          </button>
                        ) : null
                      })()}
                      <button
                        className="btn"
                        style={{ padding: '6px', marginRight: 'var(--edp-space-1)' }}
                        onClick={() => startRegisterNewVersion(agent)}
                        disabled={deleting}
                        title="在同逻辑 Agent 下注册新版本（新记录）"
                      >
                        新版本
                      </button>
                      <button
                        className="btn btn-primary"
                        style={{ padding: '6px' }}
                        onClick={() => {
                          setEditingAgent({
                            ...agent,
                            inference_config: agent.inference_config 
                              ? { ...agent.inference_config, timeout: 1800 }
                              : { ...defaultInferenceConfig }
                          })
                          setPayloadTemplateText('')
                        }}
                        disabled={deleting}
                        title="编辑"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ verticalAlign: 'middle' }}>
                          <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                          <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                        </svg>
                      </button>
                      <button
                        className="btn btn-danger"
                        style={{ padding: '6px' }}
                        onClick={() => handleDelete(agent.id)}
                        disabled={deleting}
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
          </div>
        )}
      </div>

      {/* 编辑表单 */}
      {editingAgent && (
        <div className="card" style={{ marginTop: 'var(--edp-space-5)', borderLeft: '4px solid var(--edp-primary)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h3 style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
            </svg>
            编辑 Agent: {editingAgent.name}
          </h3>
            <button className="btn" style={{ padding: '4px 12px', display: 'flex', alignItems: 'center', gap: '4px' }} onClick={() => { setEditingAgent(null); setPayloadTemplateText('') }}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
              关闭
            </button>
          </div>
          
          <div style={{ marginTop: 15 }}>
            <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>Agent 名称</label>
            <input
              type="text"
              value={editingAgent.name}
              onChange={(e) => setEditingAgent({ ...editingAgent, name: e.target.value })}
              style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)' }}
            />
            <p style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)', marginTop: 'var(--edp-space-1)' }}>
              {editingAgent.logical_agent_id
                ? `保存时将同步更新逻辑 Agent「${editingAgent.logical_agent_id}」下的所有版本名称。`
                : '无逻辑 Agent ID 时仅更新当前记录。'}
            </p>
          </div>
          
          <div style={{ marginTop: 10 }}>
            <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>Agent 描述</label>
            <textarea
              value={editingAgent.description || ''}
              onChange={(e) => setEditingAgent({ ...editingAgent, description: e.target.value })}
              style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', minHeight: 60 }}
            />
          </div>
          {renderVersionFields(
            editingAgent,
            (field, value) => setEditingAgent({ ...editingAgent, [field]: value }),
            { lockLogicalId: true, lockVersion: true },
          )}

          {editingAgent.inference_config && renderInferenceConfigForm(editingAgent.inference_config, editingAgent, true, 'edit')}

          <div style={{ marginTop: 15, display: 'flex', gap: 10 }}>
            <button className="btn btn-primary" onClick={handleUpdate} disabled={registering} style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              {registering ? (
                <>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite' }}>
                    <circle cx="12" cy="12" r="10"/>
                    <path d="M12 6v6l4 2"/>
                  </svg>
                  保存中...
                </>
              ) : (
                <>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
                    <polyline points="17 21 17 13 7 13 7 21"/>
                    <polyline points="7 3 7 8 15 8"/>
                  </svg>
                  保存
                </>
              )}
            </button>
            <button className="btn" onClick={() => { setEditingAgent(null); setPayloadTemplateText('') }} disabled={registering}>
              取消
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
