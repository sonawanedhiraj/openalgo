import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as ip from '@/api/intraday-pullback'
import { render, screen, userEvent, waitFor } from '@/test/test-utils'
import { IntradayPullbackEvalCard } from '../IntradayPullbackEvalCard'

function Wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

const TODAY = '2026-07-17'

function summary(over: Partial<ip.EntryBreakdownSummary> = {}): ip.EntryBreakdownSummary {
  return {
    eval_date: '2026-07-16',
    eval_at: '2026-07-16T15:30:00',
    mode: 'sandbox',
    side_today: 'L',
    nifty_930_pct: 0.5,
    selected: true,
    n_picks: 2,
    n_trades: 0,
    n_open: 0,
    diag: { ref_formed: 2, breakouts: 0, gate_blocked: 0, no_slot: 0, entries: 0, exits: 0 },
    picks: [
      { symbol: 'AAA', position: 'none' },
      { symbol: 'BBB', position: 'none' },
    ],
    ...over,
  }
}

function history(over: Partial<ip.EntryBreakdownHistory> = {}): ip.EntryBreakdownHistory {
  return {
    rows: [summary()],
    has_more: false,
    today: { date: TODAY, is_trading_day: true, snapshot_exists: false },
    ...over,
  }
}

function breakdown(over: Partial<ip.EntryBreakdown> = {}): ip.EntryBreakdown {
  return {
    date: '2026-07-16',
    mode: 'sandbox',
    side_today: 'L',
    nifty_930_pct: 0.5,
    selected: true,
    picks: ['AAA'],
    n_trades_today: 0,
    evaluation: [
      {
        symbol: 'AAA',
        sector: 'NIFTY IT',
        gain_930_pct: 1.5,
        sector_930_pct: 0.4,
        diag: {
          candles: 12,
          ref_formed: 1,
          breakouts: 0,
          gate_blocked: 0,
          no_slot: 0,
          entries: 0,
          exits: 0,
        },
        reason: 'no breakout after reference',
        position: 'none',
      },
    ],
    ...over,
  }
}

function mockHistory(h: ip.EntryBreakdownHistory) {
  return vi.spyOn(ip.intradayPullbackApi, 'getEntryBreakdownHistory').mockResolvedValue(h)
}

