import { webClient } from './client'

// Multi-account child broker accounts (issue #468).
// Backend: blueprints/broker_accounts.py → /broker_accounts/api

export interface ChildAccount {
  id: number
  display_name: string
  broker: string
  broker_client_id: string | null
  capital_inr: number
  is_enabled: boolean
  // Masked echo of the stored Kite api_key (issue #492) — null when the row has
  // no readable key. The secret is never echoed, only its presence.
  api_key_masked: string | null
  has_api_secret: boolean
  has_totp_secret: boolean
  // issue #654: whether a Kite login password is stored for headless auto-login.
  has_password: boolean
  // issue #654: per-child opt-in for automatic (watcher) re-login.
  auto_login_enabled: boolean
  last_login_at: string | null
  created_at: string | null
  updated_at: string | null
  connected: boolean
  strategies: string[]
  strategy_settings: StrategySetting[]
  redirect_url_hint: string
  today_mirrors: { placed: number; skipped: number; failed: number }
}

export interface StrategySetting {
  strategy_name: string
  capital_per_trade_inr: number | null
}

export interface MirrorOrder {
  id: number
  account_id: number
  account_name: string
  strategy_name: string
  symbol: string
  exchange: string
  action: string
  product: string | null
  parent_qty: number
  child_qty: number
  factor: number | null
  parent_orderid: string | null
  status:
    | 'placed'
    | 'rejected'
    | 'skipped_no_session'
    | 'skipped_zero_qty'
    | 'skipped_no_position'
    | 'skipped_no_capital'
    | 'skipped_no_quote'
    | 'error'
  sizing_price: number | null
  broker_orderid: string | null
  error_text: string | null
  created_at: string | null
}

export interface AccountsOverview {
  status: string
  multi_account_enabled: boolean
  primary_book_capital: number
  known_strategies: string[]
  accounts: ChildAccount[]
}

export interface MultiAccountSettings {
  enabled: boolean
  primary_book_capital: number
  updated_at: string | null
  updated_by: string | null
}

export interface TotpCode {
  status: string
  code: string
  seconds_remaining: number
  interval: number
}

export interface AddAccountPayload {
  display_name: string
  broker_client_id?: string
  api_key: string
  api_secret: string
  capital_inr: number
}

export interface UpdateAccountPayload {
  display_name?: string
  broker_client_id?: string
  capital_inr?: number
  is_enabled?: boolean
  api_key?: string
  api_secret?: string
  totp_secret?: string
  // issue #654: Kite login password for headless auto-login (write-only).
  password?: string
  // issue #654: per-child automatic re-login opt-in.
  auto_login_enabled?: boolean
}

export interface AutoLoginResult {
  status: string
  ok: boolean
  message: string
}

// Child open15 verification card (issue #663).

export interface Open15Trade {
  id: number
  symbol: string
  exchange: string
  action: string
  product: string | null
  child_qty: number
  status: string
  broker_orderid: string | null
  error_text: string | null
  created_at: string | null
}

export interface Open15Position {
  symbol: string
  exchange: string
  product: string
  open_qty: number
  pnl: number | null
}

export interface Open15AccountStatus {
  account_id: number
  display_name: string
  broker: string
  connected: boolean
  trades: Open15Trade[]
  positions: Open15Position[]
  positions_readable: boolean
  open_after_exit: boolean
  day_pnl: number | null
}

export interface Open15Status {
  status: string
  strategy: string
  exit_time: string
  after_exit_time: boolean
  now_ist: string
  date: string
  accounts: Open15AccountStatus[]
}

export interface SquareOffResult {
  status: string
  message: string
  broker_orderid?: string
  reason?: string
}

const BASE = '/broker_accounts/api'

export const brokerAccountsApi = {
  overview: async (): Promise<AccountsOverview> => {
    const { data } = await webClient.get(BASE)
    return data
  },

  updateSettings: async (payload: {
    enabled?: boolean
    primary_book_capital?: number
  }): Promise<MultiAccountSettings> => {
    const { data } = await webClient.put(`${BASE}/settings`, payload)
    return data.settings
  },

  add: async (payload: AddAccountPayload): Promise<ChildAccount> => {
    const { data } = await webClient.post(BASE, payload)
    return data.account
  },

  update: async (id: number, payload: UpdateAccountPayload): Promise<ChildAccount> => {
    const { data } = await webClient.put(`${BASE}/${id}`, payload)
    return data.account
  },

  remove: async (id: number): Promise<void> => {
    await webClient.delete(`${BASE}/${id}`)
  },

  loginUrl: async (id: number): Promise<string> => {
    const { data } = await webClient.get(`${BASE}/${id}/login_url`)
    return data.login_url
  },

  disconnect: async (id: number): Promise<void> => {
    await webClient.post(`${BASE}/${id}/disconnect`)
  },

  autoLogin: async (id: number): Promise<AutoLoginResult> => {
    const { data } = await webClient.post(`${BASE}/${id}/auto_login`)
    return data
  },

  setStrategies: async (
    id: number,
    strategies: string[],
    capitalPerTrade?: Record<string, number | null>
  ): Promise<string[]> => {
    const { data } = await webClient.post(`${BASE}/${id}/strategies`, {
      strategies,
      capital_per_trade: capitalPerTrade ?? {},
    })
    return data.strategies
  },

  totpCode: async (id: number): Promise<TotpCode> => {
    const { data } = await webClient.get(`${BASE}/${id}/totp`)
    return data
  },

  open15Status: async (): Promise<Open15Status> => {
    const { data } = await webClient.get(`${BASE}/open15_status`)
    return data
  },

  squareOff: async (
    id: number,
    payload: { symbol: string; exchange: string; product: string }
  ): Promise<SquareOffResult> => {
    const { data } = await webClient.post(`${BASE}/${id}/squareoff`, payload)
    return data
  },

  mirrorOrders: async (date?: string): Promise<MirrorOrder[]> => {
    const { data } = await webClient.get(`${BASE}/mirror_orders`, {
      params: date ? { date } : undefined,
    })
    return data.orders
  },
}
