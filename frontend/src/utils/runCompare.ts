const API_BASE = '/api'

export async function createRunComparison(
  leftRunId: string,
  rightRunId: string,
  comparisonScope = 'manual',
): Promise<string> {
  const res = await fetch(`${API_BASE}/comparisons/compare`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      left_run_id: leftRunId,
      right_run_id: rightRunId,
      comparison_scope: comparisonScope,
    }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(
      typeof body.detail === 'string' ? body.detail : '创建推理链对比失败',
    )
  }
  const body = await res.json()
  return String(body.id ?? body.comparison_id)
}
