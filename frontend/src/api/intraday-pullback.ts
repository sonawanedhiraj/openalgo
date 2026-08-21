import { webClient } from '@/api/client'

export type TradeSide = 'both' | 'long_only' | 'short_only'

export interface IntradayPullbackSettings {
  base_capital: number
  sizing_mode: 'fixed' | 'compound' | 'capped'
  /** issue #509 — which book(s) may run. See the day-gate note in the UI. */
  trade_side: TradeSide
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
  trade_side: string
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

  resetSettings: async (): Promise<IntradayPullbackSettings> => {
    const res = await webClient.post<Envelope<IntradayPullbackSettings>>(
      `${BASE}/settings/reset`,
      {}
    )
    return res.data.data as IntradayPullbackSettings
  },
}
