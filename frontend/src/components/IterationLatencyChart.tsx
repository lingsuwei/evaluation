/**
 * 逐轮迭代耗时图（稳定性场景）
 * 数据来源：performance-report 的 run_rows（每轮 latency_ms / tokens_total / correctness_score）
 * 仅读物化事实层，符合范式 P-16。
 */
import { useMemo } from 'react'
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  Title,
  Tooltip,
  Legend,
  type ChartOptions,
} from 'chart.js'
import { Bar } from 'react-chartjs-2'
import { formatDuration } from '../utils/dateUtils'

ChartJS.register(
  CategoryScale,
  LinearScale,
  BarElement,
  Title,
  Tooltip,
  Legend,
)

interface RunRowLike {
  agent_run_id: string
  run_index?: number
  latency_ms?: number | null
  tokens_total?: number | null
  correctness_score?: number | null
  correctness_passed?: boolean | null
}

interface Props {
  /** 同一题的多轮 Run（已按 run_index 排序） */
  runs: RunRowLike[]
  questionId?: number | null
}

/**
 * 多轮稳定性视图：逐轮耗时 + Tokens + 正确性 柱状图
 * - X 轴：第 1..N 轮
 * - 左 Y 轴 y：latency_ms（柱）
 * - 右 Y 轴 y1：tokens_total（柱）
 * - 右 Y 轴 y2：correctness 0~1（柱，答错轮标红）
 */
export function IterationLatencyChart({ runs, questionId }: Props) {
  const { labels, latencyData, tokensData, qualityData, qualityColors } = useMemo(() => {
    const labels = runs.map(r => `第 ${(r.run_index ?? 0) + 1} 轮`)
    const latencyData = runs.map(r => (r.latency_ms != null ? Math.round(r.latency_ms) : 0))
    const tokensData = runs.map(r => r.tokens_total ?? 0)
    const qualityData = runs.map(r => (r.correctness_score != null ? r.correctness_score : null))
    const qualityColors = runs.map(r => {
      if (r.correctness_score == null) return 'rgba(200,200,200,0.4)'
      return r.correctness_passed === false
        ? 'rgba(229,57,53,0.8)'
        : 'rgba(46,125,50,0.7)'
    })
    return { labels, latencyData, tokensData, qualityData, qualityColors }
  }, [runs])

  const hasQuality = qualityData.some(v => v != null)

  const data = useMemo(
    () => ({
      labels,
      datasets: [
        {
          type: 'bar' as const,
          label: '时延 (ms)',
          data: latencyData,
          backgroundColor: 'rgba(102,126,234,0.7)',
          borderColor: 'rgba(102,126,234,1)',
          borderWidth: 1,
          yAxisID: 'y',
          order: 3,
        },
        {
          type: 'bar' as const,
          label: 'Tokens',
          data: tokensData,
          backgroundColor: 'rgba(255,152,0,0.7)',
          borderColor: 'rgba(255,152,0,1)',
          borderWidth: 1,
          yAxisID: 'y1',
          order: 2,
        },
        ...(hasQuality
          ? [{
              type: 'bar' as const,
              label: '正确性',
              data: qualityData,
              backgroundColor: qualityColors,
              borderColor: qualityColors.map(c => c.replace(/0\.[78]\)/, '1)')),
              borderWidth: 1,
              yAxisID: 'y2',
              order: 1,
            }]
          : []),
      ],
    }),
    [labels, latencyData, tokensData, qualityData, qualityColors, hasQuality],
  )

  const options: ChartOptions<'bar'> = useMemo(
    () => ({
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        mode: 'index',
        intersect: false,
      },
      plugins: {
        legend: {
          position: 'bottom',
          labels: { padding: 16, font: { size: 12 } },
        },
        title: {
          display: true,
          text: questionId != null ? `题目 #${questionId} · 逐轮耗时 / Token / 正确性` : '逐轮耗时 / Token / 正确性',
          font: { size: 14 },
        },
        tooltip: {
          callbacks: {
            label(ctx) {
              const label = ctx.dataset.label || ''
              const val = ctx.parsed.y
              if (ctx.dataset.yAxisID === 'y') {
                return `${label}: ${formatDuration(val)}`
              }
              if (ctx.dataset.yAxisID === 'y2') {
                const run = runs[ctx.dataIndex]
                const passTag = run?.correctness_passed === false ? ' ✘' : run?.correctness_passed === true ? ' ✓' : ''
                return `${label}: ${val != null ? val.toFixed(2) : '—'}${passTag}`
              }
              return `${label}: ${val.toLocaleString()}`
            },
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
        },
        y: {
          type: 'linear',
          position: 'left',
          title: { display: true, text: '时延 (ms)', font: { size: 11 } },
          beginAtZero: true,
        },
        y1: {
          type: 'linear',
          position: 'right',
          title: { display: true, text: 'Tokens', font: { size: 11 } },
          beginAtZero: true,
          grid: { drawOnChartArea: false },
        },
        ...(hasQuality
          ? {
              y2: {
                type: 'linear' as const,
                position: 'right' as const,
                title: { display: true, text: '正确性 (0~1)', font: { size: 11 } },
                min: 0,
                max: 1,
                offset: true,
                grid: { drawOnChartArea: false },
              },
            }
          : {}),
      },
    }),
    [questionId, runs, hasQuality],
  )

  return (
    <div
      style={{
        marginTop: 16,
        padding: 14,
        background: '#fff',
        border: '1px solid #cfe2ff',
        borderRadius: 8,
      }}
    >
      <div style={{ height: 280 }}>
        <Bar data={data as never} options={options as never} />
      </div>
      <p style={{ margin: '8px 0 0', fontSize: 11, color: '#888', lineHeight: 1.5 }}>
        左轴为推理时延，右轴为 Tokens 总量，最右侧轴为正确性（0~1）。
        {hasQuality && ' 红色柱表示该轮答错，绿色表示通过。'}
        异常轮可点击下方「调用链」按轮下钻定位。
      </p>
    </div>
  )
}
