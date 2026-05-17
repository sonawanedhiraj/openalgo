import { webClient } from './client'

export type EngineDirection = 'BUY' | 'SELL'

export interface SimplifiedEnginePosition {
  qty: number
  side: 'LONG' | 'SHORT'
  entry_price: number
  stop_loss: number
  risk_per_share: number
}

export interface SimplifiedEngineStatus {
  mode: string
  dry_run: boolean
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
