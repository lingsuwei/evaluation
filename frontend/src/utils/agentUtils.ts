/** Agent 列表展示：强调「一条记录 = 一个可评测版本」 */

export interface AgentVersionFields {
  id: string
  name: string
  logical_agent_id?: string | null
  version?: string | null
  version_tag?: string | null
}

export function formatAgentVersionLabel(agent: AgentVersionFields): string {
  const logical = (agent.logical_agent_id || '').trim()
  const ver = (agent.version || '').trim()
  const tag = (agent.version_tag || '').trim()
  if (logical && ver) {
    const tagPart = tag ? ` [${tag}]` : ''
    return `${logical} · ${ver}${tagPart} — ${agent.name}`
  }
  if (ver) {
    return `${ver} — ${agent.name} (${agent.id})`
  }
  return `${agent.name} (${agent.id})`
}

/** 同一 logical_agent_id 下是否已有相同 version（注册新版本时用） */
export function isDuplicateAgentVersion(
  agents: AgentVersionFields[],
  logicalAgentId: string,
  version: string,
  excludeAgentId?: string,
): boolean {
  const lid = logicalAgentId.trim()
  const ver = version.trim()
  if (!lid || !ver) return false
  return agents.some((a) => {
    if (excludeAgentId && a.id === excludeAgentId) return false
    return (a.logical_agent_id || '').trim() === lid && (a.version || '').trim() === ver
  })
}

export function compareAgentsByLogicalAndVersion(
  a: AgentVersionFields,
  b: AgentVersionFields,
): number {
  const la = (a.logical_agent_id || `\xff${a.id}`).toLowerCase()
  const lb = (b.logical_agent_id || `\xff${b.id}`).toLowerCase()
  if (la !== lb) return la.localeCompare(lb)
  const va = (a.version || '').toLowerCase()
  const vb = (b.version || '').toLowerCase()
  if (va !== vb) return va.localeCompare(vb)
  return a.name.localeCompare(b.name)
}
