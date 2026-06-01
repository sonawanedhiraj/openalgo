import { webClient } from './client'

export type EngineDirection = 'BUY' | 'SELL'
export type EngineMode = 'disabled' | 'sandbox' | 'live'

export interface SimplifiedEnginePosition {
  qty: number
  side: 'LONG' | 'SHORT'
  entry_price: number
  stop_loss: number
  risk_per_share: number
}

export interface SimplifiedEngineFundsSummary {
  available_cash: number
  floor: number
  checked_at: string
}

export interface SimplifiedEngineTickLogStats {
  enabled: boolean
  directory: string | null
  file: string | null
  compress: boolean
  queued: number
  queue_max: number
  written_today: number
  dropped_today: number
  bytes_written_today: number
}

export interface SimplifiedEngineStatus {
  /** Human-readable label: 'disabled' | 'sandbox' | 'live' | 'analyze'.
   *  'analyze' means engine_mode=live but the global analyze_mode flag is on,
   *  so place_order would route to sandbox anyway. */
  mode: string
  /** Source-of-truth engine routing mode. */
  engine_mode: EngineMode
  /** ISO date of the most recent broker-position-aware EOD flatten run, or null. */
  eod_flatten_done: string | null
  /** ISO date of the most recent EOD trading summary log, or null. */
  eod_summary_done: string | null
  /** Number of round trips closed since the last day rollover. */
  completed_trades_today: number
  /** Most recent broker funds reading, populated after the first live-mode entry attempt. */
  funds: SimplifiedEngineFundsSummary | null
  /** Tick log writer state. */
  tick_log: SimplifiedEngineTickLogStats
  direction_enabled: Record<EngineDirection, boolean>
  positions: Record<string, SimplifiedEnginePosition>
  pending_entries: string[]
  pending_exits: string[]
  active_symbols: Record<string, EngineDirection>
  buy_symbols: string[]
  sell_symbols: string[]
  trades_today: number
  max_trades_per_day: number
  subscribed_symbols: { user_id: string; exchange: string; symbol: string }[]
}

export const simplifiedEngineApi = {
  getStatus: async (): Promise<SimplifiedEngineStatus> => {
    const response = await webClient.get<{ status: string; data: SimplifiedEngineStatus }>(
      '/chartink/simplified-engine/api/status'
    )
    return response.data.data
  },

  toggleDirection: async (
    direction: EngineDirection,
    enabled: boolean
  ): Promise<Record<EngineDirection, boolean>> => {
    const response = await webClient.post<{
      status: string
      direction_enabled: Record<EngineDirection, boolean>
    }>('/chartink/simplified-engine/api/toggle', { direction, enabled })
    return response.data.direction_enabled
  },
}
