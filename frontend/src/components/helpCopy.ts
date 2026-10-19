/** 过程点 / 场景子步骤等页面共用的帮助文案 */

export const ENFORCE_ORDER_CHECKPOINT = {
  title: '顺序依赖（默认关闭）',
  body:
    '勾选后，多轮稳定性评测中，过程点不仅须命中，Trace 中 Span 出现顺序还须符合「执行顺序」列。可在本表「顺序依赖」列直接勾选，或在下方新增/编辑表单中配置。',
  formHint: '默认关闭；开启后纳入多轮链条顺序校验。',
  orderFieldHint: '列表排序用；是否校验先后见「顺序依赖」列。',
} as const

export const ENFORCE_ORDER_SCENARIO = {
  title: '顺序依赖（默认关闭）',
  body:
    '勾选后，多轮稳定性会校验 Trace 绑定顺序须符合「顺序」列。可在本表「顺序依赖」列直接勾选，或在下方新增/编辑表单中配置。',
  formHint: '默认关闭；开启后纳入多轮链条顺序校验。',
  orderFieldHint: '列表排序用；是否校验先后见「顺序依赖」列。',
} as const

export const HISTORICAL_PERCENTILE = {
  title: 'P75 / P90 是什么？',
  intro: '历史 Run 的参考线（业界常用百分位写法）：',
  p75: 'P75 ≈ 约 75% 的历史 Run 比它更快；',
  p90: 'P90 ≈ 约 90% 比它更快，可当作「大多数情况的上限」。样本越少，参考价值越低。',
  checkpointFootnote: '本列展示同 Agent 历史命中 Span 的 P75；暂无数据时显示「—」。',
} as const

export const CHECKPOINT_REQUIRED = {
  title: '必达 vs 可选',
  body:
    '必达：Trace 中未命中该过程点则判为违规（必达缺失）。可选：仅观察本次是否执行；未命中显示为「可选 · 未执行」，不计入必达合规率。',
  formHint: '勾选后未命中将判为必达缺失；不勾选则仅记录是否执行。',
  tableHint: '必达列「是」表示纳入必达合规；「否」为可选观察项。',
} as const

export const TASK_PERCENTILE = {
  title: 'P75 / P90 是什么？',
  intro:
    '基于本任务内各题 Run 的分布（通常每题 1 条；同题多轮时每轮各 1 条），用来看题目间谁偏慢/偏费。',
  p75: 'P75：约 75% 的本任务 Run 时延/Tokens ≤ 这个值',
  p90: 'P90：约 90% 的本任务 Run ≤ 这个值，可理解为「本批大多数 Run 的上限参考」',
  footnote:
    '样本数较少时，P75/P90 波动较大，仅供参考；业界 APM / LLM 观测平台普遍使用这一命名。',
} as const

export const TRACE_CHAIN_COMPARE = {
  title: '多轮 trace 链条对比规则',
  body: '基准链 = 所有轮共有的 trace 步骤（aggregate_key 交集）；',
  baseline: '红 ✗ = 必达步骤缺失（计入扣分）；',
  optional: '灰 · = 可选步骤缺失（仅部分轮出现，不扣分）；',
  align: '按 aggregate_key 纵向对齐，100% = 命中全部基准链步骤；',
  click: '点击某轮查看完整推理轨迹详情。',
  badge: '节点右上角红色「必」徽章 = 该 trace 节点对应一个必达过程点；hover 查看详情。',
  placeholder: '基准行末尾红色虚线卡 = 该必达点在所有轮次中均未命中。',
} as const
