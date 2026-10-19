import { useState } from 'react'
import { PageHeader } from './PageHeader'

/** GuidePage 跳转目标页类型（对齐 App.tsx 的 TabType 子集） */
type GuideNavPage =
  | 'home' | 'settings' | 'langfuse_projects' | 'agents' | 'datasets'
  | 'online_eval' | 'offline_eval' | 'history' | 'reports' | 'chat'

/* ============ 步骤数据 ============ */
interface StepField {
  label: string
}
interface SubStep {
  id: string
  title: string
  desc: React.ReactNode
  target: string
  action: string
  page: GuideNavPage
}
interface GuideStep {
  num: number
  title: string
  tag: 'required' | 'online' | 'optional'
  tagLabel: string
  desc: React.ReactNode
  fields: StepField[]
  target: React.ReactNode
  action: string
  page: GuideNavPage
  optional?: boolean
  substeps?: SubStep[]
  /** AI 助手副入口（用于 step 1/3：能用 AI 助手完成） */
  altAction?: string
  altPage?: GuideNavPage
}

const STEPS: GuideStep[] = [
  {
    num: 1,
    title: '配置评估模型',
    tag: 'required',
    tagLabel: '必做',
    desc: (<>添加 <strong>LLM 评估模型</strong>（如 GLM-4.7），填写 API Key、Base URL、模型名称。评估模型用作 LLM Judge，对回答进行多维度语义评分。</>),
    fields: [
      { label: '厂商预设' },
      { label: 'API Key' },
      { label: 'Base URL' },
      { label: '连通性测试' },
    ],
    target: (<>前往 → <strong>系统设置 · 模型配置</strong></>),
    action: '前往配置',
    page: 'settings',
    altAction: '或：用 AI 助手添加',
    altPage: 'chat',
  },
  {
    num: 2,
    title: '配置 Langfuse 观测',
    tag: 'optional',
    tagLabel: '可选',
    desc: (<>配置 <strong>Langfuse 项目空间</strong>，用于 Trace 采集与过程分析。仅勾选<strong>质量评测</strong>维度时无需配置；若启用<strong>过程一致性</strong>、<strong>场景交付</strong>或<strong>运行稳定性</strong>维度，则必须配置 Langfuse 以采集推理 Trace。</>),
    fields: [
      { label: 'Langfuse 地址' },
      { label: 'Public Key' },
      { label: 'Secret Key' },
      { label: '项目空间' },
    ],
    target: (<>前往 → <strong>观测打点</strong></>),
    action: '前往配置',
    page: 'langfuse_projects',
  },
  {
    num: 3,
    title: '注册 Agent',
    tag: 'online',
    tagLabel: '仅在线评估',
    desc: (<>注册被评测的 <strong>Agent</strong>，填写推理接口地址、端点、超时时间、认证方式，保存后<strong>激活</strong>。<strong>离线评估无需此步</strong>（评测集需含 model_answer）。</>),
    fields: [
      { label: '推理接口 URL' },
      { label: '认证方式' },
      { label: '超时时间' },
      { label: '激活机制' },
    ],
    target: (<>前往 → <strong>Agent 管理</strong></>),
    action: '前往配置',
    page: 'agents',
    optional: true,
    altAction: '或：用 AI 助手注册',
    altPage: 'chat',
  },
  {
    num: 4,
    title: '导入评测集',
    tag: 'required',
    tagLabel: '必做',
    desc: (<>上传 <strong>JSON / CSV / XLSX</strong> 评测数据集，或从 Langfuse 导入历史 trace。每条用例含 question / ground_truth / context / model_answer / judging_mode 字段。</>),
    fields: [
      { label: 'question' },
      { label: 'ground_truth' },
      { label: 'context' },
      { label: 'model_answer' },
      { label: 'judging_mode' },
    ],
    target: (<>前往 → <strong>评测管理 · 评测集</strong></>),
    action: '前往导入',
    page: 'datasets',
  },
  {
    num: 5,
    title: '启动评估',
    tag: 'required',
    tagLabel: '必做',
    desc: (<>选择评测集、评估因子（忠诚度 / 回答相关性 / 正确性 / 时延 / Tokens）、评估模型、推理轮次。<strong>AI 助手</strong>可对话式生成配置；<strong>在线评估</strong>调 Agent 实时推理并采集 Trace；<strong>离线评估</strong>使用预置答案。</>),
    fields: [
      { label: '评测集选择' },
      { label: '评估因子' },
      { label: '推理轮次' },
      { label: '在线 / 离线' },
    ],
    target: '',
    action: '',
    page: 'home',
    substeps: [
      {
        id: '5a',
        title: 'AI 助手 · 对话生成（推荐）',
        desc: '描述评测需求（如"用 GLM 评估 agent-001 在 ds_faq 上的忠实度，跑 3 轮"），AI 自动查询资源、推荐指标并组装配置草稿，确认后即可启动。',
        target: '前往 → AI 助手',
        action: '使用 AI 助手',
        page: 'chat',
      },
      {
        id: '5b',
        title: '在线评估 · 调 Agent 实时推理',
        desc: '选择已注册并激活的 Agent，调推理接口实时生成答案并采集 Trace。适用于 Agent 已就绪、需要观测推理过程的场景。',
        target: '前往 → 开始评估（在线）',
        action: '开始在线评估',
        page: 'home',
      },
      {
        id: '5c',
        title: '离线评估 · 使用预置答案',
        desc: '评测集需含 model_answer 字段，跳过 Agent 推理直接评估。适用于答案已采集、只做质量评估的场景。',
        target: '前往 → 开始评估（离线）',
        action: '开始离线评估',
        page: 'offline_eval',
      },
    ],
  },
  {
    num: 6,
    title: '查看结果与下钻',
    tag: 'required',
    tagLabel: '必做',
    desc: (<>评估完成后查看<strong>四维结论</strong>（质量 / 过程一致性 / 场景交付 / 运行稳定性）、<strong>雷达图</strong>、<strong>CoT 思维链</strong>、通过率统计。支持指标 → 题目下钻、Trace span 链条对比、场景子步骤矩阵。</>),
    fields: [
      { label: 'verdict 四维' },
      { label: '雷达图' },
      { label: 'CoT 思维链' },
      { label: 'Trace 下钻' },
      { label: '场景矩阵' },
    ],
    target: (<>前往 → <strong>评估结果</strong></>),
    action: '查看结果',
    page: 'history',
  },
  {
    num: 7,
    title: '导出报告与归档',
    tag: 'optional',
    tagLabel: '可选',
    desc: (<>分两步：先在<strong>评估历史</strong>页对目标任务执行导出，再到<strong>报告列表</strong>页查看归档结果。支持 <strong>xlsx / html</strong> 格式，含 Verdict 标签与对比报告。</>),
    fields: [
      { label: '评测报告' },
      { label: '对比报告' },
      { label: 'Verdict 标签' },
      { label: 'xlsx / html' },
    ],
    target: '',
    action: '',
    page: 'history',
    substeps: [
      {
        id: '7a',
        title: '评估历史 · 执行导出',
        desc: '在评估历史页找到目标任务，点击「导出」按钮，选择格式（xlsx / html），系统自动归档。',
        target: '前往 → 评估历史',
        action: '前往导出',
        page: 'history',
      },
      {
        id: '7b',
        title: '报告列表 · 查看归档',
        desc: '导出的报告自动归档至报告列表，支持查看、对比、下载与版本留痕。',
        target: '前往 → 报告列表',
        action: '查看报告',
        page: 'reports',
      },
    ],
  },
]

