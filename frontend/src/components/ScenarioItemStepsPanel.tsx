import { useCallback, useEffect, useMemo, useState, type DragEvent } from 'react'
import axios from 'axios'
import { EnforceOrderTipBody, HelpTip } from './HelpTip'
import { ENFORCE_ORDER_SCENARIO } from './helpCopy'
import {
  ScenarioStepSingleTracePreview,
  ScenarioStepTraceVerifyPanel,
} from './ScenarioStepTraceVerifyPanel'
import { nextAutoStepKey } from '../utils/scenarioStepDisplay'

const API_BASE = '/api'

/** 临时屏蔽：同步 scenario_key、从过程点导入（logical_agent_id）相关工具栏 */
const HIDE_SCENARIO_IMPORT_TOOLBAR = true

interface DatasetRow {
  id: string
  name: string
  item_count: number
}

interface ScenarioStepRow {
  id: string
  step_key: string
  name: string
  step_order: number
  weight: number
  required: boolean
  enabled: boolean
  enforce_order?: boolean
  gt_criteria?: string[]
  gt_expected?: Record<string, unknown>
  match_rules?: Record<string, unknown>
}

interface WeightSummary {
  enabled_count: number
  total_count: number
  weight_sum: number
  normalized: boolean
}

interface ItemConfig {
  dataset_item_id: string
  question_index: number
  question: string
  scenario_key: string
  item_scenario_key?: string
  scenario_key_in_sync?: boolean
  has_template: boolean
  weight_summary?: WeightSummary | null
  template?: {
    name: string
    enabled: boolean
    steps: ScenarioStepRow[]
  }
}

interface ItemStepsConfigResponse {
  dataset_id: string
  dataset_name: string
  item_count: number
  llm_configured?: boolean
  warnings?: string[]
  items: ItemConfig[]
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
  if (!ax.response && ax.message) {
    return `网络或服务异常：${ax.message}`
  }
  return fallback
}

function keywordsToText(gt?: Record<string, unknown>): string {
  if (!gt) return ''
  const kws = gt.keywords
  if (Array.isArray(kws)) return kws.join(', ')
  return ''
}

function textToKeywords(text: string): Record<string, unknown> {
  const kws = splitKeywords(text)
  return { keywords: kws }
}

