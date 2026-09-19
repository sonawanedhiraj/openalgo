import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, RefreshCw, Settings2, Target } from 'lucide-react'
import { useEffect, useState } from 'react'
import {
  type CasStraddleConfig,
  type CasStraddleLeg,
  type CasStraddleSession,
  type CasStraddleUnderlying,
  getCasStraddleConfig,
  getCasStraddleSessions,
  getCasStraddleStatus,
  saveCasStraddleConfig,
} from '@/api/strategies-dashboard'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'

// cas_320_expiry_straddle (issue #740) — the strategy's own cards on
// /strategies/cas_320_expiry_straddle: the operator Settings form (per-underlying
// trade toggles + lots, premium cap, target multiple, hard exit, poll interval),
// today's arm/entry/target state and the per-expiry session digests.
//
// Live vs sandbox is the page's own toggle (strategy_mode); this card never
// routes orders. A save applies at the NEXT 15:12 IST arm.

const UNDERLYINGS = ['NIFTY', 'SENSEX'] as const
type U = (typeof UNDERLYINGS)[number]
const EXCH: Record<U, string> = { NIFTY: 'NSE / NFO', SENSEX: 'BSE / BFO' }

function inr(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined) return '—'
  return `₹${v.toLocaleString('en-IN', { maximumFractionDigits: digits })}`
}

function num(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return '—'
  return v.toLocaleString('en-IN', { maximumFractionDigits: digits })
}

function legBadge(leg: CasStraddleLeg) {
  if (leg.fill === 'paper') return <Badge variant="secondary">paper</Badge>
  if (leg.status === 'closed')
    return <Badge className="bg-emerald-600 hover:bg-emerald-600">{leg.exit_reason}</Badge>
  if (leg.status === 'error') return <Badge variant="destructive">error</Badge>
  if (leg.status === 'open') return <Badge>open</Badge>
  return <Badge variant="outline">{leg.status}</Badge>
}

// ---------------------------------------------------------------------------
// Settings form
// ---------------------------------------------------------------------------

type FormState = {
  trade_nifty: boolean
  trade_sensex: boolean
  lots_nifty: string
  lots_sensex: string
  max_premium_inr: string
  target_mult: string
  hard_exit_time: string
  poll_interval_s: string
}

function toForm(c: CasStraddleConfig): FormState {
  return {
    trade_nifty: c.trade_nifty,
    trade_sensex: c.trade_sensex,
    lots_nifty: String(c.lots_nifty),
    lots_sensex: String(c.lots_sensex),
    max_premium_inr: String(c.max_premium_inr),
    target_mult: String(c.target_mult),
    hard_exit_time: c.hard_exit_time,
    poll_interval_s: String(c.poll_interval_s),
  }
}

