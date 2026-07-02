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

export type LLMMode = 'off' | 'veto' | 'delegate'

export interface StrategySummary {
  name: string
  display_name: string
  mode: string
  llm_mode: LLMMode
  llm_veto_enabled: boolean
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

// Latest data-freshness (data_health_check) state for the strategy's feed (#237).
export interface DataHealth {
  available: boolean
  reason?: string
  feed?: string
  shared?: boolean
  overall_ok?: boolean
  check_at?: string | null
  stale_count?: number
  stale_symbols?: string[]
}

export interface StrategyDetail {
  name: string
  display_name: string
  mode: string
  llm_mode: LLMMode
  llm_veto_enabled: boolean
  deployable: boolean
  version: string
  config_snapshot: Record<string, unknown>
  active_overrides: ActiveOverride[]
  health: StrategyHealth
  data_health: DataHealth
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

  /**
   * Flip a strategy's mode (sandbox <-> live) through the preflight gate.
   *
   * Returns the {@link FlipModeOutcome}. A 409 (preflight refused) is NOT
   * thrown as an error here — the response body has `accepted=false` and a
   * `blockers` list the UI surfaces to the operator. Other HTTP failures
   * (400/404/5xx) throw normally.
   *
   * Resolves the today's-failure scenario from issue #162: the UI calls this
   * and either gets `accepted=true` (mode mutated, event published) or
   * `accepted=false` (mode unchanged, blockers explain why). Operator never
   * silently ends up in a broken LIVE state.
   */
  flipMode: async (
    name: string,
    mode: 'live' | 'sandbox',
    notes?: string
  ): Promise<FlipModeOutcome> => {
    const res = await webClient.post<FlipModeOutcome>(
      `/strategies/api/${name}/mode`,
      { mode, notes },
      // Don't throw on 409 — that's the "blocked by preflight" response,
      // not a transport-level failure. The UI inspects accepted/blockers.
      { validateStatus: (s) => s === 202 || s === 409 }
    )
    return res.data
  },

  /** Recent mode flip attempts (accepted + blocked). */
  getModeAudit: async (name: string, limit = 10): Promise<ModeAuditRow[]> => {
    const res = await webClient.get<{
      status: string
      data: { name: string; rows: ModeAuditRow[]; limit: number }
    }>(`/strategies/api/${name}/mode/audit`, { params: { limit } })
    return res.data.data.rows
  },

  /**
   * Set a strategy's LLM mode (off | veto). `delegate` is accepted by the
   * server but treated as veto for now (the response `warnings` say so) — the
   * UI shows it disabled/"coming soon".
   *
   * A 400 (bad value) is NOT thrown here — the response body has
   * `accepted=false` and an `error_message`. Transport failures throw normally.
   */
  flipLLMMode: async (name: string, llmMode: LLMMode, notes?: string): Promise<LLMFlipOutcome> => {
    const res = await webClient.post<LLMFlipOutcome>(
      `/strategies/api/${name}/llm-mode`,
      { llm_mode: llmMode, notes },
      { validateStatus: (s) => s === 202 || s === 400 }
    )
    return res.data
  },

  /** Paginated LLM-veto decision history + a health summary. */
  getLLMDecisions: async (name: string, limit = 25, offset = 0): Promise<LLMDecisionsResponse> => {
    const res = await webClient.get<{ status: string; data: LLMDecisionsResponse }>(
      `/strategies/api/${name}/llm-decisions`,
      { params: { limit, offset } }
    )
    return res.data.data
  },
}

// ---------------------------------------------------------------------------
// Mode flip types (issue #162)
// ---------------------------------------------------------------------------

export interface FlipModeOutcome {
  status: 'success' | 'blocked'
  accepted: boolean
  strategy_name: string
  target_mode: 'live' | 'sandbox'
  previous_mode: string | null
  new_mode: string | null
  blockers: string[]
  warnings: string[]
  audit_id: number | null
  error_message: string | null
}

export interface ModeAuditRow {
  id: number
  strategy_name: string
  target_mode: string
  previous_mode: string | null
  accepted: boolean
  blockers: string[]
  warnings: string[]
  snapshot: Record<string, unknown>
  flipped_at: string | null
  flipped_by: string
  error_message: string | null
}

// ---------------------------------------------------------------------------
// LLM control types (issue #266 Phase 2)
// ---------------------------------------------------------------------------

export interface LLMFlipOutcome {
  status: 'success' | 'error'
  accepted: boolean
  strategy_name: string
  target_llm_mode: LLMMode
  previous_llm_mode: LLMMode | null
  new_llm_mode: LLMMode | null
  warnings: string[]
  error_message: string | null
}

export type LLMDecisionVerdict = 'take' | 'skip' | 'review_failed' | string

export interface LLMDecisionRow {
  id: number
  candidate_at: string
  symbol: string
  source: string
  direction: string | null
  decision: LLMDecisionVerdict
  reasoning: string | null
  confidence: number | null
  enforcement_mode: string
  actually_taken: boolean | null
  bridge_latency_ms: number | null
}

export interface LLMDecisionsSummary {
  total: number
  take: number
  skip: number
  review_failed: number
  other: number
  last_decision: LLMDecisionRow | null
  recent_review_failed: number
}

export interface LLMDecisionsResponse {
  name: string
  veto_enabled: boolean
  llm_mode: LLMMode
  rows: LLMDecisionRow[]
  total: number
  limit: number
  offset: number
  summary: LLMDecisionsSummary | null
  source_filtered: boolean
}
