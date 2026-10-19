import { test, expect } from '@playwright/test'

test.describe('M-04 关键路径冒烟', () => {
  test('首页可加载', async ({ page }) => {
    const response = await page.goto('/')
    expect(response?.ok()).toBeTruthy()
    await expect(page.locator('body')).toBeVisible()
  })

  test('trace health API 可达', async ({ request }) => {
    const res = await request.get('/api/trace/health')
    expect(res.ok()).toBeTruthy()
    const body = await res.json()
    expect(body.status).toBe('ok')
  })

  test('任务列表 API 结构', async ({ request }) => {
    const res = await request.get('/api/tasks')
    expect(res.ok()).toBeTruthy()
    const body = await res.json()
    expect(Array.isArray(body.tasks ?? body)).toBeTruthy()
  })
})
