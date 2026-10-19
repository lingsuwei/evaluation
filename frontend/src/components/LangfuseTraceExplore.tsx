import { useCallback, useEffect, useMemo, useState } from 'react'
import axios from 'axios'
import { formatDate, formatDuration } from '../utils/dateUtils'

interface LangfuseTrace {
  id: string
  name: string | null
  sessionId: string | null
  timestamp: string
  tags: string[]
  metadata?: Record<string, unknown>
  input?: unknown
  latency?: number | null
  htmlPath?: string
}

interface LangfuseTraceDetail extends LangfuseTrace {
  observations?: Array<{
    id: string
    name: string | null
    type: string
    startTime?: string
    endTime?: string
    latency?: number
    input?: unknown
    metadata?: Record<string, unknown>
  }>
  scores?: Array<{ name: string; value: number | string }>
}

interface QueryGroup {
  sessionId: string
  taskId: string
  questionId: string
  questionText: string
  traces: LangfuseTrace[]
  latestTimestamp: string
  agentName: string
  agentVersion: string
}

interface ExploreFilters {
  project: string
  task_id: string
  question_id: string
  question_keyword: string
  agent: string
  agent_version: string
  limit: number
}

interface LangfuseProjectOption {
  id: string
  name: string
  langfuse_project_id?: string | null
  name_source?: string
  sync_error?: string
}

interface QuestionOption {
  id: string
  text: string
}

interface AgentTraceOption {
  value: string
  label: string
}

interface TraceFacetsResponse {
  questions: QuestionOption[]
  agents: AgentTraceOption[]
  versions_by_agent: Record<string, string[]>
  trace_count: number
  scan?: {
    scanned: number
    total_items?: number
    truncated: boolean
    max_scan?: number
    source?: string
  }
  source?: string
}

interface ListMeta {
  page?: number
  limit?: number
  totalItems?: number
  totalPages?: number
  source?: string
}

type ExploreReadSource = 'langfuse' | 'fact_layer' | null
type DetailReadSource = 'langfuse' | 'fact_layer' | null

function extractAxiosDetail(err: unknown): string | undefined {
  const ax = err as { response?: { data?: { detail?: string } } }
  const detail = ax.response?.data?.detail
  return typeof detail === 'string' && detail.trim() ? detail : undefined
}

function shouldRetryTraceDetailWithFact(err: unknown): boolean {
  const ax = err as { response?: { status?: number } }
  const status = ax.response?.status
  return status === 404 || status === 502 || status === 503 || status === undefined
}

function isFactLayerDetail(detail: LangfuseTraceDetail): boolean {
  const src = metaStr(detail, 'source')
  return src === 'fact_layer' || src === 'agent_runs'
}

const PROJECT_STORAGE_KEY = 'lf_explore_project'

const DEFAULT_FILTERS: ExploreFilters = {
  project: '',
  task_id: '',
  question_id: '',
  question_keyword: '',
  agent: '',
  agent_version: '',
  limit: 100,
}

function appendProjectParam(params: URLSearchParams, project: string) {
  const pid = project.trim()
  if (pid) params.append('project', pid)
}

function metaStr(trace: LangfuseTrace, key: string): string {
  const v = trace.metadata?.[key]
  if (v === undefined || v === null) return ''
  return String(v)
}

function extractQuestionFromTrace(trace: LangfuseTrace): string {
  const fromMeta = metaStr(trace, 'onto_eval_question')
  if (fromMeta.trim()) return fromMeta.trim()
  const input = trace.input
  if (typeof input === 'string' && input.trim()) return input.trim()
  if (input && typeof input === 'object') {
    const obj = input as Record<string, unknown>
    for (const key of ['user_query', 'question', 'q', 'query', 'input', 'prompt']) {
      const v = obj[key]
      if (v !== undefined && v !== null && String(v).trim()) return String(v).trim()
    }
  }
  return ''
}

function extractQuestionFromDetail(detail: LangfuseTraceDetail): string {
  const fromTrace = extractQuestionFromTrace(detail)
  if (fromTrace) return fromTrace
  for (const obs of detail.observations || []) {
    if (typeof obs.input === 'string' && obs.input.trim()) return obs.input.trim()
    if (obs.input && typeof obs.input === 'object') {
      const obj = obs.input as Record<string, unknown>
      for (const key of ['user_query', 'question', 'q', 'query', 'input', 'prompt']) {
        const v = obj[key]
        if (v !== undefined && v !== null && String(v).trim()) return String(v).trim()
      }
    }
  }
  return ''
}

function parseSessionId(sessionId: string | null): { taskId: string; questionId: string } | null {
  if (!sessionId || !sessionId.includes(':')) return null
  const idx = sessionId.indexOf(':')
  return {
    taskId: sessionId.slice(0, idx),
    questionId: sessionId.slice(idx + 1),
  }
}

/** 筛选用 key：业务 Agent 名称（不暴露 logical_agent_id） */
function traceAgentKey(trace: LangfuseTrace): string {
  return metaStr(trace, 'agent_name')
}

function traceVersion(trace: LangfuseTrace): string {
  const ver = metaStr(trace, 'agent_version')
  if (ver && ver !== 'unknown') return ver
  const tag = (trace.tags || []).find((t) => t.startsWith('version:'))
  return tag ? tag.slice('version:'.length) : ''
}

/** 从 trace 列表提炼问题选项（按 questionId 去重） */
function extractQuestionOptionsFromTraces(
  traces: LangfuseTrace[],
  questionMaps: Record<string, Record<string, string>>,
): QuestionOption[] {
  const map = new Map<string, string>()
  for (const trace of traces) {
    const parsed = parseSessionId(trace.sessionId)
    const qid = metaStr(trace, 'onto_eval_question_id') || parsed?.questionId || ''
    if (!qid || qid === '—') continue
    if (map.has(qid)) continue
    const taskId = metaStr(trace, 'onto_eval_task_id') || parsed?.taskId || ''
    const text =
      (taskId && questionMaps[taskId]?.[qid]) || extractQuestionFromTrace(trace) || ''
    map.set(qid, text)
  }
  return [...map.entries()]
    .map(([id, text]) => ({ id, text }))
    .sort((a, b) => {
      const na = Number(a.id)
      const nb = Number(b.id)
      if (!Number.isNaN(na) && !Number.isNaN(nb)) return na - nb
      return a.id.localeCompare(b.id)
    })
}

