import { useState, useEffect, useMemo } from 'react';
import axios from 'axios';

const API_BASE = '/api';

interface BuiltinMetric {
  key: string;
  label: string;
  threshold: number;
  is_runtime?: boolean;
}

interface MetricParamSpec {
  name: string;
  label: string;
  type: string; // text | list | json
  required: boolean;
  placeholder: string;
}

interface DeepevalMetric {
  key: string;
  label: string;
  category: string;
  category_label: string;
  requires: string[];
  note: string;
  params?: MetricParamSpec[];
  param_values?: Record<string, string | string[] | object>;
  gate_threshold: number;
  lower_is_better: boolean;
}

interface CustomMetricItem {
  id: number;
  key: string;
  name: string;
  kind: string;
  category: string | null;
  threshold: number;
  is_active: boolean;
}

interface MetricsCatalogData {
  builtin: BuiltinMetric[];
  deepeval: DeepevalMetric[];
  custom: CustomMetricItem[];
  categories: Record<string, string>;
}

// 内置指标中文名映射
const BUILTIN_LABELS: Record<string, string> = {
  correctness: '正确性',
  answer_relevancy: '回答相关性',
  faithfulness: '忠诚度',
  inference_latency: '推理时延',
  tokens_total: 'Tokens 总消耗',
};

// 内置指标类别
const BUILTIN_CATEGORY: Record<string, string> = {
  correctness: '回答质量(切题/正确/格式)',
  answer_relevancy: '回答质量(切题/正确/格式)',
  faithfulness: '回答质量(切题/正确/格式)',
  inference_latency: '运行效能',
  tokens_total: '运行效能',
};

// 内置指标说明
const BUILTIN_NOTES: Record<string, string> = {
  faithfulness: '检测回答是否忠实于检索上下文(retrieval_context)，支持 BM25/文档库。',
  answer_relevancy: '评估回答与问题的相关性，支持 BM25/文档库。',
  correctness: '基于关键词/提示词判定答案正确性，对照金标答案(ground_truth)打分。',
};

const KIND_LABELS: Record<string, string> = {
  geval: 'G-Eval',
  dag: 'DAG',
  code: 'Code',
};