describe('IntradayPullbackEvalCard', () => {
  afterEach(() => vi.restoreAllMocks())

  it('renders a day per row, newest first', async () => {
    mockHistory(
      history({
        rows: [
          summary({ eval_date: '2026-07-16', n_trades: 1 }),
          summary({ eval_date: '2026-07-15', n_trades: 0 }),
        ],
      })
    )
    vi.spyOn(ip.intradayPullbackApi, 'getEntryBreakdown').mockResolvedValue(breakdown())
    render(<IntradayPullbackEvalCard />, { wrapper: Wrapper })

    await waitFor(() => expect(screen.getByText('16 Jul')).toBeInTheDocument())
    expect(screen.getByText('15 Jul')).toBeInTheDocument()
    expect(screen.getByText(/2 days recorded · 1 trade day/)).toBeInTheDocument()
  })

  it('shows the pending row on a trading day with no snapshot yet', async () => {
    mockHistory(history())
    vi.spyOn(ip.intradayPullbackApi, 'getEntryBreakdown').mockResolvedValue(breakdown())
    render(<IntradayPullbackEvalCard />, { wrapper: Wrapper })

    await waitFor(() =>
      expect(screen.getByText('Picks are chosen at 09:30 IST')).toBeInTheDocument()
    )
  })

  it('shows no pending row on a holiday — an evaluation that never comes is not promised', async () => {
    mockHistory(history({ today: { date: TODAY, is_trading_day: false, snapshot_exists: false } }))
    vi.spyOn(ip.intradayPullbackApi, 'getEntryBreakdown').mockResolvedValue(breakdown())
    render(<IntradayPullbackEvalCard />, { wrapper: Wrapper })

    await waitFor(() => expect(screen.getByText('16 Jul')).toBeInTheDocument())
    expect(screen.queryByText('Picks are chosen at 09:30 IST')).not.toBeInTheDocument()
  })

  it('empty history with no pending row explains itself rather than showing a blank table', async () => {
    mockHistory(
      history({ rows: [], today: { date: TODAY, is_trading_day: false, snapshot_exists: false } })
    )
    render(<IntradayPullbackEvalCard />, { wrapper: Wrapper })

    await waitFor(() => expect(screen.getByText(/No evaluation recorded yet/)).toBeInTheDocument())
  })

  it('expands the newest recorded day by default and drills into its per-pick detail', async () => {
    mockHistory(history())
    const detail = vi
      .spyOn(ip.intradayPullbackApi, 'getEntryBreakdown')
      .mockResolvedValue(breakdown())
    render(<IntradayPullbackEvalCard />, { wrapper: Wrapper })

    // default-open fetches the newest row's full payload...
    await waitFor(() => expect(detail).toHaveBeenCalledWith('2026-07-16'))
    // ...and renders the per-pick reason that explains the zero-trade day
    expect(await screen.findByText('no breakout after reference')).toBeInTheDocument()
    expect(screen.getByText('NIFTY IT')).toBeInTheDocument()
  })

  it('collapsing the default-open row keeps it closed', async () => {
    mockHistory(history())
    vi.spyOn(ip.intradayPullbackApi, 'getEntryBreakdown').mockResolvedValue(breakdown())
    render(<IntradayPullbackEvalCard />, { wrapper: Wrapper })

    const row = await screen.findByText('16 Jul')
    expect(await screen.findByText('no breakout after reference')).toBeInTheDocument()

    await userEvent.click(row)
    // '' means "explicitly closed" — it must not fall back to re-opening the default
    await waitFor(() =>
      expect(screen.queryByText('no breakout after reference')).not.toBeInTheDocument()
    )
  })

  it('summarises how far a zero-trade day got, so it is not mistaken for an outage', async () => {
    mockHistory(
      history({
        rows: [
          summary({
            n_trades: 0,
            diag: {
              ref_formed: 2,
              breakouts: 3,
              gate_blocked: 3,
              no_slot: 0,
              entries: 0,
              exits: 0,
            },
          }),
        ],
      })
    )
    vi.spyOn(ip.intradayPullbackApi, 'getEntryBreakdown').mockResolvedValue(breakdown())
    render(<IntradayPullbackEvalCard />, { wrapper: Wrapper })

    await waitFor(() =>
      expect(screen.getByText('2 refs · 3 breakouts · 3 gate-blocked')).toBeInTheDocument()
    )
  })

  it('distinguishes a no-qualifier day from a no-book day', async () => {
    mockHistory(
      history({
        rows: [
          summary({ eval_date: '2026-07-16', n_picks: 0, picks: [] }),
          summary({ eval_date: '2026-07-15', selected: false, side_today: null }),
        ],
      })
    )
    vi.spyOn(ip.intradayPullbackApi, 'getEntryBreakdown').mockResolvedValue(
      breakdown({ picks: [], evaluation: [] })
    )
    render(<IntradayPullbackEvalCard />, { wrapper: Wrapper })

    await waitFor(() => expect(screen.getByText('no stock qualified at 09:30')).toBeInTheDocument())
    expect(screen.getByText('no selection')).toBeInTheDocument()
  })

  it('the day selector refetches over a wider window', async () => {
    const spy = mockHistory(history({ has_more: true }))
    vi.spyOn(ip.intradayPullbackApi, 'getEntryBreakdown').mockResolvedValue(breakdown())
    render(<IntradayPullbackEvalCard />, { wrapper: Wrapper })

    await screen.findByText('16 Jul') // the selector only renders once the table has rows
    expect(spy).toHaveBeenCalledWith(30)
    await userEvent.selectOptions(screen.getByRole('combobox'), '90')
    await waitFor(() => expect(spy).toHaveBeenCalledWith(90))
  })
})
