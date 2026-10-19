/**
 * 过程一致性视图 — 严格对标 demo eval-detail-left-right.html 的 viewConsistency()
 * 数据来源：/api/tasks/{taskId}/process-consistency + /api/runs/{runId}/span-tree（抽屉按需）
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { DimensionVerdictsPayload } from '../types/dimensionVerdict'
import { formatDuration } from '../utils/dateUtils'
import { Tooltip } from './Tooltip'
import { Spinner } from './Spinner'
import { DrawerShell } from './DrawerShell'
import { TRACE_CHAIN_COMPARE } from './helpCopy'

const API_BASE = '/api'

/* ===== 后端 process-consistency 类型 ===== */
interface CheckpointItem {
  checkpoint_id: string
  name: string
  required?: boolean | null
  passed: boolean
  hit?: boolean | null
  order_ok?: boolean | null
  outcome?: string | null
  order?: number | null
  enforce_order?: boolean | null
  evidence?: { matched_ids?: string[] | null; [k: string]: unknown } | null
}

interface RunSummary {
  agent_run_id: string
  run_index: number
  agent_name?: string | null
  required_total: number
  required_passed: number
  required_all_passed: boolean
  optional_total: number
  optional_hit: number
  items: CheckpointItem[]
}

interface QuestionConsistency {
  question_id: number
  run_count: number
  is_multi_run?: boolean
  required_status: 'green' | 'yellow' | 'red'
  chain_status: string
  chain_pass_rate?: number | null
  rounds_all_ok?: number
  round_count?: number
  all_rounds_ok?: boolean
  runs: RunSummary[]
}

interface ConsistencySummary {
  total_runs: number
  multi_run_questions: number
  single_run_questions?: number
  green: number
  yellow: number
  red: number
  chain_pass_rate?: number | null
  questions_all_rounds_ok?: number
  question_count?: number
  weakest_chain?: { name?: string; pass_rate?: string } | null
}

interface ConsistencyData {
  questions: QuestionConsistency[]
  summary: ConsistencySummary
}

/* ===== span-tree 类型（抽屉按需加载） ===== */
interface SpanTreeNode {
  id: string
  observation_id?: string | null
  parent_id?: string | null
  name: string
  node_type: string | null
  aggregate_key?: string | null
  semantic_role: string | null
  latency_ms: number | null
  tokens: number | null
  depth: number | null
  step_index: number | null
  sibling_order?: number | null
  input?: string | null
  output?: string | null
  children?: SpanTreeNode[]
}

interface SpanTreeResponse {
  agent_run_id: string
  trace_id: string | null
  tree: SpanTreeNode[]
  nodes: SpanTreeNode[]
  node_count: number
  llm_call_count: number
  tool_call_count: number
}

/** 批量 span-tree 轻量响应（仅结构字段） */
interface SpanTreeBatchEntry {
  nodes: SpanTreeNode[]
  node_count: number
}
type SpanTreeBatchResponse = Record<string, SpanTreeBatchEntry>

interface Props {
  taskId: string
  processVerdict?: DimensionVerdictsPayload['process_consistency'] | null
}

/* ===== 辅助函数 ===== */
function flattenTree(nodes: SpanTreeNode[], depth = 0): SpanTreeNode[] {
  const out: SpanTreeNode[] = []
  for (const n of nodes) {
    out.push({ ...n, depth: depth })
    if (n.children?.length) out.push(...flattenTree(n.children, depth + 1))
  }
  return out
}

const NODE_TYPE_BADGE: Record<string, { label: string; cls: string }> = {
  generation: { label: '思考', cls: 'edp-badge-think' },
  llm: { label: '思考', cls: 'edp-badge-think' },
  tool: { label: '工具', cls: 'edp-badge-tool' },
  tool_call: { label: '工具', cls: 'edp-badge-tool' },
  tool_use: { label: '工具', cls: 'edp-badge-tool' },
  router: { label: '路由', cls: 'edp-badge-span' },
  span: { label: '步骤', cls: 'edp-badge-span' },
  event: { label: '事件', cls: 'edp-badge-event' },
}

