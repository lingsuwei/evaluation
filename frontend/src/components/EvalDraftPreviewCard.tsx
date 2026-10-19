import { useState } from 'react'

/**
 * AI 配置草稿预览卡片：把 EvaluateRequest 草稿渲染成可读 + 可编辑卡片。
 * 字段改动触发 onChange → PATCH /chat/sessions/{id}/draft。
 */

export interface DraftFields {
  dataset_id?: string | null
  online_mode?: boolean
  agent_id?: string | null
  llm_model?: string | null
  inference_rounds?: number
  selected_metrics?: string[]
  use_bm25?: boolean
  use_doc_library?: boolean
  ontology_package?: string | null
  auto_scenario_score?: boolean
  enable_process_consistency?: boolean
  enable_stability?: boolean
  parallel_inference?: boolean
  selected_indices?: number[] | null
  custom_metric_ids?: number[] | null
  custom_scripts?: Record<string, string> | null
  llm_max_tokens?: number | null
  llm_temperature?: number | null
  [k: string]: any
}

interface Props {
  draft: DraftFields
  missingFields: string[]
  suggestions: string[]
  confidence?: number | null
  onChange: (patch: Partial<DraftFields>) => void
  onApplyToForm: () => void
  onApplyDirect: () => void
  applying?: boolean
}

const METRIC_LABELS: Record<string, string> = {
  faithfulness: '忠实度',
  answer_relevancy: '答案相关性',
  correctness: '正确性',
  inference_latency: '推理延迟',
  tokens_total: 'Token总数',
}

const ALL_METRICS = ['faithfulness', 'answer_relevancy', 'correctness']

