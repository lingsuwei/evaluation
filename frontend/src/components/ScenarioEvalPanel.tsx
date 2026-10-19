import { useEffect, useState } from 'react'
import {
  ScenarioMatrixPayload,
  ScenarioMatrixView,
} from './ScenarioMatrixView'
import { ScenarioStepDrilldown } from './ScenarioStepDrilldown'
import { DimensionVerdictHeadline } from './DimensionVerdictHeadline'
import type { VerdictStatus } from '../types/dimensionVerdict'

const API_BASE = '/api'

interface Props {
  taskId: string
  langfuseTraceReady?: boolean
  scenarioVerdict?: {
    status: VerdictStatus
    headline: string
    score?: number | null
  } | null
}

export function ScenarioEvalPanel({
  taskId,
  langfuseTraceReady = true,
  scenarioVerdict,
}: Props) {
  const [data, setData] = useState<ScenarioMatrixPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [drillRunId, setDrillRunId] = useState<string | null>(null)
  const [drillQuestionLabel, setDrillQuestionLabel] = useState<string | null>(null)

  useEffect(() => {
    const load = async () => {
      try {
        setLoading(true)
        const res = await fetch(
          `${API_BASE}/scenario-eval/tasks/${taskId}/matrix`,
        )
        const raw = await res.text()
        if (!res.ok) {
          let message = '加载场景矩阵失败'
          try {
            const errBody = JSON.parse(raw) as { detail?: unknown }
            const detail = errBody?.detail
            if (typeof detail === 'string' && detail) {
              message = detail
            } else if (Array.isArray(detail)) {
              message = detail
                .map((d: { msg?: string }) => d.msg || String(d))
                .join('；')
            }
          } catch {
            if (raw) message = raw
          }
          throw new Error(message)
        }
        const payload: ScenarioMatrixPayload = JSON.parse(raw)
        setData(payload)
        const firstQ = payload.questions.find(q =>
          Object.values(q.cells).some(c => c?.agent_run_id),
        )
        if (firstQ) {
          const mid =
            payload.baseline_member_id ??
            payload.member_ids[0]
          const cell = firstQ.cells[mid]
          if (cell?.agent_run_id) {
            setDrillRunId(cell.agent_run_id)
            setDrillQuestionLabel(firstQ.label)
          }
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : '加载失败')
      } finally {
        setLoading(false)
      }
    }
    void load()
  }, [taskId])

  if (loading) {
    return (
      <p style={{ padding: 16, fontSize: 13, color: '#666' }}>
        加载场景子步骤版本对比矩阵…
      </p>
    )
  }
  if (error) {
    return <p style={{ padding: 16, color: '#856404', fontSize: 13 }}>{error}</p>
  }
  if (!data) return null

  const colCount = data.member_ids.length
  const subtitle =
    colCount > 1
      ? `本任务 ${colCount} 个 Agent 版本 · 按评测题对齐`
      : '本任务单版本 · 对比不同 Agent 版本请至评估历史勾选同评测集多个任务后使用「场景/版本对比」'
  const hasScenarioConfig =
    data.step_columns.length > 0 &&
    data.questions.some(q => Object.values(q.cells).some(c => c?.agent_run_id))

  const handleStepCellSelect = (
    _questionKey: string,
    questionLabel: string,
    _memberId: string,
    agentRunId: string | undefined,
  ) => {
    if (agentRunId) {
      setDrillRunId(agentRunId)
      setDrillQuestionLabel(questionLabel)
    }
  }

  return (
    <div style={{ padding: '0 24px' }}>
      <p style={{ margin: '12px 0 4px', fontSize: 12, color: '#888' }}>
        本 Tab 按场景 Rubric 对每题子步骤打分，结论见上方「场景交付」；Agent 必达过程点是否执行请见「过程一致性」Tab。
      </p>
      {scenarioVerdict && scenarioVerdict.status !== 'na' && (
        <DimensionVerdictHeadline
          title="场景交付结论"
          status={scenarioVerdict.status}
          headline={scenarioVerdict.headline}
          scoreLine={
            scenarioVerdict.score != null
              ? `综合 ${scenarioVerdict.score.toFixed(2)}`
              : null
          }
        />
      )}
      {(data.warnings?.length ?? 0) > 0 && (
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
          {data.warnings.map((w, i) => (
            <div key={i}>{w}</div>
          ))}
        </div>
      )}
      {hasScenarioConfig ? (
        <>
          <ScenarioMatrixView
            data={data}
            subtitle={subtitle}
            enableSpanCompare={colCount >= 2}
            langfuseTraceReady={langfuseTraceReady}
            onStepCellSelect={handleStepCellSelect}
          />
          <ScenarioStepDrilldown
            agentRunId={drillRunId ?? ''}
            questionLabel={drillQuestionLabel ?? undefined}
          />
        </>
      ) : (
        <div
          style={{
            marginTop: 16,
            padding: 20,
            border: '1px dashed #d9d9d9',
            borderRadius: 8,
            background: '#fafafa',
            color: '#555',
            fontSize: 13,
          }}
        >
          {data.empty_reason ??
            '当前任务未配置评测题场景子步骤，请在评测集管理中为题目标记场景子步骤。'}
        </div>
      )}
    </div>
  )
}
