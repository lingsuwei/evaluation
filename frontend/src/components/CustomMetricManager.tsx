import { useState, useEffect, useMemo } from 'react';
import axios from 'axios';
import { MetricsCatalog } from './MetricsCatalog';
import { PageHeader } from './PageHeader';

const API_BASE = '/api';

interface CustomMetric {
  id: number;
  name: string;
  kind: string; // geval | dag | code
  category: string | null; // 自定义指标类别（指标集合）
  criteria: string | null;
  dag: any | null;
  code: string | null;
  multi_turn: boolean;
  params: any[] | null;
  threshold: number;
  is_active: boolean;
  created_at: string | null;
  updated_at: string | null;
}

const KIND_LABELS: Record<string, string> = {
  geval: 'G-Eval（自然语言）',
  dag: 'DAG（决策树）',
  code: 'Code（代码沙箱）',
};

export function CustomMetricManager() {
  const [activeView, setActiveView] = useState<'catalog' | 'custom'>('catalog');
  const [metrics, setMetrics] = useState<CustomMetric[]>([]);
  const [loading, setLoading] = useState(true);
  const [editingMetric, setEditingMetric] = useState<CustomMetric | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [testMetric, setTestMetric] = useState<CustomMetric | null>(null);

  // 表单状态
  const [formName, setFormName] = useState('');
  const [formKind, setFormKind] = useState<'geval' | 'dag' | 'code'>('geval');
  const [formCategory, setFormCategory] = useState('');
  const [formCriteria, setFormCriteria] = useState('');
  const [formDag, setFormDag] = useState('');
  const [formCode, setFormCode] = useState('');
  const [formThreshold, setFormThreshold] = useState(0.5);
  const [formMultiTurn, setFormMultiTurn] = useState(false);
  const [formError, setFormError] = useState('');
  const [saving, setSaving] = useState(false);

  // 已使用类别（供表单 datalist 下拉），从已加载指标派生 + 默认类别
  const categoryOptions = useMemo(() => {
    const set = new Set<string>(['自定义指标']);
    for (const m of metrics) {
      if (m.category) set.add(m.category);
    }
    return Array.from(set);
  }, [metrics]);

  // 试运行状态
  const [testQuestion, setTestQuestion] = useState('');
  const [testAnswer, setTestAnswer] = useState('');
  const [testContext, setTestContext] = useState('');
  const [testGroundTruth, setTestGroundTruth] = useState('');
  const [testResult, setTestResult] = useState<{ score: number; reason: string; duration_ms: number } | null>(null);
  const [testing, setTesting] = useState(false);

  const fetchMetrics = async () => {
    setLoading(true);
    try {
      const res = await axios.get(`${API_BASE}/custom-metrics`);
      setMetrics(res.data.items || []);
    } catch (err) {
      console.error('加载自定义指标失败:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchMetrics();
  }, []);

  const openCreateForm = () => {
    setEditingMetric(null);
    setFormName('');
    setFormKind('geval');
    setFormCategory('');
    setFormCriteria('');
    setFormDag('');
    setFormCode('def evaluate(question, answer, context=None, ground_truth=None):\n    score = 0.5\n    reason = "请修改此代码"\n    return {"score": score, "reason": reason}\n');
    setFormThreshold(0.5);
    setFormMultiTurn(false);
    setFormError('');
    setShowForm(true);
  };

  const openEditForm = (metric: CustomMetric) => {
    setEditingMetric(metric);
    setFormName(metric.name);
    setFormKind(metric.kind as 'geval' | 'dag' | 'code');
    setFormCategory(metric.category || '');
    setFormCriteria(metric.criteria || '');
    setFormDag(metric.dag ? JSON.stringify(metric.dag, null, 2) : '');
    setFormCode(metric.code || '');
    setFormThreshold(metric.threshold);
    setFormMultiTurn(metric.multi_turn);
    setFormError('');
    setShowForm(true);
  };

  const handleSave = async () => {
    setFormError('');
    if (!formName.trim()) {
      setFormError('指标名称不能为空');
      return;
    }
    if (formKind === 'geval' && !formCriteria.trim()) {
      setFormError('G-Eval 模式需要填写评判标准');
      return;
    }
    if (formKind === 'dag' && !formDag.trim()) {
      setFormError('DAG 模式需要填写决策树定义');
      return;
    }
    if (formKind === 'code' && !formCode.trim()) {
      setFormError('Code 模式需要填写代码');
      return;
    }

    let dagObj: any = null;
    if (formKind === 'dag' && formDag.trim()) {
      try {
        dagObj = JSON.parse(formDag);
      } catch (e) {
        setFormError('DAG JSON 格式错误：' + (e as Error).message);
        return;
      }
    }

    setSaving(true);
    try {
      const payload: any = {
        name: formName.trim(),
        kind: formKind,
        category: formCategory.trim() || null,
        criteria: formKind === 'geval' ? formCriteria.trim() : null,
        dag: formKind === 'dag' ? dagObj : null,
        code: formKind === 'code' ? formCode : null,
        multi_turn: formMultiTurn,
        threshold: formThreshold,
      };

      if (editingMetric) {
        await axios.put(`${API_BASE}/custom-metrics/${editingMetric.id}`, payload);
      } else {
        await axios.post(`${API_BASE}/custom-metrics`, payload);
      }
      setShowForm(false);
      fetchMetrics();
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err.message;
      setFormError('保存失败：' + detail);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (metric: CustomMetric) => {
    if (!confirm(`确定要停用指标「${metric.name}」吗？`)) return;
    try {
      await axios.delete(`${API_BASE}/custom-metrics/${metric.id}`);
      fetchMetrics();
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err.message;
      alert('删除失败：' + detail);
    }
  };

  const handleTest = async () => {
    if (!testMetric) return;
    setTesting(true);
    setTestResult(null);
    try {
      const res = await axios.post(`${API_BASE}/custom-metrics/${testMetric.id}/test`, {
        question: testQuestion,
        answer: testAnswer,
        context: testContext || null,
        ground_truth: testGroundTruth || null,
      });
      setTestResult(res.data);
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err.message;
      setTestResult({ score: 0.0, reason: '试运行失败：' + detail, duration_ms: 0 });
    } finally {
      setTesting(false);
    }
  };

  const modalOverlay: React.CSSProperties = {
    position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)',
    display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
  };

  return (
    <div className="card history-card">
      <PageHeader
        title="评测指标"
        description="管理评测指标库和自定义指标，评测时在「评估因子」中勾选使用。"
        actions={activeView === 'custom' ? (
          <button className="btn btn-primary" onClick={openCreateForm}>+ 新建指标</button>
        ) : undefined}
      />

      {/* Tab 切换 */}
      <div className="sub-tabs">
        <button
          className={`sub-tab${activeView === 'catalog' ? ' active' : ''}`}
          onClick={() => setActiveView('catalog')}
        >
          指标库
        </button>
        <button
          className={`sub-tab${activeView === 'custom' ? ' active' : ''}`}
          onClick={() => setActiveView('custom')}
        >
          自定义指标
        </button>
      </div>

      {/* 指标库视图 */}
      {activeView === 'catalog' && <MetricsCatalog />}

      {/* 自定义指标视图 */}
      {activeView === 'custom' && (
        <>
          <p className="page-desc" style={{ marginBottom: 16 }}>
            支持三档自定义指标：G-Eval（自然语言评判）、DAG（决策树）、Code（代码沙箱）。可填写「指标类别」按业务域组织多个自定义指标集合；创建后在评测页面的「评估因子」中勾选使用。
          </p>

          {/* 列表 */}
          {loading ? (
            <div style={{ textAlign: 'center', padding: 40, color: '#888' }}>加载中…</div>
          ) : metrics.length === 0 ? (
            <div style={{ textAlign: 'center', padding: 40, color: '#aaa' }}>
              暂无自定义指标，点击「新建指标」创建第一个
            </div>
          ) : (
            <table className="results-table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>名称</th>
                  <th>类型</th>
                  <th>类别</th>
                  <th>阈值</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {metrics.map(m => (
                  <tr key={m.id}>
                    <td>{m.id}</td>
                    <td>{m.name}</td>
                    <td>{KIND_LABELS[m.kind] || m.kind}</td>
                    <td style={{ color: '#666' }}>{m.category || '自定义指标'}</td>
                    <td>{m.threshold}</td>
                    <td>
                      <span style={{
                        padding: '2px 8px', borderRadius: 4, fontSize: 11,
                        background: m.is_active ? '#e8f5e9' : '#f5f5f5',
                        color: m.is_active ? '#2e7d32' : '#999',
                      }}>
                        {m.is_active ? '启用' : '已停用'}
                      </span>
                    </td>
                    <td>
                      <button className="btn btn-sm btn-secondary" style={{ marginRight: 8 }} onClick={() => openEditForm(m)}>编辑</button>
                      <button className="btn btn-sm btn-secondary" style={{ marginRight: 8 }} onClick={() => { setTestMetric(m); setTestResult(null); setTestQuestion(''); setTestAnswer(''); setTestContext(''); setTestGroundTruth(''); }}>试运行</button>
                      <button className="btn btn-sm btn-secondary" style={{ color: '#c0392b' }} onClick={() => handleDelete(m)}>停用</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}

      {/* 创建/编辑弹窗 */}
      {showForm && (
        <div style={modalOverlay}>
          <div className="card history-card" style={{ maxWidth: 600, maxHeight: '80vh', overflowY: 'auto', width: '90%' }}>
            <h3 style={{ marginTop: 0, marginBottom: 16 }}>{editingMetric ? '编辑指标' : '新建指标'}</h3>
            {formError && <div style={{ padding: '8px 12px', background: '#ffebee', color: '#c62828', borderRadius: 6, marginBottom: 12, fontSize: 13 }}>{formError}</div>}
            <div className="form-field" style={{ marginBottom: 12 }}>
              <label>指标名称 *</label>
              <input value={formName} onChange={e => setFormName(e.target.value)} placeholder="如：化工安全合规检查" />
            </div>
            <div className="form-field" style={{ marginBottom: 12 }}>
              <label>类型 *</label>
              <select value={formKind} onChange={e => setFormKind(e.target.value as 'geval' | 'dag' | 'code')}>
                <option value="geval">G-Eval（自然语言评判，由 LLM 打分）</option>
                <option value="dag">DAG（决策树，由 LLM 逐节点判定）</option>
                <option value="code">Code（代码沙箱，确定性无 LLM）</option>
              </select>
            </div>
            <div className="form-field" style={{ marginBottom: 12 }}>
              <label>指标类别（可选，用于自定义指标集合分组）</label>
              <input
                value={formCategory}
                onChange={e => setFormCategory(e.target.value)}
                list="custom-metric-categories"
                placeholder="如：化工安全指标集（留空则归入「自定义指标」）"
              />
              <datalist id="custom-metric-categories">
                {categoryOptions.map(c => (
                  <option key={c} value={c} />
                ))}
              </datalist>
              <p style={{ fontSize: 11, color: '#888', marginTop: 4 }}>同一类别的指标会在指标库页面单独分组展示，便于按业务域组织。</p>
            </div>
            {formKind === 'geval' && (
              <div className="form-field" style={{ marginBottom: 12 }}>
                <label>评判标准 *（自然语言描述打分依据）</label>
                <textarea style={{ minHeight: 100, fontFamily: 'monospace' }} value={formCriteria} onChange={e => setFormCriteria(e.target.value)} placeholder="如：判断回答是否包含安全违规内容，违规给 0 分，合规给 1 分" />
              </div>
            )}
            {formKind === 'dag' && (
              <div className="form-field" style={{ marginBottom: 12 }}>
                <label>决策树定义 *（JSON 格式）</label>
                <textarea style={{ minHeight: 120, fontFamily: 'monospace', fontSize: 12 }} value={formDag} onChange={e => setFormDag(e.target.value)} placeholder='{"root": {"id": "n1", "evaluate": "..."}}' />
              </div>
            )}
            {formKind === 'code' && (
              <div className="form-field" style={{ marginBottom: 12 }}>
                <label>评测代码 *（Python，沙箱执行）</label>
                <textarea style={{ minHeight: 150, fontFamily: 'monospace', fontSize: 12 }} value={formCode} onChange={e => setFormCode(e.target.value)} />
                <p style={{ fontSize: 11, color: '#888', marginTop: 4 }}>函数签名：def evaluate(question, answer, context=None, ground_truth=None) → {"{score, reason}"}</p>
              </div>
            )}
            <div style={{ display: 'flex', gap: 16, marginBottom: 12 }}>
              <div className="form-field">
                <label>门控阈值</label>
                <input type="number" style={{ width: 100 }} value={formThreshold} step={0.1} min={0} max={1} onChange={e => setFormThreshold(parseFloat(e.target.value) || 0)} />
              </div>
              <div className="form-field">
                <label>多轮对话</label>
                <label style={{ display: 'flex', alignItems: 'center', gap: 4, height: 34 }}>
                  <input type="checkbox" checked={formMultiTurn} onChange={e => setFormMultiTurn(e.target.checked)} />
                  <span style={{ fontSize: 12, color: '#888' }}>启用</span>
                </label>
              </div>
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button className="btn btn-secondary" onClick={() => setShowForm(false)}>取消</button>
              <button className="btn btn-primary" disabled={saving} onClick={handleSave}>{saving ? '保存中…' : '保存'}</button>
            </div>
          </div>
        </div>
      )}

      {/* 试运行弹窗 */}
      {testMetric && (
        <div style={modalOverlay}>
          <div className="card history-card" style={{ maxWidth: 600, maxHeight: '80vh', overflowY: 'auto', width: '90%' }}>
            <h3 style={{ marginTop: 0, marginBottom: 16 }}>试运行：{testMetric.name}（{KIND_LABELS[testMetric.kind]}）</h3>
            <div className="form-field" style={{ marginBottom: 12 }}>
              <label>问题 *</label>
              <textarea style={{ minHeight: 60 }} value={testQuestion} onChange={e => setTestQuestion(e.target.value)} placeholder="输入测试问题" />
            </div>
            <div className="form-field" style={{ marginBottom: 12 }}>
              <label>回答 *</label>
              <textarea style={{ minHeight: 60 }} value={testAnswer} onChange={e => setTestAnswer(e.target.value)} placeholder="输入测试回答" />
            </div>
            <div className="form-field" style={{ marginBottom: 12 }}>
              <label>上下文（可选）</label>
              <textarea style={{ minHeight: 40 }} value={testContext} onChange={e => setTestContext(e.target.value)} />
            </div>
            <div className="form-field" style={{ marginBottom: 12 }}>
              <label>参考答案（可选）</label>
              <textarea style={{ minHeight: 40 }} value={testGroundTruth} onChange={e => setTestGroundTruth(e.target.value)} />
            </div>
            {testResult && (
              <div style={{ padding: 12, background: '#f5f5f5', borderRadius: 6, marginBottom: 12, fontSize: 13 }}>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>得分：{testResult.score.toFixed(2)}（耗时 {testResult.duration_ms}ms）</div>
                <div style={{ color: '#555', whiteSpace: 'pre-wrap' }}>{testResult.reason}</div>
              </div>
            )}
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button className="btn btn-secondary" onClick={() => setTestMetric(null)}>关闭</button>
              <button className="btn btn-primary" disabled={testing || !testQuestion || !testAnswer} onClick={handleTest}>{testing ? '运行中…' : '试运行'}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
