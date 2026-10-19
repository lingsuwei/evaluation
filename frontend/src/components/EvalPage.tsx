import { useState, useEffect, useRef, useCallback, type ReactNode } from 'react';
import axios from 'axios';
import { TaskMonitor, CotLine } from './TaskMonitor';
import { ResultsPage } from './ResultsPage';
import { Tooltip } from './Tooltip';
import { formatDate } from '../utils/dateUtils';
import { compareAgentsByLogicalAndVersion, formatAgentVersionLabel } from '../utils/agentUtils';
import {
  confirmOnlineEvalWithoutLangfuseTrace,
  getLangfuseReadinessMeta,
  isLangfuseTraceReady,
} from '../utils/langfuseReadiness';
import { LangfuseReadinessBadge } from './LangfuseObservabilityNotice';

const API_BASE = '/api';
// 每次构建时注入: 用于强制 build hash 改变, 避免浏览器缓存旧 bundle
const BUILD_TAG = `2026-06-15-r3`;
const DEFAULT_QUALITY_METRICS = ['faithfulness', 'answer_relevancy', 'correctness'];
const DEFAULT_ONLINE_METRICS = [...DEFAULT_QUALITY_METRICS, 'inference_latency', 'tokens_total'];

interface Dataset {
  id: string;
  name: string;
  item_count: number;
  created_at: string;
}

interface DatasetDetail {
  id: string;
  item_count: number;
  created_at: string;
  data: any[];
}

interface Agent {
  id: string;
  name: string;
  inference_config: any;
  is_active: boolean;
  logical_agent_id?: string | null;
  version?: string | null;
  version_tag?: string | null;
  langfuse_project?: string | null;
}

interface Props {
  mode: 'offline' | 'online';
  onEvaluationStart?: (taskId: string) => void;
  onNavigateToResults?: () => void;
  agentId?: string | null;
  /** AI 对话配置产出的草稿，用于预填表单 */
  initialDraft?: Record<string, any> | null;
  /** 草稿灌入完成后回调（用于父组件清空，避免重复灌入） */
  onDraftConsumed?: () => void;
}

interface LLMModel {
  key: string;
  name: string;
  is_default?: boolean;
}

interface EvalScript {
  id: string;
  name: string;
  description: string | null;
  script_code_preview: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/**
 * 通用下拉弹层（combobox）：负责开关状态、外部点击/Esc 关闭、相对定位。
 * trigger 为触发器内容，children 为弹层内容（接收 close 以便选中后关闭）。
 */
function ComboDropdown({
  trigger,
  children,
  panelClassName = '',
  triggerClassName = '',
  disabled = false,
  ariaLabel,
}: {
  trigger: ReactNode;
  children: (close: () => void) => ReactNode;
  panelClassName?: string;
  triggerClassName?: string;
  disabled?: boolean;
  ariaLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div className="combo-dd" ref={containerRef}>
      <button
        type="button"
        className="combo-dd-trigger"
        disabled={disabled}
        onClick={() => !disabled && setOpen(o => !o)}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={ariaLabel}
      >
        <span className={`combo-dd-trigger-text${triggerClassName ? ` ${triggerClassName}` : ''}`}>{trigger}</span>
        <svg
          className={`combo-dd-chevron${open ? ' is-open' : ''}`}
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>
      {open && !disabled && (
        <div className={`combo-dd-panel ${panelClassName}`} role="listbox">
          {children(close)}
        </div>
      )}
    </div>
  );
}

export function EvalPage({ mode, onEvaluationStart, onNavigateToResults, agentId, initialDraft, onDraftConsumed }: Props) {
  const isOnlineMode = mode === 'online';

  // 评测集相关状态
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [loadingDatasets, setLoadingDatasets] = useState(false);
  const [currentDatasetId, setCurrentDatasetId] = useState<string | null>(null);
  const [datasetDetail, setDatasetDetail] = useState<DatasetDetail | null>(null);
  const [loadingDataset, setLoadingDataset] = useState(false);

  // 在线评估相关状态
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentsLoading, setAgentsLoading] = useState<boolean>(isOnlineMode);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // 下拉分组折叠态（按组名/逻辑 Agent ID 记忆到 localStorage，与指标库一致）
  const [collapsedMetricGroups, setCollapsedMetricGroups] = useState<Set<string>>(() => {
    try {
      const saved = localStorage.getItem('eval_metric_groups_collapsed');
      return saved ? new Set(JSON.parse(saved)) : new Set();
    } catch {
      return new Set();
    }
  });
  const [collapsedAgentGroups, setCollapsedAgentGroups] = useState<Set<string>>(() => {
    try {
      const saved = localStorage.getItem('eval_agent_groups_collapsed');
      return saved ? new Set(JSON.parse(saved)) : new Set();
    } catch {
      return new Set();
    }
  });

  const toggleMetricGroup = (label: string) => {
    setCollapsedMetricGroups(prev => {
      const next = new Set(prev);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      try {
        localStorage.setItem('eval_metric_groups_collapsed', JSON.stringify(Array.from(next)));
      } catch {
        // 忽略 localStorage 不可用
      }
      return next;
    });
  };

  const toggleAgentGroup = (lid: string) => {
    setCollapsedAgentGroups(prev => {
      const next = new Set(prev);
      if (next.has(lid)) next.delete(lid);
      else next.add(lid);
      try {
        localStorage.setItem('eval_agent_groups_collapsed', JSON.stringify(Array.from(next)));
      } catch {
        // 忽略 localStorage 不可用
      }
      return next;
    });
  };

  // Agent 推理次数（每题 K 轮）；Judge 固定每题每指标 1 次
  const [inferenceRounds, setInferenceRounds] = useState<number>(1);

  // LLM模型选择
  const [llmModels, setLlmModels] = useState<LLMModel[]>([]);
  const [selectedLlmModel, setSelectedLlmModel] = useState<string>('');

  // 任务相关状态
  const [currentTaskId, setCurrentTaskId] = useState<string | null>(null);
  const [showResults, setShowResults] = useState(false);
  const [showPartialResults, setShowPartialResults] = useState(false);
  const [cotLines, setCotLines] = useState<CotLine[]>([]);
  // 防止重复提交评测（页面卡顿或双击导致多次发起）
  const [isStarting, setIsStarting] = useState(false);
  const startingRef = useRef(false);
  // AI 对话草稿回填时记录的 dataset_id，避免被 fetchDatasets 的清空逻辑覆盖
  // （fetchDatasets 异步完成后会读取此 ref，若有值则保留并清空 ref）
  const pendingDraftDatasetIdRef = useRef<string | null>(null);

  // 折叠状态
  const [collapsedRows, setCollapsedRows] = useState<Set<number>>(new Set());

  // 选中的评测项（默认全选）
  const [selectedItems, setSelectedItems] = useState<Set<number>>(new Set());

  // 评估因子选择：在线评估默认包含运行时指标，离线评估默认只选质量指标
  const [selectedMetrics, setSelectedMetrics] = useState<Set<string>>(
    () => new Set(isOnlineMode ? DEFAULT_ONLINE_METRICS : DEFAULT_QUALITY_METRICS)
  );

  // 是否使用BM25算法（默认启用）
  const [useBM25, setUseBM25] = useState<boolean>(true);

  // 是否使用文档库作为BM25评估的参考（默认启用）
  const [useDocLibrary, setUseDocLibrary] = useState<boolean>(true);

  // 本体包选择（使用指定目录的md文档进行BM25评估）
  const [ontologyPackages, setOntologyPackages] = useState<Array<{ name: string; file_count: number }>>([]);
  const [selectedOntologyPackage, setSelectedOntologyPackage] = useState<string>('');

  // 自定义评估脚本相关状态
  const [evalScripts, setEvalScripts] = useState<EvalScript[]>([]);
  const [scriptMapping, setScriptMapping] = useState<Record<number, string>>({}); // {用例索引: 脚本ID}
  const [showScriptManager, setShowScriptManager] = useState(false);

  // LLM 调用参数临时覆盖（可选）
  const [showLlmOverride, setShowLlmOverride] = useState(false);
  const [llmOverrideParams, setLlmOverrideParams] = useState<{
    max_tokens: number | null;
    temperature: number | null;
  }>({ max_tokens: null, temperature: null });

  // Langfuse 答案源（离线评估）
  const [showLangfuseSource, setShowLangfuseSource] = useState(false);
  const [langfuseSourceProject, setLangfuseSourceProject] = useState<string>('');
  const [langfuseSourceTaskId, setLangfuseSourceTaskId] = useState<string>('');
  const [langfuseSourceTags, setLangfuseSourceTags] = useState<string>('');
  const [langfuseSourceSessionId, setLangfuseSourceSessionId] = useState<string>('');
  const [langfuseSourceAgentName, setLangfuseSourceAgentName] = useState<string>('');
  const [langfuseSourceEnableProcessEval, setLangfuseSourceEnableProcessEval] = useState<boolean>(true);
  const [autoScenarioScore, setAutoScenarioScore] = useState<boolean>(true);
  const [enableProcessConsistency, setEnableProcessConsistency] = useState<boolean>(true);
  const [enableStability, setEnableStability] = useState<boolean>(true);
  const [parallelInference, setParallelInference] = useState<boolean>(true);
  const [sourceTasks, setSourceTasks] = useState<any[]>([]);
  const [loadingSourceTasks, setLoadingSourceTasks] = useState(false);
  const [sourcePreview, setSourcePreview] = useState<any>(null);
  const [loadingSourcePreview, setLoadingSourcePreview] = useState(false);
  const [langfuseSourceMode, setLangfuseSourceMode] = useState<'task' | 'manual'>('task');
  const [langfuseProjects, setLangfuseProjects] = useState<any[]>([]);
  const [newScriptName, setNewScriptName] = useState('');
  const [newScriptDesc, setNewScriptDesc] = useState('');
  const [newScriptCode, setNewScriptCode] = useState(`def evaluate(question, answer, context=None, ground_truth=None):
    """
    自定义评估函数
    
    参数:
        question: 问题内容
        answer: Agent回答
        context: 上下文信息
        ground_truth: 标准答案
    
    返回:
        dict: {"score": 0.0-1.0, "reason": "评估说明"}
    """
    # 在此编写评估逻辑
    score = 0.5
    reason = "默认评分，请修改此脚本"
    return {"score": score, "reason": reason}
`);
  const [scriptValidation, setScriptValidation] = useState<{valid: boolean, error?: string} | null>(null);