function splitKeywords(text: string): string[] {
  return text
    .split(/[,，\n]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

function isLlmEvalMode(form: { eval_mode: string }): boolean {
  return form.eval_mode === 'llm_aggregate'
}

const emptyStepForm = () => ({
  step_key: '',
  name: '',
  step_order: '' as string | number,
  weight: 0.1,
  required: true,
  enabled: true,
  enforce_order: false,
  gt_criteria_text: '',
  keywords_text: '',
  reference_files: {} as Record<string, unknown>,
  eval_mode: 'keyword' as 'keyword' | 'llm_aggregate',
  aggregate_prompt: '',
})

function pickDefaultDatasetId(list: DatasetRow[]): string {
  const onto12 = list.find(
    (d) => d.name === 'onto-eval-12-with-context' || d.name.includes('onto-eval-12'),
  )
  if (onto12) return onto12.id
  const zc = list.find((d) => d.name.includes('ZC_question') || d.name.includes('zc0425'))
  return zc?.id || list[0]?.id || ''
}

interface CheckpointAgentOption {
  id: string
  name: string
  logical_agent_id: string
}

interface ScenarioItemStepsPanelProps {
  checkpointAgentOptions?: CheckpointAgentOption[]
  onNavigateToOnlineEval?: (agentId: string) => void
}

export function ScenarioItemStepsPanel({
  checkpointAgentOptions,
  onNavigateToOnlineEval,
}: ScenarioItemStepsPanelProps = {}) {
  const [datasets, setDatasets] = useState<DatasetRow[]>([])
  const [datasetId, setDatasetId] = useState('')
  const [config, setConfig] = useState<ItemStepsConfigResponse | null>(null)
  const [selectedItemId, setSelectedItemId] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [extracting, setExtracting] = useState(false)
  const [error, setError] = useState('')
  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState<ScenarioStepRow | null>(null)
  const [form, setForm] = useState(emptyStepForm())
  const [saving, setSaving] = useState(false)
  const [normalizing, setNormalizing] = useState(false)
  const [syncingKeys, setSyncingKeys] = useState(false)
  const [importAgentLid, setImportAgentLid] = useState('')
  const [importingCp, setImportingCp] = useState(false)
  const [agentOptions, setAgentOptions] = useState<{ logical_agent_id: string; name: string }[]>([])
  const [verifyAgentName, setVerifyAgentName] = useState('')
  const [draggingId, setDraggingId] = useState<string | null>(null)
  const [dragOverId, setDragOverId] = useState<string | null>(null)
  const [reordering, setReordering] = useState(false)
  const [uploadingFile, setUploadingFile] = useState(false)

  const verifyAgentOptions = useMemo(() => {
    if (checkpointAgentOptions?.length) {
      return checkpointAgentOptions.map(a => ({
        logical_agent_id: a.logical_agent_id,
        name: a.name,
      }))
    }
    return agentOptions
  }, [checkpointAgentOptions, agentOptions])

  const handleNavigateOnlineEval = useCallback(() => {
    if (!onNavigateToOnlineEval) return
    const name = verifyAgentName.trim()
    if (!name) return
    const id = checkpointAgentOptions?.find(o => o.name.trim() === name)?.id ?? ''
    if (id) onNavigateToOnlineEval(id)
  }, [onNavigateToOnlineEval, verifyAgentName, checkpointAgentOptions])

  const fetchDatasets = useCallback(async () => {
    try {
      const res = await axios.get<{ datasets: DatasetRow[] }>(`${API_BASE}/datasets`)
      const list = res.data.datasets || []
      setDatasets(list)
      setDatasetId((prev) => prev || pickDefaultDatasetId(list))
    } catch {
      setDatasets([])
    }
  }, [])

  const loadConfig = useCallback(
    async (opts?: { keepSelection?: boolean; preferredItemId?: string | null }) => {
      if (!datasetId) {
        setConfig(null)
        return null
      }
      setLoading(true)
      setError('')
      try {
        const res = await axios.get<ItemStepsConfigResponse>(
          `${API_BASE}/scenario-eval/datasets/${datasetId}/item-steps-config`,
        )
        const data = res.data
        setConfig(data)
        const items = data.items || []
        if (!items.length) {
          setSelectedItemId(null)
          return data
        }
        const preferred = opts?.preferredItemId
        const keep = opts?.keepSelection !== false
        setSelectedItemId((prev) => {
          if (preferred && items.some((i) => i.dataset_item_id === preferred)) {
            return preferred
          }
          if (keep && prev && items.some((i) => i.dataset_item_id === prev)) {
            return prev
          }
          return items[0].dataset_item_id
        })
        return data
      } catch (err: unknown) {
        setError(formatApiError(err, '加载场景子步骤失败'))
        setConfig(null)
        setSelectedItemId(null)
        return null
      } finally {
        setLoading(false)
      }
    },
    [datasetId],
  )

  useEffect(() => {
    void fetchDatasets()
  }, [fetchDatasets])

  useEffect(() => {
    const loadAgents = async () => {
      try {
        const res = await axios.get<{ logical_agent_id?: string; name?: string }[]>(
          `${API_BASE}/agents`,
        )
        const byLid = new Map<string, string>()
        for (const a of res.data || []) {
          const lid = (a.logical_agent_id || '').trim()
          const name = (a.name || '').trim()
          if (!lid) continue
          if (!byLid.has(lid)) byLid.set(lid, name || lid)
        }
        setAgentOptions(
          [...byLid.entries()].map(([logical_agent_id, name]) => ({
            logical_agent_id,
            name,
          })),
        )
      } catch {
        setAgentOptions([])
      }
    }
    void loadAgents()
  }, [])

  useEffect(() => {
    setVerifyAgentName(prev => {
      if (prev && verifyAgentOptions.some(o => o.name === prev)) return prev
      return verifyAgentOptions[0]?.name ?? ''
    })
  }, [verifyAgentOptions])

  useEffect(() => {
    void loadConfig({ keepSelection: true })
  }, [loadConfig])

  const selectedItem = useMemo(
    () => config?.items.find((i) => i.dataset_item_id === selectedItemId) ?? null,
    [config, selectedItemId],
  )

  const steps = useMemo(() => {
    const raw = selectedItem?.template?.steps ?? []
    return [...raw].sort((a, b) => a.step_order - b.step_order)
  }, [selectedItem])

  const canDragReorder = Boolean(selectedItemId && selectedItem?.has_template && steps.length > 0)

  const persistReorder = async (ordered: ScenarioStepRow[]) => {
    if (!selectedItemId || !canDragReorder) return
    setReordering(true)
    setError('')
    const previous = config
    setConfig((prev) => {
      if (!prev) return prev
      return {
        ...prev,
        items: prev.items.map((item) =>
          item.dataset_item_id === selectedItemId && item.template
            ? {
                ...item,
                template: {
                  ...item.template,
                  steps: ordered.map((s, index) => ({ ...s, step_order: index + 1 })),
                },
              }
            : item,
        ),
      }
    })
    try {
      await axios.put(
        `${API_BASE}/scenario-eval/items/${selectedItemId}/steps/reorder`,
        { step_ids: ordered.map((s) => s.id) },
      )
    } catch (err: unknown) {
      setConfig(previous)
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

    const fromIdx = steps.findIndex((s) => s.id === sourceId)
    const toIdx = steps.findIndex((s) => s.id === targetId)
    if (fromIdx < 0 || toIdx < 0) {
      setDraggingId(null)
      return
    }

    const next = [...steps]
    const [moved] = next.splice(fromIdx, 1)
    next.splice(toIdx, 0, moved)
    void persistReorder(next)
  }

  const handleDragEnd = () => {
    setDraggingId(null)
    setDragOverId(null)
  }

  const weightSummary = useMemo((): WeightSummary => {
    if (selectedItem?.weight_summary) {
      return selectedItem.weight_summary
    }
    const enabled = steps.filter((s) => s.enabled)
    const weight_sum = enabled.reduce((acc, s) => acc + Number(s.weight || 0), 0)
    return {
      enabled_count: enabled.length,
      total_count: steps.length,
      weight_sum,
      normalized: enabled.length > 0 && Math.abs(weight_sum - 1) <= 0.01,
    }
  }, [selectedItem, steps])

  const handleNormalizeWeights = async () => {
    if (!selectedItemId) return
    setNormalizing(true)
    setError('')
    try {
      await axios.post(
        `${API_BASE}/scenario-eval/items/${selectedItemId}/steps/normalize-weights`,
      )
      await loadConfig({ keepSelection: true, preferredItemId: selectedItemId })
    } catch (err: unknown) {
      setError(formatApiError(err, '权重归一化失败'))
    } finally {
      setNormalizing(false)
    }
  }

  const handleImportFromCheckpoints = async (scope: 'item' | 'dataset') => {
    if (!datasetId || !importAgentLid.trim()) {
      setError('请选择评测集并填写 Agent logical_agent_id')
      return
    }
    setImportingCp(true)
    setError('')
    try {
      if (scope === 'item' && selectedItemId) {
        await axios.post(
          `${API_BASE}/scenario-eval/items/${selectedItemId}/import-from-checkpoints?dataset_id=${encodeURIComponent(datasetId)}`,
          { logical_agent_id: importAgentLid.trim() },
        )
      } else {
        await axios.post(
          `${API_BASE}/scenario-eval/datasets/${datasetId}/import-from-checkpoints`,
          { logical_agent_id: importAgentLid.trim() },
        )
      }
      await loadConfig({ keepSelection: true, preferredItemId: selectedItemId })
    } catch (err: unknown) {
      setError(formatApiError(err, '从过程点导入失败'))
    } finally {
      setImportingCp(false)
    }
  }

  const handleSyncScenarioKeys = async () => {
    if (!datasetId) return
    setSyncingKeys(true)
    setError('')
    try {
      await axios.post(`${API_BASE}/scenario-eval/datasets/${datasetId}/sync-scenario-keys`)
      await loadConfig({ keepSelection: true, preferredItemId: selectedItemId })
    } catch (err: unknown) {
      setError(formatApiError(err, '同步 scenario_key 失败'))
    } finally {
      setSyncingKeys(false)
    }
  }

  const handleAutoExtract = async () => {
    if (!datasetId) {
      setError('请先选择评测集')
      return
    }
    const currentId = selectedItemId
    setExtracting(true)
    setError('')
    setShowForm(false)
    setEditing(null)
    try {
      await axios.post(`${API_BASE}/scenario-eval/datasets/${datasetId}/auto-extract-steps`)
      await loadConfig({ keepSelection: true, preferredItemId: currentId })
    } catch (err: unknown) {
      const ax = err as { response?: { status?: number; data?: { detail?: unknown } } }
      if (ax.response?.status === 409) {
        const detail = ax.response.data?.detail as
          | { message?: string; existing_template_count?: number }
          | undefined
        const msg =
          detail?.message ||
          '当前评测集下已有子步骤配置，重新提炼将覆盖已有配置（包括基于Prompt评测、Prompt等手工设置），请慎重操作。'
        const confirmed = window.confirm(`${msg}\n\n是否确认继续？`)
        if (confirmed) {
          try {
            await axios.post(
              `${API_BASE}/scenario-eval/datasets/${datasetId}/auto-extract-steps?force=true`,
            )
            await loadConfig({ keepSelection: true, preferredItemId: currentId })
          } catch (retryErr: unknown) {
            setError(formatApiError(retryErr, '自动提炼子步骤失败'))
          }
        }
      } else {
        setError(formatApiError(err, '自动提炼子步骤失败'))
      }
    } finally {
      setExtracting(false)
    }
  }

  const openCreate = () => {
    if (!selectedItemId) return
    setEditing(null)
    const nextOrder = steps.length ? Math.max(...steps.map((s) => s.step_order)) + 1 : 1
    setForm({
      ...emptyStepForm(),
      step_key: nextAutoStepKey(steps),
      step_order: nextOrder,
      weight: steps.length ? 0.1 : 1,
    })
    setShowForm(true)
  }

  const openEdit = (s: ScenarioStepRow) => {
    setEditing(s)
    const mr = (s.match_rules || {}) as Record<string, unknown>
    const evalConfig = (mr.eval_config || {}) as Record<string, unknown>
    const evalMode = (evalConfig.mode as string) === 'llm_aggregate' ? 'llm_aggregate' : 'keyword'
    const aggPrompt = (evalConfig.aggregate_prompt as string) || ''
    const gt = (s.gt_expected || {}) as Record<string, unknown>
    const kwText = keywordsToText(s.gt_expected)
    // 参考答案显示优先级：keywords > text
    let displayKwText = kwText
    if (!displayKwText && typeof gt.text === 'string' && gt.text) {
      displayKwText = gt.text
    }
    setForm({
      step_key: s.step_key,
      name: s.name,
      step_order: s.step_order,
      weight: s.weight,
      required: s.required,
      enabled: s.enabled,
      enforce_order: Boolean(s.enforce_order),
      gt_criteria_text: (s.gt_criteria || []).join(', '),
      keywords_text: displayKwText,
      reference_files: (gt.reference_files as Record<string, unknown>) || {},
      eval_mode: evalMode,
      aggregate_prompt: aggPrompt,
    })
    setShowForm(true)
  }

  const closeForm = () => {
    setShowForm(false)
    setEditing(null)
  }

  // 客户端解析文本文件为 JSON
  const parseFileToJson = (file: File): Promise<{ filename: string; json: unknown }> => {
    return new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => {
        const text = reader.result as string
        resolve({ filename: file.name, json: { content: text, filename: file.name } })
      }
      reader.onerror = () => reject(new Error('文件读取失败'))
      reader.readAsText(file)
    })
  }

  // 解析上传的文件并存入参考答案
  const handleParseFiles = async (files: FileList) => {
    setUploadingFile(true)
    setError('')
    try {
      const newRefs: Record<string, unknown> = { ...form.reference_files }
      for (const file of Array.from(files)) {
        const { filename, json } = await parseFileToJson(file)
        newRefs[filename] = json
      }
      setForm({ ...form, reference_files: newRefs })
    } catch (e) {
      setError(typeof e === 'object' && e !== null && 'message' in e ? String((e as Error).message) : '文件解析失败')
    } finally {
      setUploadingFile(false)
    }
  }

  const handleRemoveRefFile = (filename: string) => {
    const newRefs = { ...form.reference_files }
    delete newRefs[filename]
    setForm({ ...form, reference_files: newRefs })
  }

  const handleSave = async () => {
    if (!selectedItemId) return
    if (!form.name.trim()) {
      setError('名称不能为空')
      return
    }
    if (form.enabled && isLlmEvalMode(form)) {
      if (!form.aggregate_prompt.trim()) {
        setError('基于Prompt评测的Prompt不能为空')
        return
      }
    }
    setSaving(true)
    setError('')
    try {
      const gt_criteria = form.gt_criteria_text
        .split(/[,，\n]/)
        .map((s) => s.trim())
        .filter(Boolean)
      const gt_expected: Record<string, unknown> = {
        ...textToKeywords(form.keywords_text),
      }
      if (Object.keys(form.reference_files).length > 0) {
        gt_expected.reference_files = form.reference_files
      }
      const match_rules: Record<string, unknown> = {
        eval_config: {
          mode: form.eval_mode,
          ...(form.eval_mode === 'llm_aggregate' ? { aggregate_prompt: form.aggregate_prompt.trim() } : {}),
        },
      }
      if (editing) {
        const existingMr = (editing.match_rules || {}) as Record<string, unknown>
        if (existingMr.trace_binding) {
          match_rules.trace_binding = existingMr.trace_binding
        }
        await axios.put(`${API_BASE}/scenario-eval/steps/${editing.id}`, {
          name: form.name.trim(),
          step_order: form.step_order === '' ? undefined : Number(form.step_order),
          weight: Number(form.weight),
          required: form.required,
          enabled: form.enabled,
          enforce_order: form.enforce_order,
          gt_criteria: gt_criteria.length ? gt_criteria : null,
          gt_expected,
          match_rules,
        })
      } else {
        const step_key = nextAutoStepKey(steps)
        await axios.post(`${API_BASE}/scenario-eval/items/${selectedItemId}/steps`, {
          step_key,
          name: form.name.trim(),
          step_order: form.step_order === '' ? undefined : Number(form.step_order),
          weight: Number(form.weight),
          required: form.required,
          enabled: form.enabled,
          enforce_order: form.enforce_order,
          gt_criteria: gt_criteria.length ? gt_criteria : null,
          gt_expected,
          match_rules,
        })
      }
      closeForm()
      await loadConfig({ keepSelection: true, preferredItemId: selectedItemId })
    } catch (err: unknown) {
      setError(formatApiError(err, '保存失败'))
    } finally {
      setSaving(false)
    }
  }

  const handleToggleEnabled = async (s: ScenarioStepRow) => {
    try {
      await axios.put(`${API_BASE}/scenario-eval/steps/${s.id}`, { enabled: !s.enabled })
      await loadConfig({ keepSelection: true, preferredItemId: selectedItemId })
    } catch (err: unknown) {
      setError(formatApiError(err, '更新失败'))
    }
  }

  const handleToggleEnforceOrder = async (s: ScenarioStepRow) => {
    try {
      await axios.put(`${API_BASE}/scenario-eval/steps/${s.id}`, {
        enforce_order: !s.enforce_order,
      })
      await loadConfig({ keepSelection: true, preferredItemId: selectedItemId })
    } catch (err: unknown) {
      setError(formatApiError(err, '更新顺序依赖失败'))
    }
  }

  const handleDelete = async (s: ScenarioStepRow) => {
    if (!window.confirm(`删除子步骤「${s.name}」？`)) return
    try {
      await axios.delete(`${API_BASE}/scenario-eval/steps/${s.id}`)
      await loadConfig({ keepSelection: true, preferredItemId: selectedItemId })
    } catch (err: unknown) {
      setError(formatApiError(err, '删除失败'))
    }
  }

  const keywordsFieldInvalid = false
  const llmPromptRequired = showForm && form.enabled && isLlmEvalMode(form)
  const llmPromptFieldInvalid =
    llmPromptRequired && !form.aggregate_prompt.trim()

  return (
    <div className="config-page-panel">
      {error && <div className="alert-error">{error}</div>}
      {(config?.warnings?.length ?? 0) > 0 && (
        <div
          style={{
            marginBottom: 12,
            padding: '8px 12px',
            background: '#fff3cd',
            borderRadius: 4,
            color: '#856404',
            fontSize: 12,
          }}
        >
          {config!.warnings!.map((w, i) => (
            <div key={i}>{w}</div>
          ))}
        </div>
      )}

      <div className="config-toolbar" style={{ marginBottom: 12 }}>
        <label>
          评测集
          <select
            value={datasetId}
            onChange={(e) => {
              setDatasetId(e.target.value)
              setSelectedItemId(null)
              setConfig(null)
              setShowForm(false)
            }}
          >
            <option value="">请选择</option>
            {datasets.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}（{d.item_count} 题）
              </option>
            ))}
          </select>
        </label>
        <div className="config-toolbar__actions">
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={extracting || !datasetId}
            onClick={() => void handleAutoExtract()}
          >
            {extracting ? '提炼中…' : '自动提炼子步骤'}
          </button>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => void loadConfig({ keepSelection: true })}
            disabled={loading || extracting}
          >
            刷新
          </button>
          {!HIDE_SCENARIO_IMPORT_TOOLBAR && (
            <button
              type="button"
              className="btn btn-sm"
              disabled={syncingKeys || !datasetId}
              onClick={() => void handleSyncScenarioKeys()}
            >
              {syncingKeys ? '同步中…' : '同步 scenario_key'}
            </button>
          )}
        </div>
        {selectedItem?.has_template && (
          <div className="config-toolbar__meta">
            <span
              className={`weight-badge ${
                weightSummary.normalized || weightSummary.enabled_count === 0
                  ? 'is-ok'
                  : 'is-warn'
              }`}
            >
              启用 {weightSummary.enabled_count}/{weightSummary.total_count} · 权重{' '}
              {weightSummary.weight_sum.toFixed(3)}
              {!weightSummary.normalized && weightSummary.enabled_count > 0 ? ' · 建议归一化' : ''}
            </span>
          </div>
        )}
        <p className="config-hint">
          从 <code>model_answer</code> 提炼子步骤（无结构时回退 ground_truth）；打分仅统计启用步骤并按权重归一化。
        </p>
      </div>

      {!HIDE_SCENARIO_IMPORT_TOOLBAR && (
        <div className="config-toolbar" style={{ marginBottom: 12 }}>
          <label>
            从过程点导入
            <input
              type="text"
              list="scenario-agent-lids"
              value={importAgentLid}
              onChange={(e) => setImportAgentLid(e.target.value)}
              placeholder="logical_agent_id"
            />
            <datalist id="scenario-agent-lids">
              {agentOptions.map((a) => (
                <option key={a.logical_agent_id} value={a.logical_agent_id}>
                  {a.name}
                </option>
              ))}
            </datalist>
          </label>
          <div className="config-toolbar__actions">
            <button
              type="button"
              className="btn btn-sm"
              disabled={importingCp || !selectedItemId}
              onClick={() => void handleImportFromCheckpoints('item')}
            >
              {importingCp ? '导入中…' : '导入到本题'}
            </button>
            <button
              type="button"
              className="btn btn-sm"
              disabled={importingCp || !datasetId}
              onClick={() => void handleImportFromCheckpoints('dataset')}
            >
              导入到全集
            </button>
          </div>
        </div>
      )}

      {!datasetId ? (
        <p className="muted">请先选择评测集，再点击「自动提炼子步骤」。</p>
      ) : loading && !config ? (
        <p className="muted">加载中…</p>
      ) : (
        <div className="scenario-steps-layout">
          <div className="card scenario-sidebar">
            <h3 className="scenario-sidebar__title">题目列表</h3>
            <ul className="scenario-question-list">
              {(config?.items || []).map((item) => (
                <li key={item.dataset_item_id}>
                  <button
                    type="button"
                    className={`btn question-list-item ${
                      selectedItemId === item.dataset_item_id ? 'is-active' : ''
                    }`}
                    onClick={() => setSelectedItemId(item.dataset_item_id)}
                    title={item.question}
                  >
                    <div className="q-index">Q{item.question_index + 1}</div>
                    <div className="q-title">{item.question}</div>
                    <div className={`q-meta ${item.has_template ? 'ok' : 'warn'}`}>
                      {item.has_template
                        ? `${item.template?.steps?.length ?? 0} 个子步骤`
                        : '未配置'}
                      {!HIDE_SCENARIO_IMPORT_TOOLBAR &&
                      item.has_template &&
                      item.scenario_key_in_sync === false
                        ? ' · key未同步'
                        : ''}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </div>

          <div className="scenario-main">
            {selectedItem && (
              <>
                <div className="card scenario-detail-card">
                  <div className="scenario-detail-header">
                    <div className="scenario-detail-header__info">
                      <p className="scenario-detail-header__title" title={selectedItem.question}>
                        Q{selectedItem.question_index + 1} · {selectedItem.question}
                      </p>
                      <p className="scenario-detail-header__meta">
                        {selectedItem.template?.name || '未命名模板'} · scenario_key:{' '}
                        {selectedItem.scenario_key}
                        {!HIDE_SCENARIO_IMPORT_TOOLBAR &&
                        selectedItem.item_scenario_key &&
                        selectedItem.item_scenario_key !== selectedItem.scenario_key ? (
                          <span style={{ color: '#e65100', marginLeft: 6 }}>
                            条目 JSON: {selectedItem.item_scenario_key}
                          </span>
                        ) : null}
                      </p>
                    </div>
                    <div className="scenario-detail-header__actions">
                      <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        disabled={!selectedItem.has_template}
                        onClick={() => (showForm ? closeForm() : openCreate())}
                      >
                        {showForm ? '关闭表单' : '新增子步骤'}
                      </button>
                      <button
                        type="button"
                        className="btn btn-sm"
                        disabled={
                          !selectedItem.has_template ||
                          normalizing ||
                          weightSummary.enabled_count === 0
                        }
                        onClick={() => void handleNormalizeWeights()}
                      >
                        {normalizing ? '归一化中…' : '权重归一化为 1.0'}
                      </button>
                    </div>
                  </div>
                </div>

                {showForm && (
                  <div className="card scenario-form-card">
                    <h3>{editing ? `编辑：${editing.name}` : '新增子步骤'}</h3>
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                      <div className="form-field" style={{ gridColumn: '1 / -1' }}>
                        <label>名称 *</label>
                        <input
                          type="text"
                          value={form.name}
                          onChange={(e) => setForm({ ...form, name: e.target.value })}
                        />
                      </div>
                      <div className="form-field">
                        <label>顺序</label>
                        <input
                          type="number"
                          min={1}
                          value={form.step_order}
                          onChange={(e) => setForm({ ...form, step_order: e.target.value })}
                        />
                        <p style={{ fontSize: 12, color: '#666', margin: '4px 0 0' }}>
                          {ENFORCE_ORDER_SCENARIO.orderFieldHint}
                        </p>
                      </div>
                      <div className="form-field">
                        <label>权重</label>
                        <input
                          type="number"
                          min={0}
                          max={1}
                          step={0.01}
                          value={form.weight}
                          onChange={(e) => setForm({ ...form, weight: Number(e.target.value) })}
                        />
                      </div>
                    </div>
                    <div style={{ marginTop: 10, display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'center' }}>
                      <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <input
                          type="checkbox"
                          checked={form.enforce_order}
                          onChange={(e) => setForm({ ...form, enforce_order: e.target.checked })}
                        />
                        <HelpTip
                          label="启用顺序依赖"
                          align="left"
                          tip={<EnforceOrderTipBody variant="scenario" />}
                        />
                      </label>
                      <span style={{ fontSize: 12, color: '#666' }}>
                        {ENFORCE_ORDER_SCENARIO.formHint}
                      </span>
                      <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <input
                          type="checkbox"
                          checked={form.required}
                          onChange={(e) => setForm({ ...form, required: e.target.checked })}
                        />
                        必达
                      </label>
                      <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <input
                          type="checkbox"
                          checked={form.enabled}
                          onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
                        />
                        启用子步骤
                      </label>
                    </div>
                    <div className="form-field">
                      <label>
                        参考答案
                        {isLlmEvalMode(form) ? '（可选）' : ''}
                      </label>
                      <textarea
                        className={keywordsFieldInvalid ? 'input-invalid' : undefined}
                        value={form.keywords_text}
                        onChange={(e) => setForm({ ...form, keywords_text: e.target.value })}
                        rows={4}
                        style={{
                          resize: 'vertical',
                          minHeight: 80,
                          ...(form.keywords_text.startsWith('{') && form.keywords_text.includes('\n')
                            ? { fontFamily: 'monospace', fontSize: 12 }
                            : {}),
                        }}
                        placeholder="Ground_Truth评测：按逗号分隔输入命中关键词；基于Prompt评测：作为可选参考答案"
                      />
                      {keywordsFieldInvalid && (
                        <p style={{ fontSize: 12, color: '#cf1322', margin: '4px 0 0' }}>
                          基于Prompt评测模式下需填写参考答案
                        </p>
                      )}
                      <div style={{ marginTop: 4 }}>
                        <label style={{ fontSize: 12, color: '#666', cursor: 'pointer', textDecoration: 'underline' }}>
                          上传文件（txt）
                          <input
                            type="file"
                            accept=".txt"
                            multiple
                            style={{ display: 'none' }}
                            disabled={uploadingFile}
                            onChange={async (e) => {
                              const files = e.target.files
                              if (files && files.length > 0) await handleParseFiles(files)
                              e.target.value = ''
                            }}
                          />
                        </label>
                        {uploadingFile && (
                          <span style={{ fontSize: 12, color: '#999', marginLeft: 8 }}>解析中…</span>
                        )}
                        {Object.keys(form.reference_files).length > 0 && (
                          <div style={{ marginTop: 4 }}>
                            {Object.keys(form.reference_files).map((fname) => (
                              <div
                                key={fname}
                                style={{
                                  display: 'inline-flex',
                                  alignItems: 'center',
                                  gap: 6,
                                  marginRight: 12,
                                  marginBottom: 2,
                                  background: '#f6ffed',
                                  border: '1px solid #b7eb8f',
                                  borderRadius: 4,
                                  padding: '2px 8px',
                                  fontSize: 12,
                                }}
                              >
                                <span style={{ color: '#52c41a' }}>{fname}</span>
                                <button
                                  type="button"
                                  style={{
                                    fontSize: 11,
                                    color: '#cf1322',
                                    border: 'none',
                                    background: 'none',
                                    cursor: 'pointer',
                                    padding: 0,
                                    lineHeight: 1,
                                  }}
                                  title="移除此文件"
                                  onClick={() => handleRemoveRefFile(fname)}
                                >
                                  ✕
                                </button>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                    <div className="form-field eval-mode-field">
                      <span className="eval-mode-field-title">评测模式</span>
                      <div className="eval-mode-options">
                        <label
                          className={`eval-mode-option${form.eval_mode === 'keyword' ? ' is-selected' : ''}`}
                        >
                          <input
                            type="radio"
                            name="eval_mode"
                            checked={form.eval_mode === 'keyword'}
                            onChange={() => setForm({ ...form, eval_mode: 'keyword' })}
                          />
                          <span className="eval-mode-option-label">Ground_Truth评测</span>
                        </label>
                        <label
                          className={`eval-mode-option${form.eval_mode === 'llm_aggregate' ? ' is-selected' : ''}`}
                        >
                          <input
                            type="radio"
                            name="eval_mode"
                            checked={form.eval_mode === 'llm_aggregate'}
                            onChange={() => setForm({ ...form, eval_mode: 'llm_aggregate' })}
                          />
                          <span className="eval-mode-option-label">基于Prompt评测</span>
                        </label>
                      </div>
                      {form.eval_mode === 'llm_aggregate' && config?.llm_configured === false && (
                        <p style={{ margin: '8px 0 0', fontSize: 12, color: '#856404' }}>
                          当前未配置 LLM API Key，评测时将跳过基于Prompt评测。请先在「系统设置 → LLM 模型」中配置。
                        </p>
                      )}
                    </div>
                    <div className="eval-mode-aggregate-panel">
                      {form.eval_mode === 'llm_aggregate' ? (
                        <div className="form-field" style={{ marginBottom: 8 }}>
                          <label>评测Prompt *</label>
                          <textarea
                            rows={5}
                            className={llmPromptFieldInvalid ? 'input-invalid' : undefined}
                            style={{ width: '100%', resize: 'vertical', minHeight: 100 }}
                            value={form.aggregate_prompt}
                            onChange={(e) => setForm({ ...form, aggregate_prompt: e.target.value })}
                            placeholder="指导 LLM 如何评测步骤绑定的 Span output，例如：请按以下标准判断变更信息是否完整准确…"
                          />
                          {llmPromptFieldInvalid && (
                            <p style={{ fontSize: 12, color: '#cf1322', margin: '4px 0 0' }}>
                              基于Prompt评测模式下需填写Prompt
                            </p>
                          )}
                          <p style={{ fontSize: 12, color: '#666', margin: '4px 0 0' }}>
                            LLM 将按此提示词直接对 Span output 评分（0.0-1.0），参考答案仅作可选参考，非必须完全匹配。
                          </p>
                          <div style={{ marginTop: 4, display: 'flex', alignItems: 'center', gap: 8 }}>
                            <label style={{ fontSize: 12, color: '#666', cursor: 'pointer', textDecoration: 'underline' }}>
                              上传文件（自动填入下方文本框）
                              <input
                                type="file"
                                accept=".txt"
                                style={{ display: 'none' }}
                                disabled={uploadingFile}
                                onChange={async (e) => {
                                  const f = e.target.files?.[0]
                                  if (f) {
                                    const text = await f.text()
                                    setForm({ ...form, aggregate_prompt: form.aggregate_prompt + (form.aggregate_prompt ? '\n\n' : '') + text })
                                  }
                                  e.target.value = ''
                                }}
                              />
                            </label>
                          </div>
                        </div>
                      ) : (
                        <div className="form-field" style={{ marginBottom: 8 }}>
                          <label>判分细则（逗号分隔）</label>
                          <textarea
                            rows={3}
                            style={{ resize: 'vertical', minHeight: 60 }}
                            value={form.gt_criteria_text}
                            onChange={(e) => setForm({ ...form, gt_criteria_text: e.target.value })}
                            placeholder="按逗号分隔输入判分关键词，例如：变更编号,变更类型,变更前MPart"
                          />
                          <p style={{ fontSize: 12, color: '#666', margin: '4px 0 0' }}>
                            Ground_Truth评测：将步骤输出与参考答案关键词匹配，按命中率判分（关键词≤3 个命中率≥50% 通过；{'>'}3 个≥60% 通过）。
                          </p>
                        </div>
                      )}
                    </div>
                    <ScenarioStepSingleTracePreview
                      datasetItemId={selectedItemId}
                      agentName={verifyAgentName}
                      stepKey={editing?.step_key ?? nextAutoStepKey(steps)}
                      name={form.name}
                      keywordsText={form.keywords_text}
                      enabled={form.enabled}
                      required={form.required}
                      stepOrder={form.step_order}
                      formatApiError={formatApiError}
                      matchRules={{
                        eval_config: {
                          mode: form.eval_mode,
                          ...(form.eval_mode === 'llm_aggregate' ? { aggregate_prompt: form.aggregate_prompt.trim() } : {}),
                        },
                      }}
                      />
                    <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
                      <button
                        type="button"
                        className="btn btn-primary"
                        disabled={saving}
                        onClick={() => void handleSave()}
                      >
                        {saving ? '保存中…' : '保存'}
                      </button>
                      <button type="button" className="btn" onClick={closeForm}>
                        取消
                      </button>
                    </div>
                  </div>
                )}

                <div className="card scenario-detail-card" style={{ padding: '12px 14px' }}>
                  {steps.length === 0 ? (
                    <p className="muted">本题尚无子步骤，请点击「自动提炼子步骤」或「新增子步骤」。</p>
                  ) : (
                    <>
                      <div
                        style={{
                          display: 'flex',
                          justifyContent: 'space-between',
                          alignItems: 'center',
                          flexWrap: 'wrap',
                          gap: 8,
                          marginBottom: 8,
                        }}
                      >
                        <p style={{ margin: 0, fontSize: 12, color: '#666', lineHeight: 1.6 }}>
                          {canDragReorder
                            ? '拖拽左侧 ⋮⋮ 可调整子步骤执行顺序；顺序变更后自动保存。'
                            : null}
                        </p>
                        {reordering && (
                          <span style={{ fontSize: 12, color: '#666' }}>正在保存顺序…</span>
                        )}
                      </div>
                    <div className="scenario-steps-table-wrap">
                      <table className="results-table scenario-steps-table">
                        <thead>
                          <tr>
                            {canDragReorder && (
                              <th style={{ width: 44, textAlign: 'center' }}>排序</th>
                            )}
                            <th style={{ width: 48 }}>顺序</th>
                            <th>名称</th>
                            <th style={{ width: 140 }}>评测模式</th>
                            <th style={{ width: 64 }}>权重</th>
                            <th style={{ width: 44 }}>必达</th>
                            <th style={{ width: 72 }}>
                              <HelpTip
                                label="顺序依赖"
                                align="left"
                                tip={<EnforceOrderTipBody variant="scenario" />}
                              />
                            </th>
                            <th style={{ width: 52 }}>状态</th>
                            <th style={{ width: 168 }}>操作</th>
                          </tr>
                        </thead>
                        <tbody>
                          {steps.map((s) => {
                            const _mr = (s.match_rules || {}) as Record<string, unknown>
                            const _ec = (_mr.eval_config || {}) as Record<string, unknown>
                            const _isAgg = _ec.mode === 'llm_aggregate'
                            const _prompt = (_isAgg ? (_ec.aggregate_prompt as string) || '' : '')
                            return (
                              <tr
                                key={s.id}
                                onDragOver={handleDragOver(s.id)}
                                onDrop={handleDrop(s.id)}
                                style={{
                                  opacity: !s.enabled ? 0.55 : draggingId === s.id ? 0.55 : 1,
                                  background: dragOverId === s.id ? '#f0f4ff' : undefined,
                                }}
                              >
                                {canDragReorder && (
                                  <td className={`cell-drag${reordering ? ' is-disabled' : ''}`}>
                                    <span
                                      draggable={!reordering}
                                      onDragStart={handleDragStart(s.id)}
                                      onDragEnd={handleDragEnd}
                                      title="拖拽调整顺序"
                                    >
                                      ⋮⋮
                                    </span>
                                  </td>
                                )}
                                <td>{s.step_order}</td>
                                <td className="cell-name" title={s.name}>
                                  {s.name}
                                </td>
                                <td>
                                  {_isAgg ? (
                                    <span
                                      title={_prompt || '基于Prompt评测'}
                                      style={{ color: '#1565c0', fontWeight: 500 }}
                                    >
                                      基于Prompt评测
                                    </span>
                                  ) : (
                                    <span style={{ color: '#555' }}>Ground_Truth评测</span>
                                  )}
                                </td>
                                <td>{s.weight}</td>
                                <td>{s.required ? '是' : '否'}</td>
                                <td>
                                  <input
                                    type="checkbox"
                                    checked={Boolean(s.enforce_order)}
                                    title="启用顺序依赖"
                                    onChange={() => void handleToggleEnforceOrder(s)}
                                  />
                                </td>
                                <td>{s.enabled ? '启用' : '禁用'}</td>
                                <td className="cell-actions">
                                  <button
                                    type="button"
                                    className="btn btn-sm"
                                    onClick={() => openEdit(s)}
                                  >
                                    编辑
                                  </button>
                                  <button
                                    type="button"
                                    className="btn btn-sm"
                                    onClick={() => void handleToggleEnabled(s)}
                                  >
                                    {s.enabled ? '禁用' : '启用'}
                                  </button>
                                  <button
                                    type="button"
                                    className="btn btn-sm btn-danger-text"
                                    onClick={() => void handleDelete(s)}
                                  >
                                    删除
                                  </button>
                                </td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </div>
                    </>
                  )}
                </div>

                {selectedItemId && selectedItem?.has_template && (
                  <ScenarioStepTraceVerifyPanel
                    datasetItemId={selectedItemId}
                    stepCount={steps.length}
                    agentOptions={verifyAgentOptions}
                    verifyAgentName={verifyAgentName}
                    onVerifyAgentChange={setVerifyAgentName}
                    formatApiError={formatApiError}
                    onNavigateOnlineEval={
                      onNavigateToOnlineEval ? handleNavigateOnlineEval : undefined
                    }
                  />
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
