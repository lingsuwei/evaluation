/**
 * 评估详情窗口管理：同一任务的详情窗口只打开一次，再次点击仅 focus 而不导航（避免整页刷新）。
 *
 * 模块级 Map 在 opener 页面生命周期内有效。若 opener 刷新，引用丢失但具名窗口仍在，
 * 此时 window.open 会导航已有窗口（回退为刷新行为，属可接受边界场景）。
 */

const detailWindows = new Map<string, Window>()

/**
 * 打开（或聚焦）评估详情窗口。
 * @param taskId  任务 ID
 * @param extra   附加 URL 查询参数（section / qid / metric / step 等）
 */
export function openDetailWindow(
  taskId: string,
  extra?: Record<string, string | number | null | undefined>,
): void {
  const existing = detailWindows.get(taskId)
  if (existing && !existing.closed) {
    existing.focus()
    return
  }

  const params = new URLSearchParams({ detail: taskId })
  if (extra) {
    for (const [k, v] of Object.entries(extra)) {
      if (v != null) params.set(k, String(v))
    }
  }
  const url = `${window.location.pathname}?${params.toString()}`
  const win = window.open(url, `eval-detail-${taskId}`)
  if (win) detailWindows.set(taskId, win)
}
