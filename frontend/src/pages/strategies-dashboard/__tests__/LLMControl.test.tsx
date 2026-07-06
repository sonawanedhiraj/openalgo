import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as dash from '@/api/strategies-dashboard'
import { render, screen, userEvent, waitFor } from '@/test/test-utils'
import { LLMControlCard, TradesAndDecisionsCard } from '../StrategyDetail'

function Wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

const DETAIL: dash.StrategyDetail = {
  name: 'simplified_engine',
  display_name: 'Simplified Engine',
  mode: 'sandbox',
  llm_mode: 'off',
  llm_veto_enabled: true,
  deployable: true,
  version: 'v1.1',
  config_snapshot: {},
  active_overrides: [],
  health: 'healthy',
  performance: { backtest: {} as never, sandbox: null, live: null },
  recent_trades: [],
  version_log: [],
  backtest_refs: [],
}

describe('LLMControlCard', () => {
  afterEach(() => vi.restoreAllMocks())

  it('confirms then POSTs when enabling veto', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const flipSpy = vi.spyOn(dash.strategiesDashboardApi, 'flipLLMMode').mockResolvedValue({
      status: 'success',
      accepted: true,
      strategy_name: 'simplified_engine',
      target_llm_mode: 'veto',
      previous_llm_mode: 'off',
      new_llm_mode: 'veto',
      warnings: [],
      error_message: null,
    })

    render(
      <Wrapper>
        <LLMControlCard data={DETAIL} />
      </Wrapper>
    )

    await userEvent.click(screen.getByRole('button', { name: /Veto/i }))
    expect(confirmSpy).toHaveBeenCalledOnce()
    await waitFor(() => expect(flipSpy).toHaveBeenCalledWith('simplified_engine', 'veto'))
  })

  it('does not POST when confirm is cancelled', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const flipSpy = vi.spyOn(dash.strategiesDashboardApi, 'flipLLMMode')

    render(
      <Wrapper>
        <LLMControlCard data={DETAIL} />
      </Wrapper>
    )
    await userEvent.click(screen.getByRole('button', { name: /Veto/i }))
    expect(flipSpy).not.toHaveBeenCalled()
  })

  it('renders delegate as a disabled coming-soon option', () => {
    render(
      <Wrapper>
        <LLMControlCard data={DETAIL} />
      </Wrapper>
    )
    const delegate = screen.getByRole('button', { name: /Delegate/i })
    expect(delegate).toBeDisabled()
    expect(screen.getByText(/soon/i)).toBeInTheDocument()
  })
})

describe('TradesAndDecisionsCard', () => {
  beforeEach(() => {
    vi.spyOn(dash.strategiesDashboardApi, 'getLLMDecisions').mockResolvedValue({
      name: 'simplified_engine',
      veto_enabled: true,
      llm_mode: 'veto',
      total: 2,
      limit: 1,
      offset: 0,
      source_filtered: false,
      summary: {
        total: 2,
        take: 1,
        skip: 1,
        review_failed: 1,
        other: 0,
        last_decision: null,
        recent_review_failed: 1,
      },
      rows: [],
    })
  })
  afterEach(() => vi.restoreAllMocks())

  const MERGED_DETAIL: dash.StrategyDetail = {
    ...DETAIL,
    recent_trades: [
      {
        id: 1,
        side: 'LONG',
        symbol: 'FORTIS',
        quantity: 100,
        entry_price: 500,
        exit_price: 510,
        net_pnl: 1000,
        mode: 'chartink',
        status: 'closed',
        entry_date: '2026-06-29',
        created_at: '2026-06-29T03:50:00',
        llm: {
          decision_id: 1,
          decision: 'take',
          confidence: 0.7,
          reasoning: 'looks good',
          enforcement_mode: 'shadow',
          candidate_at: '2026-06-29T09:20:00+05:30',
        },
      },
    ],
    llm_unmatched_skips: [
      {
        decision_id: 2,
        decision: 'skip',
        confidence: 0.9,
        reasoning: 'regime hostile to overnight longs',
        enforcement_mode: 'active',
        candidate_at: '2026-06-29T15:20:00+05:30',
        symbol: 'ASTRAL',
        direction: 'BUY',
      },
    ],
  }

  it('renders trade rows with their LLM verdict and vetoed pseudo-rows', async () => {
    render(
      <Wrapper>
        <TradesAndDecisionsCard data={MERGED_DETAIL} />
      </Wrapper>
    )
    // Trade row with embedded verdict.
    expect(screen.getByText('FORTIS')).toBeInTheDocument()
    expect(screen.getByText('take')).toBeInTheDocument()
    // Enforced skip with no journal row → pseudo-row with a vetoed badge.
    expect(screen.getByText('ASTRAL')).toBeInTheDocument()
    expect(screen.getByText('vetoed')).toBeInTheDocument()
    expect(screen.getByText(/regime hostile/)).toBeInTheDocument()
    // recent_review_failed=1 → the unreachable warning renders from the summary.
    await waitFor(() => expect(screen.getByText(/LLM unreachable/i)).toBeInTheDocument())
  })

  it('hides LLM columns for strategies without the veto', () => {
    const noVeto: dash.StrategyDetail = {
      ...MERGED_DETAIL,
      llm_veto_enabled: false,
      recent_trades: [{ ...MERGED_DETAIL.recent_trades[0], llm: null }],
      llm_unmatched_skips: [],
    }
    render(
      <Wrapper>
        <TradesAndDecisionsCard data={noVeto} />
      </Wrapper>
    )
    expect(screen.getByText('FORTIS')).toBeInTheDocument()
    expect(screen.queryByText('Reasoning')).not.toBeInTheDocument()
  })
})