/** 从 trace 列表提炼 Agent 选项（去重） */
function extractAgentOptionsFromTraces(traces: LangfuseTrace[]): AgentTraceOption[] {
  const map = new Map<string, string>()
  for (const trace of traces) {
    const key = traceAgentKey(trace)
    if (!key) continue
    if (!map.has(key)) map.set(key, key)
  }
  return [...map.entries()]
    .map(([value, label]) => ({ value, label }))
    .sort((a, b) => a.label.localeCompare(b.label, 'zh-CN'))
}

/** 指定 Agent 下从 trace 提炼版本（去重） */
function extractVersionOptionsForAgent(traces: LangfuseTrace[], agentKey: string): string[] {
  if (!agentKey) return []
  const versions = new Set<string>()
  for (const trace of traces) {
    if (traceAgentKey(trace) !== agentKey) continue
    const ver = traceVersion(trace)
    if (ver) versions.add(ver)
  }
  return [...versions].sort()
}

function resolveQuestionText(
  group: Omit<QueryGroup, 'questionText'>,
  questionMaps: Record<string, Record<string, string>>,
): string {
  const fromDataset = questionMaps[group.taskId]?.[group.questionId]
  if (fromDataset?.trim()) return fromDataset.trim()
  for (const t of group.traces) {
    const q = extractQuestionFromTrace(t)
    if (q) return q
  }
  return ''
}

type QueryGroupBase = Omit<QueryGroup, 'questionText'>

function groupTracesByQuery(traces: LangfuseTrace[]): QueryGroupBase[] {
  const map = new Map<string, QueryGroupBase>()

  for (const trace of traces) {
    const sessionId = trace.sessionId || `_orphan_${trace.id}`
    const parsed = parseSessionId(trace.sessionId)
    const taskId = metaStr(trace, 'onto_eval_task_id') || parsed?.taskId || '—'
    const questionId = metaStr(trace, 'onto_eval_question_id') || parsed?.questionId || '—'

    if (!map.has(sessionId)) {
      map.set(sessionId, {
        sessionId,
        taskId,
        questionId,
        traces: [],
        latestTimestamp: trace.timestamp || '',
        agentName: metaStr(trace, 'agent_name'),
        agentVersion: traceVersion(trace),
      })
    }
    const group = map.get(sessionId)!
    group.traces.push(trace)
    if (trace.timestamp && trace.timestamp > group.latestTimestamp) {
      group.latestTimestamp = trace.timestamp
    }
    if (!group.agentName) group.agentName = metaStr(trace, 'agent_name')
    if (!group.agentVersion) group.agentVersion = traceVersion(trace)
  }

  for (const group of map.values()) {
    group.traces.sort((a, b) => {
      const ra = Number(metaStr(a, 'onto_eval_run_index'))
      const rb = Number(metaStr(b, 'onto_eval_run_index'))
      if (!Number.isNaN(ra) && !Number.isNaN(rb) && ra !== rb) return ra - rb
      return (b.timestamp || '').localeCompare(a.timestamp || '')
    })
  }

  return Array.from(map.values()).sort((a, b) =>
    (b.latestTimestamp || '').localeCompare(a.latestTimestamp || ''),
  )
}

/**
 * 判定一条 trace 是不是 onto-eval 自己用 trace_agent_inference 上报的"评估包装 trace"。
 * 这类 trace 的特征：
 *  - name 以 "onto-eval/" 开头（langfuse_tracing.py:91 拼出来的命名）
 *  - tags 包含 "onto-eval"
 * 浏览页默认不展示这类记录，让用户专注于业务 agent 上报的原始 trace。
 *
 * 单一来源约定：本规则在后端 ``backend/services/langfuse_trace_filter.py``
 * 里有同名 Python 实现 (is_eval_reported_trace / filter_out_eval_reported_traces)，
 * 离线评估答案映射 (fetch_langfuse_answer_map) 与 trace_sync 时间窗 fallback
 * 都复用同一份判定。前后端实现务必保持规则一致；改这条规则时两边都改。
 */
function isEvalReportedTrace(trace: LangfuseTrace): boolean {
  const name = (trace.name || '').trim()
  if (name.startsWith('onto-eval/')) return true
  const tags = trace.tags || []
  return tags.includes('onto-eval')
}

/** 从 trace 列表里剔除 eval 上报的"评估包装 trace"。 */
function filterOutEvalReportedTraces(traces: LangfuseTrace[]): LangfuseTrace[] {
  return traces.filter((t) => !isEvalReportedTrace(t))
}

function traceMatchesFilters(trace: LangfuseTrace, filters: ExploreFilters): boolean {
  // 第一关：排除 onto-eval 端 trace_agent_inference 上报的"评估包装 trace"，
  // 让默认视图只展示业务 agent 自己的原始 trace。
  if (isEvalReportedTrace(trace)) return false

  const parsed = parseSessionId(trace.sessionId)
  const qid = metaStr(trace, 'onto_eval_question_id') || parsed?.questionId || ''

  if (filters.question_id && qid !== filters.question_id) return false

  if (filters.question_keyword.trim()) {
    const kw = filters.question_keyword.trim().toLowerCase()
    const text = extractQuestionFromTrace(trace).toLowerCase()
    if (!text.includes(kw)) return false
  }

  if (filters.agent.trim()) {
    const agent = filters.agent.trim()
    if (traceAgentKey(trace) !== agent) return false
  }

  if (filters.agent_version.trim()) {
    if (traceVersion(trace) !== filters.agent_version.trim()) return false
  }

  return true
}

function filterTraces(traces: LangfuseTrace[], filters: ExploreFilters): LangfuseTrace[] {
  return traces.filter((t) => traceMatchesFilters(t, filters))
}