function formatIoText(raw: string): string {
  const s = raw.trim()
  if (!s || !(s[0] === '{' || s[0] === '[')) return raw
  try { return JSON.stringify(JSON.parse(s), null, 2) } catch { /* continue */ }
  try {
    const fixed = s
      .replace(/'/g, '"')
      .replace(/\bTrue\b/g, 'true')
      .replace(/\bFalse\b/g, 'false')
      .replace(/\bNone\b/g, 'null')
    return JSON.stringify(JSON.parse(fixed), null, 2)
  } catch { /* continue */ }
  return raw
}

/** 必达点状态：✓ 命中 / ⚠ 降质 / ✗ 缺失 */
function itemState(item: CheckpointItem | undefined): 'ok' | 'warn' | 'miss' {
  if (!item) return 'miss'
  if (item.required) {
    if (item.passed && item.order_ok !== false) return 'ok'
    if (item.passed && item.order_ok === false) return 'warn'
    return 'miss'
  }
  // optional
  if (item.hit) return 'ok'
  return 'miss'
}

function itemIcon(state: 'ok' | 'warn' | 'miss'): string {
  return state === 'ok' ? '✓' : state === 'warn' ? '⚠' : '✗'
}

function itemColor(state: 'ok' | 'warn' | 'miss'): string {
  return state === 'ok' ? 'var(--edp-green)' : state === 'warn' ? 'var(--edp-amber)' : 'var(--edp-red)'
}

/** 计算某题某轮的链条得分 */
function chainScoreForRun(run: RunSummary | undefined): { score: number; hitCount: number; total: number } {
  if (!run) return { score: 0, hitCount: 0, total: 0 }
  const required = run.items.filter(i => i.required)
  const hit = required.filter(i => i.passed && i.order_ok !== false).length
  return { score: required.length > 0 ? hit / required.length : 0, hitCount: hit, total: required.length }
}

/* ===== trace 节点对齐（多轮 trace 链条对比） ===== */
/** 判断 aggregate_key 是否有效（非空、非 unknown、非 legacy:unknown） */
function isValidAggregateKey(key: string | null | undefined): boolean {
  if (!key) return false
  const k = key.trim()
  return k !== '' && k !== 'unknown' && k !== 'legacy:unknown'
}

/** 节点类型徽章信息 */
function nodeBadgeOf(node: SpanTreeNode): { label: string; cls: string } {
  return NODE_TYPE_BADGE[node.semantic_role || ''] || NODE_TYPE_BADGE[node.node_type || ''] || { label: node.node_type || '步骤', cls: 'edp-badge-span' }
}

interface TraceAlignment {
  /** 所有轮出现的 aggregate_key 并集（按首次出现顺序） */
  unionKeys: string[]
  /** 所有轮都有的交集（基准链） */
  interKeys: Set<string>
  /** 每轮的 aggregate_key -> 节点 映射 */
  perRunKeyMaps: Map<string, SpanTreeNode>[]
  /** 基准行各 key 的参考节点（取首个含该 key 的轮） */
  canonNodes: Map<string, SpanTreeNode>
  /** 所有轮是否仍在加载 span-tree */
  loading: boolean
  /** aggregate_key → 命中的必达点列表（用于 trace 节点徽章） */
  checkpointsByAggKey: Map<string, CheckpointItem[]>
  /** 全轮均未命中的必达点（用于基准行占位卡） */
  missingRequired: CheckpointItem[]
}

/**
 * 按题构建多轮 trace 节点对齐：
 * - 将每轮 span 树扁平化，取有效 aggregate_key 的节点构建映射
 * - 并集为列（按首次出现顺序），交集为基准链
 */
function buildTraceAlignment(
  runs: RunSummary[],
  runTrees: Record<string, SpanTreeNode[] | null>,
): TraceAlignment {
  const perRunKeyMaps: Map<string, SpanTreeNode>[] = runs.map(run => {
    const nodes = runTrees[run.agent_run_id] ?? []
    const m = new Map<string, SpanTreeNode>()
    for (const n of nodes) {
      const key = n.aggregate_key ?? null
      if (isValidAggregateKey(key) && !m.has(key!)) {
        m.set(key!, n)
      }
    }
    return m
  })

  const unionKeys: string[] = []
  const seen = new Set<string>()
  for (const m of perRunKeyMaps) {
    for (const key of m.keys()) {
      if (!seen.has(key)) {
        seen.add(key)
        unionKeys.push(key)
      }
    }
  }

  // 排除无 Trace 数据的空轮（keyMap 为空），避免它们把基准链清零
  const nonEmptyMaps = perRunKeyMaps.filter(m => m.size > 0)
  const interKeys = new Set(unionKeys.filter(key => nonEmptyMaps.every(m => m.has(key))))

  const canonNodes = new Map<string, SpanTreeNode>()
  for (const key of unionKeys) {
    for (const m of perRunKeyMaps) {
      const node = m.get(key)
      if (node) { canonNodes.set(key, node); break }
    }
  }

  // 桥接：构建 observation_id → aggregate_key 反向映射（每轮），用于关联必达点与 trace 节点
  const perRunObsToAggKey: Map<string, string>[] = runs.map(run => {
    const nodes = runTrees[run.agent_run_id] ?? []
    const m = new Map<string, string>()
    for (const n of nodes) {
      const obsId = n.observation_id || n.id
      const aggKey = n.aggregate_key
      if (obsId && isValidAggregateKey(aggKey) && !m.has(obsId)) {
        m.set(obsId, aggKey!)
      }
    }
    return m
  })

  // 桥接：aggregate_key → CheckpointItem[]（跨轮合并，去重）
  const checkpointsByAggKey = new Map<string, CheckpointItem[]>()
  for (let ri = 0; ri < runs.length; ri++) {
    const obsMap = perRunObsToAggKey[ri]
    if (!obsMap || obsMap.size === 0) continue
    for (const item of runs[ri].items) {
      const matchedIds = item.evidence?.matched_ids
      if (!matchedIds || matchedIds.length === 0) continue
      for (const obsId of matchedIds) {
        const aggKey = obsMap.get(obsId)
        if (!aggKey) continue
        if (!checkpointsByAggKey.has(aggKey)) {
          checkpointsByAggKey.set(aggKey, [])
        }
        const list = checkpointsByAggKey.get(aggKey)!
        if (!list.some(c => c.checkpoint_id === item.checkpoint_id)) {
          list.push(item)
        }
      }
    }
  }

  // 桥接：全轮均未命中的必达点（无 aggregate_key 映射且 hit 为 false）
  const missingRequired: CheckpointItem[] = []
  const seenMissing = new Set<string>()
  const linkedCpIds = new Set<string>()
  for (const list of checkpointsByAggKey.values()) {
    for (const c of list) linkedCpIds.add(c.checkpoint_id)
  }
  for (const run of runs) {
    for (const item of run.items) {
      if (!item.required) continue
      if (seenMissing.has(item.checkpoint_id)) continue
      if (linkedCpIds.has(item.checkpoint_id)) continue
      const matchedIds = item.evidence?.matched_ids
      if (!matchedIds || matchedIds.length === 0 || item.hit === false) {
        missingRequired.push(item)
        seenMissing.add(item.checkpoint_id)
      }
    }
  }

  return { unionKeys, interKeys, perRunKeyMaps, canonNodes, loading: false,
           checkpointsByAggKey, missingRequired }
}

/** 计算某轮在基准(交集)中的命中率 */
function traceScoreForRun(idx: number, alignment: TraceAlignment): number {
  if (alignment.interKeys.size === 0) return 0
  const m = alignment.perRunKeyMaps[idx]
  if (!m) return 0
  let hit = 0
  for (const k of alignment.interKeys) if (m.has(k)) hit++
  return hit / alignment.interKeys.size
}

/* ===== 抽屉子组件 ===== */
interface DrawerState {
  questionId: number
  runIndex: number
  run: RunSummary
  question: QuestionConsistency
}

function DrawerContent({ drawer, onClose }: { drawer: DrawerState; onClose: () => void }) {
  const [spanTree, setSpanTree] = useState<SpanTreeNode[] | null>(null)
  const [treeLoading, setTreeLoading] = useState(false)
  const { run, questionId, runIndex } = drawer

  useEffect(() => {
    if (!run.agent_run_id) { setSpanTree(null); return }
    setTreeLoading(true); setSpanTree(null)
    fetch(`${API_BASE}/runs/${run.agent_run_id}/span-tree`)
      .then(r => r.ok ? r.json() : null)
      .then((d: SpanTreeResponse | null) => { setSpanTree(d?.tree ?? null) })
      .catch(() => setSpanTree(null))
      .finally(() => setTreeLoading(false))
  }, [run.agent_run_id])

  const flatNodes = useMemo(() => spanTree ? flattenTree(spanTree) : [], [spanTree])
  const traceTotal = flatNodes.reduce((s, n) => s + (n.latency_ms || 0), 0)

  // 链条分析
  const requiredItems = run.items.filter(i => i.required)
  const cs = chainScoreForRun(run)

  return (
    <DrawerShell
      title={<>链条分析 · Q{questionId + 1} R{runIndex + 1}</>}
      onClose={onClose}
    >
      {/* trace 链条分析 */}
      <div className="edp-sect">
        <h4>🔗 trace 链条分析 <span className="edp-trace-total">一致性 {(cs.score * 100).toFixed(0)}% · {cs.hitCount}/{cs.total} 必达点命中</span></h4>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--edp-space-1)' }}>
          {requiredItems.map((item, idx) => {
            const state = itemState(item)
            const icon = itemIcon(state)
            const col = itemColor(state)
            return (
              <div key={item.checkpoint_id || idx} style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)', fontSize: 'var(--edp-font-base)', padding: '5px 8px', borderRadius: 5, background: 'var(--edp-bg)' }}>
                <span style={{ color: col, fontWeight: 700, width: 16, textAlign: 'center' }}>{icon}</span>
                <span style={{ fontWeight: 600 }}>{item.name}</span>
                {item.order_ok === false && <span style={{ fontSize: 'var(--edp-font-xs)', color: 'var(--edp-amber)', fontWeight: 600 }}>顺序异常</span>}
              </div>
            )
          })}
          {requiredItems.length === 0 && <p style={{ fontSize: 'var(--edp-font-md)', color: 'var(--edp-text3)' }}>无必达过程点</p>}
        </div>
      </div>

      {/* 全部过程点 */}
      <div className="edp-sect">
        <h4>全部过程点</h4>
        <table className="edp-mtab">
          <thead><tr><th>过程点</th><th>类型</th><th>命中</th><th>顺序</th></tr></thead>
          <tbody>
            {run.items.map((item, idx) => (
              <tr key={item.checkpoint_id || idx}>
                <td className="edp-mn">{item.name}</td>
                <td>{item.required ? '必达' : '可选'}</td>
                <td style={{ color: item.required ? (item.passed ? 'var(--edp-green)' : 'var(--edp-red)') : (item.hit ? 'var(--edp-green)' : 'var(--edp-text3)') }}>
                  {item.required ? (item.passed ? '✓' : '✗') : (item.hit ? '✓' : '○')}
                </td>
                <td>{item.order_ok === false ? '⚠ 异常' : item.order_ok ? '✓' : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Agent 推理轨迹 */}
      <div className="edp-sect">
        <h4>🧠 Agent 推理轨迹 · Q{questionId + 1} R{runIndex + 1}
          <span className="edp-trace-total">
            {treeLoading ? <Spinner size={16} label="加载中…" /> : spanTree ? `总时延 ${formatDuration(traceTotal)} · ${flatNodes.length}步` : '无 Trace 数据'}
          </span>
        </h4>
        {spanTree && flatNodes.length > 0 ? (
          flatNodes.map((node, i) => {
            const badge = NODE_TYPE_BADGE[node.semantic_role || ''] || NODE_TYPE_BADGE[node.node_type || ''] || { label: node.node_type || '步骤', cls: 'edp-badge-span' }
            return (
              <div key={node.id || i} className="edp-trace-step">
                <span className={`edp-badge ${badge.cls}`}>{badge.label}</span>
                <div className="edp-trace-body">
                  <div className="edp-trace-d">
                    <b style={{ marginLeft: (node.depth || 0) * 12 }}>{node.name || ('步骤' + (i + 1))}</b>
                    {node.latency_ms != null && <span className="edp-tdur">{formatDuration(node.latency_ms)}</span>}
                    {node.tokens != null && <span className="edp-tdur">{node.tokens} tok</span>}
                  </div>
                  {node.input && (
                    <details className="edp-trace-io">
                      <summary className="edp-io-lbl in">Input</summary>
                      <pre className="edp-io-pre in">{formatIoText(node.input)}</pre>
                    </details>
                  )}
                  {node.output && (
                    <details className="edp-trace-io">
                      <summary className="edp-io-lbl out">Output</summary>
                      <pre className="edp-io-pre out">{formatIoText(node.output)}</pre>
                    </details>
                  )}
                </div>
              </div>
            )
          })
        ) : treeLoading ? (
          <div style={{ padding: 'var(--edp-space-3)', display: 'flex', justifyContent: 'center' }}>
            <Spinner size={20} label="正在加载 Trace…" />
          </div>
        ) : (
          <p className="edp-empty" style={{ padding: 'var(--edp-space-3)' }}>
            该轮推理未关联 Trace 数据
          </p>
        )}
      </div>

      {/* 运行数据 */}
      <div className="edp-sect">
        <h4>运行数据</h4>
        <div className="edp-kv">
          <div><div className="edp-k">必达通过</div><div className="edp-v">{run.required_passed}/{run.required_total}</div></div>
          <div><div className="edp-k">可选命中</div><div className="edp-v">{run.optional_hit}/{run.optional_total}</div></div>
          <div><div className="edp-k">链条状态</div><div className="edp-v" style={{ color: cs.score === 1 ? 'var(--edp-green)' : cs.score > 0 ? 'var(--edp-amber)' : 'var(--edp-red)' }}>{cs.score === 1 ? '全通过' : cs.score > 0 ? '部分通过' : '未通过'}</div></div>
        </div>
      </div>
    </DrawerShell>
  )
}

/* ===== 主组件 ===== */
export function ProcessConsistencyView({ taskId, processVerdict }: Props) {
  const [data, setData] = useState<ConsistencyData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [drawer, setDrawer] = useState<DrawerState | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${API_BASE}/tasks/${taskId}/process-consistency`)
      if (!res.ok) throw new Error('加载过程一致性数据失败')
      const json: ConsistencyData = await res.json()
      setData(json)
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [taskId])

  useEffect(() => { void load() }, [load])

  // 批量懒加载：IntersectionObserver 监听 trace 对比区域可见性后发起单次批量请求
  const traceSectionRef = useRef<HTMLDivElement>(null)
  const traceRequestedRef = useRef(false)
  const [runTrees, setRunTrees] = useState<Record<string, SpanTreeNode[] | null>>({})
  useEffect(() => {
    if (!data) return
    const el = traceSectionRef.current
    if (!el) return
    // 收集所有 run_id，用于失败时填充 null 避免 UI 永久 spinner
    const allRunIds = new Set<string>()
    for (const q of data.questions) {
      for (const run of q.runs) allRunIds.add(run.agent_run_id)
    }
    if (allRunIds.size === 0) return

    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some(e => e.isIntersecting)) return
        if (traceRequestedRef.current) return
        traceRequestedRef.current = true
        fetch(`${API_BASE}/tasks/${taskId}/span-tree-batch`)
          .then(r => r.ok ? r.json() : {})
          .then((batch: SpanTreeBatchResponse) => {
            const trees: Record<string, SpanTreeNode[] | null> = {}
            for (const runId of allRunIds) {
              trees[runId] = batch[runId]?.nodes ?? null
            }
            setRunTrees(trees)
          })
          .catch(() => {
            const trees: Record<string, SpanTreeNode[] | null> = {}
            for (const runId of allRunIds) trees[runId] = null
            setRunTrees(trees)
          })
        observer.disconnect()
      },
      { rootMargin: '200px' },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [data, taskId])

  // 收集所有唯一的 checkpoint_id（跨所有题所有轮）
  const allCheckpoints = useMemo(() => {
    const seen = new Map<string, { id: string; name: string; required: boolean; order: number }>()
    for (const q of data?.questions ?? []) {
      for (const run of q.runs) {
        for (const item of run.items) {
          if (!seen.has(item.checkpoint_id)) {
            seen.set(item.checkpoint_id, {
              id: item.checkpoint_id,
              name: item.name,
              required: !!item.required,
              order: item.order ?? 999,
            })
          }
        }
      }
    }
    return [...seen.values()].sort((a, b) => {
      // required first, then by order
      if (a.required !== b.required) return a.required ? -1 : 1
      return a.order - b.order
    })
  }, [data])

  // 全局一致性指标计算
  const consistencyStats = useMemo(() => {
    if (!data) return null
    const s = data.summary
    const questions = data.questions

    // 必达点通过率
    let totalRequired = 0, passedRequired = 0
    let brokenRounds: { qid: number; r: number; score: number }[] = []
    let allScores: { qid: number; r: number; score: number }[] = []

    questions.forEach(q => {
      q.runs.forEach((run, ri) => {
        const cs = chainScoreForRun(run)
        totalRequired += cs.total
        passedRequired += cs.hitCount
        allScores.push({ qid: q.question_id, r: ri + 1, score: cs.score })
        if (cs.score < 1) brokenRounds.push({ qid: q.question_id, r: ri + 1, score: cs.score })
      })
    })

    const passRate = totalRequired > 0 ? Math.round((passedRequired / totalRequired) * 100) : 0
    const chainPassRate = s.chain_pass_rate != null ? s.chain_pass_rate : 0
    const avgConsistency = allScores.length > 0 ? allScores.reduce((sum, x) => sum + x.score, 0) / allScores.length : 0
    brokenRounds.sort((a, b) => a.score - b.score)
    const weakest = brokenRounds[0]

    return {
      passRate,
      passedRequired,
      totalRequired,
      chainPassRate,
      avgConsistency,
      brokenCount: brokenRounds.length,
      brokenRounds,
      weakest,
    }
  }, [data])

  // memoize：每 run 的 checkpoint_id → CheckpointItem 查找表（O(1) 替代 items.find）
  const checkpointMapsByRun = useMemo(() => {
    const m = new Map<string, Map<string, CheckpointItem>>()
    for (const q of data?.questions ?? []) {
      for (const run of q.runs) {
        const inner = new Map<string, CheckpointItem>()
        for (const item of run.items) inner.set(item.checkpoint_id, item)
        m.set(run.agent_run_id, inner)
      }
    }
    return m
  }, [data])

  // memoize：每题的 trace 对齐结果（避免渲染时重复计算）
  const traceAlignments = useMemo(() => {
    const m = new Map<number, TraceAlignment>()
    for (const q of data?.questions ?? []) {
      m.set(q.question_id, buildTraceAlignment(q.runs, runTrees))
    }
    return m
  }, [data, runTrees])

  if (loading) return <p style={{ padding: 'var(--edp-space-6)', fontSize: 'var(--edp-font-md)', color: 'var(--edp-text2)' }}>加载过程一致性…</p>
  if (error) return <p style={{ padding: 'var(--edp-space-6)', color: 'var(--edp-red)', fontSize: 'var(--edp-font-md)' }}>{error}</p>
  if (!data || data.questions.length === 0) {
    return (
      <div style={{ padding: 'var(--edp-space-6)' }}>
        <div className="edp-view-head">
          <h2>过程一致性</h2>
          <span className="edp-desc">必达点通过率 + 多轮 trace 链条对比</span>
        </div>
        <div style={{ padding: '14px 16px', background: 'var(--edp-bg)', border: '1px solid var(--edp-border-soft)', borderRadius: 'var(--edp-radius-sm)', fontSize: 'var(--edp-font-md)', color: 'var(--edp-text2)', lineHeight: 1.7 }}>
          <p style={{ margin: '0 0 8px' }}>未找到与本任务关联的 Agent Run 记录，无法生成过程一致性视图。</p>
          <p style={{ margin: 0, fontSize: 'var(--edp-font-base)', color: 'var(--edp-text3)' }}>历史任务若缺少 Run 物化，请确认 Trace 已同步；单轮任务的过程点合规请查看「质量指标」Tab。</p>
        </div>
      </div>
    )
  }

  const stats = consistencyStats!

  // 过程一致性结论
  const dimVerdict = processVerdict
  const verdictLabel = dimVerdict?.status === 'pass' ? '达标' : dimVerdict?.status === 'warn' ? '需关注' : dimVerdict?.status === 'fail' ? '未达标' : '—'
  const verdictColor = dimVerdict?.status === 'pass' ? 'var(--edp-green)' : dimVerdict?.status === 'warn' ? 'var(--edp-amber)' : dimVerdict?.status === 'fail' ? 'var(--edp-red)' : 'var(--edp-text3)'

  return (
    <div style={{ padding: 0 }}>
      {/* 视图标题 */}
      <div className="edp-view-head">
        <h2>过程一致性</h2>
        <span className="edp-desc">必达点通过率 + 多轮 trace 链条对比（真实 trace 步骤纵向对齐）</span>
      </div>

      {/* 结论条 */}
      {dimVerdict && dimVerdict.status !== 'na' && (
        <div className="edp-perf-verdict" style={{ borderLeftColor: verdictColor }}>
          <div className="edp-pv-badge" style={{ background: verdictColor }}>{verdictLabel}</div>
          <div className="edp-pv-body">
            <div className="edp-pv-t">过程一致性结论{dimVerdict.headline ? ' · 后端裁决' : ''}</div>
            <div className="edp-pv-d">{dimVerdict.headline || '过程一致性分析'}</div>
          </div>
          <div className="edp-pv-flags">
            {stats.brokenCount > 0 && <span className="edp-pv-flag" style={{ color: 'var(--edp-red)' }}>断链轮次 {stats.brokenCount}</span>}
            {stats.passRate < 85 && <span className="edp-pv-flag" style={{ color: 'var(--edp-amber)' }}>必达点通过率 {stats.passRate}%</span>}
            {stats.brokenCount === 0 && stats.passRate >= 85 && <span className="edp-pv-flag" style={{ color: 'var(--edp-green)' }}>各项均达标</span>}
          </div>
        </div>
      )}

      {/* KPI 行 */}
      <div className="edp-kpi-row">
        <div className="edp-kpi">
          <div className="edp-k">必达点通过率</div>
          <div className="edp-v" style={{ color: stats.passRate >= 85 ? 'var(--edp-green)' : stats.passRate >= 70 ? 'var(--edp-amber)' : 'var(--edp-red)' }}>{stats.passRate}%</div>
          <div className="edp-sub">{stats.passedRequired}/{stats.totalRequired} 点次</div>
        </div>
        <div className="edp-kpi">
          <div className="edp-k">链条一致性</div>
          <div className="edp-v" style={{ color: stats.avgConsistency >= 0.9 ? 'var(--edp-green)' : stats.avgConsistency >= 0.7 ? 'var(--edp-amber)' : 'var(--edp-red)' }}>{(stats.avgConsistency * 100).toFixed(0)}%</div>
          <div className="edp-sub">跨轮均匹配率</div>
        </div>
        <div className="edp-kpi">
          <div className="edp-k">断链轮次</div>
          <div className="edp-v" style={{ color: stats.brokenCount === 0 ? 'var(--edp-green)' : 'var(--edp-red)' }}>{stats.brokenCount}</div>
          <div className="edp-sub">{stats.brokenCount > 0 ? stats.brokenRounds.slice(0, 5).map(b => `Q${b.qid}R${b.r}`).join('、') : '全轮完整'}</div>
        </div>
        <div className="edp-kpi">
          <div className="edp-k">最弱链</div>
          <div className="edp-v" style={{ fontSize: 'var(--edp-font-lg)' }}>
            {stats.weakest ? `Q${stats.weakest.qid}·R${stats.weakest.r}` : '—'}
          </div>
          <div className="edp-sub">{stats.weakest ? `一致性 ${(stats.weakest.score * 100).toFixed(0)}%` : '全轮完整'}</div>
        </div>
      </div>

      {/* 过程点通过率汇总 */}
      <div className="edp-chart-card">
        <h3>过程点通过率汇总</h3>
        <table className="edp-mtab">
          <thead>
            <tr><th>过程点</th><th>类型</th><th>通过率</th><th>命中数</th></tr>
          </thead>
          <tbody>
            {allCheckpoints.map(cp => {
              let passCount = 0, totalCount = 0
              data.questions.forEach(q => {
                q.runs.forEach(run => {
                  const item = checkpointMapsByRun.get(run.agent_run_id)?.get(cp.id)
                  if (item) {
                    totalCount++
                    if (cp.required ? item.passed : item.hit) passCount++
                  }
                })
              })
              const passRate = totalCount > 0 ? Math.round((passCount / totalCount) * 100) : 0
              return (
                <tr key={cp.id}>
                  <td className="edp-mn">{cp.name}</td>
                  <td>{cp.required ? '必达' : '可选'}</td>
                  <td style={{ color: passRate >= 85 ? 'var(--edp-green)' : passRate >= 70 ? 'var(--edp-amber)' : 'var(--edp-red)' }}>{passRate}%</td>
                  <td>{passCount}/{totalCount}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* 多轮 trace 链条对比 */}
      <div className="edp-chart-card" ref={traceSectionRef}>
        <h3>
          多轮 trace 链条对比
          <Tooltip content={
            <div style={{ maxWidth: 280 }}>
              <div style={{ fontWeight: 700, marginBottom: 4 }}>{TRACE_CHAIN_COMPARE.title}</div>
              <div>{TRACE_CHAIN_COMPARE.body}</div>
              <div>{TRACE_CHAIN_COMPARE.baseline}</div>
              <div>{TRACE_CHAIN_COMPARE.optional}</div>
              <div>{TRACE_CHAIN_COMPARE.align}</div>
              <div>{TRACE_CHAIN_COMPARE.click}</div>
              <div style={{ marginTop: 4, color: 'var(--edp-text2)' }}>{TRACE_CHAIN_COMPARE.badge}</div>
              <div style={{ color: 'var(--edp-text2)' }}>{TRACE_CHAIN_COMPARE.placeholder}</div>
            </div>
          }>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--edp-text3)"
                 strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                 style={{ verticalAlign: 'middle', marginLeft: 'var(--edp-space-1)', cursor: 'help' }}>
              <circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>
            </svg>
          </Tooltip>
          <span className="edp-hint">基准链 = 所有轮共有的步骤；红 ✗ = 必达步骤缺失（扣分）；灰 · = 可选步骤缺失（不扣分）</span>
        </h3>
        {data.questions.map(q => {
          const alignment = traceAlignments.get(q.question_id) ?? buildTraceAlignment(q.runs, runTrees)
          const { unionKeys, interKeys, perRunKeyMaps, canonNodes, checkpointsByAggKey, missingRequired } = alignment
          const allPending = q.runs.length > 0 && q.runs.every(r => !(r.agent_run_id in runTrees))
          const someLoading = q.runs.some(r => !(r.agent_run_id in runTrees))
          const qScores = q.runs.map((_, ri) => traceScoreForRun(ri, alignment))
          const qAvg = qScores.length > 0 ? qScores.reduce((a, b) => a + b, 0) / qScores.length : 0
          const qCol = qAvg >= 0.9 ? 'var(--edp-green)' : qAvg >= 0.7 ? 'var(--edp-amber)' : 'var(--edp-red)'
          const qBg = qAvg >= 0.9 ? 'var(--edp-green-soft)' : qAvg >= 0.7 ? 'var(--edp-amber-soft)' : 'var(--edp-red-soft)'

          return (
            <div key={q.question_id} style={{ marginBottom: 18 }}>
              <div style={{ fontSize: 'var(--edp-font-base)', fontWeight: 600, color: 'var(--edp-primary)', marginBottom: 'var(--edp-space-4)', display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)' }}>
                Q{q.question_id + 1} · {q.run_count}轮
                {!allPending && unionKeys.length > 0 && (
                  <span style={{ fontSize: 'var(--edp-font-sm)', fontWeight: 700, color: qCol, background: qBg, padding: '2px 8px', borderRadius: 'var(--edp-radius-sm)' }}>
                    一致性 {(qAvg * 100).toFixed(0)}%{someLoading ? '…' : ''}
                  </span>
                )}
              </div>
              {allPending ? (
                <div style={{ padding: 'var(--edp-space-3)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                  <Spinner size={20} label="正在加载 trace 节点…" />
                </div>
              ) : unionKeys.length === 0 ? (
                <p style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text3)', padding: '8px 0' }}>该题各轮未关联可对齐的 trace 节点</p>
              ) : (
                <div className="edp-chain-cmp">
                  {/* 基准行：并集节点，交集高亮 */}
                  <div className="edp-chain-canon">
                    <div className="edp-chain-score" style={{ color: 'var(--edp-primary)' }} title={`基准链 ${interKeys.size} 个步骤(所有轮都出现) / 并集 ${unionKeys.length} 个步骤(任何轮出现)`}>{interKeys.size}/{unionKeys.length}</div>
                    <div className="edp-chain-lbl">基准链</div>
                    {unionKeys.map((key, mi, arr) => {
                      const node = canonNodes.get(key)
                      const isCommon = interKeys.has(key)
                      const badge = node ? nodeBadgeOf(node) : null
                      const cpItems = checkpointsByAggKey.get(key) || []
                      const cpTip = cpItems.map(c =>
                        `${c.name}（顺序${c.order ?? '—'}，${itemState(c) === 'ok' ? '已通过' : itemState(c) === 'warn' ? '顺序异常' : '未通过'}）`
                      ).join('；')
                      return (
                        <div key={key} style={{ display: 'flex', alignItems: 'center' }}>
                          <div className="edp-chain-node canon" title={node?.name || key} style={isCommon ? undefined : { opacity: 0.45, borderStyle: 'dashed' }}>
                            {badge && <span className={`edp-chain-badge ${badge.cls}`}>{badge.label}</span>}
                            <span className="edp-chain-step-name">{node?.name || key}</span>
                            {cpItems.length > 0 && (
                              <Tooltip content={cpTip}>
                                <span className="edp-chain-cp-badge" style={{ background: 'var(--edp-red)', color: 'var(--edp-card)' }}>
                                  必{cpItems.length > 1 ? `×${cpItems.length}` : ''}
                                </span>
                              </Tooltip>
                            )}
                          </div>
                          {mi < arr.length - 1 && <span className="edp-chain-arrow">→</span>}
                        </div>
                      )
                    })}
                    {missingRequired.length > 0 && (
                      <>
                        <span className="edp-chain-arrow miss" style={{ opacity: 0.4 }}>⋮</span>
                        {missingRequired.map(cp => (
                          <Tooltip key={cp.checkpoint_id} content={`必达点「${cp.name}」在所有轮次中均未命中`}>
                            <div className="edp-chain-node miss" style={{ minWidth: 80 }}>
                              <span className="edp-chain-miss-icon">✗</span>
                              <span className="edp-chain-step-name" style={{ fontSize: 'var(--edp-font-xs)' }}>{cp.name}</span>
                            </div>
                          </Tooltip>
                        ))}
                      </>
                    )}
                  </div>
                  {/* 各轮实际 trace 节点 */}
                  {q.runs.map((run, ri) => {
                    const runLoaded = run.agent_run_id in runTrees
                    if (!runLoaded) {
                      return (
                        <div key={ri} className="edp-chain-lane" style={{ justifyContent: 'flex-start', padding: 'var(--edp-space-1) 0' }}>
                          <div className="edp-chain-lbl">R{ri + 1}</div>
                          <Spinner size={16} />
                        </div>
                      )
                    }
                    const score = qScores[ri]
                    const hitCount = [...interKeys].filter(k => perRunKeyMaps[ri].has(k)).length
                    const col = score === 1 ? 'var(--edp-green)' : score >= 0.8 ? 'var(--edp-amber)' : 'var(--edp-red)'
                    const keyMap = perRunKeyMaps[ri]
                    return (
                      <div key={ri} className="edp-chain-lane" onClick={() => setDrawer({ questionId: q.question_id, runIndex: ri, run, question: q })}>
                        <div className="edp-chain-score" style={{ color: col }} title={`命中 ${hitCount}/${interKeys.size} 个基准链步骤`}>
                          {(score * 100).toFixed(0)}%<span style={{ fontSize: 'var(--edp-font-xs)', opacity: 0.65, marginLeft: 3 }}>{hitCount}/{interKeys.size}</span>
                        </div>
                        <div className="edp-chain-lbl">R{ri + 1}</div>
                        {unionKeys.map((key, mi, arr) => {
                          const node = keyMap.get(key)
                          const badge = node ? nodeBadgeOf(node) : null
                          const hasNext = mi < arr.length - 1
                          const nextNode = hasNext ? keyMap.get(arr[mi + 1]) : null
                          const isBase = interKeys.has(key)
                          const missTitle = !node
                            ? (isBase ? '基准链步骤缺失，计入扣分' : '非基准步骤(仅部分轮出现)，不影响得分')
                            : (node?.name || key)
                          const cpItems = checkpointsByAggKey.get(key) || []
                          const cpTip = cpItems.map(c =>
                            `${c.name}（顺序${c.order ?? '—'}，${itemState(c) === 'ok' ? '已通过' : itemState(c) === 'warn' ? '顺序异常' : '未通过'}）`
                          ).join('；')
                          return (
                            <div key={key} style={{ display: 'flex', alignItems: 'center' }}>
                              <div
                                className={`edp-chain-node ${node ? 'hit' : 'miss'}`}
                                title={missTitle}
                                style={!node && !isBase ? { opacity: 0.4, borderStyle: 'dashed', color: 'var(--edp-text3)' } : undefined}
                              >
                                {node ? (
                                  <>
                                    {badge && <span className={`edp-chain-badge ${badge.cls}`}>{badge.label}</span>}
                                    <span className="edp-chain-step-name">{node.name}</span>
                                    {node.latency_ms != null && node.latency_ms > 0 && <span className="edp-chain-dur">{formatDuration(node.latency_ms)}</span>}
                                    {cpItems.length > 0 && (
                                      <Tooltip content={cpTip}>
                                        <span className="edp-chain-cp-badge" style={{ background: 'var(--edp-red)', color: 'var(--edp-card)' }}>
                                          必{cpItems.length > 1 ? `×${cpItems.length}` : ''}
                                        </span>
                                      </Tooltip>
                                    )}
                                  </>
                                ) : (
                                  <span className="edp-chain-miss-icon" style={!isBase ? { color: 'var(--edp-text3)' } : undefined}>{isBase ? '✗' : '·'}</span>
                                )}
                              </div>
                              {hasNext && (
                                <span className={`edp-chain-arrow ${node && nextNode ? '' : 'miss'}`}>
                                  {node && nextNode ? '→' : '⤫'}
                                </span>
                              )}
                            </div>
                          )
                        })}
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* 抽屉 */}
      {drawer && (
        <DrawerContent
          drawer={drawer}
          onClose={() => setDrawer(null)}
        />
      )}
    </div>
  )
}