export function EvalDraftPreviewCard({
  draft,
  missingFields,
  suggestions,
  confidence,
  onChange,
  onApplyToForm,
  onApplyDirect,
  applying,
}: Props) {
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const isOnline = draft.online_mode !== false
  const metrics = Array.isArray(draft.selected_metrics) ? draft.selected_metrics : []
  const conf = typeof confidence === 'number' ? confidence : 0
  const lowConf = conf < 0.6

  const toggleMetric = (m: string) => {
    const next = new Set(metrics)
    if (next.has(m)) next.delete(m)
    else next.add(m)
    onChange({ selected_metrics: Array.from(next) })
  }

  // 更新 langfuse_source 嵌套字段的辅助函数
  const lf = (draft.langfuse_source || null) as Record<string, any> | null
  const updateLf = (patch: Record<string, any>) => {
    const next = { ...(lf || {}), ...patch }
    onChange({ langfuse_source: next })
  }
  const clearLf = () => onChange({ langfuse_source: null })

  const missingSet = new Set(missingFields)
  const fieldMissing = (k: string) => missingSet.has(k)

  return (
    <div className={`chat-draft-card${lowConf ? ' is-low-conf' : ''}`}>
      <div className="chat-draft-header">
        <h3>配置草稿</h3>
        {conf > 0 && (
          <span className={`chat-conf-badge${lowConf ? ' is-low' : ''}`}>
            置信度 {Math.round(conf * 100)}%
          </span>
        )}
      </div>
      {lowConf && (
        <div className="chat-warn">解析置信度偏低，建议人工复核以下字段</div>
      )}

      {/* 基础分组 */}
      <div className="chat-draft-group">
        <div className="chat-draft-label">
          数据集 {fieldMissing('dataset_id') && <span className="chat-missing-tag">缺失</span>}
        </div>
        <input
          type="text"
          className="chat-draft-input"
          value={draft.dataset_id || ''}
          onChange={e => onChange({ dataset_id: e.target.value || null })}
          placeholder="数据集 ID"
        />
      </div>

      <div className="chat-draft-group">
        <div className="chat-draft-label">评估模式</div>
        <div className="chat-draft-seg">
          <button
            className={isOnline ? 'active' : ''}
            onClick={() => onChange({ online_mode: true })}
          >在线评估</button>
          <button
            className={!isOnline ? 'active' : ''}
            onClick={() => onChange({ online_mode: false })}
          >离线评估</button>
        </div>
      </div>

      {isOnline && (
        <div className="chat-draft-group">
          <div className="chat-draft-label">
            Agent {fieldMissing('agent_id') && <span className="chat-missing-tag">缺失</span>}
          </div>
          <input
            type="text"
            className="chat-draft-input"
            value={draft.agent_id || ''}
            onChange={e => onChange({ agent_id: e.target.value || null })}
            placeholder="Agent ID"
          />
        </div>
      )}

      {/* 评估分组 */}
      <div className="chat-draft-group">
        <div className="chat-draft-label">评测 LLM 模型</div>
        <input
          type="text"
          className="chat-draft-input"
          value={draft.llm_model || ''}
          onChange={e => onChange({ llm_model: e.target.value || null })}
          placeholder="glm / auto"
        />
      </div>

      <div className="chat-draft-group">
        <div className="chat-draft-label">推理轮次（1-10）</div>
        <input
          type="number"
          className="chat-draft-input"
          min={1}
          max={10}
          value={draft.inference_rounds ?? 1}
          onChange={e => onChange({ inference_rounds: Math.max(1, Math.min(10, Number(e.target.value) || 1)) })}
        />
      </div>

      {Array.isArray(draft.selected_indices) && draft.selected_indices.length > 0 && (
        <div className="chat-draft-group">
          <div className="chat-draft-label">评测项</div>
          <div className="chat-draft-readonly">
            指定第 {draft.selected_indices.map(i => i + 1).join('、')} 题（共 {draft.selected_indices.length} 题）
          </div>
        </div>
      )}

      <div className="chat-draft-group">
        <div className="chat-draft-label">评估因子</div>
        <div className="chat-draft-chips">
          {ALL_METRICS.map(m => (
            <button
              key={m}
              className={`chat-chip${metrics.includes(m) ? ' active' : ''}`}
              onClick={() => toggleMetric(m)}
            >
              {METRIC_LABELS[m] || m}
            </button>
          ))}
        </div>
      </div>

      {Array.isArray(draft.custom_metric_ids) && draft.custom_metric_ids.length > 0 && (
        <div className="chat-draft-group">
          <div className="chat-draft-label">自定义指标</div>
          <div className="chat-draft-readonly">
            ID: {draft.custom_metric_ids.join(', ')}
          </div>
        </div>
      )}

      {draft.custom_scripts && Object.keys(draft.custom_scripts).length > 0 && (
        <div className="chat-draft-group">
          <div className="chat-draft-label">脚本映射</div>
          <div className="chat-draft-readonly">
            {Object.entries(draft.custom_scripts).map(([idx, sid]) => `第${Number(idx) + 1}题→${sid}`).join('；')}
          </div>
        </div>
      )}

      {/* 维度分组 */}
      <div className="chat-draft-group">
        <div className="chat-draft-label">评测维度</div>
        <div className="chat-draft-toggles">
          <label className="chat-toggle">
            <input type="checkbox" checked={draft.auto_scenario_score !== false} onChange={e => onChange({ auto_scenario_score: e.target.checked })} />
            场景子步骤
          </label>
          <label className="chat-toggle">
            <input type="checkbox" checked={draft.enable_process_consistency !== false} onChange={e => onChange({ enable_process_consistency: e.target.checked })} />
            过程一致性
          </label>
          {isOnline && (
            <label className="chat-toggle">
              <input type="checkbox" checked={draft.enable_stability !== false} onChange={e => onChange({ enable_stability: e.target.checked })} />
              运行稳定性
            </label>
          )}
        </div>
      </div>

      {/* Langfuse 答案源（仅离线模式） */}
      {!isOnline && (
        <div className="chat-draft-group">
          <div className="chat-draft-label">Langfuse 答案源</div>
          <div className="chat-draft-toggles">
            <label className="chat-toggle">
              <input
                type="checkbox"
                checked={!!lf}
                onChange={e => e.target.checked ? updateLf({ enable_process_eval: true }) : clearLf()}
              />
              从 Langfuse 获取历史答案
            </label>
          </div>
          {lf && (
            <div className="chat-draft-langfuse">
              <input
                type="text"
                className="chat-draft-input"
                value={lf.project || ''}
                onChange={e => updateLf({ project: e.target.value || null })}
                placeholder="Langfuse 项目 ID"
              />
              <input
                type="text"
                className="chat-draft-input"
                value={lf.source_task_id || ''}
                onChange={e => updateLf({ source_task_id: e.target.value || null })}
                placeholder="来源任务 ID（可选）"
              />
              <input
                type="text"
                className="chat-draft-input"
                value={lf.tags || ''}
                onChange={e => updateLf({ tags: e.target.value || null })}
                placeholder="标签筛选，如 onto-eval,task:xxx"
              />
              <input
                type="text"
                className="chat-draft-input"
                value={lf.session_id || ''}
                onChange={e => updateLf({ session_id: e.target.value || null })}
                placeholder="session ID 筛选（可选）"
              />
              <input
                type="text"
                className="chat-draft-input"
                value={lf.agent_name || ''}
                onChange={e => updateLf({ agent_name: e.target.value || null })}
                placeholder="Agent 名称筛选（可选）"
              />
              <label className="chat-toggle">
                <input
                  type="checkbox"
                  checked={lf.enable_process_eval !== false}
                  onChange={e => updateLf({ enable_process_eval: e.target.checked })}
                />
                启用过程回放
              </label>
            </div>
          )}
        </div>
      )}

      {/* 高级分组 */}
      <button
        className="chat-advanced-toggle"
        onClick={() => setAdvancedOpen(o => !o)}
      >
        {advancedOpen ? '收起' : '展开'}高级选项
      </button>
      {advancedOpen && (
        <div className="chat-draft-advanced">
          <div className="chat-draft-toggles">
            <label className="chat-toggle">
              <input type="checkbox" checked={draft.use_bm25 === true} onChange={e => onChange({ use_bm25: e.target.checked })} />
              BM25 忠实度
            </label>
            <label className="chat-toggle">
              <input type="checkbox" checked={draft.use_doc_library === true} onChange={e => onChange({ use_doc_library: e.target.checked })} />
              文档库
            </label>
            {isOnline && (
              <label className="chat-toggle">
                <input type="checkbox" checked={draft.parallel_inference !== false} onChange={e => onChange({ parallel_inference: e.target.checked })} />
                并发推理
              </label>
            )}
          </div>
          <div className="chat-draft-group">
            <div className="chat-draft-label">本体包</div>
            <input
              type="text"
              className="chat-draft-input"
              value={draft.ontology_package || ''}
              onChange={e => onChange({ ontology_package: e.target.value || null })}
              placeholder="本体包名称"
            />
          </div>
          <div className="chat-draft-group">
            <div className="chat-draft-label">LLM 最大输出 tokens</div>
            <input
              type="number"
              className="chat-draft-input"
              min={512}
              max={8192}
              value={draft.llm_max_tokens ?? ''}
              onChange={e => {
                const v = e.target.value
                onChange({ llm_max_tokens: v === '' ? null : Math.max(512, Math.min(8192, Number(v) || 512)) })
              }}
              placeholder="默认"
            />
          </div>
          <div className="chat-draft-group">
            <div className="chat-draft-label">LLM 温度</div>
            <input
              type="number"
              className="chat-draft-input"
              min={0}
              max={1}
              step={0.1}
              value={draft.llm_temperature ?? ''}
              onChange={e => {
                const v = e.target.value
                onChange({ llm_temperature: v === '' ? null : Math.max(0, Math.min(1, Number(v) || 0)) })
              }}
              placeholder="默认"
            />
          </div>
        </div>
      )}

      {suggestions.length > 0 && (
        <div className="chat-suggestions">
          {suggestions.map((s, i) => (
            <div key={i} className="chat-suggestion-item">• {s}</div>
          ))}
        </div>
      )}

      <div className="chat-draft-actions">
        <button className="chat-btn chat-btn-secondary" onClick={onApplyToForm} disabled={applying}>
          回填到表单
        </button>
        <button
          className="chat-btn chat-btn-primary"
          onClick={onApplyDirect}
          disabled={applying || fieldMissing('dataset_id') || (isOnline && fieldMissing('agent_id'))}
          title={fieldMissing('dataset_id') ? '数据集缺失，无法启动' : ''}
        >
          {applying ? '启动中…' : '直接启动评测'}
        </button>
      </div>
    </div>
  )
}