function applyExploreFilters(groups: QueryGroup[], filters: ExploreFilters): QueryGroup[] {
  const hasSecondaryFilter = Boolean(
    filters.question_id ||
      filters.question_keyword.trim() ||
      filters.agent.trim() ||
      filters.agent_version.trim(),
  )

  return groups
    .filter((g) => {
      if (filters.question_id && g.questionId !== filters.question_id) return false
      if (filters.question_keyword.trim()) {
        const kw = filters.question_keyword.trim().toLowerCase()
        if (!g.questionText.toLowerCase().includes(kw)) return false
      }
      if (filters.agent.trim() && traceAgentKey(g.traces[0] || ({} as LangfuseTrace)) !== filters.agent.trim()) {
        const match = g.traces.some((t) => traceAgentKey(t) === filters.agent.trim())
        if (!match && g.agentName !== filters.agent.trim()) return false
      }
      if (filters.agent_version.trim()) {
        const match = g.traces.some((t) => traceVersion(t) === filters.agent_version.trim())
        if (!match && g.agentVersion !== filters.agent_version.trim()) return false
      }
      return true
    })
    .map((g) => ({
      ...g,
      traces: g.traces.filter((t) => traceMatchesFilters(t, filters)),
    }))
    .filter((g) => !hasSecondaryFilter || g.traces.length > 0)
}

const URL_FILTER_KEYS = [
  'project',
  'task_id',
  'question_id',
  'question_keyword',
  'agent',
  'agent_version',
] as const

function getInitialFilters(): ExploreFilters {
  const params =
    typeof window === 'undefined' ? null : new URLSearchParams(window.location.search)
  if (!params) return { ...DEFAULT_FILTERS }
  return {
    project: params.get('project')?.trim() || '',
    task_id: params.get('task_id')?.trim() || '',
    question_id: params.get('question_id')?.trim() || '',
    question_keyword: params.get('question_keyword')?.trim() || '',
    agent: params.get('agent')?.trim() || '',
    agent_version: params.get('agent_version')?.trim() || '',
    limit: DEFAULT_FILTERS.limit,
  }
}

function clearExploreUrlParams() {
  if (typeof window === 'undefined') return
  const url = new URL(window.location.href)
  for (const key of URL_FILTER_KEYS) {
    url.searchParams.delete(key)
  }
  const qs = url.searchParams.toString()
  window.history.replaceState({}, '', `${url.pathname}${qs ? `?${qs}` : ''}${url.hash}`)
}

function syncExploreUrlParams(filters: ExploreFilters) {
  if (typeof window === 'undefined') return
  const url = new URL(window.location.href)
  for (const key of URL_FILTER_KEYS) {
    const val = filters[key]?.trim() || ''
    if (val) url.searchParams.set(key, val)
    else url.searchParams.delete(key)
  }
  const qs = url.searchParams.toString()
  window.history.replaceState({}, '', `${url.pathname}${qs ? `?${qs}` : ''}${url.hash}`)
}

