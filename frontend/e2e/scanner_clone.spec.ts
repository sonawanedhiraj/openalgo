import { expect, test } from '@playwright/test'

const BASE_URL = process.env.OPENALGO_URL || 'http://127.0.0.1:5000'

// Fixture data matching the ScanDefinitionSummary shape (with parent_definition_id)
const CODE_BACKED = {
  id: 1,
  name: 'fno_intraday_buy',
  screener_type: 'buy',
  rule_module: 'fno_intraday_buy_chartink',
  enabled: true,
  created_at: '2026-06-01T09:00:00',
  updated_at: '2026-06-01T09:00:00',
  latest_signals: [],
  today_hit_count: 3,
  parent_definition_id: null,
}

const CLONE_DEF = {
  ...CODE_BACKED,
  id: 2,
  name: 'fno_intraday_buy_custom',
  parent_definition_id: 1,
  today_hit_count: 0,
}

test.describe('Scanner — clone and delete definitions', () => {
  test.beforeEach(async ({ page }) => {
    // Mock the definitions list
    await page.route('**/scanner/api/definitions', async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ status: 'success', data: [CODE_BACKED] }),
        })
      } else {
        await route.continue()
      }
    })
    // Mock WS proxy health (non-critical, just silence errors)
    await page.route('**/health/ws_proxy', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          status: 'down',
          last_tick_age_sec: null,
          thread_count: 0,
          subscribed_symbols: null,
        }),
      })
    })
    await page.goto(`${BASE_URL}/scanner`, { waitUntil: 'networkidle', timeout: 15000 })
  })

  test('scanner page renders definition cards without JS errors', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (e) => errors.push(e.message))

    await expect(page.locator('text=fno_intraday_buy').first()).toBeVisible({ timeout: 5000 })
    expect(errors).toHaveLength(0)
  })

  test('Clone button is visible on code-backed definitions', async ({ page }) => {
    await expect(page.locator('[aria-label="Clone fno_intraday_buy"]')).toBeVisible({
      timeout: 5000,
    })
  })

  test('Clone button opens dialog with pre-filled name', async ({ page }) => {
    await page.locator('[aria-label="Clone fno_intraday_buy"]').click()
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible({ timeout: 3000 })
    const nameInput = dialog.getByLabel(/new name/i)
    await expect(nameInput).toHaveValue('fno_intraday_buy_custom')
  })

  test('Clone submit calls API and closes dialog', async ({ page }) => {
    // Mock clone endpoint
    let cloneCalled = false
    await page.route('**/scanner/api/definitions/1/clone', async (route) => {
      cloneCalled = true
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'success', data: { id: 2, name: 'fno_intraday_buy_custom' } }),
      })
    })
    // After clone, refresh returns both definitions
    let callCount = 0
    await page.route('**/scanner/api/definitions', async (route) => {
      callCount++
      const data = callCount === 1 ? [CODE_BACKED] : [CODE_BACKED, CLONE_DEF]
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'success', data }),
      })
    })

    await page.locator('[aria-label="Clone fno_intraday_buy"]').click()
    const dialog = page.getByRole('dialog')
    await dialog.getByRole('button', { name: /^clone$/i }).click()
    await expect(dialog).not.toBeVisible({ timeout: 5000 })
    expect(cloneCalled).toBe(true)
  })

  test('Delete button only visible on clones', async ({ page }) => {
    // With only code-backed definition, delete button should not exist
    await expect(page.locator('[aria-label="Delete fno_intraday_buy"]')).not.toBeVisible()

    // Now re-mock with a clone in the list
    await page.route('**/scanner/api/definitions', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'success', data: [CODE_BACKED, CLONE_DEF] }),
      })
    })
    await page.reload({ waitUntil: 'networkidle' })

    await expect(page.locator('[aria-label="Delete fno_intraday_buy_custom"]')).toBeVisible({
      timeout: 5000,
    })
    await expect(page.locator('[aria-label="Delete fno_intraday_buy"]')).not.toBeVisible()
  })

  test('Delete confirm calls API', async ({ page }) => {
    // Show clone in list
    await page.route('**/scanner/api/definitions', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ status: 'success', data: [CODE_BACKED, CLONE_DEF] }),
      })
    })
    await page.reload({ waitUntil: 'networkidle' })

    let deleteCalled = false
    await page.route('**/scanner/api/definitions/2', async (route) => {
      if (route.request().method() === 'DELETE') {
        deleteCalled = true
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ status: 'success', data: { id: 2 } }),
        })
      } else {
        await route.continue()
      }
    })

    await page.locator('[aria-label="Delete fno_intraday_buy_custom"]').click()
    const alertDialog = page.getByRole('alertdialog')
    await expect(alertDialog).toBeVisible({ timeout: 3000 })
    await alertDialog.getByRole('button', { name: /^delete$/i }).click()
    expect(deleteCalled).toBe(true)
  })
})
