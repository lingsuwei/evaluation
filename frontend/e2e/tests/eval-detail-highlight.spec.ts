import { test, expect } from '@playwright/test'

/**
 * E2E：评估详情 — 指标行高亮 / 轮次列高亮交互
 * 前置：至少一条 completed 任务且含质量指标；无则 skip。
 */
test.describe('指标行高亮与轮次列高亮', () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => {
      window.localStorage.setItem('darkMode', 'false')
    })
  })

  // ---------- 辅助：获取一个有质量指标的已完成任务 ----------
  async function getCompletedTaskWithMetrics(request: import('@playwright/test').APIRequestContext) {
    const tasksRes = await request.get('/api/tasks')
    expect(tasksRes.ok()).toBeTruthy()
    const body = await tasksRes.json()
    const tasks: Array<{ id: string; status: string }> = body.tasks ?? body
    const candidates = tasks.filter(t => t.status === 'completed')
    for (const t of candidates) {
      const detailRes = await request.get(`/api/tasks/${t.id}/detail`)
      if (!detailRes.ok()) continue
      const detail = await detailRes.json()
      const ms: Array<{ metric_name: string; avg_score: number | null }> =
        detail.metrics_summary ?? []
      if (ms.length > 0) {
        return { id: t.id, detail, metrics: ms }
      }
    }
    return null
  }

  test('从指标概览点击跳转题目明细时高亮该指标行', async ({ page, request }) => {
    const target = await getCompletedTaskWithMetrics(request)
    test.skip(!target, '无含质量指标的已完成任务，跳过')

    await page.goto(`/?detail=${target.id}`)
    await expect(page.getByRole('heading', { name: '评测指标' })).toBeVisible({
      timeout: 15_000,
    })

    // 展开第一个指标卡片（可能默认折叠）
    const firstCard = page.locator('details.edp-cat-card').first()
    const isOpen = await firstCard.getAttribute('open')
    if (!isOpen) {
      await firstCard.locator('summary').click()
    }

    // 点击指标概览表的第一行指标
    const overviewRow = firstCard.locator('tr.edp-expand-row').first()
    await expect(overviewRow).toBeVisible()
    const metricNameText = await overviewRow.locator('.edp-mn').textContent()
    expect(metricNameText).toBeTruthy()
    await overviewRow.click()

    // 题目明细矩阵表出现，且该指标行高亮
    const matrix = page.locator('table.edp-matrix')
    await expect(matrix).toBeVisible({ timeout: 10_000 })
    const activeRow = matrix.locator('tbody tr.row-active')
    await expect(activeRow).toHaveCount(1)
    const activeRowLabel = await activeRow.locator('td.edp-lbl').textContent()
    expect(activeRowLabel).toContain(metricNameText!.trim())

    // 第一轮列也应高亮（selR 默认 = 1）
    const firstColHeader = matrix.locator('thead th').nth(1)
    await expect(firstColHeader).toHaveClass(/col-active/)
  })

  test('矩阵表中点击指标名切换行高亮，再次点击取消', async ({ page, request }) => {
    const target = await getCompletedTaskWithMetrics(request)
    test.skip(!target, '无含质量指标的已完成任务，跳过')
    // 需要至少 2 个指标行
    const qualityMetrics = target.metrics.filter(
      (m: any) => !m.metric_name.startsWith('inference_') && !m.metric_name.startsWith('tokens_'),
    )
    test.skip(qualityMetrics.length < 2, '指标行不足 2 个，跳过切换测试')

    await page.goto(`/?detail=${target.id}`)
    // 直接进入题目明细：点击「查看题目明细」
    const viewDetailLink = page.getByText('查看题目明细 →')
    await expect(viewDetailLink).toBeVisible({ timeout: 15_000 })
    await viewDetailLink.click()

    const matrix = page.locator('table.edp-matrix')
    await expect(matrix).toBeVisible({ timeout: 10_000 })

    // 初始无行高亮
    await expect(matrix.locator('tbody tr.row-active')).toHaveCount(0)

    // 点击第一个可点击的指标行首列
    const metricRows = matrix.locator('tbody tr').filter({
      has: page.locator('td.edp-lbl[style*="cursor"]'),
    })
    const firstRow = metricRows.nth(0)
    const secondRow = metricRows.nth(1)
    await firstRow.locator('td.edp-lbl').click()
    await expect(firstRow).toHaveClass(/row-active/)
    await expect(secondRow).not.toHaveClass(/row-active/)

    // 切换到第二个指标行
    await secondRow.locator('td.edp-lbl').click()
    await expect(secondRow).toHaveClass(/row-active/)
    await expect(firstRow).not.toHaveClass(/row-active/)

    // 再次点击取消
    await secondRow.locator('td.edp-lbl').click()
    await expect(matrix.locator('tbody tr.row-active')).toHaveCount(0)
  })

  test('左树点击轮次高亮整列', async ({ page, request }) => {
    const target = await getCompletedTaskWithMetrics(request)
    test.skip(!target, '无含质量指标的已完成任务，跳过')
    const numRounds = target.detail.task?.inference_rounds ?? 1
    test.skip(numRounds < 2, '单轮任务无轮次网格，跳过列高亮测试')

    await page.goto(`/?detail=${target.id}`)
    await page.getByText('查看题目明细 →').click()
    const matrix = page.locator('table.edp-matrix')
    await expect(matrix).toBeVisible({ timeout: 10_000 })

    // 点击左树第二轮
    const roundCell = page.locator('.edp-round-cell').nth(1) // R2
    await expect(roundCell).toBeVisible()
    await roundCell.click()

    // 第二轮列表头高亮
    const secondColHeader = matrix.locator('thead th').nth(2) // R2
    await expect(secondColHeader).toHaveClass(/col-active/)
    // 第一轮列表头不高亮
    const firstColHeader = matrix.locator('thead th').nth(1)
    await expect(firstColHeader).not.toHaveClass(/col-active/)

    // 第二轮列的数据单元格也有 col-active
    const activeCells = matrix.locator('tbody td.col-active')
    const count = await activeCells.count()
    expect(count).toBeGreaterThan(0)
  })
})
