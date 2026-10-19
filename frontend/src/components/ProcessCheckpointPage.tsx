import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type DragEvent,
} from 'react'
import axios from 'axios'
import { ScenarioItemStepsPanel } from './ScenarioItemStepsPanel'
import { AgentCheckpointReferencePanel } from './AgentCheckpointReferencePanel'
import { AgentCheckpointWorkbench } from './AgentCheckpointWorkbench'
import {
  CheckpointRequiredTipBody,
  EnforceOrderTipBody,
  HelpTip,
  HistoricalPercentileTipBody,
} from './HelpTip'
import { CHECKPOINT_REQUIRED, ENFORCE_ORDER_CHECKPOINT } from './helpCopy'
import { PageHeader } from './PageHeader'

const API_BASE = '/api'

interface AgentOption {
  id: string
  name: string
  logical_agent_id: string
  langfuse_project?: string
}

interface CheckpointTemplate {
  id: string
  logical_agent_id: string
  agent_name: string
  name: string
  description: string | null
  order: number | null
  enforce_order?: boolean
  required: boolean
  match_rules: { mode: string; rules: unknown[] }
  score_weight: number
  match_mode: string
  enabled: boolean
  template_set_name: string | null
  created_at: string
  updated_at: string
}

interface AgentRow {
  id: string
  name: string
  logical_agent_id?: string | null
  langfuse_project?: string | null
}

function formatAgentLabel(opt: Pick<AgentOption, 'name' | 'logical_agent_id'>): string {
  const name = (opt.name || '').trim() || '—'
  const lid = (opt.logical_agent_id || '').trim()
  return lid ? `${name}（${lid}）` : name
}

function langfuseProjectForAgent(
  options: AgentOption[],
  agentName: string,
): string {
  const name = agentName.trim()
  if (!name) return ''
  const hit = options.find((opt) => opt.name.trim() === name)
  return (hit?.langfuse_project || '').trim()
}

function formatApiError(err: unknown, fallback: string): string {
  const ax = err as {
    response?: { status?: number; data?: { detail?: unknown; message?: string } }
    message?: string
  }
  const detail = ax.response?.data?.detail
  if (typeof detail === 'string' && detail && detail !== 'Not Found') return detail
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (item && typeof item === 'object' && 'msg' in item) {
          return String((item as { msg: string }).msg)
        }
        return String(item)
      })
      .filter(Boolean)
    if (parts.length) return parts.join('；')
  }
  if (ax.response?.status === 404) {
    return '过程点接口未找到，请确认后端已启动且已加载 checkpoints 路由（需重启 backend）'
  }
  if (!ax.response && ax.message) {
    return `网络或服务异常：${ax.message}`
  }
  return fallback
}

const DEFAULT_MATCH_RULES = `{
  "mode": "any",
  "rules": [
    { "type": "observation_name", "op": "contains", "value": "retrieve" }
  ]
}`

function suggestNextOrderFromList(items: CheckpointTemplate[], agentName: string): number {
  const orders = items
    .filter((t) => t.agent_name === agentName)
    .map((t) => t.order)
    .filter((o): o is number => o != null && o >= 1)
  return orders.length ? Math.max(...orders) + 1 : 1
}

function sortTemplates(items: CheckpointTemplate[]): CheckpointTemplate[] {
  return [...items].sort((a, b) => {
    const ao = a.order ?? Number.MAX_SAFE_INTEGER
    const bo = b.order ?? Number.MAX_SAFE_INTEGER
    if (ao !== bo) return ao - bo
    return a.name.localeCompare(b.name, 'zh-CN')
  })
}

interface PreviewSampleRun {
  agent_run_id: string
  trace_id: string
  question_index: number | null
  question_preview: string
  started_at: string
  has_span_tree: boolean
}

interface PreviewHitSummary {
  hit: boolean
  matched_observation_name: string | null
  observation_count: number
  observation_source?: string
}

function formatPreviewRunLabel(run: PreviewSampleRun): string {
  const q =
    run.question_index != null
      ? `Q${run.question_index + 1}`
      : run.question_preview.slice(0, 24) || 'Run'
  const time = run.started_at ? run.started_at.replace('T', ' ').slice(0, 16) : ''
  const spanHint = run.has_span_tree ? '已物化' : '待同步'
  return `${q} · ${time} · ${spanHint}`
}

function parsePreviewResponse(data: unknown): PreviewHitSummary {
  const d = data as {
    hit?: boolean
    matched_observation_name?: string | null
    observation_count?: number
    observation_source?: string
    results?: Array<{ hit?: boolean; matched_observation_name?: string | null }>
  }
  const first = d.results?.[0]
  return {
    hit: Boolean(first?.hit ?? d.hit),
    matched_observation_name: first?.matched_observation_name ?? d.matched_observation_name ?? null,
    observation_count: d.observation_count ?? 0,
    observation_source: d.observation_source,
  }
}

const emptyForm = () => ({
  agent_name: '',
  name: '',
  description: '',
  order: '' as string | number,
  enforce_order: false,
  required: true,
  score_weight: 1,
  match_mode: 'rule',
  template_set_name: '',
  enabled: true,
  match_rules_text: DEFAULT_MATCH_RULES,
})

