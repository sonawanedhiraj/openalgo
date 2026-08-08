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
  /** Actual order routing right now (issue #440): 'live' only when the navbar
   *  is on Live AND this strategy's row is live — else 'sandbox'. */
  effective_routing?: 'live' | 'sandbox'
  llm_mode: LLMMode
  llm_veto_enabled: boolean
  /** Advisory metadata from the STATIC config_snapshot.json (issue #561).
   *  Gates the sandbox→live direction ONLY. It must never gate live→sandbox,
   *  and never decide what routing the card displays — `mode` /
   *  `effective_routing` (the strategy_mode row) are the truth for that. */
  deployable: boolean
  /** `mode` as declared in config_snapshot.json — advisory, often stale. */
  config_declared_mode?: string | null
  /** True when the static config claims scaffold/non-deployable while the
   *  strategy actually routes live. Surfaced, never silently resolved. */
  config_conflict?: boolean
  version: string
  open_positions: number
  /** Per-mode open-position split (issue #562). Sandbox and live counts are
   *  reported separately and NEVER summed — the #552 convention. */
  open_positions_by_mode?: Record<string, number> | null
  /** Which mode `open_positions` refers to (the strategy's current routing). */
  open_positions_mode?: string | null
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

// Per-direction (long/short) sub-aggregate (issue #458). A side with no closed
// trades has n_trades=0 and null stats — rendered as '—', never 0%.
export interface SideSplit {
  n_trades: number
  // Journal-derived splits always carry `wins`; a backtest split declared in a
  // config_snapshot may publish only per-side trade count + net P&L when the
  // source report never broke out per-side win rate (issue #508).
  wins?: number | null
  win_rate_pct?: number | null
  net_pnl_inr: number | null
}

// ATM-option variant of a mode's performance (issue #455). Backtest basis is
// fit-to-capital lots; sandbox/live basis is the 1-lot shadow (issue #435) —
// directionally comparable, not rupee-for-rupee.
export interface OptionsPerf {
  n_trades: number | null
  win_rate_pct: number | null
  net_pnl_inr: number | null
  max_dd_pct?: number | null
  basis?: string
  long?: SideSplit | null
  short?: SideSplit | null
}

export interface BacktestPerf {
  cagr_pct: number | null
  sharpe: number | null
  max_dd_pct: number | null
  win_rate_pct: number | null
  n_trades: number | null
  net_pnl_inr?: number | null
  window: string | null
  options?: OptionsPerf | null
  long?: SideSplit | null
  short?: SideSplit | null
}