  // 可用的评估因子（动态从 /metrics API 加载，含内置 + DeepEval + 自定义）
  const [metricsCatalog, setMetricsCatalog] = useState<{
    builtin: Array<{ key: string; label: string; threshold: number }>;
    deepeval: Array<{ key: string; label: string; category: string; category_label: string; requires: string[]; note: string; gate_threshold: number; lower_is_better: boolean }>;
    custom: Array<{ id: number; key: string; name: string; kind: string; threshold: number; is_active: boolean }>;
    categories: Record<string, string>;
  } | null>(null);
  const [metricsLoading, setMetricsLoading] = useState(false);

  // AI 对话配置草稿回填：initialDraft 变化时灌入各表单字段
  useEffect(() => {
    if (!initialDraft) return;
    // 数据集
    if (typeof initialDraft.dataset_id === 'string' && initialDraft.dataset_id) {
      // 记录到 ref，避免被 fetchDatasets 的"清空已选数据集"逻辑覆盖
      // （fetchDatasets 异步完成时会读取此 ref，若有值则保留而非清空）
      pendingDraftDatasetIdRef.current = initialDraft.dataset_id;
      setCurrentDatasetId(initialDraft.dataset_id);
    }
    // Agent（在线模式）
    if (typeof initialDraft.agent_id === 'string' && initialDraft.agent_id) {
      setSelectedAgentId(initialDraft.agent_id);
    }
    // LLM 模型
    if (typeof initialDraft.llm_model === 'string' && initialDraft.llm_model) {
      setSelectedLlmModel(initialDraft.llm_model);
    }
    // 推理轮次
    if (typeof initialDraft.inference_rounds === 'number') {
      setInferenceRounds(Math.max(1, Math.min(10, initialDraft.inference_rounds)));
    }
    // 评估因子
    if (Array.isArray(initialDraft.selected_metrics) && initialDraft.selected_metrics.length > 0) {
      setSelectedMetrics(new Set(initialDraft.selected_metrics));
    }
    // BM25 相关
    if (typeof initialDraft.use_bm25 === 'boolean') setUseBM25(initialDraft.use_bm25);
    if (typeof initialDraft.use_doc_library === 'boolean') setUseDocLibrary(initialDraft.use_doc_library);
    if (typeof initialDraft.ontology_package === 'string') setSelectedOntologyPackage(initialDraft.ontology_package);
    // 四维开关
    if (typeof initialDraft.auto_scenario_score === 'boolean') setAutoScenarioScore(initialDraft.auto_scenario_score);
    if (typeof initialDraft.enable_process_consistency === 'boolean') setEnableProcessConsistency(initialDraft.enable_process_consistency);
    if (typeof initialDraft.enable_stability === 'boolean') setEnableStability(initialDraft.enable_stability);
    if (typeof initialDraft.parallel_inference === 'boolean') setParallelInference(initialDraft.parallel_inference);
    // LLM 参数覆盖
    if (typeof initialDraft.llm_max_tokens === 'number' || typeof initialDraft.llm_temperature === 'number') {
      setShowLlmOverride(true);
      setLlmOverrideParams({
        max_tokens: typeof initialDraft.llm_max_tokens === 'number' ? initialDraft.llm_max_tokens : null,
        temperature: typeof initialDraft.llm_temperature === 'number' ? initialDraft.llm_temperature : null,
      });
    }
    // 通知父组件草稿已消费，清空避免重复灌入
    onDraftConsumed?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialDraft]);

  // 加载指标清单
  useEffect(() => {
    const fetchMetrics = async () => {
      setMetricsLoading(true);
      try {
        // 离线评估不跑 Agent 推理,仅请求不依赖推理过程产物的指标
        const res = await axios.get(`${API_BASE}/metrics`, {
          params: isOnlineMode ? {} : { evaluation_mode: 'offline' },
        });
        setMetricsCatalog(res.data);
      } catch (err) {
        // 降级：API 不可用时使用内置指标
        setMetricsCatalog(null);
      } finally {
        setMetricsLoading(false);
      }
    };
    fetchMetrics();
  }, [isOnlineMode]);

  // 构建可用指标列表（内置 + DeepEval + 自定义），含分组信息
  const availableMetrics: Array<{ key: string; name: string; group: string; note?: string }> = (() => {
    const result: Array<{ key: string; name: string; group: string; note?: string }> = [
      { key: 'correctness', name: '正确性', group: '回答质量(切题/正确/格式)' },
      { key: 'answer_relevancy', name: '回答相关性', group: '回答质量(切题/正确/格式)' },
      { key: 'faithfulness', name: '忠诚度', group: '回答质量(切题/正确/格式)' },
    ];
    // 运行效能指标依赖 Agent 推理过程(时延/Tokens),离线评估不展示
    if (isOnlineMode) {
      result.push({ key: 'inference_latency', name: '推理时延', group: '运行效能' });
      result.push({ key: 'tokens_total', name: 'Tokens 总消耗', group: '运行效能' });
    }
    if (metricsCatalog) {
      // DeepEval 指标按 category 分组
      for (const m of metricsCatalog.deepeval) {
        result.push({ key: m.key, name: m.label, group: m.category_label || m.category, note: m.note });
      }
      // 自定义指标
      for (const m of metricsCatalog.custom) {
        result.push({ key: m.key, name: m.name, group: '自定义指标' });
      }
    }
    return result;
  })();

  // 按分组组织指标（供 UI 渲染）
  const metricGroups: Array<{ label: string; metrics: Array<{ key: string; name: string; note?: string }> }> = (() => {
    const groupMap = new Map<string, Array<{ key: string; name: string; note?: string }>>();
    for (const m of availableMetrics) {
      const group = m.group;
      if (!groupMap.has(group)) groupMap.set(group, []);
      groupMap.get(group)!.push({ key: m.key, name: m.name, note: m.note });
    }
    return Array.from(groupMap.entries()).map(([label, metrics]) => ({ label, metrics }));
  })();

  // 在线评估：按 logical_agent_id 聚合的 Agent 分组（逻辑 Agent 下挂多版本）
  const agentGroups: Array<[string, Agent[]]> = isOnlineMode
    ? Array.from(
        agents.reduce((map, agent) => {
          const lid = (agent.logical_agent_id || agent.id).trim();
          if (!map.has(lid)) map.set(lid, []);
          map.get(lid)!.push(agent);
          return map;
        }, new Map<string, Agent[]>()),
      )
    : [];

  // 当前选中的 Agent 对象（供触发器与信息条展示）
  const selectedAgent = agents.find(a => a.id === selectedAgentId) || null;

  // 已选指标的展示名列表（供摘要条展示）
  const selectedMetricNames = availableMetrics
    .filter(m => selectedMetrics.has(m.key))
    .map(m => m.name);

  // 判断指标是否属于运行时效能指标（离线模式隐藏）
  const isRuntimeMetric = (key: string) =>
    key === 'inference_latency' || key === 'tokens_total';

  // 计算某分组中可见（受在线/离线模式影响）的指标 key
  const visibleKeysOfGroup = (metrics: Array<{ key: string }>) =>
    metrics.filter(m => isOnlineMode || !isRuntimeMetric(m.key)).map(m => m.key);

  // 加载评测集列表
  const fetchDatasets = async () => {
    setLoadingDatasets(true);
    try {
      const res = await axios.get(`${API_BASE}/datasets`);
      setDatasets(res.data.datasets);
      // 若 AI 对话草稿回填过 dataset_id，保留之（避免被清空）；
      // 否则清空已选择的数据集，避免自动加载大文件
      if (pendingDraftDatasetIdRef.current) {
        setCurrentDatasetId(pendingDraftDatasetIdRef.current);
        pendingDraftDatasetIdRef.current = null;
      } else {
        setCurrentDatasetId(null);
      }
    } catch (err) {
      console.error('获取评测集列表失败:', err);
    } finally {
      setLoadingDatasets(false);
    }
  };

  // 加载可用模型列表，并同步系统设置中的默认评估模型
  const fetchModels = async () => {
    try {
      const res = await axios.get(`${API_BASE}/models`);
      const models: LLMModel[] = res.data.models || [];
      setLlmModels(models);
      const defaultKey =
        res.data.default_key ||
        models.find((m) => m.is_default)?.key ||
        models[0]?.key ||
        'glm';
      setSelectedLlmModel((prev) => {
        if (prev && models.some((m) => m.key === prev)) return prev;
        return defaultKey;
      });
    } catch (err) {
      console.error('获取模型列表失败:', err);
      setSelectedLlmModel((prev) => prev || 'glm');
    }
  };

  // 加载Agent列表
  const fetchAgents = async () => {
    setAgentsLoading(true);
    try {
      const res = await axios.get(`${API_BASE}/agents`);
      // 只筛选有推理配置的Agent
      setAgents(res.data.filter((a: Agent) => a.inference_config?.url && a.is_active));
    } catch (err) {
      console.error('获取Agent列表失败:', err);
    } finally {
      setAgentsLoading(false);
    }
  };

  useEffect(() => {
    fetchDatasets();
    fetchModels();
    fetchEvalScripts();
    // 获取本体包列表
    axios.get(`${API_BASE}/ontology-packages`).then(res => {
      setOntologyPackages(res.data || []);
    }).catch(() => {});
    if (isOnlineMode) {
      fetchAgents();
    }
  }, [isOnlineMode]);

  // 获取 Langfuse 项目列表（用于下拉选择）
  useEffect(() => {
    axios.get(`${API_BASE}/trace/projects`)
      .then(res => {
        const projects = res.data.projects || [];
        setLangfuseProjects(projects);
        // 自动选中第一个项目（不再提供"默认项目"空选项）
        if (projects.length > 0 && !langfuseSourceProject) {
          setLangfuseSourceProject(projects[0].id);
        }
      })
      .catch(() => {});
  }, []);