function SettingsCard({ lotsizes }: { lotsizes: Record<string, number | null> }) {
  const qc = useQueryClient()
  const cfg = useQuery({
    queryKey: ['cas-straddle', 'config'],
    queryFn: getCasStraddleConfig,
    refetchInterval: 30_000,
  })
  const [form, setForm] = useState<FormState | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  useEffect(() => {
    if (cfg.data && form === null) setForm(toForm(cfg.data.effective))
  }, [cfg.data, form])

  const save = useMutation({
    mutationFn: (values: Partial<CasStraddleConfig>) => saveCasStraddleConfig(values),
    onSuccess: (data) => {
      setForm(toForm(data.effective))
      setMsg(`Saved — applies at the ${data.applies_at}.`)
      qc.invalidateQueries({ queryKey: ['cas-straddle'] })
    },
    onError: (e: unknown) => {
      const err = e as { response?: { data?: { message?: string } }; message?: string }
      setMsg(err.response?.data?.message ?? err.message ?? 'save failed')
    },
  })

  if (cfg.isLoading || !form) {
    return (
      <Card data-testid="cas-settings-card">
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Settings2 className="h-4 w-4" /> Settings
          </CardTitle>
        </CardHeader>
        <CardContent>
          <Skeleton className="h-24 w-full" />
        </CardContent>
      </Card>
    )
  }

  const submit = () => {
    setMsg(null)
    save.mutate({
      trade_nifty: form.trade_nifty,
      trade_sensex: form.trade_sensex,
      lots_nifty: Number(form.lots_nifty),
      lots_sensex: Number(form.lots_sensex),
      max_premium_inr: Number(form.max_premium_inr),
      target_mult: Number(form.target_mult),
      hard_exit_time: form.hard_exit_time,
      poll_interval_s: Number(form.poll_interval_s),
    })
  }

  const row = (u: U) => {
    const key = u.toLowerCase() as 'nifty' | 'sensex'
    const on = form[`trade_${key}`]
    const lots = Number(form[`lots_${key}`]) || 0
    const lot = lotsizes[u] ?? null
    return (
      <div
        key={u}
        className="grid grid-cols-[1fr_auto_auto] items-center gap-3 rounded-md border p-3"
        data-testid={`cas-underlying-${u}`}
      >
        <div>
          <div className="font-medium">
            {u} <span className="text-xs text-muted-foreground">({EXCH[u]})</span>
          </div>
          <div className="text-xs text-muted-foreground">
            lot multiple {lot ?? '—'} (master contract) · quantity{' '}
            <span className="font-mono">{lot ? lots * lot : '—'}</span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Label htmlFor={`cas-lots-${u}`} className="text-xs">
            lots
          </Label>
          <Input
            id={`cas-lots-${u}`}
            data-testid={`cas-lots-${u}`}
            type="number"
            min={1}
            max={10}
            step={1}
            className="w-20 h-8"
            value={form[`lots_${key}`]}
            onChange={(e) => setForm({ ...form, [`lots_${key}`]: e.target.value })}
          />
        </div>
        <div className="flex items-center gap-2">
          <Label htmlFor={`cas-trade-${u}`} className="text-xs">
            trade
          </Label>
          <Switch
            id={`cas-trade-${u}`}
            data-testid={`cas-trade-${u}`}
            checked={on}
            onCheckedChange={(v) => setForm({ ...form, [`trade_${key}`]: v })}
          />
        </div>
      </div>
    )
  }

  return (
    <Card data-testid="cas-settings-card">
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle className="text-base flex items-center gap-2">
          <Settings2 className="h-4 w-4" /> Settings
        </CardTitle>
        <span className="text-xs text-muted-foreground">
          applies at the next 15:12 IST arm
          {cfg.data?.override?.updated_at
            ? ` · last saved ${new Date(cfg.data.override.updated_at).toLocaleString('en-IN')}`
            : ' · no override saved (code defaults)'}
        </span>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-2 md:grid-cols-2">{UNDERLYINGS.map(row)}</div>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <Label htmlFor="cas-max-premium" className="text-xs">
              max premium / underlying / expiry (₹)
            </Label>
            <Input
              id="cas-max-premium"
              data-testid="cas-max-premium"
              type="number"
              min={1000}
              step={500}
              className="h-8"
              value={form.max_premium_inr}
              onChange={(e) => setForm({ ...form, max_premium_inr: e.target.value })}
            />
          </div>
          <div>
            <Label htmlFor="cas-target" className="text-xs">
              target multiple (×cost, net)
            </Label>
            <Input
              id="cas-target"
              data-testid="cas-target"
              type="number"
              min={1.1}
              max={5}
              step={0.1}
              className="h-8"
              value={form.target_mult}
              onChange={(e) => setForm({ ...form, target_mult: e.target.value })}
            />
          </div>
          <div>
            <Label htmlFor="cas-hard-exit" className="text-xs">
              hard exit (IST, 15:20:30–15:37:00)
            </Label>
            <Input
              id="cas-hard-exit"
              data-testid="cas-hard-exit"
              type="text"
              placeholder="15:28:00"
              className="h-8 font-mono"
              value={form.hard_exit_time}
              onChange={(e) => setForm({ ...form, hard_exit_time: e.target.value })}
            />
          </div>
          <div>
            <Label htmlFor="cas-poll" className="text-xs">
              poll interval (s, 2–60)
            </Label>
            <Input
              id="cas-poll"
              data-testid="cas-poll"
              type="number"
              min={2}
              max={60}
              step={1}
              className="h-8"
              value={form.poll_interval_s}
              onChange={(e) => setForm({ ...form, poll_interval_s: e.target.value })}
            />
          </div>
        </div>
        <div className="flex items-center gap-3">
          <Button size="sm" onClick={submit} disabled={save.isPending} data-testid="cas-save">
            {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Save settings'}
          </Button>
          {msg && (
            <span className="text-xs" data-testid="cas-save-msg">
              {msg}
            </span>
          )}
        </div>
        <p className="text-xs text-muted-foreground">
          The lot multiple is exchange-set and read from the master contract at the arm; a quantity
          that is not a multiple is a guaranteed rejection, so it is not editable. A premium above
          the cap is refused at 15:20, never trimmed. Live vs sandbox is the toggle at the top of
          this page.
        </p>
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Today
// ---------------------------------------------------------------------------

function UnderlyingToday({ u, st }: { u: U; st: CasStraddleUnderlying }) {
  const legs = st.legs ?? []
  const ev = st.last_eval
  return (
    <div className="rounded-md border p-3 space-y-2" data-testid={`cas-today-${u}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{u}</span>
        {st.expiry_day ? (
          <Badge className="bg-emerald-600 hover:bg-emerald-600">expiry day · {st.expiry}</Badge>
        ) : (
          <Badge variant="outline">not an expiry day · next {st.nearest_expiry ?? '—'}</Badge>
        )}
        <Badge variant={st.trade ? 'default' : 'secondary'}>
          {st.trade ? `trade ${st.lots} lot(s)` : 'record only'}
        </Badge>
        {st.entered && <Badge>entered</Badge>}
        {st.entry_skipped && <Badge variant="secondary">skipped: {st.entry_skipped}</Badge>}
        {st.exit_done && <Badge variant="outline">exited</Badge>}
      </div>
      {st.expiry_day && (
        <div className="text-xs text-muted-foreground">
          15:15 close {num(st.close_1515)} · ATM {num(st.atm, 0)}
          {st.atm_fixed ? '' : ' (provisional)'} · CE {st.contracts?.CE ?? '—'} · PE{' '}
          {st.contracts?.PE ?? '—'} · lot {st.lotsize ?? '—'} · polls {st.n_polls ?? 0}
        </div>
      )}
      {ev && (
        <div className="text-xs font-mono" data-testid={`cas-eval-${u}`}>
          cost {inr(ev.cost)} · combined bid {inr(ev.combined_bid)} · charges est{' '}
          {inr(ev.charges_est)} · net {inr(ev.net_value)} / target {inr(ev.target_value)} @ {ev.at}
          {st.target_polls ? ` · ${st.target_polls} confirm poll(s)` : ''}
        </div>
      )}
      {legs.length > 0 && (
        <table className="w-full text-xs">
          <thead className="text-muted-foreground">
            <tr>
              <th className="text-left font-normal">leg</th>
              <th className="text-left font-normal">symbol</th>
              <th className="text-right font-normal">qty</th>
              <th className="text-right font-normal">entry</th>
              <th className="text-right font-normal">exit</th>
              <th className="text-right font-normal">net</th>
              <th className="text-left font-normal">state</th>
            </tr>
          </thead>
          <tbody>
            {legs.map((leg) => (
              <tr key={leg.symbol} className="border-t">
                <td>{leg.side}</td>
                <td className="font-mono">{leg.symbol}</td>
                <td className="text-right font-mono">{leg.entry_qty ?? leg.quantity}</td>
                <td className="text-right font-mono">
                  {num(leg.entry_price ?? leg.entry_ref_price)}
                </td>
                <td className="text-right font-mono">{num(leg.exit_price)}</td>
                <td className="text-right font-mono">{inr(leg.net_pnl)}</td>
                <td>
                  {legBadge(leg)}
                  {leg.error_message ? (
                    <span className="ml-1 text-muted-foreground">{leg.error_message}</span>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {st.errors?.length ? (
        <div className="text-xs text-red-600">{st.errors.join(' · ')}</div>
      ) : null}
    </div>
  )
}

function SessionsTable({ rows }: { rows: CasStraddleSession[] }) {
  if (rows.length === 0)
    return (
      <p className="text-xs text-muted-foreground" data-testid="cas-sessions-empty">
        No expiry sessions recorded yet — the first rows land at 15:45 IST on the next NIFTY Tuesday
        / SENSEX Thursday.
      </p>
    )
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs" data-testid="cas-sessions-table">
        <thead className="text-muted-foreground">
          <tr>
            <th className="text-left font-normal px-2">date</th>
            <th className="text-left font-normal px-2">u/l</th>
            <th className="text-right font-normal px-2">ATM</th>
            <th className="text-right font-normal px-2">15:15</th>
            <th className="text-right font-normal px-2">first</th>
            <th className="text-right font-normal px-2">low</th>
            <th className="text-right font-normal px-2">high</th>
            <th className="text-right font-normal px-2">settle</th>
            <th className="text-right font-normal px-2">ask@15:20</th>
            <th className="text-right font-normal px-2">peak bid</th>
            <th className="text-left font-normal px-2">target hit</th>
            <th className="text-left font-normal px-2">traded</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr key={`${s.trade_date}-${s.underlying}`} className="border-t font-mono">
              <td className="px-2">{s.trade_date}</td>
              <td className="px-2">{s.underlying}</td>
              <td className="px-2 text-right">{num(s.atm_strike, 0)}</td>
              <td className="px-2 text-right">{num(s.close_1515)}</td>
              <td className="px-2 text-right">{num(s.first_print)}</td>
              <td className="px-2 text-right">{num(s.iiv_low)}</td>
              <td className="px-2 text-right">{num(s.iiv_high)}</td>
              <td className="px-2 text-right">{num(s.settle)}</td>
              <td className="px-2 text-right">{num(s.straddle_ask_1520)}</td>
              <td className="px-2 text-right">
                {num(s.max_combined_bid)}
                {s.max_combined_bid_at ? ` @${s.max_combined_bid_at}` : ''}
              </td>
              <td className="px-2">{s.t_first_target ?? 'no'}</td>
              <td className="px-2">{s.traded ? 'yes' : 'no'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function CasStraddleCard() {
  const qc = useQueryClient()
  const status = useQuery({
    queryKey: ['cas-straddle', 'status'],
    queryFn: getCasStraddleStatus,
    refetchInterval: 15_000,
  })
  const sessions = useQuery({
    queryKey: ['cas-straddle', 'sessions'],
    queryFn: () => getCasStraddleSessions(30),
    refetchInterval: 60_000,
  })
  const lotsizes: Record<string, number | null> = {}
  for (const u of UNDERLYINGS) lotsizes[u] = status.data?.underlyings?.[u]?.lotsize ?? null

  return (
    <div className="space-y-4" data-testid="cas-straddle-card">
      <SettingsCard lotsizes={lotsizes} />
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-base flex items-center gap-2">
            <Target className="h-4 w-4" /> Today — arm 15:12 · entry 15:20:00 · hard exit{' '}
            {status.data?.config.hard_exit_time ?? '—'} · fallback 15:38
          </CardTitle>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            {status.data && (
              <>
                <Badge variant="outline">{status.data.mode}</Badge>
                {status.data.armed ? (
                  <Badge className="bg-emerald-600 hover:bg-emerald-600">armed</Badge>
                ) : (
                  <Badge variant="secondary">not armed</Badge>
                )}
                {status.data.monitor_alive && <Badge>monitor live</Badge>}
                {status.data.manual_pause && <Badge variant="destructive">paused</Badge>}
              </>
            )}
            <Button
              variant="ghost"
              size="sm"
              onClick={() => qc.invalidateQueries({ queryKey: ['cas-straddle'] })}
              aria-label="refresh"
            >
              <RefreshCw className="h-4 w-4" />
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-2">
          {status.isLoading && <Skeleton className="h-16 w-full" />}
          {status.isError && (
            <p className="text-xs text-red-600">status unavailable — is the service initialised?</p>
          )}
          {status.data &&
            UNDERLYINGS.map((u) =>
              status.data.underlyings[u] ? (
                <UnderlyingToday key={u} u={u} st={status.data.underlyings[u]} />
              ) : null
            )}
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Expiry sessions (the backtest dataset)</CardTitle>
        </CardHeader>
        <CardContent>
          {sessions.isLoading ? (
            <Skeleton className="h-12 w-full" />
          ) : (
            <SessionsTable rows={sessions.data ?? []} />
          )}
        </CardContent>
      </Card>
    </div>
  )
}
