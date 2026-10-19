import { useState, useEffect, useRef } from 'react'
import { CrossTaskScenarioMatrix } from './CrossTaskScenarioMatrix'
import { EvalDetailModal } from './EvalDetailModal'
import { ColumnFilter } from './ColumnFilter'
import { ColumnConfig, type ColumnConfigItem } from './ColumnConfig'
import { formatDate, formatDateShort } from '../utils/dateUtils'
import {
  OFFLINE_NA_LABEL,
  OFFLINE_NA_TOOLTIP,
  isOfflineEvaluationTask,
  offlineNaTextStyle,
} from '../utils/evaluationDisplay'
import { PageHeader } from './PageHeader'
import { DimensionVerdictBadgesCell } from './DimensionVerdictBadgesCell'
import type { DimensionVerdictBadgesPayload } from '../types/dimensionVerdict'

const API_BASE = '/api'

const VISIBLE_COLUMNS_KEY = 'history-visible-columns'

interface Task {
  id: string
  dataset_id: string
  dataset_name?: string | null
  status: string
  created_at: string
  completed_at: string | null
  agent_id?: string | null
  agent_name?: string | null
  agent_workspace_name?: string | null
  agent_project_name?: string | null
  agent_count?: number
  agent_run_count?: number
  agent_versions_label?: string | null
  inference_model_summary?: {
    display: string
    count: number
    labels: string[]
  }
  execution_mode_summary?: {
    display: string
    count: number
    labels: string[]
    deep_thinking_enabled?: boolean | null
  }
  llm_model?: string | null
  overall_avg_score?: number | null
  total_score?: number | null
  result_score?: number | null
  process_score?: number | null
  dimension_verdict_badges?: DimensionVerdictBadgesPayload | null
}

interface Props {
  onSelectTask: (taskId: string) => void
  onResumeTask?: (taskId: string) => void
  /** 在「详情」tab 页签中打开任务详情（替代原弹窗） */
  onSelectDetail?: (taskId: string) => void
}

function formatScore(score?: number | null): string {
  return score == null ? '—' : score.toFixed(3)
}

function shouldShowHistoryScores(status: string): boolean {
  return status === 'completed'
}

function historyTaskScores(task: Task) {
  if (!shouldShowHistoryScores(task.status)) {
    return { total: null, result: null, process: null }
  }
  return {
    total: task.total_score ?? task.overall_avg_score ?? null,
    result: task.result_score ?? task.overall_avg_score ?? null,
    process: task.process_score ?? null,
  }
}

// 可配置列定义（顺序即展示顺序）。勾选框列与操作列恒定展示，不参与配置。
const COLUMN_DEFS: ColumnConfigItem[] = [
  { key: 'taskId', label: '评测任务Id', defaultVisible: true },
  { key: 'datasetName', label: '评测集名称', defaultVisible: true },
  { key: 'agentName', label: 'Agent 名称', defaultVisible: true },
  { key: 'agentVersion', label: 'Agent版本', defaultVisible: false },
  { key: 'inferenceModel', label: '被测推理模型', defaultVisible: false },
  { key: 'agentWorkspace', label: 'Agent 工作空间', defaultVisible: true },
  { key: 'agentRunCount', label: 'Agent 运行次数', defaultVisible: false },
  { key: 'thinkingMode', label: 'Thinking 模式', defaultVisible: false },
  { key: 'evalModel', label: '评估模型', defaultVisible: false },
  { key: 'totalScore', label: '质量总分', defaultVisible: true },
  { key: 'dimensionVerdict', label: '四维', defaultVisible: true },
  { key: 'type', label: '类型', defaultVisible: true },
  { key: 'status', label: '状态', defaultVisible: true },
  { key: 'createdAt', label: '创建时间', defaultVisible: false },
  { key: 'completedAt', label: '完成时间', defaultVisible: false },
]

const DEFAULT_VISIBLE_KEYS = COLUMN_DEFS.filter(c => c.defaultVisible).map(c => c.key)

function loadVisibleColumns(): string[] {
  try {
    const raw = localStorage.getItem(VISIBLE_COLUMNS_KEY)
    if (!raw) return DEFAULT_VISIBLE_KEYS
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return DEFAULT_VISIBLE_KEYS
    // 过滤掉已废弃的 key，仅保留当前定义的列
    const valid = new Set(COLUMN_DEFS.map(c => c.key))
    const filtered = parsed.filter((k: unknown) => typeof k === 'string' && valid.has(k as string))
    return filtered.length > 0 ? filtered : DEFAULT_VISIBLE_KEYS
  } catch {
    return DEFAULT_VISIBLE_KEYS
  }
}

