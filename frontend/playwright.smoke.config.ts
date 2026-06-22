import { defineConfig } from '@playwright/test'

/**
 * Smoke test config — runs against an already-booted Flask server.
 * Use: npx playwright test --config playwright.smoke.config.ts
 * Or in CI: OPENALGO_URL=http://127.0.0.1:5000 npx playwright test --config playwright.smoke.config.ts
 */
export default defineConfig({
  testDir: './e2e',
  testMatch: ['smoke.spec.ts'],
  timeout: 30000,
  expect: { timeout: 5000 },
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: process.env.CI ? [['github'], ['list']] : 'list',
  use: {
    baseURL: process.env.OPENALGO_URL || 'http://127.0.0.1:5000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { browserName: 'chromium' },
    },
  ],
  // No webServer — expects the server to already be running
})
