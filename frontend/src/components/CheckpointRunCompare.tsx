import { useEffect, useMemo, useState } from 'react'
import { RunCompareContent } from './RunComparePanel'
import { createRunComparison } from '../utils/runCompare'
import { LangfuseTraceBlockedPanel } from './LangfuseObservabilityNotice'

export interface CheckpointRunRow {
  agent_run_id: string
  run_index?: number
  question_id?: number | null
  agent_id?: string | null
  agent_name?: string | null
  checkpoint_total?: number
  required_passed?: number
  required_total?: number
}

interface Props {
  runs: CheckpointRunRow[]
  taskAgentId?: string | null
  /** false 时 L2 硬阻断 Span 树对比 */
  langfuseTraceReady?: boolean
}

function runLabel(run: CheckpointRunRow): string {
  const parts: string[] = []
  if (run.run_index != null) {
    parts.push(`第 ${run.run_index + 1} 轮`)
  }
  if (run.required_total != null && run.required_total > 0) {
    parts.push(`必达 ${run.required_passed ?? 0}/${run.required_total}`)
  }
  const name = run.agent_name?.trim() || run.agent_id?.slice(0, 8) || run.agent_run_id.slice(-6)
  parts.push(name)
  return parts.join(' · ')
}

function runIndexLabel(run: CheckpointRunRow | undefined, fallback = 0): string {
  return `第 ${(run?.run_index ?? fallback) + 1} 轮`
}

export function CheckpointRunCompare({
  runs,
  taskAgentId,
  langfuseTraceReady = true,
}: Props) {
  const groups = useMemo(() => {
    const map = new Map<number, CheckpointRunRow[]>()
    for (const run of runs) {
      const q = run.question_id ?? 0
      if (!map.has(q)) map.set(q, [])
      map.get(q)!.push(run)
    }
    return [...map.entries()]
      .filter(([, list]) => list.length >= 2)
      .sort(([a], [b]) => a - b)
  }, [runs])

  const [questionId, setQuestionId] = useState<number | null>(null)
  const [leftRunId, setLeftRunId] = useState('')
  const [rightRunId, setRightRunId] = useState('')
  const [spanCompareId, setSpanCompareId] = useState<string | null>(null)
  const [spanBusy, setSpanBusy] = useState(false)
  const [spanError, setSpanError] = useState<string | null>(null)

  const activeGroup = useMemo(
    () => groups.find(([q]) => q === questionId)?.[1] ?? [],
    [groups, questionId],
  )

  const leftRun = useMemo(
    () => activeGroup.find(r => r.agent_run_id === leftRunId),
    [activeGroup, leftRunId],
  )
  const rightRun = useMemo(
    () => activeGroup.find(r => r.agent_run_id === rightRunId),
    [activeGroup, rightRunId],
  )

  useEffect(() => {
    if (groups.length === 0) return
    const [q, list] = groups[0]
    setQuestionId(q)
    const baseline =
      list.find(r => taskAgentId && r.agent_id === taskAgentId) ?? list[0]
    const other = list.find(r => r.agent_run_id !== baseline.agent_run_id) ?? list[1]
    setLeftRunId(baseline.agent_run_id)
    setRightRunId(other?.agent_run_id ?? '')
  }, [groups, taskAgentId])

  useEffect(() => {
    if (!activeGroup.length) return
    if (!activeGroup.some(r => r.agent_run_id === leftRunId)) {
      setLeftRunId(activeGroup[0].agent_run_id)
    }
    if (!activeGroup.some(r => r.agent_run_id === rightRunId)) {
      const other = activeGroup.find(r => r.agent_run_id !== activeGroup[0].agent_run_id)
      setRightRunId(other?.agent_run_id ?? '')
    }
  }, [questionId, activeGroup, leftRunId, rightRunId])

  const canCompare = Boolean(leftRunId && rightRunId && leftRunId !== rightRunId)

  useEffect(() => {
    if (!langfuseTraceReady || !canCompare) {
      setSpanCompareId(null)
      setSpanError(null)
      setSpanBusy(false)
      return
    }

    setSpanBusy(true)
    setSpanError(null)
    const timer = window.setTimeout(() => {
      void createRunComparison(leftRunId, rightRunId, 'checkpoint_panel')
        .then(id => {
          setSpanCompareId(id)
          setSpanBusy(false)
        })
        .catch(e => {
          setSpanCompareId(null)
          setSpanError(e instanceof Error ? e.message : 'Span 树对比失败')
          setSpanBusy(false)
        })
    }, 300)

    return () => window.clearTimeout(timer)
  }, [langfuseTraceReady, canCompare, leftRunId, rightRunId])

  if (groups.length === 0) return null

  return (
    <div style={{ fontSize: 12 }}>
      <p style={{ margin: '0 0 12px', fontSize: 11, color: '#888' }}>
        对比两条 Run 的完整 Span 拓扑与过程点下钻。多轮性能波动见「运行效能 · 稳定性」。
      </p>
      {!langfuseTraceReady && (
        <div style={{ marginBottom: 12 }}>
          <LangfuseTraceBlockedPanel compact title="推理链对比不可用" />
        </div>
      )}
      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: 10,
          alignItems: 'center',
          marginBottom: 16,
        }}
      >
        {groups.length > 1 && (
          <label>
            题目
            <select
              value={questionId ?? groups[0][0]}
              onChange={e => setQuestionId(Number(e.target.value))}
              style={{ marginLeft: 6, fontSize: 12 }}
            >
              {groups.map(([q]) => (
                <option key={q} value={q}>
                  题 #{q + 1}
                </option>
              ))}
            </select>
          </label>
        )}
        <label>
          基线 Run
          <select
            value={leftRunId}
            onChange={e => setLeftRunId(e.target.value)}
            style={{ marginLeft: 6, fontSize: 12, maxWidth: 200 }}
          >
            {activeGroup.map(r => (
              <option key={r.agent_run_id} value={r.agent_run_id}>
                {runLabel(r)}
              </option>
            ))}
          </select>
        </label>
        <label>
          对比 Run
          <select
            value={rightRunId}
            onChange={e => setRightRunId(e.target.value)}
            style={{ marginLeft: 6, fontSize: 12, maxWidth: 200 }}
          >
            {activeGroup
              .filter(r => r.agent_run_id !== leftRunId)
              .map(r => (
                <option key={r.agent_run_id} value={r.agent_run_id}>
                  {runLabel(r)}
                </option>
              ))}
          </select>
        </label>
      </div>

      {spanError && (
        <p style={{ color: '#dc3545', margin: '0 0 12px', fontSize: 12 }}>{spanError}</p>
      )}

      {langfuseTraceReady && canCompare && (
        <div
          style={{
            maxHeight: 'min(72vh, 640px)',
            overflow: 'auto',
            border: '1px solid #e8eaf0',
            borderRadius: 8,
            padding: 14,
            background: '#fafbff',
          }}
        >
          {spanBusy && !spanCompareId ? (
            <p style={{ fontSize: 13, color: '#666', margin: 0 }}>加载 Span 树对比…</p>
          ) : spanCompareId ? (
            <RunCompareContent
              comparisonId={spanCompareId}
              embedded
              leftColumnTitle={`基线 · ${runIndexLabel(leftRun)}`}
              rightColumnTitle={`对比 · ${runIndexLabel(rightRun)}`}
            />
          ) : null}
        </div>
      )}
    </div>
  )
}
