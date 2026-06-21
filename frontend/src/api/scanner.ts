import { webClient } from './client'

export type ScreenerType = 'buy' | 'sell'

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
}
