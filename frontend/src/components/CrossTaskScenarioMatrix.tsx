import { useCallback, useEffect, useState } from 'react'
import { ScenarioMatrixView } from './ScenarioMatrixView'
import {
  CrossTaskMatrixPayload,
  CrossTaskOutcomeSummary,
} from './CrossTaskOutcomeSummary'
import { CrossTaskPerformanceSummary } from './CrossTaskPerformanceSummary'
import { CrossTaskQuestionRuntime } from './CrossTaskQuestionRuntime'
import { CrossTaskStabilityCompare } from './CrossTaskStabilityCompare'
import { OverallVerdictCard } from './OverallVerdictCard'

const API_BASE = '/api'

export interface CrossTaskScenarioSource {
  taskIds: string[]
  baselineTaskId?: string
}

interface Props {
  source: CrossTaskScenarioSource
}

export function CrossTaskScenarioMatrix({ source }: Props) {
  const [data, setData] = useState<CrossTaskMatrixPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  const [exportMsg, setExportMsg] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${API_BASE}/scenario-eval/from-tasks`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          task_ids: source.taskIds,
          baseline_task_id: source.baselineTaskId ?? source.taskIds[0],
        }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(
          typeof body.detail === 'string' ? body.detail : '加载场景对比矩阵失败',
        )
      }
      setData(await res.json())
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [source.taskIds, source.baselineTaskId])

  useEffect(() => {
    void load()
  }, [load])

  // 导出对比报告为 HTML（归档到"评测报告"菜单并下载）
  const handleExportComparison = async () => {
    setExporting(true)
    setExportMsg(null)
    try {
      const archiveRes = await fetch(`${API_BASE}/reports/comparison`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          task_ids: source.taskIds,
          baseline_task_id: source.baselineTaskId ?? source.taskIds[0],
        }),
      })
      if (!archiveRes.ok) {
        const body = await archiveRes.json().catch(() => ({}))
        throw new Error(
          typeof body.detail === 'string' ? body.detail : '归档失败',
        )
      }
      const report = await archiveRes.json()
      const dlRes = await fetch(`${API_BASE}/reports/${report.id}/download`)
      if (!dlRes.ok) throw new Error('下载失败')
      const blob = await dlRes.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${report.title || '版本对比报告'}.html`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      setExportMsg('对比报告已导出并归档，可在"评测报告"菜单查看')
    } catch (e) {
      setExportMsg(e instanceof Error ? e.message : '导出失败')
    } finally {
      setExporting(false)
    }
  }

  if (loading) {
    return <p style={{ padding: 16, fontSize: 13, color: '#666' }}>加载场景/版本对比矩阵…</p>
  }
  if (error) {
    return <p style={{ padding: 16, color: '#dc3545', fontSize: 13 }}>{error}</p>
  }
  if (!data) return null

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <button
          className="btn btn-secondary"
          onClick={handleExportComparison}
          disabled={exporting || !data}
          title="将当前对比结果导出为 HTML 报告并归档"
        >
          {exporting ? '导出中...' : '📥 导出对比报告 (HTML)'}
        </button>
        {exportMsg && (
          <span style={{ fontSize: 12, color: exportMsg.includes('失败') ? '#dc3545' : '#28a745' }}>
            {exportMsg}
          </span>
        )}
      </div>
      {data.overall_verdict && (
        <OverallVerdictCard verdict={data.overall_verdict} members={data.members} />
      )}
      {data.outcome_summaries && (
        <CrossTaskOutcomeSummary summaries={data.outcome_summaries} />
      )}
      {data.performance_summaries && (
        <CrossTaskPerformanceSummary summaries={data.performance_summaries} />
      )}
      {data.question_runtime_summary && (
        <CrossTaskQuestionRuntime
          summary={data.question_runtime_summary}
          members={data.members}
        />
      )}
      {data.question_runtime_summary && (
        <CrossTaskStabilityCompare
          summary={data.question_runtime_summary}
          members={data.members}
        />
      )}
      <ScenarioMatrixView
        data={data}
        subtitle={`评测集：${data.dataset_name ?? '—'} · 已选 ${data.member_ids.length} 个任务（同评测集 · 多版本对比）`}
      />
    </>
  )
}