interface GuidePageProps {
  onNavigate: (page: GuideNavPage) => void
  onOpenHelp: () => void
}

export function GuidePage({ onNavigate, onOpenHelp }: GuidePageProps) {
  const [hoveredStep, setHoveredStep] = useState<number | null>(null)

  return (
    <div className="guide-page-wrap">
      <PageHeader
        title="使用指引"
        description="7 步完成首次评测 · 约 10 分钟跑通"
        icon={
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="10" />
            <polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76" />
          </svg>
        }
        actions={
          <button className="edp-btn" onClick={onOpenHelp}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
              <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
            </svg>
            联机帮助
          </button>
        }
      />

      <div className="guide-page">
        {/* 顶部摘要条 */}
        <div className="guide-summary">
          <div className="guide-summary-stat">
            <div className="guide-summary-stat-num">7</div>
            <div className="guide-summary-stat-label">步骤</div>
          </div>
          <div className="guide-summary-divider" />
          <div className="guide-summary-stat">
            <div className="guide-summary-stat-num">~10min</div>
            <div className="guide-summary-stat-label">新手耗时</div>
          </div>
          <div className="guide-summary-divider" />
          <div className="guide-summary-text">
            按下方顺序完成 <strong>1→7</strong>，即可跑通首次评测。<strong>步骤 1/4</strong> 为必做，<strong>步骤 2</strong> 按需配置；
            <strong>步骤 5</strong> 选择在线或离线入口。
            <div className="guide-summary-skip">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10" />
                <line x1="4.93" y1="4.93" x2="19.07" y2="19.07" />
              </svg>
              离线评估可跳过步骤 3（注册 Agent）
            </div>
          </div>
        </div>

        {/* 垂直时间线 */}
        <div className="timeline">
          {STEPS.map(step => {
            const isHovered = hoveredStep === step.num
            return (
              <div
                key={step.num}
                className="timeline-step"
                onMouseEnter={() => setHoveredStep(step.num)}
                onMouseLeave={() => setHoveredStep(null)}
              >
                <div className={`timeline-node${step.optional ? ' node-optional' : ''}`}>
                  {step.num}
                </div>
                <div
                  className={`timeline-card${step.optional ? ' card-optional' : ''}${isHovered ? ' hovered' : ''}`}
                >
                  <div className="timeline-card-head">
                    <span className="timeline-card-title">{step.title}</span>
                    <div className="timeline-card-tags">
                      <span className={`timeline-tag tag-${step.tag}`}>{step.tagLabel}</span>
                    </div>
                  </div>
                  <div className="timeline-card-desc">{step.desc}</div>
                  <div className="timeline-card-fields">
                    {step.fields.map((f, i) => (
                      <span key={i} className="field-chip">{f.label}</span>
                    ))}
                  </div>

                  {step.substeps ? (
                    <div className="substeps">
                      {step.substeps.map(sub => (
                        <div key={sub.id} className="substep">
                          <div className="substep-num">{sub.id}</div>
                          <div className="substep-body">
                            <div className="substep-title">{sub.title}</div>
                            <div className="substep-desc">{sub.desc}</div>
                          </div>
                          <button className="edp-btn edp-btn-export" onClick={() => onNavigate(sub.page)}>
                            {sub.action}
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                              <line x1="5" y1="12" x2="19" y2="12" />
                              <polyline points="12 5 19 12 12 19" />
                            </svg>
                          </button>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="timeline-card-foot">
                      <span className="timeline-card-target">{step.target}</span>
                      <div className="timeline-card-foot-actions">
                        {step.altAction && step.altPage && (
                          <button className="edp-btn edp-btn-ghost" onClick={() => step.altPage && onNavigate(step.altPage)}>
                            {step.altAction}
                          </button>
                        )}
                        <button className="edp-btn edp-btn-export" onClick={() => onNavigate(step.page)}>
                          {step.action}
                          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <line x1="5" y1="12" x2="19" y2="12" />
                            <polyline points="12 5 19 12 12 19" />
                          </svg>
                        </button>
                      </div>
                    </div>
                  )}

                  {step.substeps && (
                    <div className="timeline-card-fields substep-fields">
                      {step.fields.map((f, i) => (
                        <span key={i} className="field-chip">{f.label}</span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )
          })}
        </div>

        {/* 底部帮助链接 */}
        <div className="guide-foot">
          需要更详细的说明？查看 <a onClick={onOpenHelp}>联机帮助完整文档 ›</a>
        </div>
      </div>
    </div>
  )
}
