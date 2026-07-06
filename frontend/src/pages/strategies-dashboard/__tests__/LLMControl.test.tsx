import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as dash from '@/api/strategies-dashboard'
import { render, screen, userEvent, waitFor } from '@/test/test-utils'
import { LLMControlCard, LLMDecisionsCard } from '../StrategyDetail'

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

describe('LLMDecisionsCard', () => {
  beforeEach(() => {
    vi.spyOn(dash.strategiesDashboardApi, 'getLLMDecisions').mockResolvedValue({
      name: 'simplified_engine',
      veto_enabled: true,
      llm_mode: 'veto',
      total: 2,
      limit: 25,
      offset: 0,
      source_filtered: false,
      summary: {
        total: 2,
        take: 1,
        skip: 0,
        review_failed: 1,
        other: 0,
        last_decision: null,
        recent_review_failed: 1,
      },
      rows: [
        {
          id: 2,
          candidate_at: '2026-06-29T09:30:00+05:30',
          symbol: 'ASTRAL',
          source: 'chartink_FnO_intraday_buy',
          direction: 'BUY',
          decision: 'review_failed',
          reasoning: 'bridge_error',
          confidence: null,
          enforcement_mode: 'shadow',
          actually_taken: true,
          bridge_latency_ms: 50,
        },
        {
          id: 1,
          candidate_at: '2026-06-29T09:20:00+05:30',
          symbol: 'FORTIS',
          source: 'trend-up',
          direction: 'BUY',
          decision: 'take',
          reasoning: 'looks good',
          confidence: 0.7,
          enforcement_mode: 'shadow',
          actually_taken: true,
          bridge_latency_ms: 42,
        },
      ],
    })
  })
  afterEach(() => vi.restoreAllMocks())

  it('renders decision rows and the unreachable health hint', async () => {
    render(
      <Wrapper>
        <LLMDecisionsCard name="simplified_engine" />
      </Wrapper>
    )
    await waitFor(() => expect(screen.getByText('ASTRAL')).toBeInTheDocument())
    expect(screen.getByText('FORTIS')).toBeInTheDocument()
    // Distinct styling for review_failed is present as text.
    expect(screen.getByText('review_failed')).toBeInTheDocument()
    // recent_review_failed=1 → the unreachable warning renders.
    expect(screen.getByText(/LLM unreachable/i)).toBeInTheDocument()
  })
})
