import { defineConfig } from '@playwright/test'

/**
 * Broker integration test config.
 * Runs against a live OpenAlgo + mock broker stack (docker-compose.test.yml).
 *
 * Usage:
 *   OPENALGO_URL=http://127.0.0.1:5099 MOCK_BROKER_URL=http://127.0.0.1:8001 \
 *     npx playwright test --config playwright.broker.config.ts
 */
export default defineConfig({
  testDir: './e2e',
  testMatch: ['broker_happy_path.spec.ts'],
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [['github'], ['list']] : 'list',
  use: {
    baseURL: process.env.OPENALGO_URL || 'http://127.0.0.1:5099',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { browserName: 'chromium' },
    },
  ],
})
