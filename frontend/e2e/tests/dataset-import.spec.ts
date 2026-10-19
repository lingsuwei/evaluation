import { test, expect } from '@playwright/test'
import {
  assertApiReachable,
  assertCsvApiSupported,
  csvUploadFile,
  deleteDataset,
  fillAndUploadInModal,
  gotoDatasetManager,
  jsonUploadFile,
  newDatasetModal,
  openNewDatasetModal,
  submitNewDataset,
} from '../helpers/dataset-import'

test.describe('评测集 JSON / CSV 前端导入', () => {
  let apiReady = false
  let csvApiReady = false
  const createdIds: string[] = []

  test.beforeAll(async ({ request }) => {
    apiReady = await assertApiReachable(request)
    csvApiReady = apiReady && (await assertCsvApiSupported(request))
  })

  test.afterEach(async ({ request }) => {
    while (createdIds.length > 0) {
      const id = createdIds.pop()
      if (id) {
        await deleteDataset(request, id).catch(() => undefined)
      }
    }
  })

  test.beforeEach(async () => {
    test.skip(!apiReady, 'E2E 需可达后端（默认 http://127.0.0.1:45000）')
  })

  test('JSON 导入成功并展示题目', async ({ page }) => {
    const name = `e2e-json-${Date.now()}`
    const question = `e2e-q-json-${Date.now()}`

    await gotoDatasetManager(page)
    await openNewDatasetModal(page)
    await fillAndUploadInModal(page, {
      name,
      file: jsonUploadFile([
        { question, model_answer: 'e2e-answer', ground_truth: 'e2e-gt' },
      ]),
    })

    const uploadRes = await submitNewDataset(page)
    expect(uploadRes.ok()).toBeTruthy()
    const body = await uploadRes.json()
    createdIds.push(body.dataset_id)

    await expect(page.getByRole('heading', { name: '新增评测集' })).toBeHidden()
    const row = page.locator('table.results-table tbody tr').filter({ hasText: name })
    await expect(row).toBeVisible()
    await expect(row).toContainText('1')

    await row.click()
    await expect(page.getByText('评测集信息')).toBeVisible()
    await expect(page.getByText(question)).toBeVisible()
  })

  test('CSV 中文表头导入成功', async ({ page }) => {
    test.skip(!csvApiReady, '后端未启用 CSV 导入 API（需本地最新 backend 或重建 Docker）')
    const name = `e2e-csv-${Date.now()}`
    const question = `e2e-q-csv-${Date.now()}`

    await gotoDatasetManager(page)
    await openNewDatasetModal(page)
    await fillAndUploadInModal(page, {
      name,
      file: csvUploadFile(
        `\ufeff题目,标准答案,模型答案\n${question},GT,MA\n`,
      ),
    })

    const uploadRes = await submitNewDataset(page)
    expect(uploadRes.ok()).toBeTruthy()
    const body = await uploadRes.json()
    createdIds.push(body.dataset_id)

    await expect(page.locator('table.results-table tbody tr').filter({ hasText: name })).toBeVisible()
    await page.locator('table.results-table tbody tr').filter({ hasText: name }).click()
    await expect(page.getByText(question)).toBeVisible()
    await expect(page.getByText('MA')).toBeVisible()
  })

  test('选择无效 JSON 时前端提示错误', async ({ page }) => {
    await gotoDatasetManager(page)
    await openNewDatasetModal(page)
    await fillAndUploadInModal(page, {
      name: `e2e-bad-json-${Date.now()}`,
      file: {
        name: 'broken.json',
        mimeType: 'application/json',
        buffer: Buffer.from('{not-json', 'utf-8'),
      },
    })

    await expect(page.getByText('无效的 JSON 文件')).toBeVisible()
  })

  test('选择非 json/csv 扩展名时前端拒绝', async ({ page }) => {
    await gotoDatasetManager(page)
    await openNewDatasetModal(page)
    await fillAndUploadInModal(page, {
      name: `e2e-txt-${Date.now()}`,
      file: {
        name: 'notes.txt',
        mimeType: 'text/plain',
        buffer: Buffer.from('hello', 'utf-8'),
      },
    })

    await expect(page.getByText('仅支持 .json 或 .csv 文件')).toBeVisible()
  })

  test('服务端校验失败时展示错误信息', async ({ page }) => {
    const name = `e2e-validate-fail-${Date.now()}`

    await gotoDatasetManager(page)
    await openNewDatasetModal(page)
    await fillAndUploadInModal(page, {
      name,
      file: jsonUploadFile([{ question: 'only-question' }]),
    })

    const uploadRes = await submitNewDataset(page)
    expect(uploadRes.status()).toBe(400)

    await expect(page.getByRole('heading', { name: '新增评测集' })).toBeVisible()
    await expect(page.getByText(/创建失败|Validation|model_answer/i)).toBeVisible()
  })

  test('CSV 缺题目列时服务端返回错误', async ({ page }) => {
    test.skip(!csvApiReady, '后端未启用 CSV 导入 API')
    const name = `e2e-csv-bad-${Date.now()}`

    await gotoDatasetManager(page)
    await openNewDatasetModal(page)
    await fillAndUploadInModal(page, {
      name,
      file: csvUploadFile('标准答案,模型答案\nGT,MA\n'),
    })

    const uploadRes = await submitNewDataset(page)
    expect(uploadRes.status()).toBe(400)
    await expect(newDatasetModal(page).getByText(/创建失败/)).toBeVisible()
    await expect(newDatasetModal(page).getByText(/题目|question/i)).toBeVisible()
  })

  test('下载 CSV 模板按钮可点击且模板 API 可用', async ({ page, request }) => {
    test.skip(!csvApiReady, '后端未启用 CSV 模板 API')
    await gotoDatasetManager(page)
    await openNewDatasetModal(page)

    const templateBtn = page.getByRole('button', { name: '下载 CSV 模板' })
    await expect(templateBtn).toBeVisible()
    await templateBtn.click()

    const res = await request.get('/api/datasets/template.csv')
    expect(res.ok()).toBeTruthy()
    const text = await res.text()
    expect(text).toContain('题目')
    expect(text).toContain('示例题目')
  })

  test('导入后可触发 JSON 导出下载', async ({ page }) => {
    test.skip(!csvApiReady, '后端未启用导出 API')
    const name = `e2e-export-${Date.now()}`
    const question = `e2e-q-export-${Date.now()}`

    await gotoDatasetManager(page)
    await openNewDatasetModal(page)
    await fillAndUploadInModal(page, {
      name,
      file: jsonUploadFile([{ question, model_answer: 'ans' }]),
    })

    const uploadRes = await submitNewDataset(page)
    const body = await uploadRes.json()
    createdIds.push(body.dataset_id)

    const row = page.locator('table.results-table tbody tr').filter({ hasText: name })
    const exportResponse = page.waitForResponse(
      res => res.url().includes('/export') && res.request().method() === 'GET',
      { timeout: 15_000 },
    )
    const downloadPromise = page.waitForEvent('download', { timeout: 15_000 })
    await row.getByRole('button', { name: '导出' }).click()
    await row.getByRole('menuitem', { name: '导出 JSON' }).click()
    const exportRes = await exportResponse
    expect(exportRes.ok()).toBeTruthy()
    const download = await downloadPromise

    expect(download.suggestedFilename()).toMatch(/\.json$/i)
    const path = await download.path()
    expect(path).toBeTruthy()
  })
})