export function LangfuseTraceExplore() {
  const [langfuseEnabled, setLangfuseEnabled] = useState<boolean | null>(null)
  const [rawTraces, setRawTraces] = useState<LangfuseTrace[]>([])
  const [lfLoading, setLfLoading] = useState(false)
  const [lfError, setLfError] = useState<string | null>(null)
  const [lfQuery, setLfQuery] = useState<ExploreFilters>(getInitialFilters)
  const [appliedFilters, setAppliedFilters] = useState<ExploreFilters>(getInitialFilters)
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null)
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null)
  const [lfDetail, setLfDetail] = useState<LangfuseTraceDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [langfuseHost, setLangfuseHost] = useState('')
  const [questionMaps, setQuestionMaps] = useState<Record<string, Record<string, string>>>({})
  const [facets, setFacets] = useState<TraceFacetsResponse | null>(null)
  const [facetsLoading, setFacetsLoading] = useState(false)
  const [listPage, setListPage] = useState(1)
  const [listMeta, setListMeta] = useState<ListMeta>({})
  const [projectOptions, setProjectOptions] = useState<LangfuseProjectOption[]>([])
  const [defaultProject, setDefaultProject] = useState('')
  const [initReady, setInitReady] = useState(false)
  const [readSource, setReadSource] = useState<ExploreReadSource>(null)
  const [detailSource, setDetailSource] = useState<DetailReadSource>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [infoBanner, setInfoBanner] = useState<string | null>(null)

  const canUseFactLayer = Boolean(lfQuery.task_id.trim())

  const selectedProjectName = useMemo(
    () => projectOptions.find((p) => p.id === lfQuery.project)?.name || lfQuery.project,
    [projectOptions, lfQuery.project],
  )

  const questionOptions = useMemo(
    () => facets?.questions ?? [],
    [facets],
  )

  const agentOptions = useMemo(
    () => facets?.agents ?? [],
    [facets],
  )

  const agentVersionOptions = useMemo(() => {
    const agentKey = lfQuery.agent.trim()
    if (!agentKey || !facets?.versions_by_agent) return []
    return facets.versions_by_agent[agentKey] ?? []
  }, [facets, lfQuery.agent])

  const fetchQuestionMapForTask = async (taskId: string): Promise<Record<string, string>> => {
    try {
      const res = await axios.get(`/api/trace/tasks/${taskId}/questions`)
      const m: Record<string, string> = {}
      for (const q of res.data.questions || []) {
        m[String(q.id)] = q.text || ''
      }
      setQuestionMaps((prev) => ({ ...prev, [taskId]: m }))
      return m
    } catch {
      return {}
    }
  }

  const loadQuestionMapsForTraces = async (traces: LangfuseTrace[]) => {
    const taskIds = [
      ...new Set(
        traces
          .map((t) => metaStr(t, 'onto_eval_task_id') || parseSessionId(t.sessionId)?.taskId)
          .filter((id): id is string => Boolean(id && id !== '—')),
      ),
    ]
    await Promise.all(taskIds.map((id) => fetchQuestionMapForTask(id)))
  }

  const filteredTraces = useMemo(
    () => filterTraces(rawTraces, appliedFilters),
    [rawTraces, appliedFilters],
  )

  const queryGroups = useMemo(() => {
    const taskFilter = appliedFilters.task_id.trim()
    let base = groupTracesByQuery(filteredTraces)
    if (taskFilter) {
      base = base.filter(
        (g) => g.taskId.includes(taskFilter) || g.sessionId.includes(taskFilter),
      )
    }
    const groups = base.map((g) => ({
      ...g,
      questionText: resolveQuestionText(g, questionMaps),
    }))
    // 显式剔除 trace 为空的 group：eval 包装 trace 已在数据入口被过滤掉，
    // 某些 group 可能因此没有可见 trace，不应该展示给用户。
    return applyExploreFilters(groups, appliedFilters).filter(
      (g) => g.traces.length > 0,
    )
  }, [filteredTraces, appliedFilters, questionMaps])

  const selectedGroup = useMemo(
    () => queryGroups.find((g) => g.sessionId === selectedSessionId) ?? null,
    [queryGroups, selectedSessionId],
  )

  const updateFilter = useCallback((patch: Partial<ExploreFilters>) => {
    setLfQuery((prev) => ({ ...prev, ...patch }))
  }, [])

  const handleAgentChange = (agent: string) => {
    const versions = agent ? (facets?.versions_by_agent?.[agent] ?? []) : []
    applyFilterAndRefetch({
      agent,
      agent_version: versions.length === 1 ? versions[0] : '',
    })
  }

  const handleTaskIdChange = (task_id: string) => {
    setLfQuery({
      ...lfQuery,
      task_id,
      question_id: '',
      question_keyword: '',
      agent: '',
      agent_version: '',
      // project 保持不变
    })
    setFacets(null)
    setRawTraces([])
    setListMeta({})
    setSelectedSessionId(null)
    setSelectedTraceId(null)
    setLfDetail(null)
  }

  const syncSelectionAfterFilter = useCallback(
    (traces: LangfuseTrace[], filters: ExploreFilters) => {
      setAppliedFilters({ ...filters })
      const display = filterTraces(traces, filters)
      const groups = applyExploreFilters(
        groupTracesByQuery(display).map((g) => ({
          ...g,
          questionText: resolveQuestionText(g, questionMaps),
        })),
        filters,
      )
      const withTraces = groups.filter((g) => g.traces.length > 0)
      const pickFrom = withTraces.length > 0 ? withTraces : groups
      if (pickFrom.length === 0) {
        setSelectedSessionId(null)
        setSelectedTraceId(null)
        setLfDetail(null)
        return
      }
      setSelectedSessionId((prev) => {
        const keep = pickFrom.find((g) => g.sessionId === prev)
        const next = keep ?? pickFrom.find((g) => g.traces.length > 0) ?? pickFrom[0]
        setSelectedTraceId(next.traces[0]?.id ?? null)
        return next.sessionId
      })
    },
    [questionMaps],
  )

  const buildFetchParams = (filters: ExploreFilters, taskId: string, page: number) => {
    const params = new URLSearchParams()
    appendProjectParam(params, filters.project)
    params.append('limit', String(filters.limit))
    params.append('page', String(page))
    if (filters.question_id) params.append('question_id', filters.question_id)
    if (filters.agent.trim()) params.append('agent_name', filters.agent.trim())
    if (filters.agent_version.trim()) params.append('agent_version', filters.agent_version.trim())
    if (filters.question_keyword.trim() && taskId) {
      params.append('question_keyword', filters.question_keyword.trim())
    }
    return params
  }

  const fetchFacets = async (filters: ExploreFilters) => {
    const taskId = filters.task_id.trim()
    const params = new URLSearchParams()
    appendProjectParam(params, filters.project)
    if (taskId) params.append('task_id', taskId)
    const res = await axios.get<TraceFacetsResponse>(`/api/trace/facets?${params.toString()}`)
    setFacets(res.data)
    if (taskId || res.data.scan?.source === 'agent_runs' || res.data.source === 'agent_runs') {
      setReadSource('fact_layer')
    } else {
      setReadSource('langfuse')
    }
    if (taskId) {
      const m: Record<string, string> = {}
      for (const q of res.data.questions || []) {
        m[String(q.id)] = q.text || ''
      }
      setQuestionMaps((prev) => ({ ...prev, [taskId]: m }))
    }
    return res.data
  }

  const fetchTracePage = async (page: number, filters: ExploreFilters) => {
    const taskId = filters.task_id.trim()
    const params = buildFetchParams(filters, taskId, page)

    let res
    if (taskId) {
      params.append('task_id', taskId)
      res = await axios.get(`/api/trace/sessions?${params.toString()}`)
    } else {
      res = await axios.get(`/api/trace/traces?${params.toString()}`)
      await loadQuestionMapsForTraces(res.data.data || [])
    }

    const data: LangfuseTrace[] = res.data.data || []
    // 数据流入口先剔除 onto-eval 端 trace_agent_inference 上报的"评估包装 trace"，
    // 让浏览页默认只展示业务 agent 自己的原始 trace（name 以 "onto-eval/" 开头
    // 或 tags 包含 "onto-eval" 的都视为 eval 报告，过滤掉）。
    const filteredData = filterOutEvalReportedTraces(data)
    setRawTraces(filteredData)
    setListMeta(res.data.meta || {})
    if (res.data.meta?.source === 'agent_runs' || taskId) {
      setReadSource('fact_layer')
    }
    setListPage(page)
    // sync 走的是过滤后的数据，避开被剔除的 eval 包装 trace 后再选默认 group。
    syncSelectionAfterFilter(filteredData, filters)
  }

  const refetchListPage1 = async (next: ExploreFilters) => {
    if (!facets || lfLoading) return
    setLfLoading(true)
    setLfError(null)
    try {
      await fetchTracePage(1, next)
      setLfQuery(next)
      syncExploreUrlParams(next)
    } catch (err: unknown) {
      const ax = err as { response?: { status?: number; data?: { detail?: string } } }
      const detail = ax.response?.data?.detail
      setLfError(
        detail ||
          (ax.response?.status === 503
            ? 'Langfuse 服务暂不可用，请稍后重试'
            : '拉取 Trace 失败'),
      )
    } finally {
      setLfLoading(false)
    }
  }

  const applyFilterAndRefetch = (patch: Partial<ExploreFilters>) => {
    if (!facets) return
    const next = { ...lfQuery, ...patch }
    setLfQuery(next)
    void refetchListPage1(next)
  }

  /** 统一查询入口：facets 扫描 + 列表分页，保证条件与结果一致 */
  const runExploreQuery = async (filters: ExploreFilters, page = 1) => {
    setLfLoading(true)
    setFacetsLoading(true)
    setLfError(null)
    setInfoBanner(null)
    setLfQuery(filters)
    setListPage(page)
    const taskId = filters.task_id.trim()
    try {
      if (taskId) await fetchQuestionMapForTask(taskId)

      await fetchFacets(filters)
      await fetchTracePage(page, filters)
      syncExploreUrlParams(filters)
      if (taskId) {
        setInfoBanner(
          '筛选项与列表已从 agent_runs 事实层读取；Trace 详情优先 Langfuse，失败时自动降级到 run_span_nodes。',
        )
      } else {
        setInfoBanner(null)
      }
    } catch (err: unknown) {
      setRawTraces([])
      setFacets(null)
      setListMeta({})
      setSelectedSessionId(null)
      setSelectedTraceId(null)
      setLfDetail(null)
      setReadSource(null)
      const detail = extractAxiosDetail(err)
      const ax = err as { response?: { status?: number } }
      setLfError(
        detail ||
          (taskId
            ? '拉取失败。若 Langfuse 未配置，请确认任务 ID 正确且已有 AgentRun 记录。'
            : ax.response?.status === 503
              ? 'Langfuse 服务暂不可用；填写任务 ID 可改从 agent_runs 事实层查询。'
              : '拉取 Trace 失败'),
      )
    } finally {
      setLfLoading(false)
      setFacetsLoading(false)
    }
  }

  const fetchLangfuseTraces = () => runExploreQuery(lfQuery, 1)

  const handleProjectChange = (project: string) => {
    const next: ExploreFilters = {
      ...DEFAULT_FILTERS,
      project,
      limit: lfQuery.limit,
    }
    setLfQuery(next)
    sessionStorage.setItem(PROJECT_STORAGE_KEY, project)
    setFacets(null)
    setRawTraces([])
    setListMeta({})
    void runExploreQuery(next, 1)
  }

  const handleReset = () => {
    const project = defaultProject || lfQuery.project
    const defaults: ExploreFilters = { ...DEFAULT_FILTERS, project }
    sessionStorage.setItem(PROJECT_STORAGE_KEY, project)
    void runExploreQuery(defaults, 1)
  }

  const fetchTraceDetail = async (traceId: string, project: string) => {
    setDetailLoading(true)
    setDetailError(null)
    setDetailSource(null)
    const loadDetail = async (source?: 'fact') => {
      const params = new URLSearchParams()
      appendProjectParam(params, project)
      if (source === 'fact') params.append('source', 'fact')
      const res = await axios.get<LangfuseTraceDetail>(
        `/api/trace/traces/${traceId}?${params.toString()}`,
      )
      return res.data
    }
    try {
      const detail = await loadDetail()
      setLfDetail(detail)
      setDetailSource(isFactLayerDetail(detail) ? 'fact_layer' : 'langfuse')
    } catch (err) {
      if (shouldRetryTraceDetailWithFact(err)) {
        try {
          const detail = await loadDetail('fact')
          setLfDetail(detail)
          setDetailSource('fact_layer')
          setInfoBanner((prev) =>
            prev ||
            'Trace 详情已从物化事实层（run_span_nodes）加载；完整 observation 需 Langfuse 或等待 Builder 同步。',
          )
          return
        } catch (factErr) {
          console.error('Failed to fetch trace detail from fact layer:', factErr)
          setDetailError(
            extractAxiosDetail(factErr) ||
              'Langfuse 与事实层均未找到该 Trace。离线 Run 可能仅有 stub，或 Span 尚未物化。',
          )
        }
      } else {
        console.error('Failed to fetch trace detail:', err)
        setDetailError(extractAxiosDetail(err) || '无法加载 Trace 详情')
      }
      setLfDetail(null)
    } finally {
      setDetailLoading(false)
    }
  }

  useEffect(() => {
    let cancelled = false
    const bootstrap = async () => {
      let projects: LangfuseProjectOption[] = []
      let def = ''
      try {
        const pr = await axios.get('/api/trace/projects')
        if (cancelled) return
        projects = pr.data.projects || []
        def = (pr.data.default_project as string) || projects[0]?.id || ''
        setProjectOptions(projects)
        setDefaultProject(def)
        if (pr.data.host) setLangfuseHost(String(pr.data.host).replace(/\/$/, ''))
      } catch (err: unknown) {
        if (cancelled) return
        setLangfuseEnabled(false)
        const urlFilters = getInitialFilters()
        const bootstrapTaskId = urlFilters.task_id.trim()
        const saved = sessionStorage.getItem(PROJECT_STORAGE_KEY) || ''
        const initial: ExploreFilters = {
          ...DEFAULT_FILTERS,
          ...urlFilters,
          project: urlFilters.project || saved,
        }
        setLfQuery(initial)
        setAppliedFilters(initial)
        if (bootstrapTaskId) {
          await runExploreQuery(initial, 1)
          if (!cancelled) {
            setLfError(null)
            setInfoBanner(
              'Langfuse 项目未配置：已从 agent_runs 事实层加载（填写任务 ID 即可探索，无需 Langfuse）。',
            )
          }
        } else {
          setLfError(
            extractAxiosDetail(err) ||
              '无法加载 Langfuse 项目列表。填写任务 ID 可从事实层查询；或配置 LANGFUSE_HOST 与 API Key。',
          )
        }
        setInitReady(true)
        return
      }

      const urlFilters = getInitialFilters()
      const saved = sessionStorage.getItem(PROJECT_STORAGE_KEY) || ''
      const project = urlFilters.project || saved || def
      const initial: ExploreFilters = { ...DEFAULT_FILTERS, ...urlFilters, project }
      const bootstrapTaskId = initial.task_id.trim()
      setLfQuery(initial)
      setAppliedFilters(initial)
      sessionStorage.setItem(PROJECT_STORAGE_KEY, project)

      try {
        const healthParams = new URLSearchParams()
        appendProjectParam(healthParams, project)
        const health = await axios.get(`/api/trace/health?${healthParams.toString()}`)
        if (cancelled) return

        const ok = health.data.enabled && health.data.status === 'ok'
        setLangfuseEnabled(ok)
        if (health.data.host) setLangfuseHost(String(health.data.host).replace(/\/$/, ''))
        if (!ok) {
          if (bootstrapTaskId) {
            await runExploreQuery(initial, 1)
            if (!cancelled) {
              setInfoBanner(
                'Langfuse 连接异常：已改用 agent_runs 事实层；详情将自动尝试 run_span_nodes 降级。',
              )
            }
          } else {
            setLfError(health.data.message || 'Langfuse 连接异常。填写任务 ID 可从事实层查询，或切换项目后重试。')
          }
          return
        }
        await runExploreQuery(initial, 1)
      } catch (err: unknown) {
        if (!cancelled) {
          setLangfuseEnabled(false)
          if (bootstrapTaskId) {
            await runExploreQuery(initial, 1)
            setInfoBanner('Langfuse 健康检查失败：已尝试从 agent_runs 事实层加载。')
          } else {
            setLfError(extractAxiosDetail(err) || 'Langfuse 健康检查失败；填写任务 ID 可使用事实层读路径。')
          }
        }
      } finally {
        if (!cancelled) setInitReady(true)
      }
    }
    void bootstrap()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 仅挂载时初始化
  }, [])

  useEffect(() => {
    if (selectedTraceId && (lfQuery.project || canUseFactLayer)) {
      fetchTraceDetail(selectedTraceId, lfQuery.project)
    } else {
      setLfDetail(null)
      setDetailSource(null)
      setDetailError(null)
    }
  }, [selectedTraceId, lfQuery.project, canUseFactLayer])

  useEffect(() => {
    if (!lfDetail || !selectedGroup || selectedGroup.questionText) return
    const text = extractQuestionFromDetail(lfDetail)
    if (!text || selectedGroup.taskId === '—') return
    setQuestionMaps((prev) => {
      if (prev[selectedGroup.taskId]?.[selectedGroup.questionId]) return prev
      return {
        ...prev,
        [selectedGroup.taskId]: {
          ...(prev[selectedGroup.taskId] || {}),
          [selectedGroup.questionId]: text,
        },
      }
    })
  }, [lfDetail, selectedGroup])

  /** 联动：选项变化后清除无效已选值 */
  useEffect(() => {
    if (!facets || lfLoading) return
    if (lfQuery.question_id && !questionOptions.some((q) => q.id === lfQuery.question_id)) {
      void refetchListPage1({ ...lfQuery, question_id: '' })
    }
  }, [questionOptions, facets, lfLoading])

  useEffect(() => {
    if (!facets || lfLoading) return
    if (lfQuery.agent_version && !agentVersionOptions.includes(lfQuery.agent_version)) {
      void refetchListPage1({ ...lfQuery, agent_version: '' })
    }
  }, [agentVersionOptions, facets, lfLoading])

  useEffect(() => {
    if (!lfQuery.agent && lfQuery.agent_version) {
      updateFilter({ agent_version: '' })
    }
  }, [lfQuery.agent, lfQuery.agent_version, updateFilter])

  const goListPage = (page: number) => {
    if (!facets || lfLoading) return
    if (page < 1 || (listMeta.totalPages && page > listMeta.totalPages)) return
    void (async () => {
      setLfLoading(true)
      setLfError(null)
      try {
        await fetchTracePage(page, appliedFilters)
      } catch (err: unknown) {
        const ax = err as { response?: { data?: { detail?: string } } }
        setLfError(ax.response?.data?.detail || '拉取 Trace 失败')
      } finally {
        setLfLoading(false)
      }
    })()
  }

  const selectQuery = (group: QueryGroup) => {
    setSelectedSessionId(group.sessionId)
    setSelectedTraceId(group.traces[0]?.id ?? null)
  }

  const listEmpty = !lfLoading && facets != null && queryGroups.length === 0
  const awaitingQuery = !lfLoading && facets == null && !lfError
  const factLayerExplore = canUseFactLayer || readSource === 'fact_layer'

  if (!initReady || (langfuseEnabled === null && !canUseFactLayer)) {
    return (
      <div className="lf-explore-empty">
        <p>正在加载 Trace 探索…</p>
      </div>
    )
  }

  if (langfuseEnabled === false && projectOptions.length === 0 && !factLayerExplore) {
    return (
      <div className="lf-explore-empty">
        <p>Langfuse 未配置或项目列表为空</p>
        <p className="lf-explore-hint">
          填写<strong>任务 ID</strong>可从 agent_runs 事实层查询；或于{' '}
          <code>onto-eval/.env</code> 配置 LANGFUSE_HOST 与 API Key。
        </p>
        {lfError && <p className="lf-explore-error">{lfError}</p>}
      </div>
    )
  }

  return (
    <div className="lf-explore">
      {infoBanner && (
        <div className="lf-explore-banner lf-explore-info" role="status">
          {infoBanner}
        </div>
      )}
      {langfuseEnabled === false && lfError && !factLayerExplore && (
        <div className="lf-explore-banner lf-explore-error" role="alert">
          {lfError}
        </div>
      )}
      <div className="lf-explore-toolbar">
        <div className="lf-explore-filters">
          <label className="lf-field lf-field-project">
            Langfuse 项目
            <select
              value={lfQuery.project}
              onChange={(e) => handleProjectChange(e.target.value)}
              disabled={lfLoading || facetsLoading || (projectOptions.length === 0 && !factLayerExplore)}
            >
              {projectOptions.length === 0 ? (
                <option value="">{factLayerExplore ? '事实层（无需 Langfuse）' : '无可用项目'}</option>
              ) : (
                projectOptions.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))
              )}
            </select>
          </label>
          <label className="lf-field lf-field-task">
            任务 ID
            <input
              type="text"
              value={lfQuery.task_id}
              onChange={(e) => handleTaskIdChange(e.target.value)}
              placeholder="task_xxx"
            />
          </label>
          <label className="lf-field">
            问题
            <select
              value={lfQuery.question_id}
              onChange={(e) =>
                applyFilterAndRefetch({ question_id: e.target.value, question_keyword: '' })
              }
              disabled={!facets || facetsLoading}
              title={!facets ? '请先点击查询，由后端扫描 Trace 生成问题列表' : undefined}
            >
              <option value="">全部问题</option>
              {questionOptions.map((q) => (
                <option key={q.id} value={q.id}>
                  {(q.text.length > 48 ? `${q.text.slice(0, 48)}…` : q.text) || `题目 ${q.id}`}
                </option>
              ))}
            </select>
          </label>
          <label className="lf-field lf-field-keyword">
            题干关键词
            <input
              type="text"
              value={lfQuery.question_keyword}
              onChange={(e) => {
                const kw = e.target.value
                updateFilter({ question_keyword: kw, question_id: '' })
              }}
              onBlur={() => {
                if (facets) void refetchListPage1({ ...lfQuery, question_id: '' })
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && facets) {
                  void refetchListPage1({ ...lfQuery, question_id: '' })
                }
              }}
              placeholder="模糊匹配，回车生效"
            />
          </label>
          <label className="lf-field">
            Agent 名称
            <select
              value={lfQuery.agent}
              onChange={(e) => handleAgentChange(e.target.value)}
              disabled={!facets || facetsLoading}
              title={!facets ? '请先点击查询' : undefined}
            >
              <option value="">全部 Agent</option>
              {agentOptions.map((a) => (
                <option key={a.value} value={a.value}>
                  {a.label}
                </option>
              ))}
            </select>
          </label>
          <label className="lf-field">
            Agent 版本
            <select
              value={lfQuery.agent_version}
              onChange={(e) => applyFilterAndRefetch({ agent_version: e.target.value })}
              disabled={!lfQuery.agent.trim() || agentVersionOptions.length === 0}
              title={
                !lfQuery.agent.trim()
                  ? '请先选择 Agent'
                  : agentVersionOptions.length === 0
                    ? '当前 Agent 在 Trace 中无版本记录'
                    : undefined
              }
            >
              <option value="">全部版本</option>
              {agentVersionOptions.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <label className="lf-field lf-field-limit">
            条数
            <input
              type="number"
              min={1}
              max={200}
              value={lfQuery.limit}
              onChange={(e) => updateFilter({ limit: parseInt(e.target.value, 10) || 100 })}
            />
          </label>
        </div>
        <div className="lf-explore-toolbar-foot">
          <div className="lf-explore-actions">
            <button
              type="button"
              className="lf-btn-query"
              onClick={fetchLangfuseTraces}
              disabled={lfLoading || facetsLoading}
            >
              <span className="lf-btn-query-text">
                {lfLoading ? '查询中…' : '查询'}
              </span>
            </button>
            <button
              type="button"
              className="lf-btn-reset"
              onClick={handleReset}
              disabled={lfLoading || facetsLoading}
              title="恢复默认条件并重新查询（全部 onto-eval Trace，无任务/问题/Agent 筛选）"
            >
              重置
            </button>
          </div>
        <span className="lf-explore-stat">
          项目 {selectedProjectName || (factLayerExplore ? '事实层' : '—')} · 本页 {queryGroups.length} 个问题 ·{' '}
          {filteredTraces.length} 条 Trace
            {listMeta.totalItems != null ? ` · 共约 ${listMeta.totalItems} 条` : ''}
            {readSource === 'fact_layer' ? ' · 读源 agent_runs' : ''}
            {facets?.scan?.truncated
              ? ` · 筛选项已扫描 ${facets.scan.scanned} 条（上限 ${facets.scan.max_scan}）`
              : facets?.scan
                ? ` · 筛选项已扫描 ${facets.scan.scanned} 条`
                : ''}
          </span>
        </div>
      </div>

      {listMeta.totalPages != null && listMeta.totalPages > 1 && (
        <div className="lf-explore-pagination">
          <button
            type="button"
            disabled={lfLoading || listPage <= 1}
            onClick={() => goListPage(listPage - 1)}
          >
            上一页
          </button>
          <span>
            第 {listPage} / {listMeta.totalPages} 页
          </span>
          <button
            type="button"
            disabled={lfLoading || listPage >= (listMeta.totalPages ?? 1)}
            onClick={() => goListPage(listPage + 1)}
          >
            下一页
          </button>
        </div>
      )}

      {awaitingQuery && (
        <p className="lf-explore-hint lf-explore-hint-bar">
          请输入条件后点击「查询」，或点击「重置」加载默认结果。上千条时筛选项由后端扫描聚合，列表分页展示。
        </p>
      )}

      {listEmpty && (
        <p className="lf-explore-hint lf-explore-hint-bar lf-explore-hint-warn">
          {factLayerExplore
            ? '当前任务在 agent_runs 事实层无匹配 Run；请确认任务 ID 正确且评测已产生 AgentRun。'
            : '当前条件下 Langfuse 无匹配 Trace，可填写任务 ID 改从事实层查询，或点击「重置」。'}
        </p>
      )}

      {lfError && !infoBanner && <div className="lf-explore-error">{lfError}</div>}

      <div className="lf-explore-layout">
        <aside className="lf-explore-panel lf-explore-queries">
          <div className="lf-panel-head">
            <h4>评测问题 (Query)</h4>
            <span className="lf-panel-sub">按评测问题聚合</span>
          </div>
          {lfLoading ? (
            <div className="loading">加载中…</div>
          ) : queryGroups.length === 0 ? (
            <div className="empty">
              {awaitingQuery ? '请点击查询或重置' : listEmpty ? '当前筛选无匹配 Trace' : '无匹配记录'}
            </div>
          ) : (
            <ul className="lf-query-list">
              {queryGroups.map((group) => (
                <li
                  key={group.sessionId}
                  className={`lf-query-item ${selectedSessionId === group.sessionId ? 'active' : ''}`}
                  onClick={() => selectQuery(group)}
                >
                  <div className="lf-query-title">
                    <span
                      className={`lf-badge ${group.traces.length === 0 ? 'lf-badge-empty' : ''}`}
                    >
                      {group.traces.length} runs
                    </span>
                  </div>
                  <div className="lf-query-text" title={group.questionText || undefined}>
                    {group.questionText || (
                      <span className="lf-query-text-missing">（题干未写入 Trace，可选中查看 Runs）</span>
                    )}
                  </div>
                  {!lfQuery.task_id.trim() && (
                    <div className="lf-query-meta">任务 {group.taskId}</div>
                  )}
                  {(group.agentName || group.agentVersion) && (
                    <div className="lf-query-meta">
                      {group.agentName}
                      {group.agentVersion ? ` · ${group.agentVersion}` : ''}
                    </div>
                  )}
                  <div className="lf-query-time">{formatDate(group.latestTimestamp)}</div>
                </li>
              ))}
            </ul>
          )}
        </aside>

        <section className="lf-explore-panel lf-explore-runs">
          <div className="lf-panel-head">
            <h4>推理运行 (Runs)</h4>
            {selectedGroup && (
              <span className="lf-panel-sub lf-panel-sub-question" title={selectedGroup.questionText}>
                {selectedGroup.questionText
                  ? selectedGroup.questionText.length > 120
                    ? `${selectedGroup.questionText.slice(0, 120)}…`
                    : selectedGroup.questionText
                  : '（无题干）'}
              </span>
            )}
          </div>
          {!selectedGroup ? (
            <div className="empty">请从左侧选择一个问题</div>
          ) : selectedGroup.traces.length === 0 ? (
            <div className="empty">该问题暂无匹配 Trace</div>
          ) : (
            <table className="stats-table lf-runs-table">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Trace</th>
                  <th>时间</th>
                  <th>延迟</th>
                  <th>Agent</th>
                  <th>版本</th>
                </tr>
              </thead>
              <tbody>
                {selectedGroup.traces.map((trace) => (
                  <tr
                    key={trace.id}
                    className={selectedTraceId === trace.id ? 'lf-row-active' : ''}
                    onClick={() => setSelectedTraceId(trace.id)}
                  >
                    <td>{metaStr(trace, 'onto_eval_run_index') || '—'}</td>
                    <td className="cell-ellipsis" title={trace.name || trace.id}>
                      {trace.name || trace.id.slice(0, 12) + '…'}
                    </td>
                    <td>{trace.timestamp ? formatDate(trace.timestamp) : '—'}</td>
                    <td>{trace.latency != null ? formatDuration(Math.round(trace.latency)) : '—'}</td>
                    <td className="cell-ellipsis">
                      {metaStr(trace, 'agent_name') || '—'}
                    </td>
                    <td>{traceVersion(trace) || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <aside className="lf-explore-panel lf-explore-detail">
          <div className="lf-panel-head">
            <h4>Trace 详情</h4>
            {detailSource === 'fact_layer' && (
              <span className="lf-badge lf-badge-fact" title="来自 run_span_nodes 物化层">
                事实层
              </span>
            )}
            {lfDetail?.htmlPath && langfuseHost && detailSource !== 'fact_layer' && (
              <a
                href={
                  lfDetail.htmlPath.startsWith('http')
                    ? lfDetail.htmlPath
                    : `${langfuseHost}${lfDetail.htmlPath.startsWith('/') ? '' : '/'}${lfDetail.htmlPath}`
                }
                target="_blank"
                rel="noreferrer"
                className="lf-link-langfuse"
              >
                在 Langfuse 中打开
              </a>
            )}
          </div>
          {detailLoading ? (
            <div className="loading">加载详情…</div>
          ) : !selectedTraceId ? (
            <div className="empty">选择一条 Run 查看详情</div>
          ) : !lfDetail ? (
            <div className="empty">
              {detailError || '无法加载详情'}
            </div>
          ) : (
            <div className="lf-detail-body">
              <div className="lf-detail-kv">
                <span className="lf-k">Trace ID</span>
                <code>{lfDetail.id}</code>
              </div>
              <div className="lf-detail-kv">
                <span className="lf-k">Session</span>
                <code>{lfDetail.sessionId || '—'}</code>
              </div>
              <div className="lf-detail-kv">
                <span className="lf-k">Agent</span>
                <span>{metaStr(lfDetail, 'agent_name') || '—'}</span>
              </div>
              <div className="lf-detail-kv">
                <span className="lf-k">版本</span>
                <span>{traceVersion(lfDetail) || '—'}</span>
              </div>
              <div className="lf-detail-kv">
                <span className="lf-k">时间</span>
                <span>{lfDetail.timestamp ? formatDate(lfDetail.timestamp) : '—'}</span>
              </div>

              <h5 className="lf-detail-section">Observations</h5>
              {(lfDetail.observations || []).length === 0 ? (
                <div className="empty">
                  {detailSource === 'fact_layer'
                    ? '无物化 Span（Run 可能为 offline stub，或 Builder 尚未同步 trace）'
                    : '无 observation（可能仍在写入）'}
                </div>
              ) : (
                <table className="stats-table">
                  <thead>
                    <tr>
                      <th>名称</th>
                      <th>类型</th>
                      <th>延迟</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(lfDetail.observations || []).map((obs) => (
                      <tr key={obs.id}>
                        <td>{obs.name || obs.id}</td>
                        <td>
                          <span className={`lf-obs-type lf-obs-${(obs.type || '').toLowerCase()}`}>
                            {obs.type}
                          </span>
                        </td>
                        <td>{obs.latency != null ? formatDuration(Math.round(obs.latency)) : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              <h5 className="lf-detail-section">Metadata</h5>
              <pre className="lf-metadata-pre">
                {JSON.stringify(lfDetail.metadata || {}, null, 2)}
              </pre>
            </div>
          )}
        </aside>
      </div>
    </div>
  )
}