export function HistoryPage({ onSelectTask, onResumeTask, onSelectDetail }: Props) {
  const [tasks, setTasks] = useState<Task[]>([])
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [selectedTasks, setSelectedTasks] = useState<string[]>([])
  const [deleting, setDeleting] = useState(false)
  const [showModal, setShowModal] = useState(false)
  const [modalMessage, setModalMessage] = useState('')
  const [modalType, setModalType] = useState<'confirm' | 'success' | 'error'>('confirm')
  const [confirmAction, setConfirmAction] = useState<(() => Promise<void>) | null>(null)
  const [detailTaskId, setDetailTaskId] = useState<string | null>(null)
  const [crossScenarioIds, setCrossScenarioIds] = useState<string[] | null>(null)
  const [compareError, setCompareError] = useState<string | null>(null)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  const [fetching, setFetching] = useState(false)
  const abortControllerRef = useRef<AbortController | null>(null)

  // 表头筛选状态
  const [filters, setFilters] = useState({
    taskId: '',
    datasetName: '',
    agentName: '',
    agentVersion: '',
    inferenceModel: '',
    agentWorkspace: '',
    minTotalScore: '',
    evalType: '', // '' | 'online' | 'offline'
    status: '', // '' | 'pending' | 'processing' | 'completed' | 'failed' | 'paused' | 'cancelled'
  })

  // 防抖后的 filters，避免文本输入每次按键都发请求
  const [debouncedFilters, setDebouncedFilters] = useState(filters)
  useEffect(() => {
    const t = setTimeout(() => setDebouncedFilters(filters), 300)
    return () => clearTimeout(t)
  }, [filters])

  // 列配置状态（持久化到 localStorage）
  const [visibleColumns, setVisibleColumns] = useState<string[]>(loadVisibleColumns)

  useEffect(() => {
    try {
      localStorage.setItem(VISIBLE_COLUMNS_KEY, JSON.stringify(visibleColumns))
    } catch {
      // 忽略写入失败
    }
  }, [visibleColumns])

  const toggleColumn = (key: string) => {
    setVisibleColumns(prev =>
      prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key],
    )
  }

  const resetColumns = () => {
    setVisibleColumns(DEFAULT_VISIBLE_KEYS)
  }

  const hasActiveFilters = Object.values(filters).some(v => v !== '')

  const clearFilters = () => {
    setFilters({
      taskId: '', datasetName: '', agentName: '', agentVersion: '', inferenceModel: '',
      agentWorkspace: '', minTotalScore: '',
      evalType: '', status: '',
    })
  }

  // 按可配置列顺序得到当前展示的列 key
  const visibleColumnKeys = COLUMN_DEFS
    .filter(c => visibleColumns.includes(c.key))
    .map(c => c.key)

  useEffect(() => {
    fetchTasks()
  }, [page, debouncedFilters])

  const fetchTasks = async () => {
    // 取消上一个请求，避免慢请求覆盖新结果（如：质量总分过滤后重置，慢的过滤响应晚于重置响应到达）
    abortControllerRef.current?.abort()
    const controller = new AbortController()
    abortControllerRef.current = controller

    try {
      setHistoryError(null)
      setFetching(true)
      const params = new URLSearchParams({ page: String(page), limit: '20' })
      if (debouncedFilters.taskId) params.set('task_id', debouncedFilters.taskId)
      if (debouncedFilters.datasetName) params.set('dataset_name', debouncedFilters.datasetName)
      if (debouncedFilters.agentName) params.set('agent_name', debouncedFilters.agentName)
      if (debouncedFilters.agentVersion) params.set('agent_version', debouncedFilters.agentVersion)
      if (debouncedFilters.inferenceModel) params.set('inference_model', debouncedFilters.inferenceModel)
      if (debouncedFilters.agentWorkspace) params.set('agent_workspace', debouncedFilters.agentWorkspace)
      if (debouncedFilters.evalType) params.set('eval_type', debouncedFilters.evalType)
      if (debouncedFilters.status) params.set('status', debouncedFilters.status)
      if (debouncedFilters.minTotalScore) params.set('min_total_score', debouncedFilters.minTotalScore)
      const res = await fetch(`${API_BASE}/tasks?${params.toString()}`, { signal: controller.signal })
      if (!res.ok) {
        throw new Error(`接口返回 ${res.status}`)
      }
      const data = await res.json()
      const tasksArr = Array.isArray(data.tasks) ? data.tasks : []
      const totalNum = typeof data.total === 'number' ? data.total : 0
      // 当前页超出范围但有数据时，回到第 1 页
      if (tasksArr.length === 0 && totalNum > 0 && page > 1) {
        setPage(1)
        return
      }
      setTasks(tasksArr)
      setTotal(totalNum)
      // 重置选择状态
      setSelectedTasks([])
    } catch (err) {
      // 被取消的请求不算错误，不覆盖后续请求的结果
      if (err instanceof DOMException && err.name === 'AbortError') return
      console.error('获取任务列表失败:', err)
      setTasks([])
      setHistoryError(err instanceof Error ? err.message : '获取任务列表失败')
    } finally {
      // 仅当本次请求未被取消时才清除 loading 状态，避免新请求还在进行中就被置回 false
      if (!controller.signal.aborted) {
        setFetching(false)
        setLoading(false)
      }
    }
  }

  // 单个删除任务
  const handleDeleteTask = async (taskId: string) => {
    setModalMessage(`确定要删除任务 ${taskId} 吗？此操作不可恢复。`)
    setModalType('confirm')
    setConfirmAction(async () => {
      setDeleting(true)
      try {
        const res = await fetch(`${API_BASE}/tasks/${taskId}`, { method: 'DELETE' })
        if (res.ok) {
          setModalMessage('删除成功')
          setModalType('success')
          // 重新获取任务列表
          await fetchTasks()
        } else if (res.status === 404) {
          // 任务不存在，可能是已经被删除了，直接刷新列表
          setModalMessage('任务不存在或已被删除')
          setModalType('success')
          await fetchTasks()
        } else {
          const error = await res.json()
          setModalMessage(`删除失败: ${error.detail || '未知错误'}`)
          setModalType('error')
        }
      } catch (err) {
        console.error('删除任务失败:', err)
        setModalMessage('删除失败，请稍后重试')
        setModalType('error')
      } finally {
        setDeleting(false)
      }
    })
    setShowModal(true)
  }

  // 批量删除任务
  const handleBatchDelete = () => {
    setModalMessage(`确定要删除选中的 ${selectedTasks.length} 个任务吗？此操作不可恢复。`)
    setModalType('confirm')
    setConfirmAction(async () => {
      setDeleting(true)
      try {
        // 批量删除任务并检查每个请求结果
        const deleteResults = await Promise.all(
          selectedTasks.map(async (taskId) => {
            const res = await fetch(`${API_BASE}/tasks/${taskId}`, { method: 'DELETE' })
            return { taskId, success: res.ok, status: res.status }
          })
        )

        // 统计真正失败的任务（排除404）
        const failedTasks = deleteResults.filter(result => !result.success && result.status !== 404).map(result => result.taskId)
        
        if (failedTasks.length > 0) {
          throw new Error(`任务 ${failedTasks.join(', ')} 删除失败`)
        }

        // 计算实际删除成功的数量（包括404的情况）
        const successCount = deleteResults.filter(result => result.success || result.status === 404).length
        setModalMessage(`成功删除 ${successCount} 个任务`)
        setModalType('success')
        // 重新获取任务列表
        await fetchTasks()
      } catch (err) {
        console.error('批量删除任务失败:', err)
        setModalMessage(err instanceof Error ? err.message : '批量删除失败，请稍后重试')
        setModalType('error')
      } finally {
        setDeleting(false)
      }
    })
    setShowModal(true)
  }

  // 切换任务选择状态
  const toggleTaskSelection = (taskId: string) => {
    setCompareError(null)
    setSelectedTasks(prev => {
      if (prev.includes(taskId)) {
        return prev.filter(id => id !== taskId)
      } else {
        return [...prev, taskId]
      }
    })
  }

  // 全选/取消全选（基于当前页任务）
  const toggleSelectAll = () => {
    setCompareError(null)
    const allSelected =
      tasks.length > 0 &&
      tasks.every(t => selectedTasks.includes(t.id))
    if (allSelected) {
      // 取消选中当前页任务
      setSelectedTasks(prev =>
        prev.filter(id => !tasks.some(t => t.id === id)),
      )
    } else {
      // 选中当前页所有任务
      setSelectedTasks(prev => [
        ...new Set([...prev, ...tasks.map(t => t.id)]),
      ])
    }
  }

  // 导出评测报告（归档到"评测报告"菜单并触发下载）
  const handleExportReport = async () => {
    if (selectedTasks.length === 0) return
    setExporting(true)
    try {
      // 1. 生成并归档
      const archiveRes = await fetch(`${API_BASE}/reports/evaluation`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_ids: selectedTasks }),
      })
      if (!archiveRes.ok) {
        throw new Error(`归档失败 (${archiveRes.status})`)
      }
      const report = await archiveRes.json()
      // 2. 下载归档文件
      const dlRes = await fetch(`${API_BASE}/reports/${report.id}/download`)
      if (!dlRes.ok) {
        throw new Error(`下载失败 (${dlRes.status})`)
      }
      const blob = await dlRes.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${report.title || '评测报告'}.xlsx`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      setModalMessage('评测报告已导出并归档，可在"评测报告"菜单查看')
      setModalType('success')
      setShowModal(true)
    } catch (err) {
      console.error('导出评测报告失败:', err)
      setModalMessage(err instanceof Error ? err.message : '导出失败，请稍后重试')
      setModalType('error')
      setShowModal(true)
    } finally {
      setExporting(false)
    }
  }

  // 恢复暂停的任务
  const handleResumeTask = async (taskId: string) => {
    setModalMessage(`确定要恢复任务 ${taskId} 吗？`)
    setModalType('confirm')
    setConfirmAction(async () => {
      setDeleting(true)
      try {
        const res = await fetch(`${API_BASE}/tasks/${taskId}/resume`, { method: 'POST' })
        if (res.ok) {
          setModalMessage('恢复成功')
          setModalType('success')
          // 重新获取任务列表
          await fetchTasks()
          // 调用回调函数跳转到监控页面
          if (onResumeTask) {
            onResumeTask(taskId)
          }
        } else {
          const error = await res.json()
          setModalMessage(`恢复失败: ${error.detail || '未知错误'}`)
          setModalType('error')
        }
      } catch (err) {
        console.error('恢复任务失败:', err)
        setModalMessage('恢复失败，请稍后重试')
        setModalType('error')
      } finally {
        setDeleting(false)
      }
    })
    setShowModal(true)
  }

  // 使用 dateUtils 中的 formatDate 函数

  const statusMap: Record<string, string> = {
    'pending': '等待中',
    'processing': '处理中',
    'completed': '已完成',
    'failed': '失败',
    'paused': '已暂停',
    'cancelled': '已取消'
  }

  if (loading) {
    return <div className="card">加载中...</div>
  }

  const handleModalConfirm = async () => {
    if (confirmAction) {
      await confirmAction()
    }
  }

  const handleModalClose = () => {
    setShowModal(false)
    setConfirmAction(null)
  }

  const selectedTaskRows = tasks.filter(t => selectedTasks.includes(t.id))
  const compareEligible = selectedTaskRows.filter(
    t => t.status === 'completed' || t.status === 'cancelled',
  )
  const compareDatasetIds = new Set(
    compareEligible.map(t => t.dataset_id).filter(Boolean) as string[],
  )
  const canCrossScenarioCompare =
    compareEligible.length >= 2 &&
    compareEligible.length <= 6 &&
    compareDatasetIds.size === 1

  const handleCrossScenarioCompare = () => {
    setCompareError(null)
    if (compareEligible.length < 2) {
      setCompareError('请至少选择 2 个已完成或已取消的评估任务')
      return
    }
    if (compareEligible.length > 6) {
      setCompareError('单次最多对比 6 个任务，请减少选择')
      return
    }
    if (compareEligible.length !== selectedTasks.length) {
      setCompareError('仅已完成/已取消的任务可参与对比，已自动排除进行中任务')
    }
    if (compareDatasetIds.size > 1) {
      setCompareError('场景/版本对比要求同一评测集，请只选择相同评测集下的任务')
      return
    }
    if (compareDatasetIds.size === 0) {
      setCompareError('所选任务缺少评测集信息，无法对比')
      return
    }
    setCrossScenarioIds(compareEligible.map(t => t.id))
  }

  // 单元格渲染：按列 key 返回内容
  const renderCell = (key: string, task: Task) => {
    const scores = historyTaskScores(task)
    switch (key) {
      case 'taskId':
        return (
          <td key={key} className="cell-task-id" title={task.id}>
            <code className="cell-task-id__code">{task.id}</code>
          </td>
        )
      case 'datasetName':
        return (
          <td key={key} className="cell-ellipsis" title={task.dataset_name || task.dataset_id}>
            {task.dataset_name || task.dataset_id || '—'}
          </td>
        )
      case 'agentName':
        return (
          <td key={key} className="cell-ellipsis" title={task.agent_name || undefined}>
            {task.agent_name || '—'}
          </td>
        )
      case 'agentVersion':
        return (
          <td key={key} className="cell-ellipsis" title={task.agent_versions_label || undefined}>
            {task.agent_count && task.agent_count > 1 ? (
              <span title={task.agent_versions_label || ''}>
                {task.agent_versions_label || `${task.agent_count} 版`}
              </span>
            ) : (
              task.agent_versions_label || '—'
            )}
          </td>
        )
      case 'inferenceModel':
        return (
          <td
            key={key}
            className="cell-ellipsis"
            title={
              isOfflineEvaluationTask(task)
                ? OFFLINE_NA_TOOLTIP
                : task.inference_model_summary?.labels?.join('\n') || undefined
            }
            style={isOfflineEvaluationTask(task) ? offlineNaTextStyle : undefined}
          >
            {isOfflineEvaluationTask(task)
              ? OFFLINE_NA_LABEL
              : task.inference_model_summary?.display || '未记录'}
          </td>
        )
      case 'agentWorkspace':
        return (
          <td key={key} className="cell-ellipsis" title={task.agent_workspace_name || task.agent_project_name || undefined}>
            {task.agent_workspace_name || task.agent_project_name || '—'}
          </td>
        )
      case 'agentRunCount':
        return (
          <td key={key}>{task.agent_run_count != null ? task.agent_run_count : '—'}</td>
        )
      case 'thinkingMode': {
        const mode = task.execution_mode_summary?.display || '未记录'
        const isThinking = task.execution_mode_summary?.deep_thinking_enabled === true
          || task.execution_mode_summary?.labels?.includes('深度思考')
        return (
          <td key={key}>
            <span className={`thinking-mode-badge${isThinking ? ' is-thinking' : ''}`}>{mode}</span>
          </td>
        )
      }
      case 'evalModel':
        return (
          <td key={key} className="cell-ellipsis" title={task.llm_model || undefined}>
            {task.llm_model || '—'}
          </td>
        )
      case 'totalScore':
        return <td key={key} style={{ fontWeight: 600 }}>{formatScore(scores.total)}</td>
      case 'dimensionVerdict':
        return (
          <td key={key}>
            <DimensionVerdictBadgesCell
              badges={task.dimension_verdict_badges}
              onNavigate={() => {
                if (onSelectDetail) {
                  onSelectDetail(task.id)
                } else {
                  setDetailTaskId(task.id)
                }
              }}
            />
          </td>
        )
      case 'type':
        return (
          <td key={key}>
            <span className={`eval-type-badge ${task.agent_id ? 'online' : 'offline'}`}>
              {task.agent_id ? '在线评估' : '离线评估'}
            </span>
          </td>
        )
      case 'status':
        return (
          <td key={key}>
            <span className={`status-badge status-${task.status}`}>
              {statusMap[task.status] || task.status.toUpperCase()}
            </span>
          </td>
        )
      case 'createdAt':
        return (
          <td key={key} className="cell-datetime" title={formatDate(task.created_at)}>
            {formatDateShort(task.created_at)}
          </td>
        )
      case 'completedAt':
        return (
          <td key={key} className="cell-datetime" title={task.completed_at ? formatDate(task.completed_at) : undefined}>
            {task.completed_at ? formatDateShort(task.completed_at) : '-'}
          </td>
        )
      default:
        return <td key={key}>—</td>
    }
  }

  // 列对应的 colgroup 宽度类名
  const colClassMap: Record<string, string> = {
    taskId: 'history-col-task-id',
    datasetName: 'history-col-dataset',
    agentName: 'history-col-agent',
    agentVersion: 'history-col-version',
    inferenceModel: 'history-col-model',
    agentWorkspace: 'history-col-workspace',
    agentRunCount: 'history-col-runcount',
    thinkingMode: 'history-col-thinking',
    evalModel: 'history-col-evalmodel',
    totalScore: 'history-col-score',
    dimensionVerdict: 'history-col-verdict',
    type: 'history-col-type',
    status: 'history-col-status',
    createdAt: 'history-col-time',
    completedAt: 'history-col-time',
  }

  if (crossScenarioIds && crossScenarioIds.length >= 2) {
    return (
      <div className="card history-card">
        <PageHeader
          className="history-page-header"
          title="场景/版本对比（同评测集多任务）"
          description={`已选 ${crossScenarioIds.length} 个任务，按评测题对齐展示场景子步骤（S1–S8）得分矩阵，并附逐题运行效能与多轮稳定性跨版本对比。`}
          actions={
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => {
                setCrossScenarioIds(null)
                setCompareError(null)
                setSelectedTasks([])
              }}
            >
              ← 返回评估历史
            </button>
          }
        />
        <CrossTaskScenarioMatrix
          source={{
            taskIds: crossScenarioIds,
            baselineTaskId: crossScenarioIds[0],
          }}
        />
      </div>
    )
  }

  return (
    <div>
      {/* Custom Modal */}
      {showModal && (
        <div className="modal-overlay">
          <div className="modal-content">
            <h3 style={{ color: modalType === 'success' ? 'var(--edp-green)' : modalType === 'error' ? 'var(--edp-red)' : 'var(--edp-primary)' }}>
              {modalType === 'success' ? (
                <>
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                    <polyline points="22 4 12 14.01 9 11.01"/>
                  </svg>
                  成功
                </>
              ) : modalType === 'error' ? (
                <>
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <circle cx="12" cy="12" r="10"/>
                    <line x1="15" y1="9" x2="9" y2="15"/>
                    <line x1="9" y1="9" x2="15" y2="15"/>
                  </svg>
                  失败
                </>
              ) : (
                <>
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--edp-amber)" strokeWidth="2">
                    <circle cx="12" cy="12" r="10"/>
                    <line x1="12" y1="8" x2="12" y2="12"/>
                    <line x1="12" y1="16" x2="12.01" y2="16"/>
                  </svg>
                  确认
                </>
              )}
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
      
      <div className="card history-card">
        <PageHeader
          title="评估历史"
          description={
            historyError ? (
              <span className="page-desc--error">
                评估历史加载失败：{historyError}。请确认后端服务已启动，或刷新页面重试。
              </span>
            ) : (
              '查看任务进度与评估结果，支持批量删除、场景/版本对比。'
            )
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
              <path d="M12 8v4l3 3m6-3a9 9 0 1 1-18 0 9 9 0 0 1 18 0z"/>
              <path d="M4 4v5h.582m15.356 2A8.001 8.001 0 0 0 4.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 0 1-15.357-2m15.357 2H15"/>
            </svg>
          }
        />
      
      {tasks.length === 0 ? (
        <p className="ucd-muted">暂无评估任务</p>
      ) : (
        <>
          {/* 批量操作按钮 */}
          <div className="history-toolbar page-toolbar">
            <div className="history-toolbar__actions">
            <button
              className="edp-btn-export"
              onClick={handleCrossScenarioCompare}
              disabled={deleting || exporting || !canCrossScenarioCompare}
              title="选择 2～6 个同评测集已完成任务，按场景子步骤对比"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d="M17 1l4 4-4 4"/>
                <path d="M3 11V9a4 4 0 0 1 4-4h14"/>
                <path d="M7 23l-4-4 4-4"/>
                <path d="M21 13v2a4 4 0 0 1-4 4H3"/>
              </svg>
              场景/版本对比 ({compareEligible.length})
            </button>
            <button
              className="edp-btn-export"
              onClick={handleExportReport}
              disabled={exporting || deleting || selectedTasks.length === 0}
              title="导出选中任务的评测结果为 xlsx 报告"
            >
              {exporting ? (
                <>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite' }}>
                    <circle cx="12" cy="12" r="10"/>
                    <path d="M12 6v6l4 2"/>
                  </svg>
                  导出中...
                </>
              ) : (
                <>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                    <polyline points="7 10 12 15 17 10"/>
                    <line x1="12" y1="15" x2="12" y2="3"/>
                  </svg>
                  导出评测报告 ({selectedTasks.length})
                </>
              )}
            </button>
            <span className="history-toolbar__divider" />
            <ColumnConfig
              columns={COLUMN_DEFS}
              visibleKeys={visibleColumnKeys}
              onToggle={toggleColumn}
              onReset={resetColumns}
            />
            <button
              className="edp-btn edp-btn-ghost"
              onClick={() => setSelectedTasks([])}
              disabled={deleting || exporting || selectedTasks.length === 0}
              title="清空当前选中"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d="M3 6h18"/>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>
                <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              </svg>
              清空已选
            </button>
            <button
              className="edp-btn edp-btn-danger"
              onClick={handleBatchDelete}
              disabled={deleting || exporting || selectedTasks.length === 0}
              title="删除选中任务，不可恢复"
            >
              {deleting ? (
                <>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite' }}>
                    <circle cx="12" cy="12" r="10"/>
                    <path d="M12 6v6l4 2"/>
                  </svg>
                  删除中...
                </>
              ) : (
                <>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                    <polyline points="3 6 5 6 21 6"/>
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                  </svg>
                  批量删除 ({selectedTasks.length})
                </>
              )}
            </button>
            </div>
            <div className="history-toolbar__meta">
            <span>
              已选择 {selectedTasks.length} 项
              {hasActiveFilters && (
                <span className="history-filter-count">
                  {' '}/ 共 {total} 项
                </span>
              )}
            </span>
            {fetching && (
              <span className="history-fetching-indicator">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'edp-spin 0.8s linear infinite' }}>
                  <circle cx="12" cy="12" r="10" strokeOpacity="0.25" />
                  <path d="M12 6v6l4 2" />
                </svg>
                更新中...
              </span>
            )}
            {hasActiveFilters && (
              <button
                type="button"
                className="edp-btn edp-btn-ghost history-clear-filters"
                onClick={clearFilters}
                title="清除所有筛选条件"
              >
                清除筛选
              </button>
            )}
            {compareError && (
              <span className="history-toolbar__error">{compareError}</span>
            )}
            </div>
          </div>
          
          <div className="history-table-wrap">
          <table className="results-table history-table">
            <colgroup>
              <col className="history-col-check" />
              {visibleColumnKeys.map(key => (
                <col key={key} className={colClassMap[key] || ''} />
              ))}
              <col className="history-col-actions" />
            </colgroup>
            <thead>
              <tr>
                <th>
                  <input
                    type="checkbox"
                    checked={tasks.length > 0 && tasks.every(t => selectedTasks.includes(t.id))}
                    onChange={toggleSelectAll}
                    disabled={deleting}
                  />
                </th>
                {visibleColumnKeys.includes('taskId') && (
                  <th>
                    <span className="th-filter">
                      <span className="th-filter__label">评测任务Id</span>
                      <ColumnFilter type="text" label="评测任务Id" value={filters.taskId} onChange={v => setFilters(f => ({ ...f, taskId: v }))} />
                    </span>
                  </th>
                )}
                {visibleColumnKeys.includes('datasetName') && (
                  <th>
                    <span className="th-filter">
                      <span className="th-filter__label">评测集名称</span>
                      <ColumnFilter type="text" label="评测集名称" value={filters.datasetName} onChange={v => setFilters(f => ({ ...f, datasetName: v }))} />
                    </span>
                  </th>
                )}
                {visibleColumnKeys.includes('agentName') && (
                  <th>
                    <span className="th-filter">
                      <span className="th-filter__label">Agent 名称</span>
                      <ColumnFilter type="text" label="Agent 名称" value={filters.agentName} onChange={v => setFilters(f => ({ ...f, agentName: v }))} />
                    </span>
                  </th>
                )}
                {visibleColumnKeys.includes('agentVersion') && (
                  <th>
                    <span className="th-filter">
                      <span className="th-filter__label">Agent版本</span>
                      <ColumnFilter type="text" label="Agent版本" value={filters.agentVersion} onChange={v => setFilters(f => ({ ...f, agentVersion: v }))} />
                    </span>
                  </th>
                )}
                {visibleColumnKeys.includes('inferenceModel') && (
                  <th>
                    <span className="th-filter">
                      <span className="th-filter__label">被测推理模型</span>
                      <ColumnFilter type="text" label="被测推理模型" value={filters.inferenceModel} onChange={v => setFilters(f => ({ ...f, inferenceModel: v }))} />
                    </span>
                  </th>
                )}
                {visibleColumnKeys.includes('agentWorkspace') && (
                  <th>
                    <span className="th-filter">
                      <span className="th-filter__label">Agent 工作空间</span>
                      <ColumnFilter type="text" label="Agent 工作空间" value={filters.agentWorkspace} onChange={v => setFilters(f => ({ ...f, agentWorkspace: v }))} />
                    </span>
                  </th>
                )}
                {visibleColumnKeys.includes('agentRunCount') && (
                  <th><span className="th-filter"><span className="th-filter__label">Agent 运行次数</span></span></th>
                )}
                {visibleColumnKeys.includes('thinkingMode') && (
                  <th><span className="th-filter"><span className="th-filter__label">Thinking 模式</span></span></th>
                )}
                {visibleColumnKeys.includes('evalModel') && (
                  <th><span className="th-filter"><span className="th-filter__label">评估模型</span></span></th>
                )}
                {visibleColumnKeys.includes('totalScore') && (
                  <th
                    title="= 结果分 × 权重 + 过程分 × 权重；稳定性差会触发封顶；场景交付为独立维度，不计入总分"
                  >
                    <span className="th-filter">
                      <span className="th-filter__label">质量总分</span>
                      <ColumnFilter type="number-min" label="质量总分" min={0} max={1} step={0.01} value={filters.minTotalScore} onChange={v => setFilters(f => ({ ...f, minTotalScore: v }))} />
                    </span>
                  </th>
                )}
                {visibleColumnKeys.includes('dimensionVerdict') && (
                  <th
                    title="四维评估结论：质量指标 / 过程一致性 / 运行稳定性 / 场景交付。hover 徽章看维度状态，点击跳详情对应 Tab"
                  >
                    <span className="th-filter">
                      <span className="th-filter__label">四维</span>
                    </span>
                  </th>
                )}
                {visibleColumnKeys.includes('type') && (
                  <th>
                    <span className="th-filter">
                      <span className="th-filter__label">类型</span>
                      <ColumnFilter type="enum" label="类型" value={filters.evalType} onChange={v => setFilters(f => ({ ...f, evalType: v }))} options={[{ label: '在线评估', value: 'online' }, { label: '离线评估', value: 'offline' }]} />
                    </span>
                  </th>
                )}
                {visibleColumnKeys.includes('status') && (
                  <th>
                    <span className="th-filter">
                      <span className="th-filter__label">状态</span>
                      <ColumnFilter type="enum" label="状态" value={filters.status} onChange={v => setFilters(f => ({ ...f, status: v }))} options={[{ label: '等待中', value: 'pending' }, { label: '处理中', value: 'processing' }, { label: '已完成', value: 'completed' }, { label: '失败', value: 'failed' }, { label: '已暂停', value: 'paused' }, { label: '已取消', value: 'cancelled' }]} />
                    </span>
                  </th>
                )}
                {visibleColumnKeys.includes('createdAt') && <th>创建时间</th>}
                {visibleColumnKeys.includes('completedAt') && <th>完成时间</th>}
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {tasks.length === 0 ? (
                <tr>
                  <td colSpan={visibleColumnKeys.length + 2} style={{ textAlign: 'center', padding: '24px', color: 'var(--edp-text3)' }}>
                    {hasActiveFilters ? '当前筛选条件下无匹配记录' : '暂无评估任务'}
                  </td>
                </tr>
              ) : tasks.map(task => {
                return (
                <tr key={task.id}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selectedTasks.includes(task.id)}
                      onChange={() => toggleTaskSelection(task.id)}
                      disabled={deleting}
                    />
                  </td>
                  {visibleColumnKeys.map(key => renderCell(key, task))}
                  <td className="cell-actions">
                    <div className="history-row-actions">
                      <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        onClick={() => onSelectTask(task.id)}
                        disabled={deleting}
                        title="查看结果"
                      >
                        结果
                      </button>
                      {(task.status === 'completed' ||
                        task.status === 'cancelled' ||
                        task.status === 'processing' ||
                        task.status === 'paused' ||
                        task.status === 'pending') && (
                        <button
                          type="button"
                          className="btn btn-sm history-btn-detail"
                          onClick={() => {
                            if (onSelectDetail) {
                              onSelectDetail(task.id)
                            } else {
                              setDetailTaskId(task.id)
                            }
                          }}
                          disabled={deleting}
                          title={
                            task.status === 'completed' || task.status === 'cancelled'
                              ? '查看详情'
                              : '查看已完成部分（进行中）'
                          }
                        >
                          详情
                        </button>
                      )}
                      {task.status === 'paused' && (
                        <button
                          type="button"
                          className="btn btn-success btn-sm"
                          onClick={() => handleResumeTask(task.id)}
                          disabled={deleting}
                          title="恢复任务"
                        >
                          恢复
                        </button>
                      )}
                      <button
                        type="button"
                        className="btn btn-danger btn-sm history-btn-icon"
                        onClick={() => handleDeleteTask(task.id)}
                        disabled={deleting}
                        title="删除"
                        aria-label="删除"
                      >
                        {deleting ? (
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite' }}>
                            <circle cx="12" cy="12" r="10"/>
                            <path d="M12 6v6l4 2"/>
                          </svg>
                        ) : (
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                            <polyline points="3 6 5 6 21 6"/>
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                          </svg>
                        )}
                      </button>
                    </div>
                  </td>
                </tr>
              )})}
              </tbody>
            </table>
          </div>
          
          <div className="history-pagination">
            <button
              className="btn"
              onClick={() => setPage(p => Math.max(1, p - 1))}
              disabled={page === 1 || deleting}
            >
              ← 上一页
            </button>
            <span className="history-pagination__label">第 {page} 页 / 共 {Math.max(1, Math.ceil(total / 20))} 页（{total} 项）</span>
            <button
              className="btn"
              onClick={() => setPage(p => p + 1)}
              disabled={page * 20 >= total || deleting}
            >
              下一页 →
            </button>
          </div>
        </>
      )}
    </div>
    
    {/* Detail Modal */}
    {detailTaskId && (
      <EvalDetailModal
        taskId={detailTaskId}
        onClose={() => setDetailTaskId(null)}
        isPartial={
          !['completed', 'cancelled', 'failed'].includes(
            tasks.find(t => t.id === detailTaskId)?.status || '',
          )
        }
      />
    )}
    </div>
  )
}
