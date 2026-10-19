/** Langfuse 观测就绪状态 — Agent 注册 / 评测 / Trace 对比分级提示 */

export const LANGFUSE_TRACE_FEATURES = [
  'Trace 探索与物化',
  '推理链 / Span 树对比',
  '过程点 Preview',
  '场景子步骤 trace 下钻',
] as const

export type LangfuseReadinessStatus = 'ready' | 'unbound'

export interface AgentLangfuseFields {
  langfuse_project?: string | null
}

export function isLangfuseTraceReady(
  agent: AgentLangfuseFields | null | undefined,
): boolean {
  return Boolean((agent?.langfuse_project || '').trim())
}

export function getLangfuseReadinessMeta(
  agent: AgentLangfuseFields | null | undefined,
): {
  status: LangfuseReadinessStatus
  badge: string
  color: string
  hint: string
} {
  if (isLangfuseTraceReady(agent)) {
    return {
      status: 'ready',
      badge: '观测就绪',
      color: '#389e0d',
      hint: 'Langfuse 已绑定，Trace 对比与下钻可用（仍需 Run 物化成功）。',
    }
  }
  return {
    status: 'unbound',
    badge: '观测未就绪',
    color: '#d48806',
    hint: '未绑定 Langfuse：质量/效率评测仍可进行，Trace 对比与下钻不可用。',
  }
}

export const LANGFUSE_AGENT_FORM_NOTICE =
  'Langfuse 由 onto-agent 观测配置自动匹配。未启用或未匹配时仍可保存 Agent，但 Trace 探索、推理链对比、过程点 Preview、场景 trace 下钻等能力不可用。'

export function langfuseStatusHintFromProbe(probeStatus?: string | null): string {
  switch (probeStatus) {
    case 'matched':
      return '已从 onto-agent 自动匹配；Trace 相关能力可用（Run 需物化成功）。'
    case 'not_configured':
      return 'Agent 未配置 Langfuse：可保存，但 Trace 对比/下钻/物化不可用；评测将使用 eval 默认项目读 trace（若 Key 不一致可能仍失败）。'
    case 'not_registered':
      return 'Agent 已配置 Langfuse，但 public_key 未在 eval 注册表：请在 langfuse.projects.json 补充后「从 Agent 同步」并保存。'
    case 'host_mismatch':
      return 'Agent 与 eval 的 Langfuse Host 不一致：trace 写入和读取指向不同实例，无法互通。请统一 LANGFUSE_HOST。'
    case 'eval_disabled':
      return 'eval 未启用 Langfuse，Trace 相关能力不可用。'
    default:
      return '保存时将尝试从 onto-agent 自动匹配；建议先「连接预检」或「从 Agent 同步」。'
  }
}

/** L1：在线评测软阻断 — 返回 true 表示用户确认继续 */
export function confirmOnlineEvalWithoutLangfuseTrace(agentLabel: string): boolean {
  const features = LANGFUSE_TRACE_FEATURES.map((f) => `• ${f}`).join('\n')
  return window.confirm(
    `Agent「${agentLabel}」尚未绑定 Langfuse 项目空间。\n\n` +
      `仍可继续在线评测（质量/效率分数正常），但以下能力可能不可用：\n${features}\n\n` +
      `建议：Agent 管理 → 从 Agent 同步 Langfuse → 保存后再评测。\n\n` +
      `仍要继续本次评测？`,
  )
}

/** L2：Trace 对比 / 下钻硬阻断说明 */
export function alertLangfuseTraceBlocked(actionLabel = '该操作'): void {
  const features = LANGFUSE_TRACE_FEATURES.join('、')
  window.alert(
    `${actionLabel}需要 Agent 已完成 Langfuse 绑定（onto-agent 启用观测且与 eval 注册表匹配）。\n\n` +
      `当前不可用：${features}。\n\n` +
      `请前往 Agent 管理 → 编辑 Agent →「从 Agent 同步」→ 保存。`,
  )
}

export function langfuseProbeStatusIsBlocking(probeStatus?: string | null): boolean {
  return probeStatus === 'not_configured' || probeStatus === 'not_registered' || probeStatus === 'eval_disabled' || probeStatus === 'host_mismatch'
}
