import { useState, useEffect } from 'react'
import axios from 'axios'
import {
  Chart as ChartJS,
  ArcElement,
  CategoryScale,
  LinearScale,
  BarElement,
  Title,
  Tooltip,
  Legend,
} from 'chart.js'
import { Pie, Bar } from 'react-chartjs-2'
import { formatDate, formatDuration } from '../utils/dateUtils'

ChartJS.register(
  ArcElement,
  CategoryScale,
  LinearScale,
  BarElement,
  Title,
  Tooltip,
  Legend
)

interface Trace {
  trace_id: string
  span_id: string
  name: string
  kind: string
  service_name: string
  start_time: string
  end_time: string | null
  duration_ms: number | null
  status: string
  llm_model: string | null
  llm_latency_ms: number | null
}

interface TraceDetail {
  trace_id: string
  span_count: number
  root_spans: any[]
}

interface LLMStats {
  total_calls: number
  avg_latency_ms: number
  min_latency_ms: number
  max_latency_ms: number
  total_tokens: number
  avg_tokens_per_call: number
  model_distribution: Record<string, number>
  status_distribution: Record<string, number>
  time_range?: {
    earliest: string
    latest: string
  }
}

interface ServiceStats {
  name: string
  count: number
  avg_latency: number
  models: Record<string, number>
}