const inputStyle: CSSProperties = {
  width: '100%',
  padding: '8px 12px',
  border: '1px solid #ddd',
  borderRadius: 4,
}

type ConfigTab = 'agent_checkpoint' | 'scenario_item_steps'

interface ProcessCheckpointPageProps {
  onNavigateToOnlineEval?: (agentId: string) => void
}

export function ProcessCheckpointPage({ onNavigateToOnlineEval }: ProcessCheckpointPageProps) {
  const [configTab, setConfigTab] = useState<ConfigTab>('scenario_item_steps')
  const [agentOptions, setAgentOptions] = useState<AgentOption[]>([])
  const [filterAgentName, setFilterAgentName] = useState('')
  const [templates, setTemplates] = useState<CheckpointTemplate[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState<CheckpointTemplate | null>(null)
  const [form, setForm] = useState(emptyForm())
  const [saving, setSaving] = useState(false)
  const [previewTraceId, setPreviewTraceId] = useState('')
  const [previewProject, setPreviewProject] = useState('')
  const [previewResult, setPreviewResult] = useState('')
  const [previewSummary, setPreviewSummary] = useState<PreviewHitSummary | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewSampleRuns, setPreviewSampleRuns] = useState<PreviewSampleRun[]>([])
  const [previewRunId, setPreviewRunId] = useState('')
  const [draggingId, setDraggingId] = useState<string | null>(null)
  const [dragOverId, setDragOverId] = useState<string | null>(null)
  const [reordering, setReordering] = useState(false)
  const [normalizing, setNormalizing] = useState(false)
  const [historicalPerf, setHistoricalPerf] = useState<
    Record<string, { latency_p75?: number | null; tokens_p75?: number | null }>
  >({})
  const [formError, setFormError] = useState('')
  const [invalidFormFields, setInvalidFormFields] = useState<Set<string>>(() => new Set())
  const formSectionRef = useRef<HTMLDivElement>(null)

  const canDragReorder = Boolean(filterAgentName.trim())
  const displayTemplates = useMemo(() => sortTemplates(templates), [templates])
  const configuredCheckpointNames = useMemo(
    () => displayTemplates.map((t) => t.name),
    [displayTemplates],
  )
  const weightSummary = useMemo(() => {
    const enabled = displayTemplates.filter((t) => t.enabled)
    const weightSum = enabled.reduce((acc, t) => acc + Number(t.score_weight || 0), 0)
    return {
      enabled_count: enabled.length,
      total_count: displayTemplates.length,
      weight_sum: weightSum,
      normalized: enabled.length > 0 && Math.abs(weightSum - 1) <= 0.01,
    }
  }, [displayTemplates])

  const selectedAgentId = useMemo(() => {
    const name = filterAgentName.trim()
    if (!name) return ''
    return agentOptions.find((o) => o.name === name)?.id ?? ''
  }, [filterAgentName, agentOptions])

  const fetchAgentOptions = useCallback(async () => {
    try {
      const res = await axios.get<AgentRow[]>(`${API_BASE}/agents`)
      const byLid = new Map<string, AgentOption>()
      for (const a of res.data) {
        const lid = (a.logical_agent_id || '').trim()
        const key = lid || a.id
        const name = (a.name || '').trim()
        const langfuseProject = (a.langfuse_project || '').trim()
        if (!name) continue
        const existing = byLid.get(key)
        if (!existing) {
          byLid.set(key, {
            id: a.id,
            name,
            logical_agent_id: lid,
            langfuse_project: langfuseProject,
          })
        } else if (!existing.langfuse_project && langfuseProject) {
          existing.langfuse_project = langfuseProject
        }
      }
      setAgentOptions(
        [...byLid.values()].sort((x, y) =>
          formatAgentLabel(x).localeCompare(formatAgentLabel(y), 'zh-CN'),
        ),
      )
    } catch {
      try {
        const res = await axios.get(`${API_BASE}/checkpoints/agent-names`)
        const names: string[] = res.data.agent_names || []
        setAgentOptions(names.map((name) => ({ id: '', name, logical_agent_id: '' })))
      } catch {
        setAgentOptions([])
      }
    }
  }, [])

  const fetchTemplates = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const params = filterAgentName ? { agent_name: filterAgentName } : {}
      const res = await axios.get<CheckpointTemplate[]>(`${API_BASE}/checkpoints/templates`, {
        params,
      })
      setTemplates(res.data)
    } catch (err: unknown) {
      setError(formatApiError(err, '加载过程点失败'))
    } finally {
      setLoading(false)
    }
  }, [filterAgentName])

  useEffect(() => {
    void fetchAgentOptions()
  }, [fetchAgentOptions])

  useEffect(() => {
    void fetchTemplates()
  }, [fetchTemplates])

  useEffect(() => {
    const opt = agentOptions.find(o => o.name === filterAgentName.trim())
    const lid = (opt?.logical_agent_id || '').trim()
    if (!lid) {
      setHistoricalPerf({})
      return
    }
    void axios
      .get(`${API_BASE}/checkpoints/historical-performance`, {
        params: { logical_agent_id: lid },
      })
      .then(res => {
        const map: Record<string, { latency_p75?: number | null; tokens_p75?: number | null }> = {}
        for (const row of res.data?.rows || []) {
          map[row.checkpoint_id] = {
            latency_p75: row.latency_ms?.p75,
            tokens_p75: row.tokens?.p75,
          }
        }
        setHistoricalPerf(map)
      })
      .catch(() => setHistoricalPerf({}))
  }, [filterAgentName, agentOptions])

  useEffect(() => {
    const agentName = filterAgentName.trim() || form.agent_name.trim()
    const bound = langfuseProjectForAgent(agentOptions, agentName)
    if (bound) {
      setPreviewProject(bound)
    }
  }, [filterAgentName, form.agent_name, agentOptions])

  const suggestOrderForAgent = useCallback(async (agentName: string): Promise<number> => {
    const name = agentName.trim()
    if (!name) return 1
    try {
      const res = await axios.get<CheckpointTemplate[]>(`${API_BASE}/checkpoints/templates`, {
        params: { agent_name: name },
      })
      return suggestNextOrderFromList(res.data, name)
    } catch {
      return suggestNextOrderFromList(templates, name)
    }
  }, [templates])

  const clearFormValidation = () => {
    setFormError('')
    setInvalidFormFields(new Set())
  }

  const clearPreviewState = () => {
    setPreviewResult('')
    setPreviewSummary(null)
    setPreviewError('')
  }

  const fetchPreviewSampleRuns = useCallback(async (agentName: string) => {
    const name = agentName.trim()
    if (!name) {
      setPreviewSampleRuns([])
      setPreviewRunId('')
      return
    }
    try {
      const res = await axios.get<{ runs: PreviewSampleRun[] }>(
        `${API_BASE}/checkpoints/sample-runs`,
        { params: { agent_name: name, limit: 10 } },
      )
      const runs = res.data.runs || []
      setPreviewSampleRuns(runs)
      setPreviewRunId((prev) =>
        prev && runs.some((r) => r.agent_run_id === prev) ? prev : runs[0]?.agent_run_id ?? '',
      )
      if (runs[0]?.trace_id) {
        setPreviewTraceId((prev) => prev.trim() || runs[0].trace_id)
      }
    } catch {
      setPreviewSampleRuns([])
      setPreviewRunId('')
    }
  }, [])

  useEffect(() => {
    if (!showForm) return
    const agentName = (filterAgentName || form.agent_name).trim()
    if (agentName) {
      void fetchPreviewSampleRuns(agentName)
    }
  }, [showForm, filterAgentName, form.agent_name, fetchPreviewSampleRuns])

  const openCreate = () => {
    setEditing(null)
    clearPreviewState()
    clearFormValidation()
    const agentName = filterAgentName || ''
    setForm({
      ...emptyForm(),
      agent_name: agentName,
      order: agentName ? suggestNextOrderFromList(templates, agentName) : 1,
    })
    setShowForm(true)
    if (agentName) {
      void suggestOrderForAgent(agentName).then((nextOrder) => {
        setForm((prev) => (prev.agent_name === agentName ? { ...prev, order: nextOrder } : prev))
      })
    }
  }

  const closeForm = () => {
    setShowForm(false)
    setEditing(null)
    clearPreviewState()
    clearFormValidation()
  }

  const openEdit = (t: CheckpointTemplate) => {
    clearPreviewState()
    clearFormValidation()
    setEditing(t)
    setForm({
      agent_name: t.agent_name || '',
      name: t.name,
      description: t.description || '',
      order: t.order ?? '',
      enforce_order: Boolean(t.enforce_order),
      required: t.required,
      score_weight: t.score_weight,
      match_mode: t.match_mode,
      template_set_name: t.template_set_name || '',
      enabled: t.enabled,
      match_rules_text: JSON.stringify(t.match_rules, null, 2),
    })
    setPreviewResult('')
    const bound = langfuseProjectForAgent(agentOptions, t.agent_name || '')
    if (bound) {
      setPreviewProject(bound)
    }
    setShowForm(true)
  }

  const parseMatchRules = (): Record<string, unknown> => {
    return JSON.parse(form.match_rules_text) as Record<string, unknown>
  }

  const validateForm = (): boolean => {
    const invalid = new Set<string>()
    const agentName = form.agent_name.trim()
    const name = form.name.trim()
    const messages: string[] = []

    if (!agentName) {
      invalid.add('agent_name')
      messages.push('请选择 Agent')
    }
    if (!name) {
      invalid.add('name')
      messages.push('请填写过程点名称')
    } else if (
      displayTemplates.some(
        (t) => t.name === name && (!editing || t.id !== editing.id),
      )
    ) {
      invalid.add('name')
      messages.push('该 Agent 下已存在同名过程点，请更换名称')
    }

    try {
      parseMatchRules()
    } catch {
      invalid.add('match_rules')
      messages.push('match_rules 不是合法 JSON，请检查格式')
    }

    if (messages.length > 0) {
      setInvalidFormFields(invalid)
      setFormError(messages.join('；'))
      return false
    }

    setInvalidFormFields(new Set())
    setFormError('')
    return true
  }

  useEffect(() => {
    if (!showForm) return
    formSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [showForm, editing?.id])

  const handleValidateRules = async () => {
    try {
      const rules = parseMatchRules()
      const res = await axios.post(`${API_BASE}/checkpoints/templates/validate-rules`, rules)
      setPreviewResult(JSON.stringify(res.data, null, 2))
      setError('')
    } catch (err: unknown) {
      setPreviewResult('')
      setError(formatApiError(err, '规则校验失败'))
    }
  }

  const handleSave = async () => {
    if (!validateForm()) return
    setSaving(true)
    setError('')
    try {
      const rules = parseMatchRules()
      const payload = {
        agent_name: form.agent_name.trim(),
        name: form.name.trim(),
        description: form.description.trim() || null,
        order: form.order === '' ? null : Number(form.order),
        enforce_order: form.enforce_order,
        required: form.required,
        match_rules: rules,
        score_weight: Number(form.score_weight),
        match_mode: form.match_mode,
        template_set_name: form.template_set_name.trim() || null,
        enabled: form.enabled,
      }
      if (editing) {
        const { agent_name: _omit, ...updateBody } = payload
        await axios.put(`${API_BASE}/checkpoints/templates/${editing.id}`, updateBody)
      } else {
        await axios.post(`${API_BASE}/checkpoints/templates`, payload)
      }
      closeForm()
      await fetchTemplates()
      await fetchAgentOptions()
    } catch (err: unknown) {
      setError(formatApiError(err, '保存失败'))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (t: CheckpointTemplate) => {
    if (t.enabled) {
      alert(`过程点「${t.name}」当前为启用状态，无法删除。请先点击「禁用」，再删除。`)
      return
    }
    if (!window.confirm(`删除过程点「${t.name}」？`)) return
    try {
      await axios.delete(`${API_BASE}/checkpoints/templates/${t.id}`)
      await fetchTemplates()
    } catch (err: unknown) {
      setError(formatApiError(err, '删除失败'))
    }
  }

  const handleToggleEnabled = async (t: CheckpointTemplate) => {
    try {
      await axios.put(`${API_BASE}/checkpoints/templates/${t.id}`, { enabled: !t.enabled })
      await fetchTemplates()
    } catch (err: unknown) {
      setError(formatApiError(err, '更新失败'))
    }
  }

  const handleNormalizeWeights = async () => {
    const agentName = filterAgentName.trim()
    if (!agentName) return
    setNormalizing(true)
    setError('')
    try {
      await axios.post(`${API_BASE}/checkpoints/templates/normalize-weights`, {
        agent_name: agentName,
      })
      await fetchTemplates()
    } catch (err: unknown) {
      setError(formatApiError(err, '权重归一化失败'))
    } finally {
      setNormalizing(false)
    }
  }

  const handleToggleEnforceOrder = async (t: CheckpointTemplate) => {
    try {
      await axios.put(`${API_BASE}/checkpoints/templates/${t.id}`, {
        enforce_order: !t.enforce_order,
      })
      await fetchTemplates()
    } catch (err: unknown) {
      setError(formatApiError(err, '更新顺序依赖失败'))
    }
  }

  const persistReorder = async (ordered: CheckpointTemplate[]) => {
    if (!canDragReorder) return
    setReordering(true)
    setError('')
    const previous = templates
    setTemplates(ordered.map((t, index) => ({ ...t, order: index + 1 })))
    try {
      await axios.put(`${API_BASE}/checkpoints/templates/reorder`, {
        agent_name: filterAgentName.trim(),
        template_ids: ordered.map((t) => t.id),
      })
    } catch (err: unknown) {
      setTemplates(previous)
      setError(formatApiError(err, '更新顺序失败'))
    } finally {
      setReordering(false)
      setDraggingId(null)
      setDragOverId(null)
    }
  }

  const handleDragStart = (id: string) => (event: DragEvent<HTMLSpanElement>) => {
    if (!canDragReorder || reordering) {
      event.preventDefault()
      return
    }
    setDraggingId(id)
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('text/plain', id)
  }

  const handleDragOver = (id: string) => (event: DragEvent<HTMLTableRowElement>) => {
    if (!canDragReorder || !draggingId || draggingId === id) return
    event.preventDefault()
    event.dataTransfer.dropEffect = 'move'
    setDragOverId(id)
  }

  const handleDrop = (targetId: string) => (event: DragEvent<HTMLTableRowElement>) => {
    event.preventDefault()
    const sourceId = draggingId || event.dataTransfer.getData('text/plain')
    setDragOverId(null)
    if (!canDragReorder || !sourceId || sourceId === targetId || reordering) {
      setDraggingId(null)
      return
    }

    const fromIdx = displayTemplates.findIndex((t) => t.id === sourceId)
    const toIdx = displayTemplates.findIndex((t) => t.id === targetId)
    if (fromIdx < 0 || toIdx < 0) {
      setDraggingId(null)
      return
    }

    const next = [...displayTemplates]
    const [moved] = next.splice(fromIdx, 1)
    next.splice(toIdx, 0, moved)
    void persistReorder(next)
  }

  const handleDragEnd = () => {
    setDraggingId(null)
    setDragOverId(null)
  }

  const handlePreview = async () => {
    const traceId = previewTraceId.trim()
    if (!traceId) {
      setPreviewError('请填写 trace_id，或从下方「最近 Run」中选择一条')
      setPreviewSummary(null)
      setPreviewResult('')
      return
    }

    const agentName = (form.agent_name || filterAgentName).trim()
    if (!agentName) {
      setPreviewError('请先选择 Agent')
      return
    }

    let rules: Record<string, unknown>
    try {
      rules = parseMatchRules()
    } catch {
      setPreviewError('match_rules 不是合法 JSON，无法试跑')
      return
    }

    setPreviewLoading(true)
    setPreviewError('')
    setError('')
    try {
      const res = editing
        ? await axios.post(
            `${API_BASE}/checkpoints/templates/${editing.id}/preview`,
            {
              trace_id: traceId,
              project: previewProject.trim() || null,
              match_rules: rules,
            },
          )
        : await axios.post(`${API_BASE}/checkpoints/templates/preview-match`, {
            trace_id: traceId,
            project: previewProject.trim() || null,
            agent_name: agentName,
            checkpoint_name: form.name.trim() || '未命名过程点',
            order: form.order === '' ? null : Number(form.order),
            required: form.required,
            match_rules: rules,
          })

      const summary = parsePreviewResponse(res.data)
      setPreviewSummary(summary)
      setPreviewResult(JSON.stringify(res.data, null, 2))
    } catch (err: unknown) {
      setPreviewError(formatApiError(err, '试跑匹配失败'))
      setPreviewSummary(null)
      setPreviewResult('')
    } finally {
      setPreviewLoading(false)
    }
  }

  return (
    <div className="page-container checkpoint-page">
      <div className="card history-card checkpoint-card">
        <PageHeader
          title="过程点 / 场景子步骤配置"
          description={
            <>
              Agent 过程点绑定 Langfuse 规则；场景子步骤按评测集题目配置 GT 与权重。
              <br />
              多轮稳定性中的<strong>顺序依赖</strong>可在子步骤列表勾选，或在「Agent 过程点」Tab 配置；默认均关闭。
              子步骤 Span 绑定可在下方「Trace 绑定验证」中试跑（与 Agent 过程点 Trace 验证同一流程）。
            </>
          }
          icon={
            <svg
              className="page-title-icon"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              aria-hidden
            >
              <circle cx="12" cy="12" r="10"/>
              <path d="M12 6v6l4 2"/>
            </svg>
          }
        />

        <div className="sub-tabs checkpoint-tabs">
          <button
            type="button"
            className={`sub-tab ${configTab === 'scenario_item_steps' ? 'active' : ''}`}
            onClick={() => setConfigTab('scenario_item_steps')}
          >
            场景子步骤（按题）
          </button>
          <button
            type="button"
            className={`sub-tab ${configTab === 'agent_checkpoint' ? 'active' : ''}`}
            onClick={() => setConfigTab('agent_checkpoint')}
          >
            Agent 过程点
          </button>
        </div>

        {configTab === 'scenario_item_steps' && (
          <>
            <ScenarioItemStepsPanel
              checkpointAgentOptions={agentOptions}
              onNavigateToOnlineEval={onNavigateToOnlineEval}
            />
          </>
        )}

        {configTab === 'agent_checkpoint' && (
      <div className="config-page-panel">
        {error && <div className="alert-error">{error}</div>}

        <AgentCheckpointWorkbench
          agentName={filterAgentName}
          agentId={selectedAgentId}
          agentOptions={agentOptions}
          onAgentChange={setFilterAgentName}
          formatAgentLabel={formatAgentLabel}
          templateCount={displayTemplates.length}
          weightSummary={weightSummary}
          onNavigateOnlineEval={onNavigateToOnlineEval}
          onRefreshTemplates={fetchTemplates}
          onNormalizeWeights={handleNormalizeWeights}
          normalizing={normalizing}
          formatApiError={formatApiError}
          initSection={
            <AgentCheckpointReferencePanel
              embedded
              agentName={filterAgentName}
              configuredNames={configuredCheckpointNames}
              onSeeded={fetchTemplates}
              formatApiError={formatApiError}
            />
          }
          configSection={
            <>
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  flexWrap: 'wrap',
                  gap: 10,
                  marginBottom: 12,
                }}
              >
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={!filterAgentName.trim()}
                  onClick={() => (showForm ? closeForm() : openCreate())}
                >
                  {showForm ? '关闭表单' : '新增过程点'}
                </button>
                {reordering && (
                  <span style={{ fontSize: 13, color: '#666' }}>正在保存顺序…</span>
                )}
              </div>

              {loading ? (
                <p style={{ color: '#666', fontSize: 13 }}>加载中…</p>
              ) : displayTemplates.length === 0 ? (
                <p style={{ color: '#666', fontSize: 13 }}>
                  暂无过程点，请在上一步初始化参考模板，或点击「新增过程点」。
                </p>
              ) : (
                <>
                  <p style={{ margin: '0 0 10px', fontSize: 12, color: '#666', lineHeight: 1.6 }}>
                    {CHECKPOINT_REQUIRED.tableHint} {CHECKPOINT_REQUIRED.body}
                  </p>
                  <div className="checkpoint-table-wrap">
                    <table className="results-table checkpoint-table">
                      <thead>
                        <tr>
                          {canDragReorder && <th style={{ width: 44, textAlign: 'center' }}>排序</th>}
                          {!canDragReorder && (
                            <th style={{ textAlign: 'center' }}>Agent 名称（逻辑 Agent ID）</th>
                          )}
                          <th style={{ textAlign: 'center' }}>过程点</th>
                          <th style={{ textAlign: 'center' }}>执行顺序</th>
                          <th style={{ textAlign: 'center' }}>
                            <HelpTip
                              label="顺序依赖"
                              tip={<EnforceOrderTipBody variant="checkpoint" />}
                            />
                          </th>
                          <th style={{ textAlign: 'center' }}>
                            <HelpTip label="必达" tip={<CheckpointRequiredTipBody />} />
                          </th>
                          <th style={{ textAlign: 'center' }}>模式</th>
                          <th style={{ textAlign: 'center' }}>权重</th>
                          <th style={{ textAlign: 'center' }}>
                            <HelpTip
                              label="历史 P75 耗时"
                              tip={<HistoricalPercentileTipBody showFootnote />}
                            />
                          </th>
                          <th style={{ textAlign: 'center' }}>
                            <HelpTip
                              label="历史 P75 Tokens"
                              tip={<HistoricalPercentileTipBody showFootnote />}
                            />
                          </th>
                          <th style={{ textAlign: 'center' }}>状态</th>
                          <th style={{ textAlign: 'center' }}>操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {displayTemplates.map((t) => (
                          <tr
                            key={t.id}
                            onDragOver={handleDragOver(t.id)}
                            onDrop={handleDrop(t.id)}
                            style={{
                              opacity: !t.enabled ? 0.55 : draggingId === t.id ? 0.55 : 1,
                              background:
                                editing?.id === t.id
                                  ? '#eef2ff'
                                  : dragOverId === t.id
                                    ? '#f0f4ff'
                                    : undefined,
                            }}
                          >
                            {canDragReorder && (
                              <td className={`cell-drag${reordering ? ' is-disabled' : ''}`}>
                                <span
                                  draggable={!reordering}
                                  onDragStart={handleDragStart(t.id)}
                                  onDragEnd={handleDragEnd}
                                  title="拖拽调整顺序"
                                >
                                  ⋮⋮
                                </span>
                              </td>
                            )}
                            {!canDragReorder && (
                              <td style={{ textAlign: 'center' }}>
                                {formatAgentLabel({
                                  name: t.agent_name || '—',
                                  logical_agent_id: t.logical_agent_id,
                                })}
                              </td>
                            )}
                            <td style={{ textAlign: 'center' }}>{t.name}</td>
                            <td style={{ textAlign: 'center' }}>{t.order ?? '—'}</td>
                            <td style={{ textAlign: 'center' }}>
                              <input
                                type="checkbox"
                                checked={Boolean(t.enforce_order)}
                                title="启用顺序依赖：命中且须符合执行顺序"
                                onChange={() => void handleToggleEnforceOrder(t)}
                              />
                            </td>
                            <td style={{ textAlign: 'center' }}>{t.required ? '是' : '否'}</td>
                            <td style={{ textAlign: 'center' }}>{t.match_mode}</td>
                            <td style={{ textAlign: 'center' }}>{t.score_weight}</td>
                            <td>
                              {historicalPerf[t.id]?.latency_p75 != null
                                ? `${Math.round(historicalPerf[t.id].latency_p75!)}ms`
                                : '—'}
                            </td>
                            <td>
                              {historicalPerf[t.id]?.tokens_p75 != null
                                ? Math.round(historicalPerf[t.id].tokens_p75!)
                                : '—'}
                            </td>
                            <td>{t.enabled ? '启用' : '禁用'}</td>
                            <td className="cell-actions">
                              <button type="button" className="btn" onClick={() => openEdit(t)}>
                                编辑
                              </button>
                              <button type="button" className="btn" onClick={() => void handleToggleEnabled(t)}>
                                {t.enabled ? '禁用' : '启用'}
                              </button>
                              <button
                                type="button"
                                className="btn btn-danger-text"
                                onClick={() => void handleDelete(t)}
                              >
                                删除
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}

              {showForm && (
                <div
                  ref={formSectionRef}
                  style={{
                    marginTop: 16,
                    padding: 16,
                    borderRadius: 8,
                    border: editing ? '1px solid #667eea' : '1px solid #e0e0e0',
                    background: '#fcfcfc',
                    boxShadow: editing ? '0 0 0 1px rgba(102, 126, 234, 0.15)' : undefined,
                  }}
                >
                  <h3 style={{ marginTop: 0, fontSize: 16 }}>
                    {editing ? `编辑过程点：${editing.name}` : '新增过程点'}
                  </h3>
                  <p style={{ margin: '0 0 12px', fontSize: 12, color: '#666' }}>
                    表单位于列表下方，便于对照当前过程点配置。
                  </p>

                  {formError && (
                    <div
                      style={{
                        marginBottom: 12,
                        padding: '8px 12px',
                        borderRadius: 6,
                        background: '#fdecea',
                        border: '1px solid #f5c6cb',
                        color: '#c0392b',
                        fontSize: 13,
                      }}
                    >
                      {formError}
                    </div>
                  )}

                  <div style={{ marginTop: 15 }}>
                    <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>
                      Agent 名称（逻辑 Agent ID） *
                    </label>
                    <select
                      value={form.agent_name}
                      onChange={(e) => {
                        const name = e.target.value
                        if (formError) clearFormValidation()
                        if (editing) {
                          setForm({ ...form, agent_name: name })
                          return
                        }
                        void suggestOrderForAgent(name).then((nextOrder) => {
                          setForm({ ...form, agent_name: name, order: nextOrder })
                        })
                      }}
                      disabled={!!editing}
                      style={{
                        ...inputStyle,
                        border: invalidFormFields.has('agent_name')
                          ? '1px solid #c0392b'
                          : inputStyle.border,
                        ...(editing ? { background: '#f5f5f5', color: '#555' } : {}),
                      }}
                    >
                      <option value="">请选择 Agent</option>
                      {agentOptions.map((opt) => (
                        <option key={`${opt.name}-${opt.logical_agent_id}`} value={opt.name}>
                          {formatAgentLabel(opt)}
                        </option>
                      ))}
                    </select>
                    {editing && (
                      <p style={{ fontSize: 12, color: '#666', marginTop: 4 }}>
                        已绑定：{formatAgentLabel({
                          name: editing.agent_name,
                          logical_agent_id: editing.logical_agent_id,
                        })}
                      </p>
                    )}
                  </div>

                  <div style={{ marginTop: 10 }}>
                    <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>过程点名称 *</label>
                    <input
                      type="text"
                      value={form.name}
                      onChange={(e) => {
                        if (formError) clearFormValidation()
                        setForm({ ...form, name: e.target.value })
                      }}
                      placeholder="如：检索上下文、工具调用-搜索"
                      style={{
                        ...inputStyle,
                        border: invalidFormFields.has('name')
                          ? '1px solid #c0392b'
                          : inputStyle.border,
                      }}
                    />
                  </div>

                  <div style={{ marginTop: 10 }}>
                    <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>说明</label>
                    <input
                      type="text"
                      value={form.description}
                      onChange={(e) => setForm({ ...form, description: e.target.value })}
                      style={inputStyle}
                    />
                  </div>

                  <div style={{ display: 'flex', gap: 10, marginTop: 10, flexWrap: 'wrap' }}>
                    <div style={{ flex: 1, minWidth: 120 }}>
                      <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>执行顺序</label>
                      <input
                        type="number"
                        min={1}
                        value={form.order}
                        onChange={(e) => setForm({ ...form, order: e.target.value })}
                        style={inputStyle}
                        placeholder="1、2、3…"
                      />
                      <p style={{ fontSize: 12, color: '#666', margin: '4px 0 0' }}>
                        {ENFORCE_ORDER_CHECKPOINT.orderFieldHint}
                      </p>
                    </div>
                    <div style={{ flex: 1, minWidth: 120 }}>
                      <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>权重</label>
                      <input
                        type="number"
                        min={0}
                        step={0.1}
                        value={form.score_weight}
                        onChange={(e) => setForm({ ...form, score_weight: Number(e.target.value) })}
                        style={inputStyle}
                      />
                    </div>
                    <div style={{ flex: 1, minWidth: 160 }}>
                      <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>模板集</label>
                      <input
                        type="text"
                        value={form.template_set_name}
                        onChange={(e) => setForm({ ...form, template_set_name: e.target.value })}
                        style={inputStyle}
                        placeholder="可选"
                      />
                    </div>
                  </div>

                  <div style={{ marginTop: 10, display: 'flex', gap: 20, flexWrap: 'wrap', alignItems: 'center' }}>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 14 }}>
                      <input
                        type="checkbox"
                        checked={form.enforce_order}
                        onChange={(e) => setForm({ ...form, enforce_order: e.target.checked })}
                      />
                      <HelpTip
                        label="启用顺序依赖"
                        align="left"
                        tip={<EnforceOrderTipBody variant="checkpoint" />}
                      />
                    </label>
                    <span style={{ fontSize: 12, color: '#666' }}>
                      {ENFORCE_ORDER_CHECKPOINT.formHint}
                    </span>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 14 }}>
                      <input
                        type="checkbox"
                        checked={form.required}
                        onChange={(e) => setForm({ ...form, required: e.target.checked })}
                      />
                      <HelpTip label="必达" tip={<CheckpointRequiredTipBody />} />
                    </label>
                    <span style={{ fontSize: 12, color: '#666' }}>
                      {CHECKPOINT_REQUIRED.formHint}
                    </span>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 14 }}>
                      <input
                        type="checkbox"
                        checked={form.enabled}
                        onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
                      />
                      启用
                    </label>
                  </div>

                  <div style={{ marginTop: 10 }}>
                    <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>match_rules JSON *</label>
                    <textarea
                      rows={10}
                      value={form.match_rules_text}
                      onChange={(e) => {
                        if (formError) clearFormValidation()
                        setForm({ ...form, match_rules_text: e.target.value })
                      }}
                      spellCheck={false}
                      style={{
                        ...inputStyle,
                        fontFamily: 'Consolas, monospace',
                        fontSize: 13,
                        border: invalidFormFields.has('match_rules')
                          ? '1px solid #c0392b'
                          : inputStyle.border,
                      }}
                    />
                  </div>

                  <div style={{ marginTop: 12, display: 'flex', gap: 10 }}>
                    <button type="button" className="btn" onClick={() => void handleValidateRules()}>
                      校验规则
                    </button>
                  </div>

                  {showForm && (
                    <div
                      style={{
                        marginTop: 16,
                        padding: 14,
                        background: '#f8f9fc',
                        borderRadius: 8,
                        border: '1px solid #c5cae9',
                      }}
                    >
                      <h4 style={{ margin: '0 0 6px', fontSize: 15 }}>Trace 试跑匹配</h4>
                      <p style={{ margin: '0 0 10px', fontSize: 12, color: '#666', lineHeight: 1.6 }}>
                        使用当前表单中的 match_rules 在 Trace 上试跑，无需先保存。
                        {editing ? ' 已保存的过程点也可在此验证规则修改。' : ' 新增过程点保存前即可验证规则。'}
                      </p>

                      {previewError && (
                        <div
                          style={{
                            marginBottom: 10,
                            padding: '8px 12px',
                            borderRadius: 6,
                            background: '#fdecea',
                            border: '1px solid #f5c6cb',
                            color: '#c0392b',
                            fontSize: 13,
                          }}
                        >
                          {previewError}
                        </div>
                      )}

                      {previewSampleRuns.length > 0 && (
                        <div style={{ marginBottom: 10 }}>
                          <label style={{ display: 'block', marginBottom: 5, fontWeight: 500, fontSize: 13 }}>
                            最近 Run（可选）
                          </label>
                          <select
                            value={previewRunId}
                            onChange={(e) => {
                              const run = previewSampleRuns.find((r) => r.agent_run_id === e.target.value)
                              setPreviewRunId(e.target.value)
                              if (run?.trace_id) setPreviewTraceId(run.trace_id)
                            }}
                            style={{ ...inputStyle, maxWidth: 480 }}
                          >
                            {previewSampleRuns.map((run) => (
                              <option key={run.agent_run_id} value={run.agent_run_id}>
                                {formatPreviewRunLabel(run)}
                              </option>
                            ))}
                          </select>
                        </div>
                      )}

                      <div style={{ marginBottom: 8 }}>
                        <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>
                          Langfuse trace_id *
                        </label>
                        <input
                          type="text"
                          value={previewTraceId}
                          onChange={(e) => {
                            setPreviewTraceId(e.target.value)
                            if (previewError) setPreviewError('')
                          }}
                          placeholder="从 Trace 探索复制，或选上方最近 Run"
                          style={inputStyle}
                        />
                      </div>
                      <div style={{ marginBottom: 8 }}>
                        <label style={{ display: 'block', marginBottom: 5, fontWeight: 500 }}>
                          项目 id（可选，留空则用 Agent 绑定的 Langfuse 项目）
                        </label>
                        <input
                          type="text"
                          value={previewProject}
                          onChange={(e) => setPreviewProject(e.target.value)}
                          placeholder="如 cc-consume-2.0"
                          style={inputStyle}
                        />
                      </div>
                      <button
                        type="button"
                        className="btn btn-primary"
                        disabled={previewLoading}
                        onClick={() => void handlePreview()}
                      >
                        {previewLoading ? '试跑中…' : '试跑匹配'}
                      </button>

                      {previewSummary && (
                        <div
                          style={{
                            marginTop: 12,
                            padding: '10px 12px',
                            borderRadius: 6,
                            fontSize: 13,
                            background: previewSummary.hit ? '#e8f5e9' : '#fff3e0',
                            border: previewSummary.hit ? '1px solid #a5d6a7' : '1px solid #ffcc80',
                            color: previewSummary.hit ? '#1b5e20' : '#e65100',
                          }}
                        >
                          {previewSummary.hit ? '✓ 命中' : '✗ 未命中'}
                          {previewSummary.matched_observation_name
                            ? ` · 匹配 Span：${previewSummary.matched_observation_name}`
                            : ''}
                          {previewSummary.observation_count > 0
                            ? ` · ${previewSummary.observation_count} 条 observation（${previewSummary.observation_source || '—'}）`
                            : ''}
                        </div>
                      )}
                    </div>
                  )}

                  {previewResult && (
                    <pre
                      style={{
                        marginTop: 12,
                        padding: 12,
                        background: '#1e1e1e',
                        color: '#d4d4d4',
                        borderRadius: 6,
                        fontSize: 12,
                        overflow: 'auto',
                        maxHeight: 240,
                      }}
                    >
                      {previewResult}
                    </pre>
                  )}

                  <div style={{ marginTop: 15, display: 'flex', gap: 10 }}>
                    <button
                      type="button"
                      className="btn btn-primary"
                      disabled={saving}
                      onClick={() => void handleSave()}
                    >
                      {saving ? '保存中…' : '保存'}
                    </button>
                    <button type="button" className="btn" onClick={closeForm} disabled={saving}>
                      取消
                    </button>
                  </div>
                </div>
              )}
            </>
          }
        />
      </div>
        )}
      </div>
    </div>
  )
}
