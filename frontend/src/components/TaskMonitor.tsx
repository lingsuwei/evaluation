import { useState, useEffect, useRef, useCallback } from 'react'
import { EvalDetailModal } from './EvalDetailModal'
import type { ScenarioMatrixPayload, ScenarioMatrixQuestion, ScenarioMatrixCell, ScenarioStepCell } from './ScenarioMatrixView'

const API_BASE = '/api'

function safeParseSSE(e: MessageEvent, label: string): any | null {
  try {
    return JSON.parse(e.data)
  } catch (err) {
    console.error(`SSE ${label} parse error:`, err)
    return null
  }
}

export interface CotLine {
  id: number
  questionId: number
  model: string
  metric: string
  text: string
  type: 'cot' | 'result' | 'error' | 'inference' | 'phase' | 'heartbeat'
  scriptEval?: boolean
  scriptName?: string | null
}

const PHASE_LABELS: Record<string, string> = {
  started: '任务已启动',
  inference: 'Agent 推理中',
  inference_done: '推理完成',
  runtime: '运行效能统计',
  runtime_done: '运行效能已记录',
  judge: 'Judge 评估中',
  judge_done: 'Judge 完成',
}

const METRIC_LABELS: Record<string, string> = {
  faithfulness: '忠诚度',
  answer_relevancy: '回答相关性',
  correctness: '正确性',
}

function statusBadgeClass(status: string): string {
  const map: Record<string, string> = {
    pending: 'status-pending',
    processing: 'status-processing',
    completed: 'status-completed',
    failed: 'status-failed',
    paused: 'status-paused',
  }
  return map[status] || 'status-pending'
}

function metricLabel(name: string) {
  return METRIC_LABELS[name] || name
}

interface Props {
  taskId: string
  onComplete: () => void
  cotLines: CotLine[]
  setCotLines: React.Dispatch<React.SetStateAction<CotLine[]>>
  onViewPartialResults?: () => void
}

