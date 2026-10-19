import { expect, type APIRequestContext, type Page } from '@playwright/test'

export const API_PREFIX = '/api'

export type UploadFilePayload = {
  name: string
  mimeType: string
  buffer: Buffer
}

export async function assertApiReachable(request: APIRequestContext): Promise<boolean> {
  try {
    const res = await request.get(`${API_PREFIX}/trace/health`, { timeout: 5_000 })
    return res.ok()
  } catch {
    return false
  }
}

export async function assertCsvApiSupported(request: APIRequestContext): Promise<boolean> {
  try {
    const res = await request.get(`${API_PREFIX}/datasets/template.csv`, { timeout: 5_000 })
    return res.ok()
  } catch {
    return false
  }
}

export async function gotoDatasetManager(page: Page): Promise<void> {
  await page.goto('/')
  const resourceSection = page.locator('.nav-section').filter({
    has: page.locator('.nav-parent', { hasText: '资源管理' }),
  })
  const resourceChildren = resourceSection.locator('.nav-children').first()
  const resourceOpen = await resourceChildren.evaluate(el => el.classList.contains('open'))
  if (!resourceOpen) {
    await resourceSection.locator('.nav-parent').click()
  }
  await expect(resourceChildren).toHaveClass(/open/)
  await resourceSection
    .getByRole('button', { name: '评测集管理' })
    .click({ force: true })
  await page.getByRole('heading', { name: /评测集管理/ }).waitFor({ timeout: 15_000 })
}

export async function openNewDatasetModal(page: Page): Promise<void> {
  await page.getByRole('button', { name: '新增评测集' }).click()
  await page.getByRole('heading', { name: '新增评测集' }).waitFor()
}

export function newDatasetModal(page: Page) {
  return page.locator('.modal-content').filter({
    has: page.getByRole('heading', { name: '新增评测集' }),
  })
}

export async function fillAndUploadInModal(
  page: Page,
  opts: { name: string; file: UploadFilePayload },
): Promise<void> {
  const modal = newDatasetModal(page)
  await modal.getByPlaceholder('请输入评测集名称').fill(opts.name)
  await modal.locator('input[type="file"]').setInputFiles(opts.file)
}

export async function submitNewDataset(page: Page) {
  const uploadPromise = page.waitForResponse(
    res => res.url().includes('/api/upload') && res.request().method() === 'POST',
    { timeout: 30_000 },
  )
  await newDatasetModal(page).getByRole('button', { name: '确认创建' }).click()
  return uploadPromise
}

export async function deleteDataset(
  request: APIRequestContext,
  datasetId: string,
): Promise<void> {
  await request.delete(`${API_PREFIX}/datasets/${datasetId}`)
}

export function jsonUploadFile(items: unknown[]): UploadFilePayload {
  return {
    name: 'e2e-import.json',
    mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify(items), 'utf-8'),
  }
}

export function csvUploadFile(content: string, name = 'e2e-import.csv'): UploadFilePayload {
  return {
    name,
    mimeType: 'text/csv',
    buffer: Buffer.from(content, 'utf-8'),
  }
}