export function MetricsCatalog() {
  const [data, setData] = useState<MetricsCatalogData | null>(null);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [filterCategory, setFilterCategory] = useState<string>('all');
  // 折叠的分组标题集合（记忆到 localStorage）
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(() => {
    try {
      const saved = localStorage.getItem('metrics_catalog_collapsed');
      return saved ? new Set(JSON.parse(saved)) : new Set();
    } catch {
      return new Set();
    }
  });
  // 运行时阈值编辑
  const [editingThreshold, setEditingThreshold] = useState<{ key: string; label: string; value: number } | null>(null);
  const [thresholdValue, setThresholdValue] = useState('');
  const [savingThreshold, setSavingThreshold] = useState(false);
  const [thresholdError, setThresholdError] = useState('');

  // 指标参数编辑
  const [editingParams, setEditingParams] = useState<{ metricKey: string; label: string; specs: MetricParamSpec[] } | null>(null);
  // 表单值：每个参数名 → 字符串（list/json 类型也先转成可编辑字符串）
  const [paramFormValues, setParamFormValues] = useState<Record<string, string>>({});
  const [savingParams, setSavingParams] = useState(false);
  const [paramError, setParamError] = useState('');

  const toggleGroup = (groupLabel: string) => {
    setCollapsedGroups(prev => {
      const next = new Set(prev);
      if (next.has(groupLabel)) next.delete(groupLabel);
      else next.add(groupLabel);
      try {
        localStorage.setItem('metrics_catalog_collapsed', JSON.stringify(Array.from(next)));
      } catch {
        // 忽略 localStorage 不可用
      }
      return next;
    });
  };

  const fetchCatalog = async () => {
    setLoading(true);
    try {
      const res = await axios.get(`${API_BASE}/metrics`);
      setData(res.data);
    } catch (err) {
      console.error('加载指标目录失败:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchCatalog();
  }, []);

  const handleSaveThreshold = async () => {
    if (!editingThreshold) return;
    const val = parseFloat(thresholdValue);
    if (!val || val <= 0) {
      setThresholdError('请输入大于 0 的数值');
      return;
    }
    setSavingThreshold(true);
    setThresholdError('');
    try {
      const payload: Record<string, number> = {};
      if (editingThreshold.key === 'inference_latency') {
        payload.inference_latency_ms = val;
      } else if (editingThreshold.key === 'tokens_total') {
        payload.tokens_total = val;
      }
      await axios.put(`${API_BASE}/metrics/runtime-thresholds`, payload);
      setEditingThreshold(null);
      await fetchCatalog();
    } catch (err: any) {
      setThresholdError(err?.response?.data?.detail || err.message || '保存失败');
    } finally {
      setSavingThreshold(false);
    }
  };

  // 打开指标参数编辑弹窗：把 param_values（list 数组/json 对象）转成可编辑字符串
  const openParamsEditor = (m: { key: string; name: string; params?: MetricParamSpec[]; paramValues?: Record<string, unknown> }) => {
    if (!m.params || m.params.length === 0) return;
    const formValues: Record<string, string> = {};
    for (const spec of m.params) {
      const v = m.paramValues?.[spec.name];
      if (spec.type === 'list') {
        formValues[spec.name] = Array.isArray(v) ? v.join('\n') : (typeof v === 'string' ? v : '');
      } else if (spec.type === 'json') {
        formValues[spec.name] = typeof v === 'string' ? v : (v != null ? JSON.stringify(v, null, 2) : '');
      } else {
        formValues[spec.name] = typeof v === 'string' ? v : (v != null ? String(v) : '');
      }
    }
    setParamFormValues(formValues);
    setEditingParams({ metricKey: m.key, label: m.name, specs: m.params });
    setParamError('');
  };

  const handleSaveParams = async () => {
    if (!editingParams) return;
    setSavingParams(true);
    setParamError('');
    try {
      const payload: Record<string, unknown> = {};
      for (const spec of editingParams.specs) {
        const v = paramFormValues[spec.name] ?? '';
        if (spec.type === 'list') {
          payload[spec.name] = v.split('\n').map(s => s.trim()).filter(Boolean);
        } else if (spec.type === 'json') {
          payload[spec.name] = v; // 字符串形式传，后端 JSON.parse 规范化
        } else {
          payload[spec.name] = v;
        }
      }
      await axios.put(`${API_BASE}/metrics/${editingParams.metricKey}/params`, { params: payload });
      setEditingParams(null);
      await fetchCatalog();
    } catch (err: any) {
      setParamError(err?.response?.data?.detail || err.message || '保存失败');
    } finally {
      setSavingParams(false);
    }
  };

  // 合并所有指标为统一列表
  const allMetrics = useMemo(() => {
    if (!data) return [];
    const list: Array<{
      key: string;
      name: string;
      category: string;
      categoryLabel: string;
      type: string; // 单轮/多轮/效能
      threshold: string;
      note: string;
      source: string; // 内置/自定义
      isRuntime?: boolean;
      rawThreshold?: number;
      params?: MetricParamSpec[];
      paramValues?: Record<string, string | string[] | object>;
    }> = [];

    // 内置指标
    for (const m of data.builtin) {
      const isRuntime = m.is_runtime || m.key === 'inference_latency' || m.key === 'tokens_total';
      const thresholdDisplay = isRuntime
        ? m.key === 'inference_latency'
          ? `${(m.threshold / 1000).toFixed(0)}s`
          : `${(m.threshold / 1000).toFixed(0)}K`
        : String(m.threshold);
      list.push({
        key: m.key,
        name: BUILTIN_LABELS[m.key] || m.key,
        category: BUILTIN_CATEGORY[m.key] || '内置指标',
        categoryLabel: BUILTIN_CATEGORY[m.key] || '内置指标',
        type: isRuntime ? '效能' : '单轮',
        threshold: thresholdDisplay,
        note: BUILTIN_NOTES[m.key] || (isRuntime ? '越低越好，用于运行稳定性评测' : ''),
        source: '内置',
        isRuntime,
        rawThreshold: m.threshold,
      });
    }

    // DeepEval 指标
    for (const m of data.deepeval) {
      list.push({
        key: m.key,
        name: m.label,
        category: m.category,
        categoryLabel: m.category_label || m.category,
        type: m.requires.includes('turns') ? '多轮' : '单轮',
        threshold: `≥${m.gate_threshold.toFixed(1)}`,
        note: m.note || '',
        source: '内置',
        params: m.params,
        paramValues: m.param_values,
      });
    }

    // 自定义指标
    for (const m of data.custom) {
      const catLabel = m.category || '自定义指标';
      list.push({
        key: m.key,
        name: m.name,
        category: catLabel,
        categoryLabel: catLabel,
        type: '—',
        threshold: String(m.threshold),
        note: `${KIND_LABELS[m.kind] || m.kind} · ${m.is_active ? '启用' : '已停用'}`,
        source: '自定义',
      });
    }

    return list;
  }, [data]);

  // 过滤后的指标（按 categoryLabel 过滤，与下拉选项及分组字段保持一致）
  const filteredMetrics = useMemo(() => {
    let result = allMetrics;
    if (filterCategory !== 'all') {
      result = result.filter(m => m.categoryLabel === filterCategory);
    }
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      result = result.filter(
        m => m.name.toLowerCase().includes(q) || m.key.toLowerCase().includes(q) || m.categoryLabel.toLowerCase().includes(q)
      );
    }
    return result;
  }, [allMetrics, search, filterCategory]);

  // 按类别分组
  const groupedMetrics = useMemo(() => {
    const groups = new Map<string, typeof allMetrics>();
    for (const m of filteredMetrics) {
      if (!groups.has(m.categoryLabel)) groups.set(m.categoryLabel, []);
      groups.get(m.categoryLabel)!.push(m);
    }
    return Array.from(groups.entries());
  }, [filteredMetrics]);

  // 当前可见分组是否全部处于折叠态（用于「全部展开/收起」按钮态判定）
  const visibleGroupLabels = groupedMetrics.map(([label]) => label);
  const allCollapsed =
    visibleGroupLabels.length > 0 && visibleGroupLabels.every(l => collapsedGroups.has(l));

  // 全部展开：清空折叠集合（清空 localStorage 持久值，回到默认全展开）
  const expandAll = () => {
    setCollapsedGroups(new Set());
    try {
      localStorage.setItem('metrics_catalog_collapsed', JSON.stringify([]));
    } catch {
      // 忽略 localStorage 不可用
    }
  };

  // 全部收起：把当前可见分组全部加入折叠集合并持久化
  const collapseAll = () => {
    const next = new Set(collapsedGroups);
    for (const l of visibleGroupLabels) next.add(l);
    setCollapsedGroups(next);
    try {
      localStorage.setItem('metrics_catalog_collapsed', JSON.stringify(Array.from(next)));
    } catch {
      // 忽略 localStorage 不可用
    }
  };

  // 所有类别选项
  const categoryOptions = useMemo(() => {
    const cats = new Set<string>();
    for (const m of allMetrics) cats.add(m.categoryLabel);
    return Array.from(cats);
  }, [allMetrics]);

  // 来源标签颜色
  const sourceColor = (source: string): React.CSSProperties => {
    switch (source) {
      case '内置':
        return { background: '#e3f2fd', color: '#1565c0' };
      case '自定义':
        return { background: '#e8f5e9', color: '#2e7d32' };
      default:
        return { background: '#f5f5f5', color: '#666' };
    }
  };

  if (loading) {
    return <div style={{ textAlign: 'center', padding: 60, color: '#888' }}>加载指标列表中…</div>;
  }

  return (
    <div>
      <p className="page-desc" style={{ marginBottom: 12 }}>
        系统支持的全部评测指标（{allMetrics.length} 项），包括内置质量指标、运行效能指标和自定义指标。
      </p>

      {/* 筛选栏 */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 16, alignItems: 'center' }}>
        <div className="form-field" style={{ marginBottom: 0 }}>
          <input
            style={{ width: 220 }}
            placeholder="搜索指标名称/key/类别…"
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
        </div>
        <div className="form-field" style={{ marginBottom: 0 }}>
          <select style={{ width: 150, cursor: 'pointer' }} value={filterCategory} onChange={e => setFilterCategory(e.target.value)}>
            <option value="all">全部类别</option>
            {categoryOptions.map(cat => (
              <option key={cat} value={cat}>{cat}</option>
            ))}
          </select>
        </div>
        <span style={{ fontSize: 12, color: '#aaa' }}>
          共 {filteredMetrics.length} 项
        </span>
        {groupedMetrics.length > 0 && (
          <button
            type="button"
            className="btn btn-sm btn-secondary"
            style={{ marginLeft: 'auto' }}
            onClick={allCollapsed ? expandAll : collapseAll}
            title={allCollapsed ? '展开所有分组' : '收起所有分组'}
          >
            {allCollapsed ? '全部展开' : '全部收起'}
          </button>
        )}
      </div>

      {/* 分组表格 */}
      {groupedMetrics.length === 0 ? (
        <div style={{ textAlign: 'center', padding: 40, color: '#aaa' }}>
          未找到匹配的指标
        </div>
      ) : (
        groupedMetrics.map(([groupLabel, items]) => {
          const collapsed = collapsedGroups.has(groupLabel);
          return (
          <div key={groupLabel} style={{ marginBottom: 20 }}>
            <div
              onClick={() => toggleGroup(groupLabel)}
              style={{
                fontSize: 13,
                fontWeight: 600,
                color: '#555',
                marginBottom: 8,
                paddingBottom: 6,
                borderBottom: '2px solid #eee',
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                cursor: 'pointer',
                userSelect: 'none',
              }}
              title={collapsed ? '点击展开' : '点击收起'}
            >
              <span style={{ display: 'inline-block', width: 12, fontSize: 10, color: '#999' }}>{collapsed ? '▶' : '▼'}</span>
              {groupLabel}
              <span style={{ fontSize: 11, color: '#aaa', fontWeight: 400 }}>({items.length})</span>
              <span style={{ fontSize: 11, color: '#bbb', fontWeight: 400, marginLeft: 'auto' }}>{collapsed ? '展开' : '收起'}</span>
            </div>
            {!collapsed && (
            <table className="results-table">
              <thead>
                <tr>
                  <th style={{ width: 180 }}>指标名称</th>
                  <th style={{ width: 70 }}>来源</th>
                  <th style={{ width: 60 }}>类型</th>
                  <th style={{ width: 120 }}>阈值</th>
                  <th>说明</th>
                  <th style={{ width: 140 }}>Key</th>
                </tr>
              </thead>
              <tbody>
                {items.map(m => (
                  <tr key={m.key}>
                    <td style={{ fontWeight: 500 }}>{m.name}</td>
                    <td>
                      <span
                        style={{
                          padding: '2px 8px',
                          borderRadius: 4,
                          fontSize: 11,
                          ...sourceColor(m.source),
                        }}
                      >
                        {m.source}
                      </span>
                    </td>
                    <td style={{ color: '#666' }}>{m.type}</td>
                    <td style={{ color: '#666' }}>
                      {m.threshold}
                      {m.isRuntime && (
                        <button
                          className="btn btn-sm btn-secondary"
                          style={{ marginLeft: 6, fontSize: 11, padding: '1px 6px' }}
                          onClick={() => {
                            setEditingThreshold({ key: m.key, label: m.name, value: m.rawThreshold ?? 0 });
                            setThresholdValue(String(m.rawThreshold ?? ''));
                            setThresholdError('');
                          }}
                        >
                          编辑
                        </button>
                      )}
                    </td>
                    <td style={{ color: '#888', fontSize: 12 }}>
                      {m.note || '—'}
                      {m.params && m.params.length > 0 && (
                        <button
                          className="btn btn-sm btn-secondary"
                          style={{ marginLeft: 8, fontSize: 11, padding: '1px 6px' }}
                          onClick={() => openParamsEditor(m)}
                          title="配置该指标所需的参数"
                        >
                          参数
                        </button>
                      )}
                    </td>
                    <td style={{ fontFamily: 'monospace', fontSize: 11, color: '#999' }}>
                      {m.key}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            )}
          </div>
          );
        })
      )}

      {/* 未安装 deepeval 提示 */}
      {data && data.deepeval.length === 0 && (
        <div style={{ marginTop: 16, padding: 16, textAlign: 'center', fontSize: 13, color: '#888' }}>
          当前未安装 deepeval，仅展示内置指标。
          安装 <code style={{ background: '#f5f5f5', padding: '2px 6px', borderRadius: 3 }}>deepeval&gt;=4.0.6</code> 可解锁 25+ 项高级评测指标（安全/RAG/工具/推理/多轮等）。
        </div>
      )}

      {/* 运行时阈值编辑弹窗 */}
      {editingThreshold && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}>
          <div className="card history-card" style={{ maxWidth: 400, width: '90%' }}>
            <h3 style={{ marginTop: 0, marginBottom: 16 }}>编辑全局默认阈值 · {editingThreshold.label}</h3>
            {thresholdError && <div style={{ padding: '8px 12px', background: '#ffebee', color: '#c62828', borderRadius: 6, marginBottom: 12, fontSize: 13 }}>{thresholdError}</div>}
            <div className="form-field" style={{ marginBottom: 12 }}>
              <label>
                {editingThreshold.key === 'inference_latency' ? '推理时延上限（毫秒）' : 'Tokens 总消耗上限'}
              </label>
              <input
                type="number"
                value={thresholdValue}
                onChange={e => setThresholdValue(e.target.value)}
                placeholder={editingThreshold.key === 'inference_latency' ? '如 120000' : '如 1000000'}
              />
              <p style={{ fontSize: 11, color: '#888', marginTop: 4 }}>
                此阈值为全局默认值，新创建的任务将使用此值。已有任务可在任务详情页单独配置。
              </p>
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button className="btn btn-secondary" onClick={() => setEditingThreshold(null)}>取消</button>
              <button className="btn btn-primary" disabled={savingThreshold} onClick={handleSaveThreshold}>
                {savingThreshold ? '保存中…' : '保存'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 指标参数编辑弹窗 */}
      {editingParams && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}>
          <div className="card history-card" style={{ maxWidth: 560, width: '90%', maxHeight: '85vh', overflowY: 'auto' }}>
            <h3 style={{ marginTop: 0, marginBottom: 4 }}>配置指标参数 · {editingParams.label}</h3>
            <p style={{ fontSize: 11, color: '#888', marginTop: 0, marginBottom: 16 }}>
              参数为全局默认值，新创建的评测任务将使用此配置。未配置必填参数的指标会被自动跳过。
            </p>
            {paramError && <div style={{ padding: '8px 12px', background: '#ffebee', color: '#c62828', borderRadius: 6, marginBottom: 12, fontSize: 13 }}>{paramError}</div>}
            {editingParams.specs.map(spec => (
              <div key={spec.name} className="form-field" style={{ marginBottom: 16 }}>
                <label>
                  {spec.label}
                  {spec.required && <span style={{ color: '#c62828', marginLeft: 4 }}>*</span>}
                </label>
                {spec.type === 'list' ? (
                  <textarea
                    rows={4}
                    style={{ width: '100%', fontFamily: 'monospace', fontSize: 12 }}
                    value={paramFormValues[spec.name] ?? ''}
                    onChange={e => setParamFormValues(prev => ({ ...prev, [spec.name]: e.target.value }))}
                    placeholder={spec.placeholder}
                  />
                ) : spec.type === 'json' ? (
                  <textarea
                    rows={8}
                    style={{ width: '100%', fontFamily: 'monospace', fontSize: 12 }}
                    value={paramFormValues[spec.name] ?? ''}
                    onChange={e => setParamFormValues(prev => ({ ...prev, [spec.name]: e.target.value }))}
                    placeholder={spec.placeholder}
                  />
                ) : (
                  <input
                    style={{ width: '100%' }}
                    value={paramFormValues[spec.name] ?? ''}
                    onChange={e => setParamFormValues(prev => ({ ...prev, [spec.name]: e.target.value }))}
                    placeholder={spec.placeholder}
                  />
                )}
                {spec.type === 'list' && (
                  <p style={{ fontSize: 11, color: '#888', marginTop: 4 }}>每行一项</p>
                )}
                {spec.type === 'json' && (
                  <p style={{ fontSize: 11, color: '#888', marginTop: 4 }}>合法 JSON 对象</p>
                )}
              </div>
            ))}
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button className="btn btn-secondary" onClick={() => setEditingParams(null)}>取消</button>
              <button className="btn btn-primary" disabled={savingParams} onClick={handleSaveParams}>
                {savingParams ? '保存中…' : '保存'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