export function TaskMonitor({ taskId, onComplete, cotLines, setCotLines, onViewPartialResults }: Props) {
  const [status, setStatus] = useState<string>('pending')
  const [progress, setProgress] = useState<string>('0/0')
  const [error, setError] = useState<string | null>(null)
  const [expandedCot, setExpandedCot] = useState<number | null>(null)
  const [showPauseConfirm, setShowPauseConfirm] = useState(false)
  const [phaseLabel, setPhaseLabel] = useState<string | null>(null)
  const [taskPhaseInfo, setTaskPhaseInfo] = useState<string | null>(null)
  const [sseNotice, setSseNotice] = useState<string | null>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  const [completedResultCount, setCompletedResultCount] = useState(0)
  const [scenarioLines, setScenarioLines] = useState<CotLine[]>([])
  const [scenarioStatus, setScenarioStatus] = useState<'idle' | 'running' | 'done' | 'error' | 'skipped'>('idle')
  const [scenarioProgress, setScenarioProgress] = useState<{ current: number; total: number }>({ current: 0, total: 0 })
  const [qualityProgress, setQualityProgress] = useState<{ current: number; total: number }>({ current: 0, total: 0 })
  const [scenarioQuestionId, setScenarioQuestionId] = useState<number>(-1)
  const [scenarioStepName, setScenarioStepName] = useState<string>('')
  const [capabilities, setCapabilities] = useState<string[]>([])
  const cotPanelRef = useRef<HTMLDivElement>(null)
  // 根据传入的 cotLines 初始化 lineId，确保新增行的 ID 不会重复
  const lineIdRef = useRef<number>(0)
  const syncedResultKeysRef = useRef<Set<string>>(new Set())
  // refs 用于轮询 effect 中读取最新值，避免过期闭包
  const scenarioStatusRef = useRef(scenarioStatus)
  const statusRef = useRef(status)
  const phaseLabelRef = useRef(phaseLabel)
  scenarioStatusRef.current = scenarioStatus
  statusRef.current = status
  phaseLabelRef.current = phaseLabel

  useEffect(() => {
    if (cotLines.length > 0) {
      lineIdRef.current = Math.max(...cotLines.map(l => l.id)) + 1
    } else {
      lineIdRef.current = 0
    }
  }, [cotLines])
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const eventSourceRef = useRef<EventSource | null>(null)
  const sseRetryRef = useRef(0)

  const appendLine = useCallback(
    (line: Omit<CotLine, 'id'>) => {
      setCotLines(prev => [...prev, { ...line, id: lineIdRef.current++ }])
    },
    [setCotLines],
  )

  const formatPhaseLabel = (data: {
    phase?: string
    question_id?: number
    metric?: string
    run_index?: number
  }) => {
    const base = PHASE_LABELS[data.phase || ''] || data.phase || '处理中'
    const parts = [base]
    if (data.question_id != null && data.question_id >= 0) {
      parts.push(`第 ${data.question_id + 1} 题`)
    }
    if (data.run_index != null && data.run_index > 0) {
      parts.push(`第 ${data.run_index + 1} 轮`)
    }
    if (data.metric) {
      parts.push(metricLabel(data.metric))
    }
    return parts.join(' · ')
  }

  // 轮询获取进度
  useEffect(() => {
    const pollProgress = async () => {
      try {
        const res = await fetch(`${API_BASE}/result/${taskId}?partial=1`)
        const data = await res.json()

        if (data.progress) {
          setProgress(data.progress)
          // 不再从总进度推算质量指标进度，使用 SSE 独立字段
        }
        setStatus(data.status)
        if (Array.isArray(data.capabilities)) {
          setCapabilities(data.capabilities)
        }
        if (data.partial && Array.isArray(data.details)) {
          setCompletedResultCount(data.details.length)

          // 补充已完成的指标结果到日志（防止 SSE 连接建立前事件丢失）
          if (data.status === 'processing' || data.status === 'completed') {
            const newLines: Omit<CotLine, 'id'>[] = []
            let totalQualityResults = 0
            let latestDetail: any = null
            data.details.forEach((detail: any) => {
              const qId = detail.question_id ?? -1
              const metrics = detail.metrics || []
              metrics.forEach((m: any) => {
                totalQualityResults++
                latestDetail = { qId, metric: m.name }
                const key = `${qId}_${m.name}`
                if (!syncedResultKeysRef.current.has(key)) {
                  syncedResultKeysRef.current.add(key)
                  newLines.push({
                    questionId: qId,
                    model: detail.model || 'unknown',
                    metric: m.name,
                    text: `得分: ${(m.score ?? 0).toFixed(2)} ${m.passed ? '✓ 通过' : '✗ 未通过'}`,
                    type: 'result',
                  })
                }
              })
            })
            if (newLines.length > 0) {
              newLines.forEach(line => appendLine(line))
            }
            // 用已完成结果总数更新质量指标进度，确保总数被初始化
            if (totalQualityResults > 0) {
              setQualityProgress(prev => {
                // 如果质量总数为0或小于当前已完成数量，说明后端未推送过
                // 从 taskPhaseInfo 中的 quality_total 或使用当前已完成数推算
                const total = prev.total && prev.total >= totalQualityResults
                  ? prev.total
                  : totalQualityResults
                return {
                  current: Math.max(prev.current, totalQualityResults),
                  total,
                }
              })
            }
            // 同步阶段标签（防止 SSE judge_start 事件丢失导致 phaseLabel 卡住）
            if (latestDetail && (data.status === 'processing' || !phaseLabelRef.current)) {
              const label = `Judge 完成 · 第 ${(latestDetail.qId ?? 0) + 1} 题 · ${metricLabel(latestDetail.metric)}`
              // 仅当 phaseLabel 为空或卡在之前的指标时更新
              setPhaseLabel(prev => {
                if (!prev) return label
                // 如果 phaseLabel 已显示某个后续阶段的指标，不覆盖
                // 这里用 latestDetail 推断最新进度
                return label
              })
            }
          }
        }

        // 对于已完成或失败的任务，停止轮询并尝试加载场景数据
        if (data.status === 'completed' || data.status === 'failed') {
          if (pollingRef.current) {
            clearInterval(pollingRef.current)
            pollingRef.current = null
          }

          // 如果场景状态仍为idle且任务已完成，尝试从API加载场景数据
          if (scenarioStatusRef.current === 'idle' || scenarioStatusRef.current === 'running') {
            try {
              const scenarioRes = await fetch(`${API_BASE}/scenario-eval/tasks/${taskId}/matrix`)
              if (scenarioRes.ok) {
                const scenarioData = await scenarioRes.json()
                // 如果有场景数据，构建场景行并显示
                if (scenarioData.questions && scenarioData.questions.length > 0) {
                  const lines: CotLine[] = []
                  let stepIndex = 0
                  let totalSteps = 0

                  // 计算总步骤数
                  const questions = scenarioData.questions as ScenarioMatrixQuestion[]
                  questions.forEach((q: ScenarioMatrixQuestion) => {
                    Object.values(q.cells).forEach((cell: ScenarioMatrixCell | null | undefined) => {
                      if (cell?.steps) {
                        totalSteps += cell.steps.length
                      }
                    })
                  })

                  lines.push({
                    id: lineIdRef.current++,
                    questionId: -1,
                    model: 'system',
                    metric: 'scenario',
                    text: `场景交付评估完成 · 共 ${questions.length} 个运行 · ${totalSteps} 个子步骤`,
                    type: 'phase',
                  })

                  // 添加每个问题的步骤
                  questions.forEach((q: ScenarioMatrixQuestion, qIdx: number) => {
                    const firstCell: ScenarioMatrixCell | null | undefined = Object.values(q.cells)[0]
                    if (firstCell?.steps) {
                      lines.push({
                        id: lineIdRef.current++,
                        questionId: q.question_id ?? -1,
                        model: 'system',
                        metric: 'scenario',
                        text: `[${qIdx + 1}/${questions.length}] 第 ${(q.question_id ?? 0) + 1} 题评测完成 · 综合得分: ${(firstCell.composite_score || 0).toFixed(2)} · ${firstCell.steps.length} 个步骤`,
                        type: 'result',
                      })

                      firstCell.steps.forEach((step: ScenarioStepCell) => {
                        stepIndex++
                        lines.push({
                          id: lineIdRef.current++,
                          questionId: q.question_id ?? -1,
                          model: 'system',
                          metric: 'scenario',
                          text: `步骤 ${stepIndex}/${totalSteps}: ${step.name || step.step_key} — 得分: ${step.score.toFixed(2)} ${step.passed ? '✓ 通过' : '✗ 未通过'}`,
                          type: step.passed ? 'result' : 'error',
                        })
                      })
                    }
                  })

                  setScenarioLines(lines)
                  setScenarioStatus('done')
                  setScenarioProgress({ current: totalSteps, total: totalSteps })
                }
              }
            } catch (e) {
              console.error('Failed to load scenario data:', e)
            }
          }
        }
      } catch (e) {
        console.error('Progress polling error:', e)
      }
    }

    // 初始执行一次
    pollProgress()

    // 持续轮询（包括暂停状态，以便检测恢复状态）
    pollingRef.current = setInterval(pollProgress, 1000)

    return () => {
      if (pollingRef.current) {
        clearInterval(pollingRef.current)
        pollingRef.current = null
      }
    }
  }, [taskId])

  useEffect(() => {
    if (status === 'completed' || status === 'failed') {
      return
    }

    let disposed = false

    const connect = () => {
      if (disposed) return
      if (eventSourceRef.current) {
        eventSourceRef.current.close()
      }

      const eventSource = new EventSource(`${API_BASE}/stream/${taskId}`)
      eventSourceRef.current = eventSource

      eventSource.addEventListener('open', () => {
        sseRetryRef.current = 0
        setSseNotice(null)
      })

      eventSource.addEventListener('task_phase', (e) => {
        const data = safeParseSSE(e, 'task_phase')
        if (data == null) return
        if (data.phase === 'started') {
          const info = `共 ${data.question_count} 题 · ${data.metrics_count} 个指标 · 总步数 ${data.progress_total}`
          setTaskPhaseInfo(info)
          // 使用后端推送的独立进度总数初始化
          if (data.quality_total != null) {
            setQualityProgress(prev => ({ current: prev.current, total: data.quality_total }))
          }
          // 场景交付进度不由 task_phase 初始化（值可能是 question_count 而非子步骤数），
          // 等后续 scenario_eval_start / process_eval 推送真实值
          appendLine({
            questionId: -1,
            model: 'system',
            metric: 'phase',
            text: `任务已启动（${data.mode === 'offline' ? '离线' : '在线'}）${info}`,
            type: 'phase',
          })
        }
      })

      eventSource.addEventListener('inference_start', (e) => {
        const data = safeParseSSE(e, 'inference_start')
        if (data == null) return
        setPhaseLabel(formatPhaseLabel({ phase: 'inference', question_id: data.question_id, run_index: data.run_index }))
        appendLine({
          questionId: data.question_id,
          model: 'agent',
          metric: 'inference',
          text: `开始推理：${data.question || ''}`,
          type: 'inference',
        })
      })

      eventSource.addEventListener('inference_complete', (e) => {
        const data = safeParseSSE(e, 'inference_complete')
        if (data == null) return
        appendLine({
          questionId: data.question_id,
          model: 'agent',
          metric: 'inference',
          text: `推理完成${data.answer ? `：${data.answer}` : ''}`,
          type: 'inference',
        })
      })

      eventSource.addEventListener('inference_error', (e) => {
        const data = safeParseSSE(e, 'inference_error')
        if (data == null) return
        appendLine({
          questionId: data.question_id,
          model: 'agent',
          metric: 'inference',
          text: `推理失败：${data.error}`,
          type: 'error',
        })
      })

      eventSource.addEventListener('inference_heartbeat', (e) => {
        const data = safeParseSSE(e, 'inference_heartbeat')
        if (data == null) return
        setPhaseLabel(`Agent 响应中 · 第 ${data.question_id + 1} 题 · 已等待 ${data.elapsed_s}s`)
        appendLine({
          questionId: data.question_id,
          model: 'agent',
          metric: 'inference',
          text: `仍在等待 Agent 响应（${data.elapsed_s}s）`,
          type: 'heartbeat',
        })
      })

      eventSource.addEventListener('judge_start', (e) => {
        const data = safeParseSSE(e, 'judge_start')
        if (data == null) return
        setPhaseLabel(formatPhaseLabel({ phase: 'judge', question_id: data.question_id, metric: data.metric }))
      })

      eventSource.addEventListener('cot_update', (e) => {
        const data = safeParseSSE(e, 'cot_update')
        if (data == null) return
        appendLine({
          questionId: data.question_id,
          model: data.model,
          metric: data.metric,
          scriptEval: data.script_eval || false,
          scriptName: data.script_name || null,
          text: data.cot_chunk,
          type: 'cot',
        })
      })

      eventSource.addEventListener('metric_result', (e) => {
        const data = safeParseSSE(e, 'metric_result')
        if (data == null) return
        // 标记已通过 SSE 接收，避免轮询重复添加
        const key = `${data.question_id ?? -1}_${data.metric}`
        syncedResultKeysRef.current.add(key)
        const isScriptEval = data.script_eval === true
        appendLine({
          questionId: data.question_id,
          model: data.model,
          metric: data.metric,
          scriptEval: isScriptEval,
          scriptName: data.script_name || null,
          text: isScriptEval
            ? `🐍 脚本评估得分: ${data.score.toFixed(2)} ${data.passed ? '✓ 通过' : '✗ 未通过'}`
            : `得分: ${data.score.toFixed(2)} ${data.passed ? '✓ 通过' : '✗ 未通过'}`,
          type: 'result',
        })
      })

      eventSource.addEventListener('metric_error', (e) => {
        const data = safeParseSSE(e, 'metric_error')
        if (data == null) return
        appendLine({
          questionId: data.question_id,
          model: data.model,
          metric: data.metric,
          text: data.error,
          type: 'error',
        })
      })

      eventSource.addEventListener('task_complete', () => {
        setStatus('completed')
        eventSource.close()
        onComplete()
      })

      eventSource.addEventListener('summary_generated', () => {
        appendLine({
          questionId: -1,
          model: 'system',
          metric: 'summary',
          text: '评测总结已生成，请在"评估结果"页面查看',
          type: 'cot',
        })
      })

      eventSource.addEventListener('task_error', (e) => {
        const data = safeParseSSE(e, 'task_error')
        if (data == null) return
        setStatus('failed')
        setError(data.error)
        eventSource.close()
      })

      // 任务取消：可能由当前用户或其它客户端触发，需停止监控并标记为已取消
      eventSource.addEventListener('task_cancelled', () => {
        setStatus('failed')
        setError('任务已取消')
        eventSource.close()
      })

      // 场景交付（过程评估）SSE 事件
      eventSource.addEventListener('scenario_eval_start', (e) => {
        const data = safeParseSSE(e, 'scenario_eval_start')
        if (data == null) return
        setScenarioStatus('running')
        setScenarioProgress({ current: 0, total: data.total_steps || 0 })
        setScenarioLines(prev => [...prev, {
          id: prev.length,
          questionId: -1,
          model: 'system',
          metric: 'scenario',
          text: `场景交付评估开始 · 共 ${data.total_runs} 个运行 · ${data.total_steps} 个子步骤`,
          type: 'phase',
        }])
      })

      eventSource.addEventListener('scenario_run_start', (e) => {
        const data = safeParseSSE(e, 'scenario_run_start')
        if (data == null) return
        setScenarioQuestionId(data.question_id ?? -1)
        setScenarioLines(prev => [...prev, {
          id: prev.length,
          questionId: data.question_id ?? -1,
          model: 'system',
          metric: 'scenario',
          text: `[${data.run_index}/${data.total_runs}] 开始评测第 ${data.question_id + 1} 题 · 场景: ${data.scenario_key}`,
          type: 'phase',
        }])
      })

      eventSource.addEventListener('scenario_step_done', (e) => {
        const data = safeParseSSE(e, 'scenario_step_done')
        if (data == null) return
        setScenarioProgress({ current: data.step_index, total: data.total_steps })
        if (data.question_id != null) setScenarioQuestionId(data.question_id)
        if (data.step_name) setScenarioStepName(data.step_name)
        const scoreText = typeof data.score === 'number' ? data.score.toFixed(2) : '—'
        const passIcon = data.passed ? '✓ 通过' : '✗ 未通过'
        setScenarioLines(prev => [...prev, {
          id: prev.length,
          questionId: data.question_id ?? -1,
          model: 'system',
          metric: 'scenario',
          text: `步骤 ${data.step_index}/${data.total_steps}: ${data.step_name || data.step_key} — 得分: ${scoreText} ${passIcon}`,
          type: data.passed ? 'result' : 'error',
        }])
      })

      eventSource.addEventListener('scenario_run_done', (e) => {
        const data = safeParseSSE(e, 'scenario_run_done')
        if (data == null) return
        setScenarioLines(prev => [...prev, {
          id: prev.length,
          questionId: data.question_id ?? -1,
          model: 'system',
          metric: 'scenario',
          text: `[${data.run_index}/${data.total_runs}] 第 ${data.question_id + 1} 题评测完成 · 综合得分: ${(data.composite_score || 0).toFixed(2)} · ${data.steps_count} 个步骤`,
          type: 'result',
        }])
      })

      eventSource.addEventListener('progress_tick', (e) => {
        const data = safeParseSSE(e, 'progress_tick')
        if (data == null) return
        setPhaseLabel(formatPhaseLabel(data))
        // 质量指标进度（judge 阶段或 process_eval 阶段携带时）
        if (data.phase === 'judge' || data.phase === 'judge_done' ||
            (data.phase === 'process_eval' && data.quality_current != null && data.quality_total != null)) {
          if (data.quality_current != null && data.quality_total != null) {
            setQualityProgress({ current: data.quality_current, total: data.quality_total })
          } else {
            setQualityProgress({ current: data.current || 0, total: data.total || 0 })
          }
        }
        // 过程评估仅记录日志，进度由 scenario_eval_start/scenario_step_done 管理
        if (data.phase === 'process_eval') {
          setScenarioStatus('running')
          setScenarioLines(prev => [...prev, {
            id: prev.length,
            questionId: data.question_id ?? -1,
            model: 'system',
            metric: 'scenario',
            text: data.question_id != null
              ? `场景评估进行中 · 第 ${data.question_id + 1} 题`
              : '场景评估进行中…',
            type: 'phase',
          }])
        }
      })

      eventSource.addEventListener('process_eval_done', (e) => {
        const data = safeParseSSE(e, 'process_eval_done')
        if (data == null) return
        // 基于Prompt评测由 _run_auto_scenario_scoring 中的 post eval 执行
        // 才真正执行。process_eval 只完成了 trace 拉取 + checkpoint，后面还有场景步骤评分。
        // 状态最终由 scenario_scored 事件设置为 'done'。
        if (!data.skipped) {
          setScenarioLines(prev => [...prev, {
            id: prev.length,
            questionId: -1,
            model: 'system',
            metric: 'scenario',
            text: `离线过程评估完成 · 共 ${data.run_count || 0} 题 Trace 拉取，成功 ${data.succeeded || 0} 题`,
            type: 'phase',
          }])
        }
      })

      eventSource.addEventListener('process_eval_skipped', (e) => {
        setScenarioStatus('skipped')
        const data = safeParseSSE(e, 'process_eval_skipped')
        if (data == null) return
        setScenarioLines(prev => [...prev, {
          id: prev.length,
          questionId: -1,
          model: 'system',
          metric: 'scenario',
          text: `场景交付评估跳过（${data.reason || '未知原因'}）`,
          type: 'phase',
        }])
      })

      eventSource.addEventListener('process_eval_error', (e) => {
        const data = safeParseSSE(e, 'process_eval_error')
        if (data == null) return
        setScenarioStatus('error')
        setScenarioLines(prev => [...prev, {
          id: prev.length,
          questionId: -1,
          model: 'system',
          metric: 'scenario',
          text: `场景交付评估失败：${data.error || '未知错误'}`,
          type: 'error',
        }])
      })

      eventSource.addEventListener('scenario_scored', (e) => {
        const data = safeParseSSE(e, 'scenario_scored')
        if (data == null) return
        setScenarioStatus('done')
        const steps = data.steps || data.results || []
        const summary = steps.length > 0
          ? steps.map((s: any) => `  ${s.name || s.step_key}: ${typeof s.score === 'number' ? s.score.toFixed(2) : '—'} ${s.passed ? '✓' : '✗'}`).join('\n')
          : JSON.stringify(data).slice(0, 500)
        setScenarioLines(prev => [...prev, {
          id: prev.length,
          questionId: -1,
          model: 'system',
          metric: 'scenario',
          text: `场景子步骤打分完成：\n${summary}`,
          type: 'result',
        }])
      })

      eventSource.onerror = () => {
        if (disposed || statusRef.current === 'completed' || statusRef.current === 'failed') return
        setSseNotice('实时连接中断，进度仍通过轮询更新；正在尝试重连…')
        eventSource.close()
        const delay = Math.min(30000, 1000 * 2 ** sseRetryRef.current)
        sseRetryRef.current += 1
        setTimeout(connect, delay)
      }
    }

    connect()

    return () => {
      disposed = true
      if (eventSourceRef.current) {
        eventSourceRef.current.close()
        eventSourceRef.current = null
      }
    }
  }, [taskId, status, onComplete, appendLine])

  useEffect(() => {
    if (cotPanelRef.current) {
      cotPanelRef.current.scrollTop = cotPanelRef.current.scrollHeight
    }
  }, [cotLines])

  const progressParts = progress.split('/')
  const current = parseInt(progressParts[0]) || 0
  const total = parseInt(progressParts[1]) || 0
  const progressPercent = total > 0 ? (current / total) * 100 : 0

  const statusMap: Record<string, string> = {
    'pending': '等待中',
    'processing': '处理中',
    'completed': '已完成',
    'failed': '失败',
    'paused': '已暂停',
    'cancelled': '已取消'
  }

  const isPaused = status === 'paused'
  const canPause = status === 'processing' && !isPaused
  const canResume = status === 'paused'
  const canCancel = status === 'processing' || status === 'pending' || isPaused

  const handlePause = async () => {
    try {
      const response = await fetch(`${API_BASE}/tasks/${taskId}/pause`, { method: 'POST' })
      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}))
        throw new Error(errorData.detail || `HTTP error ${response.status}`)
      }
      // 不立即设置状态，让轮询获取最新的状态
      // 手动触发一次轮询以尽快更新状态
      const res = await fetch(`${API_BASE}/result/${taskId}`)
      const data = await res.json()
      if (data.progress) {
        setProgress(data.progress)
      }
      setStatus(data.status)
      setShowPauseConfirm(false)
    } catch (err) {
      console.error('暂停任务失败:', err)
      alert('暂停任务失败')
    }
  }

  const handleResume = async () => {
    try {
      const response = await fetch(`${API_BASE}/tasks/${taskId}/resume`, { method: 'POST' })
      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}))
        throw new Error(errorData.detail || `HTTP error ${response.status}`)
      }
      // 不立即设置状态，让轮询获取最新的状态
      // 手动触发一次轮询以尽快更新状态
      const res = await fetch(`${API_BASE}/result/${taskId}`)
      const data = await res.json()
      if (data.progress) {
        setProgress(data.progress)
      }
      setStatus(data.status)
    } catch (err) {
      console.error('恢复任务失败:', err)
      alert('恢复任务失败')
    }
  }

  const handleCancel = async () => {
    if (!confirm('确定要取消任务吗？')) return
    try {
      const response = await fetch(`${API_BASE}/tasks/${taskId}/cancel`, { method: 'POST' })
      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}))
        throw new Error(errorData.detail || `HTTP error ${response.status}`)
      }
      setStatus('failed')
      setError('任务已取消')
    } catch (err) {
      console.error('取消任务失败:', err)
      alert(`取消任务失败: ${err instanceof Error ? err.message : '未知错误'}`)
    }
  }

  return (
    <div className="task-monitor-page">
      <div className="card task-monitor-card">
        <div className="task-monitor-header">
          <div>
            <h2>
              <svg className="task-monitor-header-icon" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/>
                <line x1="8" y1="21" x2="16" y2="21"/>
                <line x1="12" y1="17" x2="12" y2="21"/>
              </svg>
              任务监控
            </h2>
            <p className="task-monitor-task-id">任务 ID：<code>{taskId}</code></p>
          </div>
        </div>

        <div className="task-monitor-status-row">
          <span className={`task-monitor-status-badge status-badge ${statusBadgeClass(status)}`}>
            {status === 'processing' && <span className="status-dot" aria-hidden />}
            {statusMap[status] || status}
          </span>
        </div>

        {/* 进度区域：左右两栏，与下方面板对齐 */}
        <div className="task-monitor-progress-row">
          {/* 质量指标进度 */}
          {(() => {
            const qPct = qualityProgress.total > 0 ? (qualityProgress.current / qualityProgress.total) * 100 : 0
            return (
              <div className="task-monitor-progress-col">
                <div className="task-monitor-progress-label">
                  <span className="task-monitor-progress-title">质量指标进度</span>
                  <span className="task-monitor-progress-value">
                    {qualityProgress.total > 0 ? `${qualityProgress.current} / ${qualityProgress.total} (${qPct.toFixed(1)}%)` : '—'}
                  </span>
                </div>
                <div className="task-monitor-progress-track">
                  <div className="task-monitor-progress-fill" style={{ width: `${qPct}%` }} />
                </div>
                {phaseLabel && (
                  <div className="task-monitor-progress-detail">当前：{phaseLabel}</div>
                )}
              </div>
            )
          })()}
          {/* 场景交付进度：未启用场景子步骤维度时不展示，左侧质量指标进度自动占满整行 */}
          {capabilities.includes('scenario') && (() => {
            const sPct = scenarioProgress.total > 0 ? (scenarioProgress.current / scenarioProgress.total) * 100 : 0
            const sText = scenarioStatus === 'done'
              ? '已完成'
              : scenarioStatus === 'error'
                ? '失败'
                : scenarioStatus === 'skipped'
                  ? '跳过'
                  : scenarioProgress.total > 0
                    ? `${scenarioProgress.current} / ${scenarioProgress.total} (${sPct.toFixed(1)}%)`
                    : '—'
            const detailParts: string[] = []
            if (scenarioQuestionId >= 0) detailParts.push(`第 ${scenarioQuestionId + 1} 题`)
            if (scenarioStepName) detailParts.push(scenarioStepName)
            return (
              <div className="task-monitor-progress-col">
                <div className="task-monitor-progress-label">
                  <span className="task-monitor-progress-title">
                    场景交付进度
                    {scenarioStatus === 'running' && <span className="status-dot" style={{ display: 'inline-block', marginLeft: 6 }} />}
                  </span>
                  <span className="task-monitor-progress-value">{sText}</span>
                </div>
                <div className="task-monitor-progress-track">
                  <div className="task-monitor-progress-fill" style={{ width: `${scenarioStatus === 'done' ? 100 : sPct}%` }} />
                </div>
                {detailParts.length > 0 && (
                  <div className="task-monitor-progress-detail">当前：{detailParts.join(' · ')}</div>
                )}
              </div>
            )
          })()}
        </div>

        {taskPhaseInfo && (
          <p className="task-monitor-phase-info">{taskPhaseInfo}</p>
        )}
        {phaseLabel && (
          <p className="task-monitor-phase-current">
            当前阶段：<strong>{phaseLabel}</strong>
          </p>
        )}
        {sseNotice && (
          <p className="task-monitor-sse-notice" role="status">{sseNotice}</p>
        )}

        {(status === 'processing' || status === 'paused') && (
          <div className="task-monitor-partial-actions">
            <button
              type="button"
              className="task-monitor-btn task-monitor-btn-primary"
              onClick={() => setDetailOpen(true)}
              disabled={completedResultCount === 0 && cotLines.filter(l => l.type === 'result').length === 0}
            >
              查看已完成结果
            </button>
            {onViewPartialResults && (
              <button
                type="button"
                className="task-monitor-btn task-monitor-btn-secondary"
                onClick={onViewPartialResults}
              >
                在结果页查看（进行中）
              </button>
            )}
          </div>
        )}

        {error && (
          <div className="task-monitor-error">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ flexShrink: 0, marginTop: 2 }}>
              <circle cx="12" cy="12" r="10"/>
              <line x1="15" y1="9" x2="9" y2="15"/>
              <line x1="9" y1="9" x2="15" y2="15"/>
            </svg>
            <span>{error}</span>
          </div>
        )}

        {canPause && (
          <div className="task-monitor-actions">
            <button type="button" className="task-monitor-btn task-monitor-btn-secondary" onClick={() => setShowPauseConfirm(true)}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <rect x="6" y="4" width="4" height="16"/>
                <rect x="14" y="4" width="4" height="16"/>
              </svg>
              暂停评估
            </button>
            <button type="button" className="task-monitor-btn task-monitor-btn-danger" onClick={handleCancel}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10"/>
                <line x1="15" y1="9" x2="9" y2="15"/>
                <line x1="9" y1="9" x2="15" y2="15"/>
              </svg>
              取消任务
            </button>
          </div>
        )}

        {canResume && (
          <div className="task-monitor-actions">
            <button type="button" className="task-monitor-btn task-monitor-btn-primary" onClick={handleResume}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <polygon points="5 3 19 12 5 21 5 3"/>
              </svg>
              继续评估
            </button>
            <button type="button" className="task-monitor-btn task-monitor-btn-danger" onClick={handleCancel}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10"/>
                <line x1="15" y1="9" x2="9" y2="15"/>
                <line x1="9" y1="9" x2="15" y2="15"/>
              </svg>
              取消任务
            </button>
          </div>
        )}
      </div>

      <div className="task-monitor-panels-row">
      {/* 质量指标评估过程 */}
      <div className="card task-monitor-log-card">
        <div className="task-monitor-log-header">
          <h3>质量指标评估过程</h3>
          <span className="task-monitor-log-count">
            共 {cotLines.length} 条{qualityProgress.total > 0 ? ` · ${qualityProgress.current}/${qualityProgress.total}` : ''}
          </span>
        </div>
        <p className="task-monitor-log-hint">实时记录 Judge 评估过程与得分，点击条目展开完整内容</p>
        <div className="task-monitor-log-panel" ref={cotPanelRef}>
          {cotLines.length === 0 ? (
            <div className="task-monitor-log-empty">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10"/>
                <path d="M12 6v6l4 2"/>
              </svg>
              {taskPhaseInfo ? '任务已启动，正在准备第 1 题…' : '任务已提交，正在启动 Pipeline…'}
            </div>
          ) : (
            cotLines.map(line => {
              const isExpanded = expandedCot === line.id
              const scriptEval = line.scriptEval
              const itemClass = [
                'task-monitor-log-item',
                line.type === 'result' ? 'task-monitor-log-item--result' : '',
                line.type === 'error' ? 'task-monitor-log-item--error' : '',
                line.type === 'inference' ? 'task-monitor-log-item--inference' : '',
                line.type === 'phase' ? 'task-monitor-log-item--phase' : '',
                line.type === 'heartbeat' ? 'task-monitor-log-item--heartbeat' : '',
                scriptEval ? 'task-monitor-log-item--script' : '',
              ].filter(Boolean).join(' ')

              return (
                <div
                  key={line.id}
                  className={itemClass}
                  onClick={() => setExpandedCot(isExpanded ? null : line.id)}
                >
                  <div className="task-monitor-log-item-top">
                    <div className="task-monitor-log-meta">
                      {line.questionId >= 0 && (
                        <span className="task-monitor-log-chip task-monitor-log-chip--question">
                          题 #{line.questionId + 1}
                        </span>
                      )}
                      {line.model !== 'system' && (
                        <span className="task-monitor-log-chip">{line.model}</span>
                      )}
                      {scriptEval ? (
                        <span className="task-monitor-log-chip task-monitor-log-chip--script">
                          🐍 {metricLabel(line.metric)}
                        </span>
                      ) : line.metric !== 'summary' ? (
                        <span className="task-monitor-log-chip task-monitor-log-chip--metric">
                          {metricLabel(line.metric)}
                        </span>
                      ) : null}
                    </div>
                    <span className={`task-monitor-log-type${line.type === 'result' ? ' task-monitor-log-type--result' : line.type === 'error' ? ' task-monitor-log-type--error' : line.type === 'inference' ? ' task-monitor-log-type--inference' : line.type === 'phase' ? ' task-monitor-log-type--phase' : line.type === 'heartbeat' ? ' task-monitor-log-type--heartbeat' : ''}`}>
                      {line.type === 'cot' ? (
                        <>
                          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
                          </svg>
                          评估过程
                        </>
                      ) : line.type === 'result' ? (
                        <>
                          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <polyline points="20 6 9 17 4 12"/>
                          </svg>
                          结果
                        </>
                      ) : line.type === 'error' ? (
                        <>
                          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <circle cx="12" cy="12" r="10"/>
                            <line x1="12" y1="8" x2="12" y2="12"/>
                            <line x1="12" y1="16" x2="12.01" y2="16"/>
                          </svg>
                          错误
                        </>
                      ) : line.type === 'inference' ? (
                        <>
                          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
                          </svg>
                          推理
                        </>
                      ) : line.type === 'phase' ? (
                        <>
                          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/>
                            <line x1="4" y1="22" x2="4" y2="15"/>
                          </svg>
                          阶段
                        </>
                      ) : (
                        <>
                          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <circle cx="12" cy="12" r="10"/>
                            <polyline points="12 6 12 12 16 14"/>
                          </svg>
                          等待中
                        </>
                      )}
                    </span>
                  </div>
                  <div
                    className={[
                      'task-monitor-log-body',
                      line.type === 'result' ? 'task-monitor-log-body--result' : '',
                      line.type === 'error' ? 'task-monitor-log-body--error' : '',
                      isExpanded ? 'task-monitor-log-body--expanded' : 'task-monitor-log-body--collapsed',
                    ].filter(Boolean).join(' ')}
                  >
                    {line.text}
                  </div>
                  {line.text.length > 100 && !isExpanded && (
                    <div className="task-monitor-log-expand">点击展开完整内容</div>
                  )}
                </div>
              )
            })
          )}
        </div>
      </div>

      {/* 场景交付评估过程 */}
      <div className="card task-monitor-log-card">
        <div className="task-monitor-log-header">
          <h3>场景交付评估过程</h3>
          <span className="task-monitor-log-count">
            {!capabilities.includes('scenario') && '未启用'}
            {capabilities.includes('scenario') && scenarioStatus === 'idle' && '等待启动'}
            {capabilities.includes('scenario') && scenarioStatus === 'running' && scenarioProgress.total > 0
              ? `${scenarioProgress.current}/${scenarioProgress.total} (${(scenarioProgress.current / scenarioProgress.total * 100).toFixed(0)}%)`
              : capabilities.includes('scenario') && scenarioStatus === 'running' && '评估中…'}
            {capabilities.includes('scenario') && scenarioStatus === 'done' && '已完成'}
            {capabilities.includes('scenario') && scenarioStatus === 'error' && '评估失败'}
            {capabilities.includes('scenario') && scenarioStatus === 'skipped' && '已跳过'}
          </span>
        </div>
        {scenarioStatus === 'running' && (scenarioQuestionId >= 0 || scenarioStepName) && (
          <p className="task-monitor-phase-current" style={{ fontSize: 12, color: '#888', margin: '0 0 8px' }}>
            {scenarioQuestionId >= 0 && `第 ${scenarioQuestionId + 1} 题`}
            {scenarioQuestionId >= 0 && scenarioStepName && ' · '}
            {scenarioStepName && scenarioStepName}
          </p>
        )}
        {!capabilities.includes('scenario') ? (
          <div className="task-monitor-log-empty" style={{ color: '#9e9e9e' }}>
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ opacity: 0.5 }}>
              <circle cx="12" cy="12" r="10"/>
              <path d="M4 4l16 16"/>
            </svg>
            本次评测未启用场景子步骤维度
          </div>
        ) : (
          <>
        <p className="task-monitor-log-hint">实时记录场景子步骤评估（Langfuse trace + LLM 评判）</p>
        <div className="task-monitor-log-panel">
          {scenarioLines.length === 0 ? (
            <div className="task-monitor-log-empty">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10"/>
                <path d="M12 6v6l4 2"/>
              </svg>
              {scenarioStatus === 'idle' ? '等待质量指标评估启动后，场景交付将并行执行…' : '正在加载…'}
            </div>
          ) : (
            scenarioLines.map(line => {
              const isExpanded = expandedCot === line.id + 10000
              const itemClass = [
                'task-monitor-log-item',
                line.type === 'result' ? 'task-monitor-log-item--result' : '',
                line.type === 'error' ? 'task-monitor-log-item--error' : '',
                line.type === 'phase' ? 'task-monitor-log-item--phase' : '',
              ].filter(Boolean).join(' ')
              return (
                <div
                  key={line.id}
                  className={itemClass}
                  onClick={() => setExpandedCot(isExpanded ? null : line.id + 10000)}
                >
                  <div className="task-monitor-log-item-top">
                    <div className="task-monitor-log-meta">
                      <span className="task-monitor-log-chip task-monitor-log-chip--metric">场景交付</span>
                    </div>
                    <span className={`task-monitor-log-type${line.type === 'result' ? ' task-monitor-log-type--result' : line.type === 'error' ? ' task-monitor-log-type--error' : ''}`}>
                      {line.type === 'result' ? '结果' : line.type === 'error' ? '错误' : '阶段'}
                    </span>
                  </div>
                  <div
                    className={[
                      'task-monitor-log-body',
                      line.type === 'result' ? 'task-monitor-log-body--result' : '',
                      line.type === 'error' ? 'task-monitor-log-body--error' : '',
                      isExpanded ? 'task-monitor-log-body--expanded' : 'task-monitor-log-body--collapsed',
                    ].filter(Boolean).join(' ')}
                  >
                    {line.text}
                  </div>
                  {line.text.length > 100 && !isExpanded && (
                    <div className="task-monitor-log-expand">点击展开完整内容</div>
                  )}
                </div>
              )
            })
          )}
        </div>
        </>
        )}
      </div>
      </div>

      {detailOpen && (
        <EvalDetailModal
          taskId={taskId}
          onClose={() => setDetailOpen(false)}
          isPartial
        />
      )}

      {showPauseConfirm && (
        <div className="modal-overlay" onClick={() => setShowPauseConfirm(false)}>
          <div className="modal-content" onClick={e => e.stopPropagation()}>
            <h3>确认暂停评估</h3>
            <p>暂停后任务将保存当前进度，可随时恢复并从上次位置继续。</p>
            <div style={{ display: 'flex', gap: 10, justifyContent: 'center' }}>
              <button type="button" className="task-monitor-btn task-monitor-btn-secondary" onClick={handlePause}>
                确认暂停
              </button>
              <button type="button" className="task-monitor-btn task-monitor-btn-primary" onClick={() => setShowPauseConfirm(false)}>
                继续运行
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
