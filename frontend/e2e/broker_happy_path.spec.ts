/**
 * Broker happy-path E2E test.
 *
 * Requires:
 *   - OpenAlgo running (OPENALGO_URL, default http://127.0.0.1:5099)
 *   - Mock broker running (MOCK_BROKER_URL, default http://127.0.0.1:8001)
 *   - MOCK_BROKER_ENABLED=true set in the OpenAlgo container
 *
 * Flow:
 *   1. Reset mock state, set mocked balance to ₹15 L
 *   2. POST /setup  — create the test admin account (fresh container)
 *   3. POST /auth/login  — log in to OpenAlgo
 *   4. GET /_test/mock_auth  — complete broker auth via the test shortcut
 *      (mock's /session/token returns a fixed access_token; no real Kite OAuth)
 *   5. GET /apikey (Accept: application/json)  — retrieve the generated API key
 *   6. POST /api/v1/funds  — call funds endpoint with the API key
 *   7. Assert returned availablecash ≈ ₹15 L (mocked equity balance)
 */

import { test, expect } from '@playwright/test'

const BASE_URL = process.env.OPENALGO_URL || 'http://127.0.0.1:5099'
const MOCK_URL = process.env.MOCK_BROKER_URL || 'http://127.0.0.1:8001'

const TEST_USER = 'testadmin'
const TEST_EMAIL = 'test@openalgo.test'
const TEST_PASS = 'Test1234!'
const MOCKED_BALANCE = 1_500_000

test('happy-path: mock broker auth → /api/v1/funds returns mocked balance', async ({ page }) => {
  // ── 1. Configure mock state ──────────────────────────────────────────────
  const resetResp = await page.request.post(`${MOCK_URL}/_mock/reset`)
  expect(resetResp.ok(), 'mock reset failed').toBeTruthy()

  const balanceResp = await page.request.post(`${MOCK_URL}/_mock/balance`, {
    headers: { 'Content-Type': 'application/json' },
    data: JSON.stringify({ amount: MOCKED_BALANCE }),
  })
  expect(balanceResp.ok(), 'mock balance set failed').toBeTruthy()

  // ── 2. Create OpenAlgo admin account ────────────────────────────────────
  // The container starts with an empty database; /setup creates the first user.
  const setupResp = await page.request.post(`${BASE_URL}/setup`, {
    form: {
      username: TEST_USER,
      email: TEST_EMAIL,
      password: TEST_PASS,
    },
  })
  // /setup redirects to /login on success (3xx) — any non-5xx is acceptable here
  expect(
    setupResp.status(),
    `setup failed with status ${setupResp.status()}`,
  ).toBeLessThan(500)

  // ── 3. Log in to OpenAlgo ────────────────────────────────────────────────
  const loginResp = await page.request.post(`${BASE_URL}/auth/login`, {
    form: {
      username: TEST_USER,
      password: TEST_PASS,
    },
  })
  const loginBody = await loginResp.json()
  expect(loginBody.status, `login failed: ${JSON.stringify(loginBody)}`).toBe('success')

  // ── 4. Complete mock broker auth ─────────────────────────────────────────
  // /_test/mock_auth calls authenticate_broker("mock_request_token"), which hits
  // the mock's POST /session/token and stores the returned access_token.  The
  // endpoint then calls handle_auth_success and redirects to /dashboard.
  const mockAuthResp = await page.goto(`${BASE_URL}/_test/mock_auth`, {
    waitUntil: 'networkidle',
    timeout: 20_000,
  })
  expect(
    mockAuthResp?.status(),
    '/_test/mock_auth should not 4xx/5xx',
  ).toBeLessThan(400)
  // Should land on dashboard (or redirect chain ending there)
  await expect(page).toHaveURL(/dashboard/, { timeout: 15_000 })

  // ── 5. Retrieve the OpenAlgo API key ─────────────────────────────────────
  const apikeyResp = await page.request.get(`${BASE_URL}/apikey`, {
    headers: { Accept: 'application/json' },
  })
  expect(apikeyResp.ok(), 'GET /apikey failed').toBeTruthy()
  const apikeyBody = await apikeyResp.json()
  const apiKey: string = apikeyBody.api_key
  expect(apiKey, 'api_key should be non-empty').toBeTruthy()

  // ── 6. Call /api/v1/funds ────────────────────────────────────────────────
  const fundsResp = await page.request.post(`${BASE_URL}/api/v1/funds`, {
    headers: { 'Content-Type': 'application/json' },
    data: JSON.stringify({ apikey: apiKey }),
  })
  expect(fundsResp.ok(), `POST /api/v1/funds failed (${fundsResp.status()})`).toBeTruthy()
  const fundsBody = await fundsResp.json()

  // ── 7. Assert mocked balance ─────────────────────────────────────────────
  expect(fundsBody.status, `funds status: ${JSON.stringify(fundsBody)}`).toBe('success')
  const availableCash = parseFloat(fundsBody.data.availablecash)
  // Equity net (1,500,000) + commodity net (0) = 1,500,000
  expect(availableCash).toBeCloseTo(MOCKED_BALANCE, 0)
})
