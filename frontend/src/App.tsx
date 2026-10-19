import { useState, useEffect, useCallback, useRef } from 'react'
import axios from 'axios'
import { DatasetManager } from './components/DatasetManager'
import { EvalPage } from './components/EvalPage'
import { TaskMonitor, CotLine } from './components/TaskMonitor'
import { ResultsPage } from './components/ResultsPage'
import { HistoryPage } from './components/HistoryPage'
import { EvalDetailPage } from './components/EvalDetailPage'
import { AgentManagePage } from './components/AgentManagePage'
import { ProcessCheckpointPage } from './components/ProcessCheckpointPage'
import { TraceMonitorPage } from './components/TraceMonitorPage'
import { LangfuseTraceExplore } from './components/LangfuseTraceExplore'
import { SystemSettingsPage } from './components/SystemSettingsPage'
import { OntologyPackagePage } from './components/OntologyPackagePage'
import { ReportArchivePage } from './components/ReportArchivePage'
import { HelpPage } from './components/HelpPage'
import { GuidePage } from './components/GuidePage'
import { CustomMetricManager } from './components/CustomMetricManager'
import { ChatPage } from './components/ChatPage'
import { ErrorBoundary } from './components/ErrorBoundary'
import { openDetailWindow } from './utils/detailWindow'
import type { DimensionTabId } from './types/dimensionVerdict'
import type { PerfDeepLink } from './components/RuntimePerformanceReport'

const API_BASE = '/api'

type TabType =
  | 'home'
  | 'datasets'
  | 'offline_eval'
  | 'online_eval'
  | 'results'
  | 'history'
  | 'detail'
  | 'reports'
  | 'agents'
  | 'checkpoints'
  | 'trace'
  | 'settings'
  | 'langfuse_projects'
  | 'custom_metrics'
  | 'help'
  | 'guide'
  | 'chat'

/**
 * 只有 results / detail 走多标签模型（可并存、可关闭）。
 * 其余页面（home/datasets/agents/history/...）为「单例页」，不进入 tab 栏。
 */
interface TabInstance {
  id: string
  type: 'results' | 'detail'
  taskId?: string
  title: string
  /** tooltip 全称（含完整 taskId） */
  tooltip: string
  closable: boolean
  /** 详情 tab 携带的初始主视图（从结果页图表下钻时使用） */
  initialMainTab?: DimensionTabId
  /** 详情 tab 携带的初始 perf 深链 */
  initialPerfDeepLink?: PerfDeepLink | null
}

function shortId(id?: string): string {
  if (!id) return ''
  return id.length > 8 ? id.slice(0, 8) : id
}

interface Dataset {
  id: string
  item_count: number
  created_at: string
}

// 任务结果包装组件 - 根据任务状态显示监控或结果
interface TaskResultsWrapperProps {
  taskId: string
  cotLines: CotLine[]
  setCotLines: React.Dispatch<React.SetStateAction<CotLine[]>>
  onOpenDetail?: (
    taskId: string,
    initialMainTab?: DimensionTabId,
    initialPerfDeepLink?: PerfDeepLink | null,
  ) => void
}

function TaskResultsWrapper({ taskId, cotLines, setCotLines, onOpenDetail }: TaskResultsWrapperProps) {
  const [taskStatus, setTaskStatus] = useState<string>('pending')
  const [loading, setLoading] = useState(true)
  const [showPartialResults, setShowPartialResults] = useState(false)

  // 使用 useCallback 固定 onComplete 引用，避免 cotLines 变化导致 TaskMonitor
  // 的 SSE useEffect 重新执行（重连），从而丢失 task_complete 事件。
  const handleComplete = useCallback(() => setTaskStatus('completed'), [])

  useEffect(() => {
    const checkTaskStatus = async () => {
      try {
        const res = await fetch(`${API_BASE}/result/${taskId}`)
        const data = await res.json()
        setTaskStatus(data.status)
      } catch (err) {
        console.error('获取任务状态失败:', err)
      } finally {
        setLoading(false)
      }
    }

    checkTaskStatus()
    // 仅任务进行中才轮询，完成/失败/取消后停止
    const isRunning =
      taskStatus === 'pending' ||
      taskStatus === 'processing' ||
      taskStatus === 'paused'
    if (isRunning) {
      const interval = setInterval(checkTaskStatus, 3000)
      return () => clearInterval(interval)
    }
  }, [taskId, taskStatus])

  if (loading) {
    return <div className="card">加载中...</div>
  }

  if (showPartialResults && (taskStatus === 'pending' || taskStatus === 'processing' || taskStatus === 'paused')) {
    return (
      <div>
        <div className="card" style={{ marginBottom: 16, display: 'flex', justifyContent: 'flex-end' }}>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => setShowPartialResults(false)}>
            返回任务监控
          </button>
        </div>
        <ResultsPage taskId={taskId} partialMode onOpenDetail={onOpenDetail} />
      </div>
    )
  }

  // 如果任务还在进行中（pending, processing, paused），显示任务监控
  if (taskStatus === 'pending' || taskStatus === 'processing' || taskStatus === 'paused') {
    return (
      <TaskMonitor
        taskId={taskId}
        onComplete={handleComplete}
        cotLines={cotLines}
        setCotLines={setCotLines}
        onViewPartialResults={() => setShowPartialResults(true)}
      />
    )
  }

  // 任务已完成或失败，显示结果页面
  return <ResultsPage taskId={taskId} onOpenDetail={onOpenDetail} />
}

