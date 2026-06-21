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
  limit: number
  count: number
}

export const scannerApi = {
  /** List all enabled scan definitions with latest signals and today's hit count. */
  getDefinitions: async (): Promise<ScanDefinitionSummary[]> => {
    const res = await webClient.get<{ status: string; data: ScanDefinitionSummary[] }>(
      '/scanner/api/definitions'
    )
    return res.data.data
  },

  /** Signal history for a single definition.
   *  @param id  scan_definition id
   *  @param since  ISO-8601 lower bound (default: server applies now-24h)
   *  @param limit  max rows (default 200, max 500)
   */
  getSignals: async (id: number, since?: string, limit?: number): Promise<SignalsResponse> => {
    const params: Record<string, string | number> = {}
    if (since) params.since = since
    if (limit !== undefined) params.limit = limit
    const res = await webClient.get<{ status: string; data: SignalsResponse }>(
      `/scanner/api/definitions/${id}/signals`,
      { params }
    )
    return res.data.data
  },
}
