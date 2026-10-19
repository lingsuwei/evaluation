import { test, expect } from '@playwright/test'

/**
 * P2.5 E2E：评估历史 → 查看详情 → 切换三 Tab
 * 前置：至少一条 completed/cancelled 任务；无则 skip。
 */
test.describe('P2.5 任务详情三 Tab', () => {
  test('评估历史打开详情并切换 Tab', async ({ page, request }) => {
    const tasksRes = await request.get('/api/tasks')
    expect(tasksRes.ok()).toBeTruthy()
    const body = await tasksRes.json()
    const tasks: Array<{ id: string; status: string }> = body.tasks ?? body
    const target = tasks.find(
      t => t.status === 'completed' || t.status === 'cancelled',
    )
    test.skip(!target, '无 completed/cancelled 任务，跳过详情 Tab 测试')

    await page.goto('/')
    await page.getByRole('button', { name: '评估历史' }).click()
    await expect(page.getByRole('heading', { name: '评估历史' })).toBeVisible()

    const detailBtn = page.getByRole('button', { name: '查看详情' }).first()
    await expect(detailBtn).toBeVisible()
    await detailBtn.click()

    await expect(page.getByRole('heading', { name: '评估结果详情' })).toBeVisible({
      timeout: 15_000,
    })

    const tabQuality = page.getByRole('button', { name: '质量指标' })
    const tabPerf = page.getByRole('button', { name: /运行效能/ })
    const tabScenario = page.getByRole('button', { name: '场景子步骤' })

    await expect(tabQuality).toBeVisible()
    await expect(tabPerf).toBeVisible()
    await expect(tabScenario).toBeVisible()

    await tabPerf.click()
    await expect(page.getByText(/运行效能|加载中|暂无/i).first()).toBeVisible()

    await tabScenario.click()
    await expect(
      page.getByText(/场景子步骤|加载场景|暂无/i).first(),
    ).toBeVisible({ timeout: 15_000 })

    await tabQuality.click()
    await expect(page.getByText(/质量指标概览|过程点合规|暂无/i).first()).toBeVisible()

    await page.getByRole('button', { name: '关闭' }).first().click()
    await expect(page.getByRole('heading', { name: '评估结果详情' })).toBeHidden()
  })

  test('质量 Tab 运行效能深链可切换到 perf Tab', async ({ page, request }) => {
    const tasksRes = await request.get('/api/tasks')
    expect(tasksRes.ok()).toBeTruthy()
    const body = await tasksRes.json()
    const tasks: Array<{ id: string; status: string }> = body.tasks ?? body
    const target = tasks.find(
      t => t.status === 'completed' || t.status === 'cancelled',
    )
    test.skip(!target, '无 completed/cancelled 任务，跳过深链测试')

    await page.goto('/')
    await page.getByRole('button', { name: '评估历史' }).click()
    await page.getByRole('button', { name: '查看详情' }).first().click()
    await expect(page.getByRole('heading', { name: '评估结果详情' })).toBeVisible({
      timeout: 15_000,
    })

    await page.getByRole('button', { name: '质量指标' }).click()

    const firstQuestion = page.locator('[id^="quality-question-"]').first()
    const hasQuestion = await firstQuestion.isVisible().catch(() => false)
    test.skip(!hasQuestion, '本任务无质量指标单题明细')

    await firstQuestion.click()

    const perfLink = page.getByRole('button', { name: /运行效能 · 题/i }).first()
    const hasPerfLink = await perfLink.isVisible().catch(() => false)
    test.skip(!hasPerfLink, '本任务无运行效能深链按钮（可能无 runtime 数据）')

    await perfLink.click()
    await expect(page.getByRole('button', { name: '单题 Run 明细' })).toBeVisible({
      timeout: 10_000,
    })
  })

  test('详情 API 对已完成任务返回 200', async ({ request }) => {
    const tasksRes = await request.get('/api/tasks')
    const body = await tasksRes.json()
    const tasks: Array<{ id: string; status: string }> = body.tasks ?? body
    const target = tasks.find(
      t => t.status === 'completed' || t.status === 'cancelled',
    )
    test.skip(!target, '无 completed/cancelled 任务')

    const detailRes = await request.get(`/api/tasks/${target!.id}/detail`)
    expect(detailRes.ok()).toBeTruthy()
    const detail = await detailRes.json()
    expect(detail.task?.id).toBe(target!.id)
    expect(Array.isArray(detail.metrics_summary)).toBeTruthy()
  })
})
