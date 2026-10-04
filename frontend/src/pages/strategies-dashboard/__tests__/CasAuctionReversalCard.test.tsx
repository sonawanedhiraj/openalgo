import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { CasArConfigResponse, CasArStatus } from '@/api/strategies-dashboard'
import { render, screen, userEvent, waitFor } from '@/test/test-utils'
import { CasAuctionReversalCard } from '../CasAuctionReversalCard'

// issue #755 — the cas_auction_reversal Settings card: shows where each value
// comes from, saves, surfaces the backend's refusal text, and clears overrides.

const api = vi.hoisted(() => ({
  getCasArConfig: vi.fn(),
  saveCasArConfig: vi.fn(),
  getCasArStatus: vi.fn(),
}))
vi.mock('@/api/strategies-dashboard', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/strategies-dashboard')>()),
  ...api,
}))

function Wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

const DEFAULTS = {
  threshold_pct: 0.5,
  max_positions: 10,
  capital_per_trade_inr: 50000,
  poll_interval_s: 15,
}
const BOUNDS = {
  threshold_pct: { min: 0.2, max: 5, integer: false },
  max_positions: { min: 1, max: 30, integer: true },
  capital_per_trade_inr: { min: 5000, max: 1000000, integer: false },
  poll_interval_s: { min: 5, max: 60, integer: true },
}

function cfg(over: Partial<CasArConfigResponse> = {}): CasArConfigResponse {
  return {
    defaults: DEFAULTS,
    bounds: BOUNDS,
    override: null,
    effective: DEFAULTS,
    sources: {
      threshold_pct: 'default',
      max_positions: 'env',
      capital_per_trade_inr: 'default',
      poll_interval_s: 'default',
    },
    applies_at: 'next 15:14:30 IST arm',
    day_config: null,
    ...over,
  }
}

const STATUS: CasArStatus = {
  strategy: 'cas_auction_reversal',
  trade_date: '2026-10-05',
  mode: 'sandbox',
  armed: false,
  manual_pause: false,
  config: DEFAULTS,
  universe: 0,
  polls: 0,
  last_poll_at: null,
  decided: false,
  iep_feed: null,
  candidates: null,
  entries: null,
  schedule: {
    arm: '15:14:30',
    decide: '15:23:30',
    fill: '15:32:00',
    summary: '15:45:00',
    exit: '09:16:00',
    exit_retry: '09:20:00',
  },
}

describe('CasAuctionReversalCard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getCasArStatus.mockResolvedValue(STATUS)
  })

  it('shows effective values and where each one comes from', async () => {
    api.getCasArConfig.mockResolvedValue(cfg())
    render(
      <Wrapper>
        <CasAuctionReversalCard />
      </Wrapper>
    )
    expect(await screen.findByTestId('cas-ar-threshold_pct')).toHaveValue(0.5)
    expect(screen.getByTestId('cas-ar-max_positions')).toHaveValue(10)
    expect(screen.getByTestId('cas-ar-source-max_positions')).toHaveTextContent('.env')
    expect(screen.getByTestId('cas-ar-source-threshold_pct')).toHaveTextContent('default')
    expect(screen.getByTestId('cas-ar-reset')).toBeDisabled() // nothing saved yet
    expect(await screen.findByTestId('cas-ar-today-card')).toHaveTextContent('sandbox')
  })

  it('saves the edited values and reports when they apply', async () => {
    api.getCasArConfig.mockResolvedValue(cfg())
    api.saveCasArConfig.mockResolvedValue(
      cfg({
        override: { threshold_pct: 0.75, updated_at: '2026-10-04T13:00:00' },
        effective: { ...DEFAULTS, threshold_pct: 0.75 },
        sources: { ...cfg().sources, threshold_pct: 'ui' },
      })
    )
    render(
      <Wrapper>
        <CasAuctionReversalCard />
      </Wrapper>
    )
    const input = await screen.findByTestId('cas-ar-threshold_pct')
    await userEvent.clear(input)
    await userEvent.type(input, '0.75')
    await userEvent.click(screen.getByTestId('cas-ar-save'))
    await waitFor(() =>
      expect(api.saveCasArConfig).toHaveBeenCalledWith({
        threshold_pct: 0.75,
        max_positions: 10,
        capital_per_trade_inr: 50000,
        poll_interval_s: 15,
      })
    )
    expect(await screen.findByTestId('cas-ar-msg')).toHaveTextContent(
      'Saved — applies at the next 15:14:30 IST arm.'
    )
  })

  it("shows the backend's refusal message instead of clamping", async () => {
    api.getCasArConfig.mockResolvedValue(cfg())
    api.saveCasArConfig.mockRejectedValue({
      response: { data: { message: 'max_positions must be between 1 and 30' } },
    })
    render(
      <Wrapper>
        <CasAuctionReversalCard />
      </Wrapper>
    )
    const input = await screen.findByTestId('cas-ar-max_positions')
    await userEvent.clear(input)
    await userEvent.type(input, '99')
    await userEvent.click(screen.getByTestId('cas-ar-save'))
    expect(await screen.findByTestId('cas-ar-msg')).toHaveTextContent(
      'max_positions must be between 1 and 30'
    )
  })

  it('clears saved values back to .env / defaults', async () => {
    api.getCasArConfig.mockResolvedValue(
      cfg({ override: { threshold_pct: 0.75, updated_at: '2026-10-04T13:00:00' } })
    )
    api.saveCasArConfig.mockResolvedValue(cfg())
    render(
      <Wrapper>
        <CasAuctionReversalCard />
      </Wrapper>
    )
    const reset = await screen.findByTestId('cas-ar-reset')
    expect(reset).toBeEnabled()
    await userEvent.click(reset)
    await waitFor(() =>
      expect(api.saveCasArConfig).toHaveBeenCalledWith({
        threshold_pct: null,
        max_positions: null,
        capital_per_trade_inr: null,
        poll_interval_s: null,
      })
    )
    expect(await screen.findByTestId('cas-ar-msg')).toHaveTextContent('Cleared')
  })
})
