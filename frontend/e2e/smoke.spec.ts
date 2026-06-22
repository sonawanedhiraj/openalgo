import { test, expect } from '@playwright/test'

const BASE_URL = process.env.OPENALGO_URL || 'http://127.0.0.1:5000'

const ROUTES_TO_SMOKE = [
  { path: '/login', titleContains: ['Login', 'OpenAlgo'] },
  { path: '/', titleContains: ['OpenAlgo', 'Login'] },
  { path: '/scanner', titleContains: ['OpenAlgo'] },
  { path: '/strategies', titleContains: ['OpenAlgo'] },
  { path: '/analyzer', titleContains: ['OpenAlgo'] },
  { path: '/tools', titleContains: ['OpenAlgo'] },
]

// Known benign noise — allowlisted explicitly and conservatively.
// Add only what is truly harmless; wrong allows mean missed bugs.
const CONSOLE_ERROR_ALLOWLIST: RegExp[] = [
  /Failed to load resource.*favicon/,
  /HTTP\/2 disabled/,
  /net::ERR_BLOCKED_BY_CLIENT/,  // ad blocker in some CI images
]

for (const { path: routePath, titleContains } of ROUTES_TO_SMOKE) {
  test(`smoke: ${routePath} loads without JS errors`, async ({ page }) => {
    const pageErrors: Error[] = []
    const consoleErrors: string[] = []

    page.on('pageerror', (err) => pageErrors.push(err))
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        const text = msg.text()
        if (!CONSOLE_ERROR_ALLOWLIST.some((re) => re.test(text))) {
          consoleErrors.push(text)
        }
      }
    })

    await page.goto(`${BASE_URL}${routePath}`, {
      waitUntil: 'networkidle',
      timeout: 15000,
    })

    // Allow client-side routing/hydration to settle
    await page.waitForTimeout(500)

    // 1. No uncaught JS errors — even one forwardRef crash fails here
    expect(
      pageErrors,
      `pageerror on ${routePath}:\n${pageErrors.map((e) => e.message).join('\n')}`,
    ).toHaveLength(0)

    // 2. No unallowlisted console.error
    expect(
      consoleErrors,
      `console.error on ${routePath}:\n${consoleErrors.join('\n')}`,
    ).toHaveLength(0)

    // 3. #root has content — React mounted and rendered something
    const rootChildren = await page.locator('#root > *').count()
    expect(
      rootChildren,
      `#root is empty on ${routePath} — React did not mount`,
    ).toBeGreaterThan(0)

    // 4. Title contains at least one expected string
    const title = await page.title()
    const titleOk = titleContains.some((t) => title.includes(t))
    expect(
      titleOk,
      `title "${title}" on ${routePath} does not contain any of ${JSON.stringify(titleContains)}`,
    ).toBe(true)
  })
}
