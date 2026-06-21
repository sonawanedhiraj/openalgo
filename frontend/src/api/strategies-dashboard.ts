import { webClient } from './client'

// ---------------------------------------------------------------------------
// Shared types
// ---------------------------------------------------------------------------

export type StrategyHealth = 'healthy' | 'paused' | 'scaffold' | 'unknown'
export type PnlWindow = '1d' | '1w' | '1m' | 'all'

export interface ActiveOverride {
  type: 'pause' | 'kill_switch'
  reason: string | null
  expires_at: string | null
  set_by: string | null
}

// ---------------------------------------------------------------------------
// List endpoint
// ---------------------------------------------------------------------------

export interface StrategySummary {
  name: string
  display_name: string
  mode: string
  deployable: boolean
  version: string
  open_positions: number
  today_net_pnl: number | null
  today_trade_count: number
  last_trade_at: string | null
  active_overrides: ActiveOverride[]
  health: StrategyHealth
  error?: boolean
}

// ---------------------------------------------------------------------------
// Detail endpoint
// ---------------------------------------------------------------------------

export interface BacktestPerf {
  cagr_pct: number | null
  sharpe: number | null
  max_dd_pct: number | null
  win_rate_pct: number | null
  n_trades: number | null
  window: string | null
}

export interface LivePerf {
  open_positions?: number
  today_net_pnl?: number | null
  last_trade_at?: string | null
}

export interface StrategyPerformance {
  backtest: BacktestPerf
  sandbox: LivePerf | null
  live: LivePerf | null
}

export interface VersionLogEntry {
  version: string
  date: string
  body: string
}

export interface RecentTrade {
  id: number
  side: 'BUY' | 'SELL'
  symbol: string
  quantity: number
  lots?: number
  price?: number
  entry_price?: number | null
  exit_price?: number | null
  gross_pnl?: number | null
  net_pnl?: number | null
  mode: string
  status: string
  entry_date: string
  created_at: string | null
}

export interface StrategyDetail {
  name: string
  display_name: string
  mode: string
  deployable: boolean
  version: string
  config_snapshot: Record<string, unknown>
  active_overrides: ActiveOverride[]
  health: StrategyHealth
  performance: StrategyPerformance
  recent_trades: RecentTrade[]
  version_log: VersionLogEntry[]
  backtest_refs: string[]
}

// ---------------------------------------------------------------------------
// P&L curve endpoint
// ---------------------------------------------------------------------------

export interface PnlPoint {
  date: string
  pnl: number
}

export interface PnlCurveResponse {
  window: PnlWindow
  points: PnlPoint[]
}

// ---------------------------------------------------------------------------
// Parameters diff endpoint
// ---------------------------------------------------------------------------

export interface ChangedKey {
  key: string
  current: unknown
  previous: unknown
}

export interface ParametersDiff {
  name: string
  current_version: string
  vs_version: string | null
  current: Record<string, unknown>
  previous: Record<string, unknown>
  changed_keys: ChangedKey[]
}

// ---------------------------------------------------------------------------
// API client
// ---------------------------------------------------------------------------

export const strategiesDashboardApi = {
  /** List all strategies with summary metrics. */
  listStrategies: async (): Promise<StrategySummary[]> => {
    const res = await webClient.get<{ status: string; data: StrategySummary[] }>(
      '/strategies/api/list'
    )
    return res.data.data
  },

  /** Full detail for one strategy. */
  getStrategy: async (name: string): Promise<StrategyDetail> => {
    const res = await webClient.get<{ status: string; data: StrategyDetail }>(
      `/strategies/api/${name}`
    )
    return res.data.data
  },

  /** Daily P&L time series. */
  getPnlCurve: async (name: string, window: PnlWindow = 'all'): Promise<PnlCurveResponse> => {
    const res = await webClient.get<{ status: string; data: PnlCurveResponse }>(
      `/strategies/api/${name}/pnl-curve`,
      { params: { window } }
    )
    return res.data.data
  },

  /** Parameter diff against a named version. */
  getParametersDiff: async (name: string, vs?: string): Promise<ParametersDiff> => {
    const params: Record<string, string> = {}
    if (vs) params.vs = vs
    const res = await webClient.get<{ status: string; data: ParametersDiff }>(
      `/strategies/api/${name}/parameters/diff`,
      { params }
    )
    return res.data.data
  },
}