export interface LivePerf {
  open_positions?: number
  today_net_pnl?: number | null
  last_trade_at?: string | null
  // Since-inception (issue #323): cumulative realized P&L, running win-rate, and
  // the closed-trade denominator. Null when the mode has no closed trades yet.
  cum_net_pnl?: number | null
  win_rate_pct?: number | null
  closed_trades?: number
  options?: OptionsPerf | null
  long?: SideSplit | null
  short?: SideSplit | null
  // Realized performance metrics computed from the closed-trade daily P&L
  // series (issue #568). Null while there is not yet enough history — `notes`
  // carries the human-readable reason, surfaced as a cell tooltip.
  // `max_dd_inr` is the honest absolute figure; `max_dd_pct` is relative to
  // `capital_basis_inr`, which for the simplified engine is a per-trade
  // risk-sizing base rather than a compounding book (`capital_basis_is_notional`).
  cagr_pct?: number | null
  sharpe?: number | null
  max_dd_pct?: number | null
  max_dd_inr?: number | null
  roc_pct?: number | null
  trading_days?: number
  capital_basis_inr?: number | null
  capital_basis_is_notional?: boolean
  notes?: string | null
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

// Compact Stage-1 LLM veto decision embedded on a trade row (issue #358).
export interface TradeLLMReview {
  decision_id: number | null
  decision: string
  confidence: number | null
  reasoning: string | null
  enforcement_mode: string
  candidate_at: string | null
}

// An enforced LLM skip that blocked an entry and therefore has no trade row —
// rendered as a pseudo-row in the merged trades table (issue #358).
export interface UnmatchedSkipDecision extends TradeLLMReview {
  symbol: string
  direction: string | null
}

export interface RecentTrade {
  id: number
  side: 'BUY' | 'SELL' | 'LONG' | 'SHORT'
  symbol: string
  quantity: number
  lots?: number
  price?: number
  entry_price?: number | null
  exit_price?: number | null
  gross_pnl?: number | null
  charges_inr?: number | null
  net_pnl?: number | null
  margin_inr?: number | null
  mode: string
  status: string
  entry_date: string
  created_at: string | null
  llm?: TradeLLMReview | null
  /** open15: mid-bar entry moment "HH:MM:SS" IST (trigger_minute + trigger_second) */
  trigger?: string | null
  /** open15: exit timestamp (ISO, IST offset) stamped at the 09:30 flatten */
  exit_ts?: string | null
  /** open15: ATM option shadow trade (issue #435) — research-only, 1 lot */
  opt_symbol?: string | null
  opt_lot_size?: number | null
  opt_entry_premium?: number | null
  opt_exit_premium?: number | null
  opt_charges_inr?: number | null
  opt_pnl?: number | null
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
  /** Actual order routing right now (issue #440) — see StrategySummary. */
  effective_routing?: 'live' | 'sandbox'
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
  llm_unmatched_skips?: UnmatchedSkipDecision[]
  version_log: VersionLogEntry[]
  backtest_refs: string[]
  /** Optional per-strategy console page (decision log / settings), e.g. open15's /logs */
  console_url?: string | null
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
// Entry-evaluation breakdown (issue #352) — futures_follow_cap50 only
// ---------------------------------------------------------------------------

export type EntryBreakdownOutcome =
  | 'in_cap_placed'
  | 'cap_skipped'
  | 'vetoed'
  | 'placement_failed'
  | 'not_selected'
  | 'first_failed_gate'
  | 'missing_data'

export interface EntryBreakdownSymbol {
  symbol: string
  sector_index: string | null
  sector_ret: number | null
  stock_ret: number | null
  vol_ratio: number | null
  intraday_source: string | null
  outcome: EntryBreakdownOutcome
  fail_reason: string | null
}

export interface EntryBreakdownPayload {
  eval_at: string
  mode: string
  n_signals: number
  intraday_source_counts: {
    quotes: number
    aggregator: number
    historify: number
    none: number
  }
  cap_skipped: number
  vetoed: number
  per_gate_fail_counts: {
    sector: number
    stock: number
    vol: number
    missing_data: number
  }
  symbols: EntryBreakdownSymbol[]
}

export interface EntryBreakdownSnapshot {
  id: number
  strategy_name: string
  eval_date: string
  eval_at: string | null
  payload: EntryBreakdownPayload
  created_at: string | null
}

/** One day's digest in the evaluation-history table (issue #395). */
export interface EntryBreakdownSummary {
  eval_date: string
  eval_at: string | null
  mode: string | null
  n_signals: number
  placed: number
  cap_skipped: number
  vetoed: number
  placement_failed: number
  missing_data: number
  total_symbols: number
  evaluated_symbols: number
  /** Symbols clearing each gate, of `evaluated_symbols`. */
  gates_passed: { sector: number; stock: number; vol: number }
  /** Raw stored fail counts — shown in the tooltip so log lines reconcile. */
  gates_failed: { sector: number; stock: number; vol: number }
  dominant_source: string
  live_source_count: number
  passed_symbols: Array<{ symbol: string; outcome: EntryBreakdownOutcome }>
}

export interface EntryBreakdownHistory {
  rows: EntryBreakdownSummary[]
  has_more: boolean
  /**
   * Whether a pending row belongs at the top. The snapshot is only written at
   * 15:20 IST, so `snapshot_exists` is false all morning — but on a weekend or
   * an NSE holiday none is ever coming, and `is_trading_day` says so.
   */
  today: { date: string; is_trading_day: boolean; snapshot_exists: boolean }
}

// ---------------------------------------------------------------------------
// API client
// ---------------------------------------------------------------------------

export const strategiesDashboardApi = {
  /**
   * Today's 15:20 entry-evaluation breakdown (issue #352) — served by the
   * futures_follow control blueprint (session-cookie auth accepted for this
   * read-only endpoint). `null` data means no evaluation recorded yet.
   */
  getEntryBreakdown: async (date?: string): Promise<EntryBreakdownSnapshot | null> => {
    const params: Record<string, string> = {}
    if (date) params.date = date
    const res = await webClient.get<{ status: string; data: EntryBreakdownSnapshot | null }>(
      '/futures_follow_cap50/api/entry_breakdown',
      { params }
    )
    return res.data.data
  },

  /**
   * Per-day digests of past 15:20 evaluations, newest first (issue #395).
   * `before` (exclusive `YYYY-MM-DD`) pages backwards.
   */
  getEntryBreakdownHistory: async (limit = 30, before?: string): Promise<EntryBreakdownHistory> => {
    const params: Record<string, string> = { limit: String(limit) }
    if (before) params.before = before
    const res = await webClient.get<{ status: string; data: EntryBreakdownHistory }>(
      '/futures_follow_cap50/api/entry_breakdown/history',
      { params }
    )
    return res.data.data
  },

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

  /**
   * On-demand liveness probe of the shared claude CLI used by every strategy's
   * LLM veto. Spawns a real `claude -p` subprocess server-side, so the caller
   * must only invoke this manually (never on a poll). Reachability is
   * install-global — one result covers all strategies.
   */
  getLLMHealth: async (): Promise<LLMHealth> => {
    const res = await webClient.get<{ status: string; data: LLMHealth }>(
      '/strategies/api/llm/health'
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

// ---------------------------------------------------------------------------
// LLM health probe (issue #297)
// ---------------------------------------------------------------------------

export type LLMHealthReason = 'ok' | 'timeout' | 'cli_missing' | 'not_logged_in' | 'error'

export interface LLMHealth {
  reachable: boolean
  latency_ms: number
  reason: LLMHealthReason
  detail: string
  checked_at: string
}
