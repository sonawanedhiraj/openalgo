import { webClient } from '@/api/client'

export interface IntradayPullbackSettings {
  base_capital: number
  sizing_mode: 'fixed' | 'compound' | 'capped'
  slots: number
  margin_per_slot: number
  morning: [string, string]
  no_trade: [string, string]
  afternoon: [string, string]
  eod_flatten: string
  realized_pnl_to_date: number
  deployable_capital: number
}

export interface IntradayPullbackSettingsUpdate {
  base_capital: number
  sizing_mode: string
  no_trade_start: string
  no_trade_end: string
  afternoon_start: string
  afternoon_end: string
}

export interface PickEvaluation {
  symbol: string
  sector: string | null
  gain_930_pct: number | null
  sector_930_pct: number | null
  diag: Record<string, number> | null
  reason: string
  position: 'open' | 'closed' | 'none'
}

export interface EntryBreakdown {
  date: string
  mode: string
  side_today: 'L' | 'S' | null
  nifty_930_pct: number | null
  selected: boolean
  picks: string[]
  n_trades_today: number
  evaluation: PickEvaluation[]
}

/** Per-day digest of one recorded evaluation (issue #422) — one row of the history table. */
export interface EntryBreakdownSummary {
  eval_date: string
  eval_at: string | null
  mode: string | null
  side_today: 'L' | 'S' | null
  nifty_930_pct: number | null
  selected: boolean
  n_picks: number
  n_trades: number
  n_open: number
  /** Totals across the day's picks — how far the day got before it stopped. */
  diag: {
    ref_formed: number
    breakouts: number
    gate_blocked: number
    no_slot: number
    entries: number
    exits: number
  }
  picks: { symbol: string; position: 'open' | 'closed' | 'none' }[]
}

export interface EntryBreakdownHistory {
  rows: EntryBreakdownSummary[]
  has_more: boolean
  /**
   * Whether a pending row belongs at the top. The first snapshot of a day is written once the
   * 09:30 IST selection runs, so `snapshot_exists` is false before that — but on a weekend or an
   * NSE holiday none is ever coming, and `is_trading_day` says so.
   */
  today: { date: string; is_trading_day: boolean; snapshot_exists: boolean }
}

const BASE = '/intraday_pullback_top2/api'

interface Envelope<T> {
  status: string
  data?: T
  message?: string
}

export const intradayPullbackApi = {
  getSettings: async (): Promise<IntradayPullbackSettings> => {
    const res = await webClient.get<Envelope<IntradayPullbackSettings>>(`${BASE}/settings`)
    return res.data.data as IntradayPullbackSettings
  },

  updateSettings: async (
    body: IntradayPullbackSettingsUpdate
  ): Promise<IntradayPullbackSettings> => {
    const res = await webClient.post<Envelope<IntradayPullbackSettings>>(`${BASE}/settings`, body, {
      validateStatus: (s) => s === 200 || s === 400,
    })
    if (res.data.status === 'error') {
      throw new Error(res.data.message || 'Failed to save settings')
    }
    return res.data.data as IntradayPullbackSettings
  },

  getEntryBreakdown: async (date?: string): Promise<EntryBreakdown | null> => {
    const res = await webClient.get<Envelope<EntryBreakdown | null>>(`${BASE}/entry_breakdown`, {
      params: date ? { date } : undefined,
    })
    return (res.data.data as EntryBreakdown | null) ?? null
  },

  /**
   * Per-day digests of the recorded evaluations, newest first (issue #422). Compact summaries —
   * drill into one day with `getEntryBreakdown(date)`.
   */
  getEntryBreakdownHistory: async (limit = 30, before?: string): Promise<EntryBreakdownHistory> => {
    const params: Record<string, string> = { limit: String(limit) }
    if (before) params.before = before
    const res = await webClient.get<Envelope<EntryBreakdownHistory>>(
      `${BASE}/entry_breakdown/history`,
      { params }
    )
    return res.data.data as EntryBreakdownHistory
  },

  resetSettings: async (): Promise<IntradayPullbackSettings> => {
    const res = await webClient.post<Envelope<IntradayPullbackSettings>>(
      `${BASE}/settings/reset`,
      {}
    )
    return res.data.data as IntradayPullbackSettings
  },
}