function StatsCharts({ stats, serviceStats }: { stats: LLMStats | null; serviceStats: ServiceStats[] }) {
  if (!stats) {
    return <div className="loading">加载统计中...</div>
  }

  const modelData = {
    labels: Object.keys(stats.model_distribution),
    datasets: [
      {
        data: Object.values(stats.model_distribution),
        backgroundColor: [
          'rgba(54, 162, 235, 0.8)',
          'rgba(255, 99, 132, 0.8)',
          'rgba(255, 206, 86, 0.8)',
          'rgba(75, 192, 192, 0.8)',
          'rgba(153, 102, 255, 0.8)',
          'rgba(255, 159, 64, 0.8)',
        ],
        borderColor: [
          'rgba(54, 162, 235, 1)',
          'rgba(255, 99, 132, 1)',
          'rgba(255, 206, 86, 1)',
          'rgba(75, 192, 192, 1)',
          'rgba(153, 102, 255, 1)',
          'rgba(255, 159, 64, 1)',
        ],
        borderWidth: 2,
      },
    ],
  }

  const statusLabels: Record<string, string> = {
    ok: '成功',
    error: '错误',
    active: '进行中',
  }

  const statusData = {
    labels: Object.keys(stats.status_distribution).map(k => statusLabels[k] || k),
    datasets: [
      {
        label: '调用次数',
        data: Object.values(stats.status_distribution),
        backgroundColor: [
          'rgba(75, 192, 192, 0.8)',
          'rgba(255, 99, 132, 0.8)',
          'rgba(54, 162, 235, 0.8)',
        ],
        borderColor: [
          'rgba(75, 192, 192, 1)',
          'rgba(255, 99, 132, 1)',
          'rgba(54, 162, 235, 1)',
        ],
        borderWidth: 2,
      },
    ],
  }

  const latencyData = {
    labels: ['最小延迟', '平均延迟', '最大延迟'],
    datasets: [
      {
        label: '延迟 (ms)',
        data: [
          stats.min_latency_ms || 0,
          stats.avg_latency_ms || 0,
          stats.max_latency_ms || 0
        ],
        backgroundColor: [
          'rgba(75, 192, 192, 0.6)',
          'rgba(54, 162, 235, 0.6)',
          'rgba(255, 99, 132, 0.6)',
        ],
        borderColor: [
          'rgba(75, 192, 192, 1)',
          'rgba(54, 162, 235, 1)',
          'rgba(255, 99, 132, 1)',
        ],
        borderWidth: 2,
      },
    ],
  }

  const serviceData = {
    labels: serviceStats.map(s => s.name),
    datasets: [
      {
        label: '调用次数',
        data: serviceStats.map(s => s.count),
        backgroundColor: 'rgba(54, 162, 235, 0.6)',
        borderColor: 'rgba(54, 162, 235, 1)',
        borderWidth: 2,
      },
    ],
  }

  const modelOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        position: 'bottom' as const,
        labels: {
          padding: 'var(--edp-space-5)',
          font: { size: 12 },
        },
      },
      title: {
        display: true,
        text: '模型调用分布',
        font: { size: 16 },
      },
    },
  }

  const statusOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        position: 'bottom' as const,
        labels: {
          padding: 'var(--edp-space-5)',
          font: { size: 12 },
        },
      },
      title: {
        display: true,
        text: '状态分布',
        font: { size: 16 },
      },
    },
  }

  const barOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: {
        display: false,
      },
    },
  }

  const latencyOptions = {
    ...barOptions,
    plugins: {
      ...barOptions.plugins,
      title: {
        display: true,
        text: '延迟统计 (ms)',
        font: { size: 16 },
      },
    },
  }

  const serviceOptions = {
    ...barOptions,
    plugins: {
      ...barOptions.plugins,
      title: {
        display: true,
        text: '服务调用次数',
        font: { size: 16 },
      },
    },
  }

  return (
    <div className="stats-charts">
      <div className="stats-overview">
        <div className="stat-card highlight">
          <div className="stat-icon">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 3v18h18"/>
              <path d="M18.7 8l-5.1 5.2-2.8-2.7L7 14.3"/>
            </svg>
          </div>
          <div className="stat-content">
            <div className="stat-value">{stats.total_calls}</div>
            <div className="stat-label">总调用次数</div>
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-icon">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10"/>
              <polyline points="12 6 12 12 16 14"/>
            </svg>
          </div>
          <div className="stat-content">
            <div className="stat-value">{formatDuration(stats.avg_latency_ms)}</div>
            <div className="stat-label">平均延迟</div>
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-icon">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
              <line x1="16" y1="13" x2="8" y2="13"/>
              <line x1="16" y1="17" x2="8" y2="17"/>
            </svg>
          </div>
          <div className="stat-content">
            <div className="stat-value">{stats.total_tokens}</div>
            <div className="stat-label">总Token数</div>
          </div>
        </div>
        <div className="stat-card">
          <div className="stat-icon">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="12" y1="20" x2="12" y2="10"/>
              <line x1="18" y1="20" x2="18" y2="4"/>
              <line x1="6" y1="20" x2="6" y2="16"/>
            </svg>
          </div>
          <div className="stat-content">
            <div className="stat-value">{stats.avg_tokens_per_call}</div>
            <div className="stat-label">平均Token/次</div>
          </div>
        </div>
      </div>

      <div className="charts-grid">
        <div className="chart-card">
          <div className="chart-container" style={{ height: '280px' }}>
            <Pie data={modelData} options={modelOptions} />
          </div>
        </div>

        <div className="chart-card">
          <div className="chart-container" style={{ height: '280px' }}>
            <Pie data={statusData} options={statusOptions} />
          </div>
        </div>

        <div className="chart-card">
          <div className="chart-container" style={{ height: '280px' }}>
            <Bar data={latencyData} options={latencyOptions} />
          </div>
        </div>

        <div className="chart-card">
          <div className="chart-container" style={{ height: '280px' }}>
            <Bar data={serviceData} options={serviceOptions} />
          </div>
        </div>
      </div>

      <div className="stats-table-section">
        <h3 style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M9 11l3 3L22 4"/>
              <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
            </svg>
            服务详细统计
          </h3>
        <table className="stats-table">
          <thead>
            <tr>
              <th>服务名称</th>
              <th>调用次数</th>
              <th>平均延迟</th>
              <th>使用模型</th>
            </tr>
          </thead>
          <tbody>
            {serviceStats.map((service, idx) => (
              <tr key={idx}>
                <td>{service.name}</td>
                <td>{service.count}</td>
                <td>{formatDuration(Math.round(service.avg_latency))}</td>
                <td>
                  <div className="model-tags">
                    {Object.entries(service.models).map(([model, count]) => (
                      <span key={model} className="model-tag">
                        {model}: {count}
                      </span>
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="time-range">
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>
            <line x1="16" y1="2" x2="16" y2="6"/>
            <line x1="8" y1="2" x2="8" y2="6"/>
            <line x1="3" y1="10" x2="21" y2="10"/>
          </svg>
          数据时间范围:
        </span>
        {stats.time_range && (
          <>
            <span>{formatDate(stats.time_range.earliest)}</span>
            <span> ~ </span>
            <span>{formatDate(stats.time_range.latest)}</span>
          </>
        )}
      </div>
    </div>
  )
}

export function TraceMonitorPage() {
  const [activeSubTab, setActiveSubTab] = useState<'list' | 'stats'>('stats')
  const [traces, setTraces] = useState<Trace[]>([])
  const [loading, setLoading] = useState(false)
  const [selectedTrace, setSelectedTrace] = useState<TraceDetail | null>(null)
  const [queryParams, setQueryParams] = useState({
    service_name: '',
    limit: 100
  })
  const [stats, setStats] = useState<LLMStats | null>(null)
  const [serviceStats, setServiceStats] = useState<ServiceStats[]>([])

  const fetchTraces = async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams()
      if (queryParams.service_name) params.append('service_name', queryParams.service_name)
      params.append('limit', queryParams.limit.toString())
      
      const res = await axios.get(`/api/extended/otel/traces?${params.toString()}`)
      setTraces(res.data.traces)
      calculateServiceStats(res.data.traces)
    } catch (err) {
      console.error('Failed to fetch traces:', err)
    } finally {
      setLoading(false)
    }
  }

  const calculateServiceStats = (tracesData: Trace[]) => {
    const serviceMap = new Map<string, ServiceStats>()
    
    tracesData.forEach(trace => {
      if (!trace.llm_model) return
      
      if (!serviceMap.has(trace.service_name)) {
        serviceMap.set(trace.service_name, {
          name: trace.service_name,
          count: 0,
          avg_latency: 0,
          models: {}
        })
      }
      
      const service = serviceMap.get(trace.service_name)!
      service.count++
      service.avg_latency += trace.llm_latency_ms || 0
      
      if (!service.models[trace.llm_model]) {
        service.models[trace.llm_model] = 0
      }
      service.models[trace.llm_model]++
    })
    
    serviceMap.forEach(service => {
      service.avg_latency = service.count > 0 ? service.avg_latency / service.count : 0
    })
    
    setServiceStats(Array.from(serviceMap.values()))
  }

  const fetchTraceDetail = async (traceId: string) => {
    try {
      const res = await axios.get(`/api/extended/otel/traces/${traceId}`)
      setSelectedTrace(res.data)
    } catch (err) {
      console.error('Failed to fetch trace detail:', err)
    }
  }

  const fetchStats = async () => {
    try {
      const res = await axios.get('/api/extended/otel/llm/stats')
      setStats(res.data)
    } catch (err) {
      console.error('Failed to fetch stats:', err)
    }
  }

  useEffect(() => {
    if (activeSubTab === 'list') {
      fetchTraces()
    } else {
      fetchStats()
    }
  }, [activeSubTab])

  return (
    <div className="page-container">
      <div className="page-header">
        <h2><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ marginRight: 'var(--edp-space-2)', verticalAlign: 'middle' }}><path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/></svg>Trace 监控</h2>
        <p>监控LLM应用的调用链路和性能指标</p>
      </div>

      <div className="sub-tabs">
        <button 
          className={`sub-tab ${activeSubTab === 'list' ? 'active' : ''}`}
          onClick={() => setActiveSubTab('list')}
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M9 11l3 3L22 4"/>
              <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
            </svg>
            Trace 列表
        </button>
        <button 
          className={`sub-tab ${activeSubTab === 'stats' ? 'active' : ''}`}
          onClick={() => setActiveSubTab('stats')}
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 3v18h18"/>
              <path d="M18.7 8l-5.1 5.2-2.8-2.7L7 14.3"/>
            </svg>
            LLM 统计
        </button>
      </div>

      {activeSubTab === 'list' && (
        <div className="trace-list-section">
          <div className="query-form">
            <label>
              服务名称:
              <input 
                type="text" 
                value={queryParams.service_name}
                onChange={(e) => setQueryParams({...queryParams, service_name: e.target.value})}
                placeholder="可选服务名筛选"
              />
            </label>
            <label>
              数量限制:
              <input 
                type="number" 
                value={queryParams.limit}
                onChange={(e) => setQueryParams({...queryParams, limit: parseInt(e.target.value) || 100})}
                min={1}
                max={1000}
              />
            </label>
            <button onClick={fetchTraces} disabled={loading}>
              {loading ? (
              <>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite' }}>
                  <circle cx="12" cy="12" r="10"/>
                  <path d="M12 6v6l4 2"/>
                </svg>
                加载中...
              </>
            ) : (
              <>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <circle cx="11" cy="11" r="8"/>
                  <line x1="21" y1="21" x2="16.65" y2="16.65"/>
                </svg>
                查询
              </>
            )}
            </button>
          </div>

          {selectedTrace ? (
            <div className="trace-detail">
              <button onClick={() => setSelectedTrace(null)} className="back-btn">
                ← 返回列表
              </button>
              <h3>Trace: {selectedTrace.trace_id}</h3>
              <p>Span数量: {selectedTrace.span_count}</p>
              
              <div className="span-tree">
                {selectedTrace.root_spans.map((span: any, idx: number) => (
                  <div key={idx} className="span-item">
                    <div className="span-header">
                      <span className="span-name">{span.name}</span>
                      <span className={`span-status ${span.status}`}>{span.status}</span>
                      {span.duration_ms && (
                        <span className="span-duration">{formatDuration(span.duration_ms)}</span>
                      )}
                    </div>
                    {span.attributes && (
                      <div className="span-attributes">
                        {span.attributes.llm_model && (
                          <span className="attr-tag">模型: {span.attributes.llm_model}</span>
                        )}
                        {span.attributes.llm_latency_ms && (
                          <span className="attr-tag">延迟: {formatDuration(span.attributes.llm_latency_ms)}</span>
                        )}
                      </div>
                    )}
                    {span.children && span.children.map((child: any, childIdx: number) => (
                      <div key={childIdx} className="span-child">
                        <div className="span-header">
                          <span className="span-name">└ {child.name}</span>
                          <span className={`span-status ${child.status}`}>{child.status}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="traces-grid">
              {loading ? (
                <div className="loading">加载中...</div>
              ) : traces.length === 0 ? (
                <div className="empty">暂无Trace数据</div>
              ) : (
                traces.map((trace, idx) => (
                  <div 
                    key={idx} 
                    className="trace-card"
                    onClick={() => fetchTraceDetail(trace.trace_id)}
                  >
                    <div className="trace-header">
                      <span className="trace-name">{trace.name}</span>
                      <span className={`trace-status ${trace.status}`}>{trace.status}</span>
                    </div>
                    <div className="trace-info">
                      <span>服务: {trace.service_name}</span>
                      <span>类型: {trace.kind}</span>
                    </div>
                    {trace.llm_model && (
                      <div className="trace-llm">
                        <span className="model-tag">{trace.llm_model}</span>
                        {trace.llm_latency_ms && (
                          <span className="latency-tag">{formatDuration(trace.llm_latency_ms)}</span>
                        )}
                      </div>
                    )}
                    <div className="trace-time">
                      {trace.start_time && formatDate(trace.start_time)}
                    </div>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      )}

      {activeSubTab === 'stats' && (
        <div className="stats-section">
          <StatsCharts stats={stats} serviceStats={serviceStats} />
        </div>
      )}
    </div>
  )
}
