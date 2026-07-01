import { webClient } from './client'

export type ScreenerType = 'buy' | 'sell'

// Tier-3 additions
export interface ScanDefinitionFull extends ScanDefinitionDetail {
  parameters_json: string | null
  parent_definition_id: number | null
}

// Keys accepted by each rule
export interface BuyRuleParams {
  gap_pct?: number
  atr_pct?: number
  vol_5m_mult?: number
  rsi_threshold?: number
  supertrend_period?: number
  supertrend_mult?: number
  price_min?: number
  price_max?: number
  vol_sma_short?: number
  vol_sma_long?: number
}

export interface SellRuleParams {
  gap_pct?: number
  atr_pct?: number
  rsi_threshold?: number
  supertrend_period?: number
  supertrend_mult?: number
  price_min?: number
  price_max?: number
}

export type RuleParams = BuyRuleParams | SellRuleParams

export interface ScanSignal {
  id: number
  run_at: string
  symbols: string[]
  source: string
  posted_to_engine: boolean
  notes?: string | null
}

export interface ScanDefinitionSummary {
  id: number
  name: string
  screener_type: ScreenerType
  rule_module: string | null
  enabled: boolean
  created_at: string
  updated_at: string
  latest_signals: Omit<ScanSignal, 'notes'>[]
  today_hit_count: number
  parent_definition_id: number | null
}

export interface ScanDefinitionDetail {
  id: number
  name: string
  screener_type: ScreenerType
  rule_module: string | null
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface SignalsResponse {
  definition: ScanDefinitionDetail
  signals: ScanSignal[]
  since: string
  until: string | null
  limit: number
  count: number
}

export interface SymbolHit {
  symbol: string
  hit_count: number
  definitions: string[]
  latest_hit: string
}

export interface HitsBySymbolResponse {
  date: string
  symbols: SymbolHit[]
}

export const scannerApi = {
  getDefinitions: async (): Promise<ScanDefinitionSummary[]> => {
    const res = await webClient.get<{ status: string; data: ScanDefinitionSummary[] }>(
      '/scanner/api/definitions'
    )
    return res.data.data
  },

  getSignals: async (
    id: number,
    since?: string,
    until?: string,
    limit?: number
  ): Promise<SignalsResponse> => {
    const params: Record<string, string | number> = {}
    if (since) params.since = since
    if (until) params.until = until
    if (limit !== undefined) params.limit = limit
    const res = await webClient.get<{ status: string; data: SignalsResponse }>(
      `/scanner/api/definitions/${id}/signals`,
      { params }
    )
    return res.data.data
  },

  toggleDefinition: async (id: number): Promise<{ id: number; enabled: boolean }> => {
    const res = await webClient.post<{
      status: string
      data: { id: number; enabled: boolean }
    }>(`/scanner/api/definitions/${id}/toggle`)
    return res.data.data
  },

  getHitsBySymbol: async (date?: string): Promise<HitsBySymbolResponse> => {
    const params: Record<string, string> = {}
    if (date) params.date = date
    const res = await webClient.get<{ status: string; data: HitsBySymbolResponse }>(
      '/scanner/api/hits-by-symbol',
      { params }
    )
    return res.data.data
  },

  getDefinition: async (id: number): Promise<ScanDefinitionFull> => {
    const res = await webClient.get<{ status: string; data: ScanDefinitionFull }>(
      `/scanner/api/definitions/${id}`
    )
    return res.data.data
  },

  cloneDefinition: async (
    id: number,
    body: { name: string; parameters_json?: RuleParams | null }
  ): Promise<{ id: number; name: string }> => {
    const res = await webClient.post<{ status: string; data: { id: number; name: string } }>(
      `/scanner/api/definitions/${id}/clone`,
      body
    )
    return res.data.data
  },

  updateParams: async (
    id: number,
    parameters_json: RuleParams | null
  ): Promise<{ id: number; parameters_json: string | null }> => {
    const res = await webClient.put<{
      status: string
      data: { id: number; parameters_json: string | null }
    }>(`/scanner/api/definitions/${id}/params`, { parameters_json })
    return res.data.data
  },

  deleteDefinition: async (id: number, force = false): Promise<{ id: number }> => {
    // force=true asks the backend to delete a code-backed *orphan* definition
    // (a leaked rule like _p0_always_true with no registered production rule).
    // Cloned definitions delete without force. Live built-ins stay protected —
    // the backend still returns 403 for a code-backed row with a registered rule.
    const res = await webClient.delete<{ status: string; data: { id: number } }>(
      `/scanner/api/definitions/${id}`,
      force ? { params: { force: 'true' } } : undefined
    )
    return res.data.data
  },
}