function App() {
  // 当URL中有task参数时，使用独立监控模式（不显示侧边栏）
  const isMonitorMode = new URLSearchParams(window.location.search).has('task')
  // 评估详情独立窗口模式：URL 带 detail=<taskId> 时全屏渲染（无侧边栏/tab栏），同 demo 效果
  const detailTaskId = new URLSearchParams(window.location.search).get('detail')
  const isDetailMode = !!detailTaskId
  // 评估详情独立窗口：URL ?detail=<taskId>&section=<tabId> 指定初始展开的左树维度
  const detailSection = new URLSearchParams(window.location.search).get('section') as DimensionTabId | null
  // 深链定位：?qid=<> & ?metric=<> & ?step=<> 用于在详情页定位到具体题目/指标/子步骤
  const detailQid = new URLSearchParams(window.location.search).get('qid')
  const detailMetric = new URLSearchParams(window.location.search).get('metric')
  const detailStep = new URLSearchParams(window.location.search).get('step')
  // URL ?page=<type> 指定初始单例页（从详情页返回评估历史等场景）
  const pageParam = new URLSearchParams(window.location.search).get('page') as TabType | null

  // 多标签：仅 history / results:<id> / detail:<id>
  const [tabs, setTabs] = useState<TabInstance[]>([])
  // 当前活动的 tab id；为 null 时表示正在查看单例页
  const [activeTabId, setActiveTabId] = useState<string | null>(null)
  // 单例页（不进入 tab 栏）：home/datasets/agents/...
  const [singletonPage, setSingletonPage] = useState<TabType | null>(isMonitorMode ? null : (pageParam ?? 'home'))

  const [currentTaskId, setCurrentTaskId] = useState<string | null>(null)
  const [currentDatasetId, setCurrentDatasetId] = useState<string | null>(null)
  const [currentAgentId, setCurrentAgentId] = useState<string | null>(null)
  // AI 对话配置产出的草稿，回填到 EvalPage 表单
  const [chatDraft, setChatDraft] = useState<Record<string, any> | null>(null)
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [loadingDatasets, setLoadingDatasets] = useState(false)
  // 思维链状态提升到 App 层，使用 localStorage 持久化
  const [cotLines, setCotLines] = useState<CotLine[]>([]);
  // 下拉菜单展开状态
  const [resourceMenuOpen, setResourceMenuOpen] = useState(false);
  const [evalMenuOpen, setEvalMenuOpen] = useState(false);
  const [monitorMenuOpen, setMonitorMenuOpen] = useState(false);
  const [reportMenuOpen, setReportMenuOpen] = useState(true);
  const [traceSubView, setTraceSubView] = useState<'langfuse' | 'otel'>('langfuse');
  // 评测管理页内子 tab：评测集管理 / 过程点配置 / 本体包管理
  const [evalManageSubTab, setEvalManageSubTab] = useState<'datasets' | 'checkpoints' | 'ontology'>('datasets');
  // 侧边栏折叠状态（localStorage 持久化）
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(
    () => localStorage.getItem('sidebar_collapsed') === '1',
  );
  // 折叠态下点击父级图标弹出的二级菜单（sticky），与展开态的 *MenuOpen 独立
  // 参考 Trae CN：折叠态单击图标弹出对应功能面板，移开/选中/点外即收起
  const [stickyFlyout, setStickyFlyout] = useState<'resource' | 'eval' | 'report' | 'monitor' | null>(null)
  const navRef = useRef<HTMLDivElement>(null)

  /** 折叠态切换 sticky flyout：再次点击同一项则收起，点击另一项则切换 */
  const toggleStickyFlyout = useCallback((menu: 'resource' | 'eval' | 'report' | 'monitor') => {
    setStickyFlyout(prev => (prev === menu ? null : menu))
  }, [])

  const activeTab: TabInstance | null = tabs.find(t => t.id === activeTabId) || null
  // 当前活动视图类型（用于侧边栏高亮）：tab 优先，否则单例页
  const activeType: TabType | null = activeTab ? activeTab.type : singletonPage

  /** 打开或激活一个任务 tab（同 id 已存在则更新 deep-link 并激活） */
  const openTab = (tab: TabInstance) => {
    setTabs(prev => {
      const exists = prev.find(t => t.id === tab.id)
      if (exists) {
        return prev.map(t =>
          t.id === tab.id
            ? { ...t, ...tab, type: tab.type }
            : t,
        )
      }
      return [...prev, tab]
    })
    setActiveTabId(tab.id)
    setSingletonPage(null)
    setStickyFlyout(null)
    if (tab.taskId) {
      setCurrentTaskId(tab.taskId)
    }
  }

  /** 打开单例页（不进入 tab 栏） */
  const openSingletonPage = (type: TabType) => {
    setSingletonPage(type)
    setActiveTabId(null)
    setStickyFlyout(null)
  }

  /** 关闭一个 tab；若关闭的是活动 tab，激活邻居或回退到首页单例 */
  const closeTab = (id: string) => {
    setTabs(prev => {
      const idx = prev.findIndex(t => t.id === id)
      if (idx === -1) return prev
      const next = prev.filter(t => t.id !== id)
      if (activeTabId === id) {
        const neighbor = next[Math.min(idx, next.length - 1)]
        if (neighbor) {
          setActiveTabId(neighbor.id)
          if (neighbor.taskId) setCurrentTaskId(neighbor.taskId)
        } else {
          // 没有其它 tab，回退到首页
          setActiveTabId(null)
          setSingletonPage('home')
        }
      }
      return next
    })
  }

  /** 打开「评估结果」tab（按 taskId 复用或新建） */
  const openResultsTab = (taskId: string) => {
    openTab({
      id: `results:${taskId}`,
      type: 'results',
      taskId,
      title: `评估结果 · ${shortId(taskId)}`,
      tooltip: `评估结果 · ${taskId}`,
      closable: true,
    })
  }

  /** 打开「评估详情」tab（按 taskId 复用或新建，可携带初始下链） */
  const openDetailTab = (
    taskId: string,
    initialMainTab?: DimensionTabId,
    initialPerfDeepLink?: PerfDeepLink | null,
  ) => {
    openTab({
      id: `detail:${taskId}`,
      type: 'detail',
      taskId,
      title: `评估详情 · ${shortId(taskId)}`,
      tooltip: `评估详情 · ${taskId}`,
      closable: true,
      initialMainTab,
      initialPerfDeepLink,
    })
  }

  // 当 currentTaskId 变化时，从 localStorage 加载对应的思维链记录
  useEffect(() => {
    if (currentTaskId) {
      const saved = localStorage.getItem(`cotLines_${currentTaskId}`);
      setCotLines(saved ? JSON.parse(saved) : []);
    } else {
      setCotLines([]);
    }
  }, [currentTaskId]);

  // 每次 cotLines 变化时，保存到 localStorage
  useEffect(() => {
    if (currentTaskId) {
      localStorage.setItem(`cotLines_${currentTaskId}`, JSON.stringify(cotLines));
    }
  }, [cotLines, currentTaskId]);

  // 侧边栏折叠状态持久化
  useEffect(() => {
    localStorage.setItem('sidebar_collapsed', sidebarCollapsed ? '1' : '0');
  }, [sidebarCollapsed]);

  // 折叠态 sticky flyout 点击外部收起
  useEffect(() => {
    if (!sidebarCollapsed) return
    const handler = (e: MouseEvent) => {
      if (navRef.current && !navRef.current.contains(e.target as Node)) {
        setStickyFlyout(null)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [sidebarCollapsed])

  // 展开侧边栏时清空 sticky（避免残留 class 干扰）
  useEffect(() => {
    if (!sidebarCollapsed) setStickyFlyout(null)
  }, [sidebarCollapsed])

  // 获取数据集列表
  const fetchDatasets = async () => {
    setLoadingDatasets(true)
    try {
      const res = await axios.get(`${API_BASE}/datasets`)
      setDatasets(res.data.datasets)
    } catch (err) {
      console.error('Failed to fetch datasets:', err)
    } finally {
      setLoadingDatasets(false)
    }
  }

  // 组件挂载时获取数据集列表
  useEffect(() => {
    fetchDatasets()
  }, [])

  // 从 URL 参数读取 task_id，支持 client.py 自动打开浏览器后同步显示思维链
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const taskFromUrl = params.get('task')
    if (taskFromUrl) {
      setCurrentTaskId(taskFromUrl)
      setCotLines([])
      // 监控页面模式，不改变 activeTab
      // 清除 URL 参数，避免刷新时重复触发
      window.history.replaceState({}, '', window.location.pathname)
    }
  }, [])

  const handleUploadSuccess = (datasetId: string) => {
    setCurrentDatasetId(datasetId)
    fetchDatasets() // 上传成功后刷新数据集列表
  }

  const handleEvaluationStart = (taskId: string) => {
    setCurrentTaskId(taskId)
    setCotLines([]) // 新任务时清空思维链
    // 直接打开该任务的评估结果 tab（含任务监控）
    openResultsTab(taskId)
  }

  const handleTaskComplete = () => {
    // 不需要自动跳转，因为已经在独立窗口显示
  }

  // 评估详情独立窗口：全屏渲染，无侧边栏/tab栏/悬浮按钮，等同 demo 的独立页面效果
  if (isDetailMode && detailTaskId) {
    return (
      <ErrorBoundary>
        <EvalDetailPage taskId={detailTaskId} initialMainTab={detailSection || undefined} initialQid={detailQid || undefined} initialMetric={detailMetric || undefined} initialStep={detailStep || undefined} onClose={() => {
          // 从详情页返回：导航到评估历史页（去掉 detail 参数，加 page=history）
          window.location.href = `${window.location.pathname}?page=history`
        }} />
      </ErrorBoundary>
    )
  }

  return (
    <div className="app-container" style={{ display: isMonitorMode ? 'block' : 'flex' }}>
      {!isMonitorMode && (
      <aside className={`sidebar${sidebarCollapsed ? ' collapsed' : ''}`}>
        <div className="sidebar-header" onClick={() => openSingletonPage('home')} title={sidebarCollapsed ? '本体评测平台' : undefined}>
          <svg className="sidebar-logo" width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M12 2L2 7L12 12L22 7L12 2Z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
            <path d="M2 17L12 22L22 17" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
            <path d="M2 12L12 17L22 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
          <span className="sidebar-title-text">本体评测平台</span>
        </div>

        {/* 使用指引入口 - 紧凑卡片式，置顶独立行 */}
        <button
          className={`sidebar-guide-entry${activeType === 'guide' ? ' active' : ''}`}
          onClick={() => openSingletonPage('guide')}
          title={sidebarCollapsed ? '使用指引 · 7 步完成首次评测' : undefined}
        >
          <svg className="sidebar-guide-entry-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="10" />
            <polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76" />
          </svg>
          <div className="sidebar-guide-entry-text">
            <span className="sidebar-guide-entry-title">使用指引</span>
            <span className="sidebar-guide-entry-hint">7 步完成首次评测</span>
          </div>
          <span className="sidebar-guide-entry-new">新</span>
        </button>

        {/* AI 助手入口 - 与使用指引对称，对话式配置中心 */}
        <button
          className={`sidebar-guide-entry sidebar-assistant-entry${activeType === 'chat' ? ' active' : ''}`}
          onClick={() => openSingletonPage('chat')}
          title={sidebarCollapsed ? 'AI 助手 · 对话配置 Agent / LLM / 评估任务' : undefined}
        >
          <svg className="sidebar-guide-entry-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
            <path d="M8 10h.01M12 10h.01M16 10h.01"/>
          </svg>
          <div className="sidebar-guide-entry-text">
            <span className="sidebar-guide-entry-title">AI 助手</span>
            <span className="sidebar-guide-entry-hint">对话配置资源 · 组装评测</span>
          </div>
        </button>

        <div className="sidebar-nav" ref={navRef}>
          {/* 资源管理部分 - 可展开下拉 */}
          <div
            className={`nav-section ${stickyFlyout === 'resource' ? 'sticky-open' : ''}`}
            onMouseEnter={() => { if (sidebarCollapsed && stickyFlyout && stickyFlyout !== 'resource') setStickyFlyout(null) }}
          >
            <div
              className={`nav-parent ${resourceMenuOpen ? 'expanded' : ''}`}
              onClick={() => sidebarCollapsed ? toggleStickyFlyout('resource') : setResourceMenuOpen(!resourceMenuOpen)}
              title={sidebarCollapsed ? '资源管理' : undefined}
            >
              <div className="nav-parent-content">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <rect x="3" y="3" width="7" height="7"/>
                  <rect x="14" y="3" width="7" height="7"/>
                  <rect x="14" y="14" width="7" height="7"/>
                  <rect x="3" y="14" width="7" height="7"/>
                </svg>
                <span className="nav-parent-label">资源管理</span>
              </div>
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                className={`nav-arrow ${resourceMenuOpen ? 'rotated' : ''}`}
              >
                <polyline points="9 18 15 12 9 6"/>
              </svg>
            </div>
            <div className={`nav-children ${resourceMenuOpen ? 'open' : ''}`}>
              <div className="nav-flyout-title">资源管理</div>
              <button
                className={`nav-item ${activeType === 'datasets' ? 'active' : ''}`}
                onClick={() => openSingletonPage('datasets')}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ opacity: 0.7 }}>
                  <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
                  <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
                </svg>
                <span className="nav-item-label">评测管理</span>
              </button>
              <button
                className={`nav-item ${activeType === 'agents' ? 'active' : ''}`}
                onClick={() => openSingletonPage('agents')}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ opacity: 0.7 }}>
                  <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
                  <circle cx="9" cy="7" r="4"/>
                  <path d="M23 21v-2a4 4 0 0 0-3-3.87"/>
                  <path d="M16 3.13a4 4 0 0 1 0 7.75"/>
                </svg>
                <span className="nav-item-label">Agent管理</span>
              </button>
              <button
                className={`nav-item ${activeType === 'langfuse_projects' ? 'active' : ''}`}
                onClick={() => openSingletonPage('langfuse_projects')}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ opacity: 0.7 }}>
                  <rect x="3" y="3" width="7" height="7"/>
                  <rect x="14" y="3" width="7" height="7"/>
                  <rect x="14" y="14" width="7" height="7"/>
                  <rect x="3" y="14" width="7" height="7"/>
                </svg>
                <span className="nav-item-label">观测打点</span>
              </button>
              <button
                className={`nav-item ${activeType === 'settings' ? 'active' : ''}`}
                onClick={() => openSingletonPage('settings')}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ opacity: 0.7 }}>
                  <circle cx="12" cy="12" r="3"/>
                  <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
                </svg>
                <span className="nav-item-label">模型配置</span>
              </button>
              <button
                className={`nav-item ${activeType === 'custom_metrics' ? 'active' : ''}`}
                onClick={() => openSingletonPage('custom_metrics')}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ opacity: 0.7 }}>
                  <path d="M9 11l3 3L22 4"/>
                  <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
                </svg>
                <span className="nav-item-label">评测指标</span>
              </button>
            </div>
          </div>

          {/* 评估功能部分 - 可展开下拉 */}
          <div
            className={`nav-section ${stickyFlyout === 'eval' ? 'sticky-open' : ''}`}
            onMouseEnter={() => { if (sidebarCollapsed && stickyFlyout && stickyFlyout !== 'eval') setStickyFlyout(null) }}
          >
            <div
              className={`nav-parent ${evalMenuOpen ? 'expanded' : ''}`}
              onClick={() => sidebarCollapsed ? toggleStickyFlyout('eval') : setEvalMenuOpen(!evalMenuOpen)}
              title={sidebarCollapsed ? '评估功能' : undefined}
            >
              <div className="nav-parent-content">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M3 3v18h18"/>
                  <path d="M18.7 8l-5.1 5.2-2.8-2.7L7 14.3"/>
                </svg>
                <span className="nav-parent-label">评估功能</span>
              </div>
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                className={`nav-arrow ${evalMenuOpen ? 'rotated' : ''}`}
              >
                <polyline points="9 18 15 12 9 6"/>
              </svg>
            </div>
            <div className={`nav-children ${evalMenuOpen ? 'open' : ''}`}>
              <div className="nav-flyout-title">评估功能</div>
              <button
                className={`nav-item ${activeType === 'home' ? 'active' : ''}`}
                onClick={() => openSingletonPage('home')}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ opacity: 0.7 }}>
                  <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
                </svg>
                <span className="nav-item-label">开始评估</span>
              </button>
              <button
                className={`nav-item ${activeType === 'results' ? 'active' : ''}`}
                onClick={() => {
                  if (currentTaskId) openResultsTab(currentTaskId)
                }}
                disabled={!currentTaskId}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ opacity: 0.7 }}>
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                  <polyline points="14 2 14 8 20 8"/>
                  <line x1="16" y1="13" x2="8" y2="13"/>
                </svg>
                <span className="nav-item-label">评估结果</span>
              </button>
              <button
                className={`nav-item ${activeType === 'history' ? 'active' : ''}`}
                onClick={() => openSingletonPage('history')}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ opacity: 0.7 }}>
                  <circle cx="12" cy="12" r="10"/>
                  <polyline points="12 6 12 12 16 14"/>
                </svg>
                <span className="nav-item-label">评估历史</span>
              </button>
            </div>
          </div>

          {/* 评测报告部分 - 可展开下拉 */}
          <div
            className={`nav-section ${stickyFlyout === 'report' ? 'sticky-open' : ''}`}
            onMouseEnter={() => { if (sidebarCollapsed && stickyFlyout && stickyFlyout !== 'report') setStickyFlyout(null) }}
          >
            <div
              className={`nav-parent ${reportMenuOpen ? 'expanded' : ''}`}
              onClick={() => sidebarCollapsed ? toggleStickyFlyout('report') : setReportMenuOpen(!reportMenuOpen)}
              title={sidebarCollapsed ? '评测报告' : undefined}
            >
              <div className="nav-parent-content">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                  <polyline points="14 2 14 8 20 8"/>
                  <line x1="9" y1="13" x2="15" y2="13"/>
                  <line x1="9" y1="17" x2="13" y2="17"/>
                </svg>
                <span className="nav-parent-label">评测报告</span>
              </div>
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                className={`nav-arrow ${reportMenuOpen ? 'rotated' : ''}`}
              >
                <polyline points="9 18 15 12 9 6"/>
              </svg>
            </div>
            <div className={`nav-children ${reportMenuOpen ? 'open' : ''}`}>
              <div className="nav-flyout-title">评测报告</div>
              <button
                className={`nav-item ${activeType === 'reports' ? 'active' : ''}`}
                onClick={() => openSingletonPage('reports')}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ opacity: 0.7 }}>
                  <path d="M4 6h16M4 12h16M4 18h12"/>
                </svg>
                <span className="nav-item-label">报告列表</span>
              </button>
            </div>
          </div>

          {/* 系统监控部分 - 可展开下拉 */}
          <div
            className={`nav-section ${stickyFlyout === 'monitor' ? 'sticky-open' : ''}`}
            onMouseEnter={() => { if (sidebarCollapsed && stickyFlyout && stickyFlyout !== 'monitor') setStickyFlyout(null) }}
          >
            <div
              className={`nav-parent ${monitorMenuOpen ? 'expanded' : ''}`}
              onClick={() => sidebarCollapsed ? toggleStickyFlyout('monitor') : setMonitorMenuOpen(!monitorMenuOpen)}
              title={sidebarCollapsed ? '系统监控' : undefined}
            >
              <div className="nav-parent-content">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>
                  <circle cx="12" cy="12" r="3"/>
                </svg>
                <span className="nav-parent-label">系统监控</span>
              </div>
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                className={`nav-arrow ${monitorMenuOpen ? 'rotated' : ''}`}
              >
                <polyline points="9 18 15 12 9 6"/>
              </svg>
            </div>
            <div className={`nav-children ${monitorMenuOpen ? 'open' : ''}`}>
              <div className="nav-flyout-title">系统监控</div>
              <button
                className={`nav-item ${activeType === 'trace' ? 'active' : ''}`}
                onClick={() => openSingletonPage('trace')}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ opacity: 0.7 }}>
                  <path d="M22 12h-4l-3 9L9 3l-3 9H2"/>
                </svg>
                <span className="nav-item-label">Trace监控</span>
              </button>
            </div>
          </div>

          {/* 联机帮助 - 独立菜单项 */}
          <div className="nav-section nav-section--help">
            <button
              className={`nav-item ${activeType === 'help' ? 'active' : ''}`}
              onClick={() => openSingletonPage('help')}
              title={sidebarCollapsed ? '联机帮助' : undefined}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ opacity: 0.7, flexShrink: 0 }}>
                <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/>
                <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>
              </svg>
              <span className="nav-item-label">联机帮助</span>
            </button>
          </div>
        </div>

        <button
          className="sidebar-toggle"
          onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
          title={sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'}
        >
          <svg className={`sidebar-toggle-icon${sidebarCollapsed ? ' rotated' : ''}`} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <polyline points="15 18 9 12 15 6"/>
          </svg>
        </button>
      </aside>
      )}

      <div className="main-wrapper" style={{ marginLeft: isMonitorMode ? 0 : undefined }}>
        {/* 多标签页栏（仅当前处于 history/results/detail tab 时显示）
            放在滚动容器之外，避免 sticky 失效导致内容越过 tab 栏 */}
        {!isMonitorMode && activeTab && (
          <div className="app-tab-bar">
            {tabs.map(tab => (
              <div
                key={tab.id}
                className={`app-tab-chip ${tab.id === activeTabId ? 'active' : ''}`}
                onClick={() => {
                  setActiveTabId(tab.id)
                  setSingletonPage(null)
                  if (tab.taskId) setCurrentTaskId(tab.taskId)
                }}
                title={tab.tooltip}
              >
                <span className="app-tab-chip__title">{tab.title}</span>
                {tab.closable && (
                  <button
                    type="button"
                    className="app-tab-chip__close"
                    onClick={(e) => {
                      e.stopPropagation()
                      closeTab(tab.id)
                    }}
                    title="关闭"
                    aria-label="关闭标签"
                  >
                    ×
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
        <div className="main-content">
        {/* 悬浮返回首页按钮 */}
        {!isMonitorMode && activeType !== 'home' && (
          <button
            onClick={() => openSingletonPage('home')}
            style={{
              position: 'fixed',
              bottom: '30px',
              right: '30px',
              width: '56px',
              height: '56px',
              borderRadius: '50%',
              background: '#6b5ce7',
              color: '#fff',
              border: 'none',
              boxShadow: '0 4px 15px rgba(107, 92, 231, 0.3)',
              cursor: 'pointer',
              fontSize: '20px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              zIndex: 1000,
              transition: 'all 0.3s ease'
            }}
            onMouseOver={(e) => {
              e.currentTarget.style.transform = 'scale(1.1)'
              e.currentTarget.style.boxShadow = '0 6px 20px rgba(107, 92, 231, 0.4)'
            }}
            onMouseOut={(e) => {
              e.currentTarget.style.transform = 'scale(1)'
              e.currentTarget.style.boxShadow = '0 4px 15px rgba(107, 92, 231, 0.3)'
            }}
            title="返回首页"
          >
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
              <polyline points="9 22 9 12 15 12 15 22"/>
            </svg>
          </button>
        )}
        {/* 独立监控页面模式（新窗口打开） */}
        {currentTaskId && !activeTab && !singletonPage && (
          <TaskMonitor
            taskId={currentTaskId}
            onComplete={handleTaskComplete}
            cotLines={cotLines}
            setCotLines={setCotLines}
          />
        )}

        {/* ===== 单例页（不进入 tab 栏） ===== */}
        {singletonPage === 'home' && (
          <div className="home-hero">
            <div className="home-hero-inner">
              <div className="home-hero-badge">
                <span className="home-hero-badge-dot" />
                Onto-Eval · Agent 全链路评测工作台
              </div>
              <h1 className="home-hero-title">本体评测平台</h1>
              <p className="home-hero-subtitle">
                联通 Agent 在线推理、Langfuse 可观测与 LLM Judge
              </p>
              <p className="home-hero-desc">
                从<strong>质量指标</strong>（正确性、相关性、忠诚度）到<strong>运行效能</strong>（时延、Tokens、多轮稳定性），
                再到<strong>场景子步骤</strong>版本对比与过程点合规，提供可复现、可下钻的企业级评测能力。
              </p>

              <div className="home-hero-features">
                <div className="home-hero-feature">
                  <div className="home-hero-feature-icon">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <line x1="18" y1="20" x2="18" y2="10"/>
                      <line x1="12" y1="20" x2="12" y2="4"/>
                      <line x1="6" y1="20" x2="6" y2="14"/>
                    </svg>
                  </div>
                  <div className="home-hero-feature-title">质量指标</div>
                  <div className="home-hero-feature-desc">LLM Judge · 脚本评测</div>
                </div>
                <div className="home-hero-feature">
                  <div className="home-hero-feature-icon">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>
                    </svg>
                  </div>
                  <div className="home-hero-feature-title">运行效能</div>
                  <div className="home-hero-feature-desc">Trace 物化 · 推理链下钻</div>
                </div>
                <div className="home-hero-feature">
                  <div className="home-hero-feature-icon">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <rect x="3" y="3" width="7" height="7"/>
                      <rect x="14" y="3" width="7" height="7"/>
                      <rect x="14" y="14" width="7" height="7"/>
                      <rect x="3" y="14" width="7" height="7"/>
                    </svg>
                  </div>
                  <div className="home-hero-feature-title">场景子步骤</div>
                  <div className="home-hero-feature-desc">矩阵对比 · 版本 Diff</div>
                </div>
                <div className="home-hero-feature">
                  <div className="home-hero-feature-icon">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
                    </svg>
                  </div>
                  <div className="home-hero-feature-title">过程点合规</div>
                  <div className="home-hero-feature-desc">Langfuse Span 联动</div>
                </div>
              </div>

              <div className="home-hero-actions">
                <button
                  type="button"
                  className="home-hero-btn home-hero-btn-primary"
                  onClick={() => openSingletonPage('online_eval')}
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <circle cx="12" cy="12" r="10"/>
                    <line x1="2" y1="12" x2="22" y2="12"/>
                    <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
                  </svg>
                  在线评估
                </button>
                <button
                  type="button"
                  className="home-hero-btn home-hero-btn-secondary"
                  onClick={() => openSingletonPage('offline_eval')}
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
                    <line x1="3" y1="9" x2="21" y2="9"/>
                    <line x1="9" y1="21" x2="9" y2="9"/>
                  </svg>
                  离线评估
                </button>
                <button
                  type="button"
                  className="home-hero-btn home-hero-btn-secondary"
                  onClick={() => openSingletonPage('chat')}
                  title="对话式配置：AI 自动查询资源、推荐指标、组装草稿"
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
                    <path d="M8 10h.01M12 10h.01M16 10h.01"/>
                  </svg>
                  AI 助手
                </button>
                <button
                  type="button"
                  className="home-hero-guide-link"
                  onClick={() => openSingletonPage('guide')}
                  title="新手快速上手 · 7 步完成首次评测"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <circle cx="12" cy="12" r="10"/>
                    <polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/>
                  </svg>
                  使用指引
                  <span className="home-hero-guide-link-new">新</span>
                </button>
              </div>

              <p className="home-hero-footnote">
                在线评估调用 Onto-Agent 推理并采集 Trace · 离线评估适用于评测集已含模型答案的场景 · AI 助手对话式生成配置
              </p>
            </div>
          </div>
        )}

        {singletonPage === 'datasets' && (
          <div>
            <div className="sub-tabs" style={{ marginBottom: 16 }}>
              <button
                type="button"
                className={`sub-tab ${evalManageSubTab === 'datasets' ? 'active' : ''}`}
                onClick={() => setEvalManageSubTab('datasets')}
              >
                评测集管理
              </button>
              <button
                type="button"
                className={`sub-tab ${evalManageSubTab === 'checkpoints' ? 'active' : ''}`}
                onClick={() => setEvalManageSubTab('checkpoints')}
              >
                过程点配置
              </button>
              <button
                type="button"
                className={`sub-tab ${evalManageSubTab === 'ontology' ? 'active' : ''}`}
                onClick={() => setEvalManageSubTab('ontology')}
              >
                本体包管理
              </button>
            </div>
            {evalManageSubTab === 'datasets' ? (
              <DatasetManager
                onUploadSuccess={handleUploadSuccess}
                agentId={currentAgentId}
              />
            ) : evalManageSubTab === 'checkpoints' ? (
              <ProcessCheckpointPage
                onNavigateToOnlineEval={(agentId) => {
                  setCurrentAgentId(agentId)
                  openSingletonPage('online_eval')
                }}
              />
            ) : (
              <OntologyPackagePage />
            )}
          </div>
        )}

        {singletonPage === 'offline_eval' && (
          <EvalPage
            mode="offline"
            onEvaluationStart={handleEvaluationStart}
            onNavigateToResults={() => {
              /* 评估启动时已由 handleEvaluationStart 打开结果 tab，这里无需再处理 */
            }}
            agentId={currentAgentId}
            initialDraft={chatDraft}
            onDraftConsumed={() => setChatDraft(null)}
          />
        )}

        {singletonPage === 'online_eval' && (
          <EvalPage
            mode="online"
            onEvaluationStart={handleEvaluationStart}
            onNavigateToResults={() => {
              /* 评估启动时已由 handleEvaluationStart 打开结果 tab，这里无需再处理 */
            }}
            agentId={currentAgentId}
            initialDraft={chatDraft}
            onDraftConsumed={() => setChatDraft(null)}
          />
        )}

        {singletonPage === 'chat' && (
          <ChatPage
            onApplyToEvalPage={(draft, onlineMode) => {
              setChatDraft(draft)
              openSingletonPage(onlineMode ? 'online_eval' : 'offline_eval')
            }}
            onNavigateToDetail={(taskId) => openDetailTab(taskId)}
          />
        )}

        {singletonPage === 'agents' && (
          <AgentManagePage />
        )}

        {singletonPage === 'settings' && (
          <SystemSettingsPage />
        )}

        {singletonPage === 'reports' && (
          <ReportArchivePage />
        )}

        {singletonPage === 'history' && (
          <HistoryPage
            onSelectTask={(id) => {
              setCurrentTaskId(id)
              // 从localStorage加载历史任务的思维链
              const savedCotLines = localStorage.getItem(`cotLines_${id}`)
              if (savedCotLines) {
                setCotLines(JSON.parse(savedCotLines))
              } else {
                setCotLines([])
              }
              openResultsTab(id)
            }}
            onSelectDetail={(id) => {
              openDetailWindow(id)
            }}
            onResumeTask={(id) => {
              setCurrentTaskId(id)
              // 清空思维链，开始新的监控
              setCotLines([])
              // 跳转到结果页面（会显示任务监控）
              openResultsTab(id)
            }}
          />
        )}

        {singletonPage === 'langfuse_projects' && (
          <SystemSettingsPage initialTab="langfuse" />
        )}

        {singletonPage === 'trace' && (
          <div className="card history-card" style={{ padding: '20px 24px' }}>
            <div className="sub-tabs checkpoint-tabs">
              <button
                type="button"
                className={`sub-tab ${traceSubView === 'langfuse' ? 'active' : ''}`}
                onClick={() => setTraceSubView('langfuse')}
              >
                Langfuse Trace 探索
              </button>
              <button
                type="button"
                className={`sub-tab ${traceSubView === 'otel' ? 'active' : ''}`}
                onClick={() => setTraceSubView('otel')}
              >
                OTEL Trace（旧）
              </button>
            </div>
            <div className="lf-explore-wrap">
              {traceSubView === 'langfuse' ? <LangfuseTraceExplore /> : <TraceMonitorPage />}
            </div>
          </div>
        )}

        {singletonPage === 'custom_metrics' && (
          <CustomMetricManager />
        )}

        {singletonPage === 'help' && (
          <HelpPage />
        )}

        {singletonPage === 'guide' && (
          <GuidePage
            onNavigate={(page) => openSingletonPage(page)}
            onOpenHelp={() => openSingletonPage('help')}
          />
        )}

        {/* ===== 多标签页（results / detail）- 保持存活，display 控制显隐 ===== */}
        {tabs.map(tab => {
          const isActive = tab.id === activeTabId
          if (tab.type === 'results' && tab.taskId) {
            return (
              <div key={tab.id} style={{ display: isActive ? 'block' : 'none' }}>
                <TaskResultsWrapper
                  taskId={tab.taskId}
                  cotLines={cotLines}
                  setCotLines={setCotLines}
                  onOpenDetail={openDetailTab}
                />
              </div>
            )
          }
          if (tab.type === 'detail' && tab.taskId) {
            return (
              <div key={tab.id} style={{ display: isActive ? 'block' : 'none' }}>
                <ErrorBoundary>
                  <EvalDetailPage
                    taskId={tab.taskId}
                    initialMainTab={tab.initialMainTab}
                    onClose={() => closeTab(tab.id)}
                  />
                </ErrorBoundary>
              </div>
            )
          }
          return null
        })}
      </div>
      </div>
    </div>
  )
}

export default App