  // 项目变化时联动刷新历史任务列表并清空已选任务
  useEffect(() => {
    if (!showLangfuseSource || !langfuseSourceProject) return;
    setLangfuseSourceTaskId('');
    fetchSourceTasks();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [langfuseSourceProject]);

  // 获取 Langfuse 答案源任务列表
  const fetchSourceTasks = async () => {
    setLoadingSourceTasks(true);
    try {
      const params = new URLSearchParams();
      if (langfuseSourceProject) params.append('project', langfuseSourceProject);
      const res = await axios.get(`${API_BASE}/trace/langfuse-source/tasks?${params}`);
      setSourceTasks(res.data.tasks || []);
    } catch (err) {
      console.error('获取 Langfuse 源任务列表失败:', err);
    } finally {
      setLoadingSourceTasks(false);
    }
  };

  // 预览 Langfuse 答案源映射
  const handlePreviewSource = async () => {
    setLoadingSourcePreview(true);
    setSourcePreview(null);
    try {
      const params = new URLSearchParams();
      if (langfuseSourceProject) params.append('project', langfuseSourceProject);
      if (langfuseSourceTaskId) params.append('source_task_id', langfuseSourceTaskId);
      if (langfuseSourceTags) params.append('tags', langfuseSourceTags);
      if (langfuseSourceSessionId) params.append('session_id', langfuseSourceSessionId);
      if (langfuseSourceAgentName) params.append('agent_name', langfuseSourceAgentName);
      const res = await axios.get(`${API_BASE}/trace/langfuse-source/preview?${params}`);
      setSourcePreview(res.data);
    } catch (err: any) {
      setSourcePreview({ error: err.response?.data?.detail || err.message });
    } finally {
      setLoadingSourcePreview(false);
    }
  };

  // 构建 langfuse_source 配置
  const buildLangfuseSourcePayload = () => {
    if (!showLangfuseSource) return null;
    const cfg: any = {};
    if (langfuseSourceProject) cfg.project = langfuseSourceProject;
    if (langfuseSourceMode === 'task' && langfuseSourceTaskId) {
      cfg.source_task_id = langfuseSourceTaskId;
    } else {
      if (langfuseSourceTags) cfg.tags = langfuseSourceTags;
      if (langfuseSourceSessionId) cfg.session_id = langfuseSourceSessionId;
      if (langfuseSourceAgentName) cfg.agent_name = langfuseSourceAgentName;
    }
    cfg.enable_process_eval = langfuseSourceEnableProcessEval;
    if (Object.keys(cfg).length === 1 && !cfg.project && !cfg.source_task_id && !cfg.tags && !cfg.session_id && !cfg.agent_name) {
      return langfuseSourceEnableProcessEval ? cfg : null;
    }
    return cfg;
  };

  useEffect(() => {
    if (isOnlineMode && agentId) {
      setSelectedAgentId(agentId);
    }
  }, [isOnlineMode, agentId]);

  // 加载评估脚本列表
  const fetchEvalScripts = async () => {
    try {
      const res = await axios.get(`${API_BASE}/eval-scripts`);
      setEvalScripts(res.data);
    } catch (err) {
      console.error('获取评估脚本列表失败:', err);
    }
  };

  // 创建评估脚本
  const handleCreateScript = async () => {
    if (!newScriptName.trim() || !newScriptCode.trim()) {
      alert('请填写脚本名称和代码');
      return;
    }
    try {
      await axios.post(`${API_BASE}/eval-scripts`, {
        name: newScriptName.trim(),
        description: newScriptDesc.trim() || null,
        script_code: newScriptCode
      });
      setNewScriptName('');
      setNewScriptDesc('');
      fetchEvalScripts();
      alert('脚本创建成功');
    } catch (err: any) {
      alert(`创建失败: ${err.response?.data?.detail || err.message}`);
    }
  };

  // 验证脚本
  const handleValidateScript = async () => {
    try {
      const res = await axios.post(`${API_BASE}/eval-scripts/validate`, {
        name: 'validation_test',
        script_code: newScriptCode
      });
      setScriptValidation(res.data);
    } catch (err: any) {
      setScriptValidation({ valid: false, error: err.response?.data?.detail || err.message });
    }
  };

  // 删除评估脚本
  const handleDeleteScript = async (scriptId: string) => {
    if (!confirm('确定删除此评估脚本？')) return;
    try {
      await axios.delete(`${API_BASE}/eval-scripts/${scriptId}`);
      // 清理映射中对该脚本的引用
      setScriptMapping(prev => {
        const next = { ...prev };
        for (const key of Object.keys(next)) {
          if (next[Number(key)] === scriptId) {
            delete next[Number(key)];
          }
        }
        return next;
      });
      fetchEvalScripts();
    } catch (err: any) {
      alert(`删除失败: ${err.response?.data?.detail || err.message}`);
    }
  };

  // 设置用例的评估脚本
  const setItemScript = (itemIndex: number, scriptId: string) => {
    setScriptMapping(prev => {
      const next = { ...prev };
      if (scriptId) {
        next[itemIndex] = scriptId;
      } else {
        delete next[itemIndex];
      }
      return next;
    });
  };

  // 清除用例的评估脚本
  const clearItemScript = (itemIndex: number) => {
    setScriptMapping(prev => {
      const next = { ...prev };
      delete next[itemIndex];
      return next;
    });
  };

  // 加载评测集详情
  useEffect(() => {
    const fetchDatasetDetail = async () => {
      if (!currentDatasetId) {
        setDatasetDetail(null);
        setCollapsedRows(new Set());
        setSelectedItems(new Set());
        return;
      }
      setLoadingDataset(true);
      try {
        const res = await axios.get(`${API_BASE}/datasets/${currentDatasetId}`);
        setDatasetDetail(res.data);
        // 默认折叠所有行（单行省略号截断，tooltip 查看完整内容）
        if (res.data && res.data.data) {
          setCollapsedRows(new Set(res.data.data.map((_: any, index: number) => index)));
          // 默认全选所有评测项
          setSelectedItems(new Set(res.data.data.map((_: any, index: number) => index)));
        }
      } catch (err: any) {
        console.error('获取评测集详情失败:', err);
        setError(err.response?.data?.detail || '获取评测集详情失败');
      } finally {
        setLoadingDataset(false);
      }
    };
    fetchDatasetDetail();
  }, [currentDatasetId]);

  // 开始评估
  const handleStartEvaluation = async () => {
    if (!currentDatasetId) return;

    // 防止重复提交：使用 ref 作为同步锁，避免 setState 异步窗口内双击穿透
    if (startingRef.current) return;

    // 在线评估模式需要选择Agent
    if (isOnlineMode && !selectedAgentId) {
      alert('在线评估模式需要选择一个已配置推理接口的 Agent');
      return;
    }

    // 检查是否选择了评测项
    if (selectedItems.size === 0) {
      alert('请至少选择一条评测项');
      return;
    }

    // 检查是否选择了评估因子
    if (selectedMetrics.size === 0) {
      alert('请至少选择一个评估因子');
      return;
    }

    const effectiveLlmModel =
      selectedLlmModel ||
      llmModels.find((m) => m.is_default)?.key ||
      llmModels[0]?.key ||
      '';
    if (!effectiveLlmModel) {
      alert('评估模型加载中，请稍候再试');
      return;
    }

    if (isOnlineMode && selectedAgentId) {
      const agent = agents.find((a) => a.id === selectedAgentId);
      if (agent && !isLangfuseTraceReady(agent)) {
        const label = formatAgentVersionLabel(agent);
        if (!confirmOnlineEvalWithoutLangfuseTrace(label)) {
          return;
        }
      }
    }

    startingRef.current = true;
    setIsStarting(true);
    try {
      const payload: any = {
        dataset_id: currentDatasetId,
        online_mode: isOnlineMode,
        inference_rounds: isOnlineMode ? inferenceRounds : 1,
        llm_model: effectiveLlmModel,
        selected_indices: Array.from(selectedItems),  // 传递选中的评测项索引
        selected_metrics: Array.from(selectedMetrics),  // 传递选中的评估因子
        custom_metric_ids: (metricsCatalog?.custom || [])
          .filter(m => selectedMetrics.has(m.key))
          .map(m => m.id),  // 选中的自定义指标 ID 列表
        use_bm25: useBM25,  // 是否使用BM25算法
        use_doc_library: useDocLibrary && useBM25,  // 是否使用文档库(仅当启用BM25时有效)
        ontology_package: selectedOntologyPackage || null,  // 本体包名称
        auto_scenario_score: autoScenarioScore,  // 是否评测场景子步骤维度
        enable_process_consistency: enableProcessConsistency,  // 是否评测过程一致性维度
        enable_stability: isOnlineMode ? enableStability : false,  // 离线模式强制 1 轮推理，无法算稳定性分数 → 强制禁用
        parallel_inference: parallelInference,  // 在线推理并发/串行开关（False=题目+轮次全串行，适合小参数模型 Agent）
      };

      // LLM 调用参数临时覆盖（仅传递非 null 的值）
      if (llmOverrideParams.max_tokens !== null) {
        payload.llm_max_tokens = llmOverrideParams.max_tokens;
      }
      if (llmOverrideParams.temperature !== null) {
        payload.llm_temperature = llmOverrideParams.temperature;
      }

      // 如果有自定义脚本映射，传递给后端
      if (Object.keys(scriptMapping).length > 0) {
        // 只包含被选中的用例的脚本映射
        const filteredScripts: Record<string, string> = {};
        for (const [idx, scriptId] of Object.entries(scriptMapping)) {
          if (selectedItems.has(Number(idx))) {
            filteredScripts[idx] = scriptId;
          }
        }
        if (Object.keys(filteredScripts).length > 0) {
          payload.custom_scripts = filteredScripts;
        }
      }

      if (isOnlineMode) {
        payload.agent_id = selectedAgentId;
      }

      // 离线评估时，使用选择的LLM模型名称作为模型名称
      if (!isOnlineMode) {
        const selectedModel = llmModels.find(m => m.key === effectiveLlmModel);
        payload.model_name = selectedModel ? selectedModel.name : effectiveLlmModel;
        // 离线模式：仅当用户勾选「启用 Langfuse 答案源」时才传 langfuse_source，
      // 未勾选时不传，后端直接使用数据集中的 model_answer 进行评测。
      const langfuseCfg = buildLangfuseSourcePayload();
      if (langfuseCfg) {
        payload.langfuse_source = langfuseCfg;
      }
      }

      const res = await axios.post(`${API_BASE}/evaluate`, payload);
      const taskId = res.data.task_uuid;
      setCurrentTaskId(taskId);
      setShowResults(false);
      setShowPartialResults(false);
      setCotLines([]);
      onEvaluationStart?.(taskId);
      // 导航到评估结果页面
      onNavigateToResults?.();
    } catch (err: any) {
      const detail = err.response?.data?.detail
      const message =
        typeof detail === 'string'
          ? detail
          : detail && typeof detail === 'object' && 'message' in detail
            ? String((detail as { message?: unknown }).message)
            : detail
              ? JSON.stringify(detail)
              : err.message || '启动评估失败'
      setError(message)
    } finally {
      startingRef.current = false;
      setIsStarting(false);
    }
  };

  // 处理任务完成
  const handleTaskComplete = () => {
    setShowPartialResults(false);
    setShowResults(true);
  };

  // 重置评估状态
  const handleReset = () => {
    setCurrentTaskId(null);
    setShowResults(false);
    setShowPartialResults(false);
    setCotLines([]);
  };

  // 切换折叠状态
  const toggleCollapse = (index: number) => {
    setCollapsedRows(prev => {
      const newSet = new Set(prev);
      if (newSet.has(index)) {
        newSet.delete(index);
      } else {
        newSet.add(index);
      }
      return newSet;
    });
  };

  // 切换单个评测项选择状态
  const toggleItemSelection = (index: number) => {
    setSelectedItems(prev => {
      const newSet = new Set(prev);
      if (newSet.has(index)) {
        newSet.delete(index);
      } else {
        newSet.add(index);
      }
      return newSet;
    });
  };

  // 全选所有评测项
  const selectAllItems = () => {
    if (datasetDetail?.data) {
      setSelectedItems(new Set(datasetDetail.data.map((_: any, index: number) => index)));
    }
  };

  // 取消全选
  const deselectAllItems = () => {
    setSelectedItems(new Set());
  };

  // 检查是否全选
  const isAllSelected = datasetDetail?.data ? selectedItems.size === datasetDetail.data.length : false;

  // 切换单个评估因子选择
  const toggleMetricSelection = (metricKey: string) => {
    setSelectedMetrics(prev => {
      const newSet = new Set(prev);
      if (newSet.has(metricKey)) {
        newSet.delete(metricKey);
      } else {
        newSet.add(metricKey);
      }
      return newSet;
    });
  };

  // 全选所有评估因子
  const selectAllMetrics = () => {
    setSelectedMetrics(new Set(availableMetrics.map(metric => metric.key)));
  };

  // 取消全选评估因子
  const deselectAllMetrics = () => {
    setSelectedMetrics(new Set());
  };

  // 按类别批量切换：组内可见指标全选时→全不选，否则→全选
  const toggleCategory = (metrics: Array<{ key: string }>) => {
    const keys = visibleKeysOfGroup(metrics);
    const allSelected = keys.every(k => selectedMetrics.has(k));
    setSelectedMetrics(prev => {
      const next = new Set(prev);
      if (allSelected) {
        keys.forEach(k => next.delete(k));
      } else {
        keys.forEach(k => next.add(k));
      }
      return next;
    });
  };

  // 检查评估因子是否全选
  const isAllMetricsSelected = selectedMetrics.size === availableMetrics.length;

  // 如果有正在进行的任务，显示任务监控或结果
  if (currentTaskId) {
    if (showPartialResults) {
      return (
        <div>
          <div className="card" style={{ marginBottom: 'var(--edp-space-5)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <h2 style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                进行中 · 部分结果
              </h2>
              <button
                type="button"
                onClick={() => setShowPartialResults(false)}
                style={{
                  padding: '8px 16px',
                  background: 'var(--edp-text2)',
                  color: 'var(--edp-card)',
                  border: 'none',
                  borderRadius: 'var(--edp-radius-sm)',
                  cursor: 'pointer',
                }}
              >
                返回任务监控
              </button>
            </div>
          </div>
          <ResultsPage taskId={currentTaskId} partialMode />
        </div>
      );
    }

    if (showResults) {
      return (
        <div>
          <div className="card" style={{ marginBottom: 'var(--edp-space-5)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <h2 style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--edp-green)" strokeWidth="2">
                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                <polyline points="22 4 12 14.01 9 11.01"/>
              </svg>
              评估完成
            </h2>
              <button
                onClick={handleReset}
                style={{
                  padding: '8px 16px',
                  background: 'var(--edp-text2)',
                  color: 'var(--edp-card)',
                  border: 'none',
                  borderRadius: 'var(--edp-radius-sm)',
                  cursor: 'pointer'
                }}
              >
                🔄 返回评估页面
              </button>
            </div>
          </div>
          <ResultsPage taskId={currentTaskId} />
        </div>
      );
    }

    return (
      <div>
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 'var(--edp-space-5)' }}>
          <button
            onClick={handleReset}
            style={{
              padding: '8px 16px',
              background: 'var(--edp-text2)',
              color: 'var(--edp-card)',
              border: 'none',
              borderRadius: 'var(--edp-radius-sm)',
              cursor: 'pointer'
            }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10"/>
                <line x1="15" y1="9" x2="9" y2="15"/>
                <line x1="9" y1="9" x2="15" y2="15"/>
              </svg>
              取消监控
          </button>
        </div>
        <TaskMonitor
          taskId={currentTaskId}
          onComplete={handleTaskComplete}
          cotLines={cotLines}
          setCotLines={setCotLines}
          onViewPartialResults={() => setShowPartialResults(true)}
        />
      </div>
    );
  }

  return (
    <div className="eval-page page-container">
      {/* 页头 */}
      <div className="card eval-page-card">
        <span className={`eval-page-badge ${isOnlineMode ? 'eval-page-badge--online' : 'eval-page-badge--offline'}`}>
          {isOnlineMode ? 'Onto-Agent 在线推理' : '评测集已有答案'}
        </span>
        <h2 className="page-title">
          <svg className="page-title-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
            {isOnlineMode ? (
              <>
                <circle cx="12" cy="12" r="10"/>
                <line x1="2" y1="12" x2="22" y2="12"/>
                <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
              </>
            ) : (
              <>
                <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
                <line x1="3" y1="9" x2="21" y2="9"/>
                <line x1="9" y1="21" x2="9" y2="9"/>
              </>
            )}
          </svg>
          {isOnlineMode ? '在线评估' : '离线评估'}
        </h2>
        <p className="page-desc">
          {isOnlineMode
            ? '调用 Onto-Agent 推理接口获取答案，同步采集 Trace，并完成质量指标与运行效能评测。'
            : '使用评测集中已收录的模型答案进行 LLM Judge 质量评估；配置 Langfuse 答案源后，可基于历史 Trace 进行场景子步骤与过程点评估。'}
        </p>
        {isOnlineMode && (
          <div className="eval-page-features">
            <span className="eval-page-feature-chip">质量指标 · Judge</span>
            <span className="eval-page-feature-chip">运行效能 · Trace</span>
            <span className="eval-page-feature-chip">多轮稳定性</span>
          </div>
        )}
        {!isOnlineMode && (
          <div className="eval-page-features">
            <span className="eval-page-feature-chip">质量指标 · Judge</span>
            <span className="eval-page-feature-chip">脚本评估可选</span>
            <span className="eval-page-feature-chip">Langfuse 场景步骤评测</span>
          </div>
        )}
        {isOnlineMode && agentId && selectedAgentId === agentId && (
          <div className="eval-notice eval-notice--info" style={{ marginTop: 14 }}>
            来自过程点配置：选 1 个评测集、勾选 <strong>1 题</strong> 运行即可产生 Trace 样本。完成后回到
            「过程点配置 → Agent 过程点」点击「刷新 Run 列表」并批量验证。
          </div>
        )}
      </div>

      {/* Step 1: 选择评测集 */}
      <div className="card eval-section">
        <div className="eval-step-header">
          <span className="eval-step-num">1</span>
          <div>
            <h3 className="eval-section-title">
              <svg className="eval-section-title-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
              </svg>
              选择评测集
            </h3>
            <p className="eval-section-desc">从已上传的评测集中选择一个进行评估</p>
          </div>
        </div>

        {loadingDatasets ? (
          <div className="eval-loading">加载评测集中…</div>
        ) : datasets.length === 0 ? (
          <div className="eval-empty">暂无评测集，请先在「评测集管理」中上传</div>
        ) : (
          <select
            className="eval-select"
            value={currentDatasetId || ''}
            onChange={(e) => setCurrentDatasetId(e.target.value || null)}
          >
            <option value="">— 选择评测集 —</option>
            {datasets.map(dataset => (
              <option key={dataset.id} value={dataset.id}>
                {dataset.name} · {dataset.item_count} 题 · {formatDate(dataset.created_at)}
              </option>
            ))}
          </select>
        )}
      </div>

      {/* Step 2: Langfuse 答案源（离线评估） */}
      {!isOnlineMode && currentDatasetId && (
        <div className="card eval-section eval-section--accent">
          <div className="eval-step-header">
            <span className="eval-step-num">2</span>
            <div>
              <h3 className="eval-section-title">
                <svg className="eval-section-title-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                  <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/>
                </svg>
                Langfuse 答案源（可选）
              </h3>
              <p className="eval-section-desc">从 Langfuse 历史 Trace 中获取 Agent 答案，替代评测集中的预置答案进行离线评估</p>
            </div>
          </div>

          <div className="eval-config-body">
            <div className="eval-form-row">
              <label>启用 Langfuse 答案源</label>
              <div className="eval-form-control">
                <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)', cursor: 'pointer', fontSize: 'var(--edp-font-md)' }}>
                  <input
                    type="checkbox"
                    checked={showLangfuseSource}
                    onChange={(e) => {
                      setShowLangfuseSource(e.target.checked);
                      if (e.target.checked && sourceTasks.length === 0) {
                        fetchSourceTasks();
                      }
                    }}
                  />
                  从 Langfuse 获取历史答案进行评估
                </label>
                <span className="eval-form-hint">启用后，评测集 item 中的 model_answer 将被 Langfuse Trace 的输出覆盖</span>
              </div>
            </div>

            {showLangfuseSource && (
              <>
                <div className="eval-form-row">
                  <label>Langfuse 项目</label>
                  <div className="eval-form-control">
                    <select
                      className="eval-select eval-select--sm"
                      value={langfuseSourceProject}
                      onChange={(e) => setLangfuseSourceProject(e.target.value)}
                      style={{ width: '100%', maxWidth: 400 }}
                    >
                      {langfuseProjects.map(p => (
                        <option key={p.id} value={p.id}>
                          {p.name || p.id}{p.id !== p.name ? ` (${p.id})` : ''}
                        </option>
                      ))}
                    </select>
                    <span className="eval-form-hint">onto-eval 中配置的 Langfuse 项目</span>
                  </div>
                </div>

                <div className="eval-form-row">
                  <label>筛选模式</label>
                  <div className="eval-form-control">
                    <div style={{ display: 'flex', gap: 'var(--edp-space-3)' }}>
                      <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', fontSize: 'var(--edp-font-md)' }}>
                        <input
                          type="radio"
                          name="langfuse-source-mode"
                          checked={langfuseSourceMode === 'task'}
                          onChange={() => setLangfuseSourceMode('task')}
                        />
                        按历史任务
                      </label>
                      <label style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', fontSize: 'var(--edp-font-md)' }}>
                        <input
                          type="radio"
                          name="langfuse-source-mode"
                          checked={langfuseSourceMode === 'manual'}
                          onChange={() => setLangfuseSourceMode('manual')}
                        />
                        手动筛选
                      </label>
                    </div>
                  </div>
                </div>

                {langfuseSourceMode === 'task' && (
                  <div className="eval-form-row">
                    <label>历史评测任务</label>
                    <div className="eval-form-control">
                      <div style={{ display: 'flex', gap: 'var(--edp-space-2)', alignItems: 'center' }}>
                        <select
                          className="eval-select eval-select--sm"
                          value={langfuseSourceTaskId}
                          onChange={(e) => setLangfuseSourceTaskId(e.target.value)}
                          style={{ flex: 1, maxWidth: 400 }}
                        >
                          <option value="">— 选择历史任务 —</option>
                          {sourceTasks.map((t: any) => (
                            <option key={t.task_id} value={t.task_id}>
                              {t.task_id} · {t.agent_id} · {t.evaluation_mode} · {formatDate(t.created_at)}
                              {t.synced_trace_count != null
                                ? ` · ${t.synced_trace_count} traces`
                                : ''}
                            </option>
                          ))}
                        </select>
                        <button
                          type="button"
                          className="eval-btn-outline eval-btn-outline--primary"
                          style={{ fontSize: 'var(--edp-font-base)' }}
                          onClick={fetchSourceTasks}
                          disabled={loadingSourceTasks}
                        >
                          {loadingSourceTasks ? '加载中...' : '刷新列表'}
                        </button>
                      </div>
                      <span className="eval-form-hint">选择已完成在线评估任务，拉取其 Langfuse Trace 中的答案</span>
                    </div>
                  </div>
                )}

                {langfuseSourceMode === 'manual' && (
                  <>
                    <div className="eval-form-row">
                      <label>标签筛选</label>
                      <div className="eval-form-control">
                        <input
                          type="text"
                          className="eval-select eval-select--sm"
                          placeholder="如 onto-eval,task:xxx"
                          value={langfuseSourceTags}
                          onChange={(e) => setLangfuseSourceTags(e.target.value)}
                          style={{ width: '100%', maxWidth: 400 }}
                        />
                        <span className="eval-form-hint">按 Langfuse Trace 标签筛选，多个标签用逗号分隔</span>
                      </div>
                    </div>

                    <div className="eval-form-row">
                      <label>Session ID</label>
                      <div className="eval-form-control">
                        <input
                          type="text"
                          className="eval-select eval-select--sm"
                          placeholder="如 task_id:1"
                          value={langfuseSourceSessionId}
                          onChange={(e) => setLangfuseSourceSessionId(e.target.value)}
                          style={{ width: '100%', maxWidth: 400 }}
                        />
                        <span className="eval-form-hint">按 Session ID 前缀筛选</span>
                      </div>
                    </div>

                    <div className="eval-form-row">
                      <label>Agent 名称</label>
                      <div className="eval-form-control">
                        <input
                          type="text"
                          className="eval-select eval-select--sm"
                          placeholder="如 东风agent"
                          value={langfuseSourceAgentName}
                          onChange={(e) => setLangfuseSourceAgentName(e.target.value)}
                          style={{ width: '100%', maxWidth: 400 }}
                        />
                        <span className="eval-form-hint">按 Agent 名称筛选 Trace</span>
                      </div>
                    </div>
                  </>
                )}

                <div className="eval-form-row">
                  <label>过程回放</label>
                  <div className="eval-form-control">
                    <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)', cursor: 'pointer', fontSize: 'var(--edp-font-md)' }}>
                      <input
                        type="checkbox"
                        checked={langfuseSourceEnableProcessEval}
                        onChange={(e) => setLangfuseSourceEnableProcessEval(e.target.checked)}
                      />
                      启用过程回放（与在线评估一致的 Checkpoint + Scenario 评估）
                    </label>
                    <span className="eval-form-hint">
                      启用后，会基于 Langfuse Trace 的 observations 自动重建 AgentRun，触发 Checkpoint 与 Scenario 子步骤打分，并写入综合评分
                    </span>
                  </div>
                </div>

                <div className="eval-form-row">
                  <label>预览映射</label>
                  <div className="eval-form-control">
                    <button
                      type="button"
                      className="eval-btn-outline eval-btn-outline--primary"
                      style={{ fontSize: 'var(--edp-font-base)' }}
                      onClick={handlePreviewSource}
                      disabled={loadingSourcePreview}
                    >
                      {loadingSourcePreview ? '预览中...' : '🔍 预览答案映射'}
                    </button>
                    <span className="eval-form-hint">
                      确认 Langfuse 拉取的答案与评测集题目的对应关系
                    </span>
                  </div>
                </div>

                {sourcePreview && (
                  <div className="eval-panel" style={{ marginTop: 'var(--edp-space-3)' }}>
                    {sourcePreview.error ? (
                      <div className="eval-notice" style={{ background: 'var(--edp-red-soft)', border: '1px solid var(--edp-red-soft)', color: 'var(--edp-red)' }}>
                        {sourcePreview.error}
                      </div>
                    ) : (
                      <div>
                        <div style={{ fontSize: 'var(--edp-font-md)', fontWeight: 500, marginBottom: 'var(--edp-space-2)' }}>
                          共匹配 {sourcePreview.total_answers} 条答案 / {sourcePreview.total_traces} 条 Trace
                        </div>
                        <div style={{ maxHeight: 200, overflow: 'auto', fontSize: 'var(--edp-font-base)' }}>
                          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                            <thead>
                              <tr>
                                <th style={{ textAlign: 'left', padding: '4px 8px', borderBottom: '1px solid var(--edp-border)' }}>题目#</th>
                                <th style={{ textAlign: 'left', padding: '4px 8px', borderBottom: '1px solid var(--edp-border)' }}>Trace ID</th>
                                <th style={{ textAlign: 'left', padding: '4px 8px', borderBottom: '1px solid var(--edp-border)' }}>答案预览</th>
                              </tr>
                            </thead>
                            <tbody>
                              {sourcePreview.traces && sourcePreview.traces.slice(0, 20).map((t: any) => (
                                <tr key={t.trace_id}>
                                  <td style={{ padding: '4px 8px', borderBottom: '1px solid var(--edp-border-soft)' }}>{t.question_id}</td>
                                  <td style={{ padding: '4px 8px', borderBottom: '1px solid var(--edp-border-soft)', fontFamily: 'monospace', fontSize: 'var(--edp-font-sm)' }}>
                                    {t.trace_id?.slice(0, 16)}...
                                  </td>
                                  <td style={{ padding: '4px 8px', borderBottom: '1px solid var(--edp-border-soft)', maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                    {t.answer_preview || '—'}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {/* Step 2: 选择 Agent（在线） */}
      {isOnlineMode && currentDatasetId && (
        <div className="card eval-section eval-section--accent">
          <div className="eval-step-header">
            <span className="eval-step-num">2</span>
            <div>
              <h3 className="eval-section-title">
                <svg className="eval-section-title-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                  <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
                  <circle cx="12" cy="7" r="4"/>
                </svg>
                选择 Agent
              </h3>
              <p className="eval-section-desc">选择已配置推理接口的 Agent 版本</p>
            </div>
          </div>
          <div className="eval-notice eval-notice--warn">
            <strong>Langfuse 提示：</strong>
            未绑定 Langfuse 的 Agent 仍可评测分数，但 Trace 物化、推理链对比与下钻不可用。请在 Agent 管理中「从 Agent 同步」后保存。
          </div>

          {agentsLoading ? (
            <div className="eval-loading">加载 Agent 列表中…</div>
          ) : agents.length === 0 ? (
            <p className="eval-empty" style={{ color: 'var(--edp-red)' }}>
              没有可用的 Agent，请先在 Agent 管理中注册并配置推理接口。
            </p>
          ) : (
            <div className="eval-agent-combo">
              <ComboDropdown
                ariaLabel="选择 Agent 版本"
                trigger={
                  selectedAgent ? (
                    <>
                      <span className="combo-dd-trigger-main">
                        {formatAgentVersionLabel(selectedAgent)}
                      </span>
                      <span
                        className="combo-dd-chip"
                        title={selectedAgent.inference_config?.workspace_id ? `workspace_id: ${selectedAgent.inference_config.workspace_id}` : '未配置工作空间'}
                      >
                        工作空间：{selectedAgent.inference_config?.workspace_name || '-'}
                      </span>
                      <LangfuseReadinessBadge status={getLangfuseReadinessMeta(selectedAgent).status} />
                    </>
                  ) : (
                    <span className="combo-dd-placeholder">-- 选择 Agent 版本 --</span>
                  )
                }
                panelClassName="combo-dd-panel--agent"
              >
                {(close) => agentGroups.map(([lid, group]) => {
                  const groupCollapsed = collapsedAgentGroups.has(lid);
                  return (
                  <div key={lid} className={`combo-dd-group${groupCollapsed ? ' is-collapsed' : ''}`}>
                    <div
                      className="combo-dd-group-title"
                      onClick={() => toggleAgentGroup(lid)}
                      title={groupCollapsed ? '点击展开' : '点击收起'}
                      role="button"
                      aria-expanded={!groupCollapsed}
                    >
                      <svg
                        className={`combo-dd-group-chevron${groupCollapsed ? ' is-collapsed' : ''}`}
                        width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden
                      >
                        <polyline points="6 9 12 15 18 9" />
                      </svg>
                      <span className="combo-dd-group-title-text">{group[0]?.name || lid}</span>
                      <span className="combo-dd-group-count">({lid}) · {group.length} 版本</span>
                    </div>
                    {!groupCollapsed && group.map(agent => {
                      const meta = getLangfuseReadinessMeta(agent);
                      const selected = selectedAgentId === agent.id;
                      return (
                        <button
                          type="button"
                          key={agent.id}
                          className={`combo-dd-option${selected ? ' is-selected' : ''}`}
                          onClick={() => { setSelectedAgentId(agent.id); close(); }}
                          role="option"
                          aria-selected={selected}
                        >
                          <span className={`combo-dd-radio${selected ? ' is-checked' : ''}`} aria-hidden />
                          <span className="combo-dd-option-label">{formatAgentVersionLabel(agent)}</span>
                          <span
                            className="combo-dd-chip"
                            title={agent.inference_config?.workspace_id ? `workspace_id: ${agent.inference_config.workspace_id}` : '未配置工作空间'}
                          >
                            工作空间：{agent.inference_config?.workspace_name || '-'}
                          </span>
                          <LangfuseReadinessBadge status={meta.status} />
                        </button>
                      );
                    })}
                  </div>
                  );
                })}
              </ComboDropdown>
            </div>
          )}
        </div>
      )}

      {/* Step 2/3: 评估配置 */}
      {currentDatasetId && (
        <div className="card eval-section eval-section--accent">
          <div className="eval-step-header">
            <span className="eval-step-num">{isOnlineMode ? 3 : 2}</span>
            <div>
              <h3 className="eval-section-title">
                <svg className="eval-section-title-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                  <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
                </svg>
                评估配置
              </h3>
              <p className="eval-section-desc">
                评测集 <code style={{ fontSize: 'var(--edp-font-base)', background: 'var(--edp-bg)', padding: '1px 6px', borderRadius: 'var(--edp-radius-sm)' }}>{currentDatasetId}</code>
                {datasetDetail && <span> · 共 {datasetDetail.item_count} 题</span>}
              </p>
            </div>
          </div>

          {/* 自定义评估脚本管理 */}
          <div className="eval-panel eval-panel--script">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--edp-space-2)' }}>
              <label style={{ fontWeight: 500, display: 'flex', alignItems: 'center', gap: 6 }}>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <polyline points="16 18 22 12 16 6"/>
                  <polyline points="8 6 2 12 8 18"/>
                </svg>
                自定义评估脚本
              </label>
              <button
                onClick={() => setShowScriptManager(!showScriptManager)}
                style={{
                  padding: '4px 10px',
                  background: showScriptManager ? 'var(--edp-text2)' : 'var(--edp-blue)',
                  color: 'var(--edp-card)',
                  border: 'none',
                  borderRadius: 'var(--edp-radius-sm)',
                  cursor: 'pointer',
                  fontSize: 'var(--edp-font-base)'
                }}
              >
                {showScriptManager ? '收起' : '管理脚本'}
              </button>
            </div>
            <div style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-2)' }}>
              💡 可为单个用例指定Python脚本评估（代码评分器），替代LLM评估。在下方用例表格中选择脚本。
            </div>

            {/* 当前脚本映射统计 */}
            {Object.keys(scriptMapping).length > 0 && (
              <div style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-blue)', marginBottom: 'var(--edp-space-1)' }}>
                📋 已为 {Object.keys(scriptMapping).length} 个用例指定了脚本评估
              </div>
            )}

            {/* 已有脚本列表 */}
            {evalScripts.length > 0 && (
              <div style={{ marginBottom: 'var(--edp-space-2)' }}>
                <div style={{ fontSize: 'var(--edp-font-base)', fontWeight: 500, marginBottom: 'var(--edp-space-1)' }}>可用脚本：</div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                  {evalScripts.map(script => (
                    <span key={script.id} style={{
                      padding: '2px 8px',
                      background: 'var(--edp-blue-soft)',
                      borderRadius: 'var(--edp-radius)',
                      fontSize: 'var(--edp-font-sm)',
                      display: 'flex',
                      alignItems: 'center',
                      gap: 'var(--edp-space-1)'
                    }}>
                      📜 {script.name}
                      <button
                        onClick={() => handleDeleteScript(script.id)}
                        style={{
                          background: 'none',
                          border: 'none',
                          color: 'var(--edp-red)',
                          cursor: 'pointer',
                          fontSize: 'var(--edp-font-base)',
                          padding: '0 2px'
                        }}
                        title="删除脚本"
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* 脚本管理器 */}
            {showScriptManager && (
              <div style={{ marginTop: 10, padding: 'var(--edp-space-3)', background: 'var(--edp-card)', borderRadius: 'var(--edp-radius-sm)', border: '1px solid var(--edp-border)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--edp-space-2)' }}>
                  <div style={{ fontWeight: 500 }}>新增评估脚本</div>
                  <div>
                    <label style={{
                      padding: '4px 10px',
                      background: 'var(--edp-primary)',
                      color: 'var(--edp-card)',
                      borderRadius: 'var(--edp-radius-sm)',
                      cursor: 'pointer',
                      fontSize: 'var(--edp-font-base)',
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: 'var(--edp-space-1)'
                    }}>
                      📂 导入脚本文件
                      <input
                        type="file"
                        accept=".py"
                        style={{ display: 'none' }}
                        onChange={(e) => {
                          const file = e.target.files?.[0];
                          if (!file) return;
                          const reader = new FileReader();
                          reader.onload = (ev) => {
                            const code = ev.target?.result as string;
                            setNewScriptCode(code);
                            setNewScriptName(file.name.replace(/\.py$/, ''));
                            setScriptValidation(null);
                          };
                          reader.readAsText(file);
                          e.target.value = '';
                        }}
                      />
                    </label>
                  </div>
                </div>
                <div style={{ marginBottom: 'var(--edp-space-2)' }}>
                  <input
                    type="text"
                    placeholder="脚本名称"
                    value={newScriptName}
                    onChange={(e) => setNewScriptName(e.target.value)}
                    style={{
                      padding: '6px 10px',
                      border: '1px solid var(--edp-border)',
                      borderRadius: 'var(--edp-radius-sm)',
                      width: '100%',
                      maxWidth: 300,
                      fontSize: 'var(--edp-font-md)'
                    }}
                  />
                </div>
                <div style={{ marginBottom: 'var(--edp-space-2)' }}>
                  <input
                    type="text"
                    placeholder="脚本描述（可选）"
                    value={newScriptDesc}
                    onChange={(e) => setNewScriptDesc(e.target.value)}
                    style={{
                      padding: '6px 10px',
                      border: '1px solid var(--edp-border)',
                      borderRadius: 'var(--edp-radius-sm)',
                      width: '100%',
                      fontSize: 'var(--edp-font-md)'
                    }}
                  />
                </div>
                <div style={{ marginBottom: 'var(--edp-space-2)' }}>
                  <textarea
                    placeholder="Python脚本代码"
                    value={newScriptCode}
                    onChange={(e) => { setNewScriptCode(e.target.value); setScriptValidation(null); }}
                    style={{
                      padding: '8px 10px',
                      border: '1px solid var(--edp-border)',
                      borderRadius: 'var(--edp-radius-sm)',
                      width: '100%',
                      minHeight: 200,
                      fontFamily: 'Consolas, Monaco, monospace',
                      fontSize: 'var(--edp-font-base)',
                      lineHeight: 1.5
                    }}
                  />
                </div>
                <div style={{ display: 'flex', gap: 'var(--edp-space-2)', alignItems: 'center' }}>
                  <button
                    onClick={handleValidateScript}
                    style={{
                      padding: '6px 12px',
                      background: 'var(--edp-amber)',
                      color: 'var(--edp-text)',
                      border: 'none',
                      borderRadius: 'var(--edp-radius-sm)',
                      cursor: 'pointer',
                      fontSize: 'var(--edp-font-base)'
                    }}
                  >
                    ✅ 验证脚本
                  </button>
                  <button
                    onClick={handleCreateScript}
                    style={{
                      padding: '6px 12px',
                      background: 'var(--edp-green)',
                      color: 'var(--edp-card)',
                      border: 'none',
                      borderRadius: 'var(--edp-radius-sm)',
                      cursor: 'pointer',
                      fontSize: 'var(--edp-font-base)'
                    }}
                  >
                    💾 保存脚本
                  </button>
                  {scriptValidation && (
                    <span style={{ fontSize: 'var(--edp-font-base)', color: scriptValidation.valid ? 'var(--edp-green)' : 'var(--edp-red)' }}>
                      {scriptValidation.valid ? '✅ 验证通过' : `❌ ${scriptValidation.error}`}
                    </span>
                  )}
                </div>
              </div>
            )}
          </div>

          {!isOnlineMode && agentId && (
            <div className="eval-notice eval-notice--info" style={{ marginBottom: 'var(--edp-space-4)' }}>
              <strong>过程点配置上下文：</strong>
              <code style={{ marginLeft: 'var(--edp-space-2)' }}>{agentId}</code>
              <span style={{ marginLeft: 'var(--edp-space-2)', fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)' }}>
                离线评估不会调用该 Agent；配置 Langfuse 答案源后可基于历史 Trace 评估场景子步骤与过程点。
              </span>
            </div>
          )}

          <div className="eval-config-body">
            <div className="eval-form-grid">
              {isOnlineMode && (
                <div className="eval-form-row">
                  <label>Agent 运行次数</label>
                  <div className="eval-form-control">
                    <select
                      className="eval-select eval-select--sm"
                      value={inferenceRounds}
                      onChange={(e) => setInferenceRounds(Number(e.target.value))}
                    >
                      {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(n => (
                        <option key={n} value={n}>{n} 次/题</option>
                      ))}
                    </select>
                    <span className="eval-form-hint">
                      {inferenceRounds > 1
                        ? `稳定性模式：每题 ${inferenceRounds} 轮推理；所有质量指标逐轮 Judge，各轮对比见详情`
                        : '常规模式：每题 1 轮推理，质量指标 Judge 1 次'}
                    </span>
                  </div>
                </div>
              )}

              {isOnlineMode && (
                <div className="eval-form-row">
                  <label>推理并发模式</label>
                  <div className="eval-form-control">
                    <select
                      className="eval-select eval-select--sm"
                      value={parallelInference ? 'concurrent' : 'serial'}
                      onChange={(e) => setParallelInference(e.target.value === 'concurrent')}
                    >
                      <option value="concurrent">并发（默认）</option>
                      <option value="serial">串行</option>
                    </select>
                    <span className="eval-form-hint">
                      {parallelInference
                        ? '多题多轮同时下发到 Agent，吞吐高（适合大参数 Agent）'
                        : '题目与轮次逐个下发，避免并发干扰（适合小参数模型 Agent）'}
                    </span>
                  </div>
                </div>
              )}

              <div className="eval-form-row">
                <label>评估模型</label>
                <div className="eval-form-control">
                  <select
                    className="eval-select eval-select--sm"
                    value={selectedLlmModel || llmModels.find((m) => m.is_default)?.key || llmModels[0]?.key || ''}
                    onChange={(e) => setSelectedLlmModel(e.target.value)}
                  >
                    {llmModels.map(model => (
                      <option key={model.key} value={model.key}>
                        {model.name}{model.is_default ? '（默认）' : ''}
                      </option>
                    ))}
                  </select>
                  <span className="eval-form-hint">用于 LLM Judge 打分</span>
                </div>
              </div>

              {/* LLM 调用参数临时覆盖 */}
              <div className="eval-form-row">
                <label>LLM 调用参数</label>
                <div className="eval-form-control">
                  <button
                    type="button"
                    className="eval-btn-outline eval-btn-outline--primary"
                    style={{ fontSize: 'var(--edp-font-base)' }}
                    onClick={() => setShowLlmOverride(!showLlmOverride)}
                  >
                    {showLlmOverride ? '收起参数' : '临时覆盖参数'}
                  </button>
                  <span className="eval-form-hint">
                    {llmOverrideParams.max_tokens !== null || llmOverrideParams.temperature !== null
                      ? `已覆盖: max_tokens=${llmOverrideParams.max_tokens ?? '默认'}, temperature=${llmOverrideParams.temperature ?? '默认'}`
                      : '默认使用系统设置中的参数'}
                  </span>
                  {showLlmOverride && (
                    <div style={{
                      marginTop: 10,
                      padding: 'var(--edp-space-3)',
                      background: 'var(--edp-bg)',
                      borderRadius: 'var(--edp-radius-sm)',
                      border: '1px solid var(--edp-border)'
                    }}>
                      <div style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-2)' }}>
                        临时覆盖本次评测的 LLM 调用参数（不影响系统设置）。留空则使用模型配置中的默认值。
                      </div>
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--edp-space-3)' }}>
                        <div>
                          <label style={{ display: 'block', fontSize: 'var(--edp-font-base)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 3 }}>
                            max_tokens（最大输出tokens）
                          </label>
                          <input
                            type="number"
                            value={llmOverrideParams.max_tokens ?? ''}
                            placeholder="默认4096"
                            min={1}
                            step={256}
                            onChange={(e) => {
                              const v = e.target.value === '' ? null : parseInt(e.target.value)
                              setLlmOverrideParams(prev => ({ ...prev, max_tokens: v }))
                            }}
                            style={{
                              padding: '6px 10px',
                              border: '1px solid var(--edp-border)',
                              borderRadius: 'var(--edp-radius-sm)',
                              fontSize: 'var(--edp-font-md)',
                              width: '100%',
                              boxSizing: 'border-box'
                            }}
                          />
                          <div style={{ fontSize: 'var(--edp-font-sm)', color: 'var(--edp-text3)', marginTop: 2 }}>最小值 1，留空用默认</div>
                        </div>
                        <div>
                          <label style={{ display: 'block', fontSize: 'var(--edp-font-base)', fontWeight: 500, color: 'var(--edp-text2)', marginBottom: 3 }}>
                            temperature（温度）
                          </label>
                          <input
                            type="number"
                            value={llmOverrideParams.temperature ?? ''}
                            placeholder="默认0"
                            min={0}
                            max={1}
                            step={0.1}
                            onChange={(e) => {
                              const v = e.target.value === '' ? null : parseFloat(e.target.value)
                              setLlmOverrideParams(prev => ({ ...prev, temperature: v }))
                            }}
                            style={{
                              padding: '6px 10px',
                              border: '1px solid var(--edp-border)',
                              borderRadius: 'var(--edp-radius-sm)',
                              fontSize: 'var(--edp-font-md)',
                              width: '100%',
                              boxSizing: 'border-box'
                            }}
                          />
                          <div style={{ fontSize: 'var(--edp-font-sm)', color: 'var(--edp-text3)', marginTop: 2 }}>范围 0~1，留空用默认</div>
                        </div>
                      </div>
                      <button
                        type="button"
                        style={{
                          marginTop: 'var(--edp-space-2)',
                          padding: '4px 10px',
                          background: 'var(--edp-text2)',
                          color: 'var(--edp-card)',
                          border: 'none',
                          borderRadius: 'var(--edp-radius-sm)',
                          cursor: 'pointer',
                          fontSize: 'var(--edp-font-sm)'
                        }}
                        onClick={() => setLlmOverrideParams({ max_tokens: null, temperature: null })}
                      >
                        重置为默认值
                      </button>
                    </div>
                  )}
                </div>
              </div>

              {/* 评测维度勾选：质量评测为必选（禁用取消），过程/稳定性/场景子步骤 可按需关闭 */}
              <div className="eval-form-row">
                <label>评测维度</label>
                <div className="eval-form-control">
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--edp-space-4)', alignItems: 'center' }}>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)', cursor: 'not-allowed', fontSize: 'var(--edp-font-md)', opacity: 0.85 }}>
                      <input
                        type="checkbox"
                        checked
                        disabled
                      />
                      质量评测
                    </label>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)', cursor: 'pointer', fontSize: 'var(--edp-font-md)' }}>
                      <input
                        type="checkbox"
                        checked={autoScenarioScore}
                        onChange={(e) => setAutoScenarioScore(e.target.checked)}
                      />
                      场景子步骤
                    </label>
                    {(isOnlineMode || showLangfuseSource) && (
                      <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)', cursor: 'pointer', fontSize: 'var(--edp-font-md)' }}>
                        <input
                          type="checkbox"
                          checked={enableProcessConsistency}
                          onChange={(e) => setEnableProcessConsistency(e.target.checked)}
                        />
                        过程一致性
                      </label>
                    )}
                    {isOnlineMode && (
                      <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)', cursor: 'pointer', fontSize: 'var(--edp-font-md)' }}>
                        <input
                          type="checkbox"
                          checked={enableStability}
                          onChange={(e) => setEnableStability(e.target.checked)}
                        />
                        运行稳定性
                      </label>
                    )}
                  </div>
                  <span className="eval-form-hint">
                    {isOnlineMode
                      ? '质量评测为必选项；取消勾选后该维度结论显示为"未启用"，稳定性取消不影响推理轮次'
                      : '质量评测为必选项；离线模式配置 Langfuse 答案源后可勾选过程一致性（基于历史 Trace 回放）'}
                  </span>
                </div>
              </div>

              <div className="eval-metrics-block">
                <div className="eval-metrics-row">
                  <label className="eval-metrics-label">评估因子</label>
                  <ComboDropdown
                    ariaLabel="选择评估因子"
                    triggerClassName="combo-dd-trigger-text--chips"
                    trigger={
                      metricsLoading ? (
                        <span className="combo-dd-placeholder">加载指标列表…</span>
                      ) : selectedMetrics.size === 0 ? (
                        <span className="combo-dd-placeholder">未选择指标</span>
                      ) : (
                        <>
                          {selectedMetricNames.slice(0, 3).map((name, i) => (
                            <span key={`${name}-${i}`} className="combo-dd-chip">{name}</span>
                          ))}
                          {selectedMetricNames.length > 3 && (
                            <span className="combo-dd-chip combo-dd-chip--more">+{selectedMetricNames.length - 3}</span>
                          )}
                        </>
                      )
                    }
                    panelClassName="combo-dd-panel--metrics"
                  >
                    {() => (
                      <div className="metric-combobox">
                        <div className="metric-combobox-body">
                          {metricsLoading && <span style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text3)', padding: 'var(--edp-space-2)' }}>加载指标列表…</span>}
                          {!metricsLoading && metricGroups.map(group => {
                            const visibleMetrics = group.metrics.filter(m => isOnlineMode || !isRuntimeMetric(m.key));
                            if (visibleMetrics.length === 0) return null;
                            const selectedInGroup = visibleMetrics.filter(m => selectedMetrics.has(m.key));
                            const triState = selectedInGroup.length === 0
                              ? 'none'
                              : selectedInGroup.length === visibleMetrics.length ? 'all' : 'partial';
                            const groupCollapsed = collapsedMetricGroups.has(group.label);
                            return (
                              <div key={group.label} className={`metric-combobox-group${groupCollapsed ? ' is-collapsed' : ''}`}>
                                <div className="metric-combobox-group-header">
                                  <button
                                    type="button"
                                    className="metric-combobox-group-toggle"
                                    onClick={() => toggleMetricGroup(group.label)}
                                    title={groupCollapsed ? '展开此分组' : '收起此分组'}
                                    aria-expanded={!groupCollapsed}
                                  >
                                    <svg
                                      className={`metric-combobox-chevron${groupCollapsed ? ' is-collapsed' : ''}`}
                                      width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden
                                    >
                                      <polyline points="6 9 12 15 18 9" />
                                    </svg>
                                  </button>
                                  <label className="metric-combobox-check">
                                    <input
                                      type="checkbox"
                                      checked={triState === 'all'}
                                      ref={(el: HTMLInputElement | null) => { if (el) el.indeterminate = triState === 'partial'; }}
                                      onChange={() => toggleCategory(group.metrics)}
                                    />
                                    <span className="metric-combobox-group-label">{group.label}</span>
                                    <span className="metric-combobox-group-count">{selectedInGroup.length}/{visibleMetrics.length}</span>
                                  </label>
                                </div>
                                {!groupCollapsed && visibleMetrics.map(metric => {
                                  const isSelected = selectedMetrics.has(metric.key);
                                  const isRuntime = isRuntimeMetric(metric.key);
                                  return (
                                    <label
                                      key={metric.key}
                                      className={`metric-combobox-item${isSelected ? ' is-checked' : ''}`}
                                    >
                                      <input
                                        type="checkbox"
                                        checked={isSelected}
                                        onChange={() => toggleMetricSelection(metric.key)}
                                      />
                                      <span className="metric-combobox-item-name">{metric.name}</span>
                                      {isRuntime && <span className="metric-combobox-tag">效能</span>}
                                      {metric.note && (
                                        <span className="metric-combobox-note" title={metric.note}>?</span>
                                      )}
                                    </label>
                                  );
                                })}
                              </div>
                            );
                          })}
                          {!metricsLoading && metricsCatalog && metricsCatalog.deepeval.length === 0 && metricsCatalog.custom.length === 0 && (
                            <div className="metric-combobox-hint">
                              安装 deepeval 可解锁 25+ 项高级指标（安全/RAG/工具/推理等）
                            </div>
                          )}
                        </div>
                        <div className="metric-combobox-footer">
                          <span className="metric-combobox-summary">
                            已选 {selectedMetrics.size} / {availableMetrics.length} 项
                          </span>
                          <div className="metric-combobox-actions">
                            <button type="button" className="eval-btn-outline eval-btn-outline--primary" onClick={selectAllMetrics}>全选</button>
                            <button type="button" className="eval-btn-outline" onClick={deselectAllMetrics}>清空</button>
                          </div>
                        </div>
                      </div>
                    )}
                  </ComboDropdown>
                </div>

                {/* 本体包选择 - 与评估模型风格一致 */}
                <div className="eval-form-row">
                  <label>本体包</label>
                  <div className="eval-form-control">
                    {ontologyPackages.length > 0 ? (
                      <>
                        <select
                          className="eval-select eval-select--sm"
                          value={selectedOntologyPackage}
                          onChange={(e) => setSelectedOntologyPackage(e.target.value)}
                        >
                          <option value="">无（不使用本体包）</option>
                          {ontologyPackages.map(pkg => (
                            <option key={pkg.name} value={pkg.name}>
                              {pkg.name}（{pkg.file_count} 个文件）
                            </option>
                          ))}
                        </select>
                        <span className="eval-form-hint">
                          {selectedOntologyPackage
                            ? `用于过程一致性评测的证据集评分`
                            : '未选择本体包，将使用默认启发式绑定'}
                        </span>
                      </>
                    ) : (
                      <span className="eval-form-hint">
                        暂无本体包。可在"评测管理 → 本体包管理"页上传维护。
                      </span>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </div>

          {error && (
            <div className="eval-notice" style={{ background: 'var(--edp-red-soft)', border: '1px solid var(--edp-red-soft)', color: 'var(--edp-red)', marginTop: 'var(--edp-space-3)' }}>
              {error}
            </div>
          )}

          <div className="eval-config-actions">
            <button
              className="btn btn-primary eval-start-btn"
              onClick={handleStartEvaluation}
              disabled={isStarting || (isOnlineMode && !selectedAgentId)}
            >
              {isStarting ? (
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
                  {isOnlineMode ? '开始在线评估' : '开始离线评估'}
                </>
              )}
            </button>
          </div>
        </div>
      )}

      {/* 评测题目选择 */}
      {currentDatasetId && datasetDetail && (
        <div className="card eval-section">
          <div className="eval-items-header">
            <div className="eval-step-header" style={{ marginBottom: 0, flex: 1 }}>
              <span className="eval-step-num">{isOnlineMode ? 4 : 3}</span>
              <div>
                <h3 className="eval-section-title">
                  <svg className="eval-section-title-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
                    <polyline points="9 11 12 14 22 4"/>
                    <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
                  </svg>
                  评测题目
                </h3>
                <p className="eval-section-desc">
                  共 {datasetDetail.item_count} 题 · 已选 {selectedItems.size} 题
                </p>
              </div>
            </div>
            <div className="eval-toolbar">
              <button type="button" className="eval-btn-outline eval-btn-outline--primary" onClick={selectAllItems}>
                全选
              </button>
              <button type="button" className="eval-btn-outline" onClick={deselectAllItems}>
                取消全选
              </button>
            </div>
          </div>

          {loadingDataset ? (
            <div className="eval-loading">加载题目中…</div>
          ) : (
            <div className="eval-items-table-wrap">
              <table className="results-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
                <thead>
                  <tr>
                    <th style={{ width: '60px', border: '1px solid var(--edp-border)', textAlign: 'center' }}>
                      <input
                        type="checkbox"
                        checked={isAllSelected}
                        onChange={() => isAllSelected ? deselectAllItems() : selectAllItems()}
                        style={{ cursor: 'pointer' }}
                      />
                    </th>
                    <th style={{ width: '10px', border: '1px solid var(--edp-border)', textAlign: 'center' }}>No.</th>
                    <th style={{ width: '18%', border: '1px solid var(--edp-border)' }}>Question</th>
                    <th style={{ width: '22%', border: '1px solid var(--edp-border)' }}>Prompt</th>
                    <th style={{ width: '18%', border: '1px solid var(--edp-border)' }}>Ground_Truth</th>
                    <th style={{ width: '14%', border: '1px solid var(--edp-border)' }}>Mode_Answer</th>
                    <th style={{ width: '10%', border: '1px solid var(--edp-border)' }}>Judging_Mode</th>
                    <th style={{ width: '8%', border: '1px solid var(--edp-border)', textAlign: 'center' }}>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {datasetDetail.data.map((item, index) => {
                    const isCollapsed = collapsedRows.has(index);
                    const isSelected = selectedItems.has(index);

                    const questionVal: string = item.question || item.q || '-';
                    const promptVal: string = item.prompt || '-';
                    const gtVal: string = item.ground_truth || item.gt || item.true_answer || item.reference || '-';
                    const modeAnswerVal: string = item.answer || item.a || item.response || item.model_answer || '-';

                    const truncate = (v: string) => {
                      if (!v || v === '-') return '-';
                      const lines = v.split('\n');
                      if (lines.length <= 2) return v;
                      return lines.slice(0, 2).join('\n') + '...';
                    };

                    return (
                    <tr key={index} className={isSelected ? '' : 'is-unselected'} style={{ verticalAlign: 'middle' }}>
                      <td style={{ verticalAlign: 'middle', border: '1px solid var(--edp-border)', textAlign: 'center' }}>
                        <input
                          type="checkbox"
                          checked={isSelected}
                          onChange={() => toggleItemSelection(index)}
                          style={{ cursor: 'pointer' }}
                        />
                      </td>
                      <td style={{ verticalAlign: 'middle', border: '1px solid var(--edp-border)', textAlign: 'center' }}>
                        <div style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--edp-space-1)' }}>
                          <Tooltip content={isCollapsed ? '展开' : '折叠'}>
                            <button
                              className="btn"
                              style={{
                                padding: '2px 6px',
                                fontSize: 'var(--edp-font-lg)',
                                background: isCollapsed ? 'var(--edp-primary)' : 'var(--edp-text2)',
                                minWidth: '20px',
                                height: '20px',
                                lineHeight: '1',
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center'
                              }}
                              onClick={() => toggleCollapse(index)}
                            >
                              {isCollapsed ? '+' : '-'}
                            </button>
                          </Tooltip>
                          <span>{index + 1}</span>
                        </div>
                      </td>
                      <td style={{ verticalAlign: 'middle', border: '1px solid var(--edp-border)' }}>
                        <Tooltip content={questionVal}>
                          <div style={{ maxWidth: '250px', wordWrap: 'break-word', fontSize: 'var(--edp-font-md)' }}>
                            {isCollapsed ? truncate(questionVal) : questionVal}
                          </div>
                        </Tooltip>
                      </td>
                      <td style={{ verticalAlign: 'middle', border: '1px solid var(--edp-border)' }}>
                        <Tooltip content={promptVal}>
                          <div style={{ maxWidth: '320px', wordWrap: 'break-word', fontSize: 'var(--edp-font-md)', whiteSpace: 'pre-wrap' }}>
                            {isCollapsed ? truncate(promptVal) : promptVal}
                          </div>
                        </Tooltip>
                      </td>
                      <td style={{ verticalAlign: 'middle', border: '1px solid var(--edp-border)' }}>
                        <Tooltip content={gtVal}>
                          <div style={{ maxWidth: '250px', wordWrap: 'break-word', fontSize: 'var(--edp-font-md)' }}>
                            {isCollapsed ? truncate(gtVal) : gtVal}
                          </div>
                        </Tooltip>
                      </td>
                      <td style={{ verticalAlign: 'middle', border: '1px solid var(--edp-border)' }}>
                        <Tooltip content={modeAnswerVal}>
                          <div style={{ maxWidth: '200px', wordWrap: 'break-word', fontSize: 'var(--edp-font-md)', whiteSpace: 'pre-wrap' }}>
                            {isCollapsed ? truncate(modeAnswerVal) : modeAnswerVal}
                          </div>
                        </Tooltip>
                      </td>
                      <td style={{ verticalAlign: 'middle', border: '1px solid var(--edp-border)', textAlign: 'center' }}>
                        <span style={{
                          padding: '2px 8px',
                          borderRadius: 'var(--edp-radius-sm)',
                          fontSize: 'var(--edp-font-base)',
                          background: (item.judging_mode || '').includes('Prompt') ? 'var(--edp-blue-soft)' : 'var(--edp-amber-soft)',
                          color: (item.judging_mode || '').includes('Prompt') ? 'var(--edp-blue)' : 'var(--edp-amber)',
                          whiteSpace: 'nowrap'
                        }}>
                          {item.judging_mode || 'Ground_Truth评测'}
                        </span>
                      </td>
                      <td style={{ verticalAlign: 'middle', border: '1px solid var(--edp-border)', textAlign: 'center' }}>
                        <button
                          onClick={() => clearItemScript(index)}
                          disabled={!scriptMapping[index]}
                          title={scriptMapping[index] ? '清除该题脚本' : 'LLM 评估无需操作'}
                          style={{
                            padding: '2px 8px',
                            background: scriptMapping[index] ? 'var(--edp-red)' : 'var(--edp-text2)',
                            color: 'var(--edp-card)',
                            border: 'none',
                            borderRadius: 3,
                            cursor: scriptMapping[index] ? 'pointer' : 'not-allowed',
                            fontSize: 'var(--edp-font-sm)',
                            opacity: scriptMapping[index] ? 1 : 0.5
                          }}
                        >
                          {scriptMapping[index] ? `📜 ${evalScripts.find(s => s.id === scriptMapping[index])?.name || '脚本'}` : '默认 LLM'}
                        </button>
                      </td>
                    </tr>
                  );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
