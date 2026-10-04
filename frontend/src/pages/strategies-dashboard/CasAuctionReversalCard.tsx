import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, Loader2, RotateCcw, Settings2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import {
  type CasArConfig,
  type CasArConfigResponse,
  type CasArSource,
  getCasArConfig,
  getCasArStatus,
  saveCasArConfig,
} from '@/api/strategies-dashboard'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'

// cas_auction_reversal (issues #752 / #755) — the strategy's own cards on
// /strategies/cas_auction_reversal: the operator Settings form (threshold, max
// positions, capital per trade, poll interval) and today's arm/decision state.
//
// Per field the effective value is the saved UI row, else the CAS_AR_* env
// seed, else the code default — each field shows which. A save applies at the
// NEXT 15:14:30 IST arm. Live vs sandbox is the page's own toggle; this card
// never routes orders.

type Key = keyof CasArConfig

const FIELDS: { key: Key; label: string; step: number; hint: string }[] = [
  {
    key: 'threshold_pct',
    label: 'Dislocation threshold (%)',
    step: 0.05,
    hint: 'buy when the indicative close is at least this far below the last continuous print',
  },
  {
    key: 'max_positions',
    label: 'Max positions / day',
    step: 1,
    hint: 'deepest dislocations first',
  },
  {
    key: 'capital_per_trade_inr',
    label: 'Capital per trade (₹)',
    step: 5000,
    hint: 'shares = floor(capital / price)',
  },
  {
    key: 'poll_interval_s',
    label: 'Poll interval (s)',
    step: 1,
    hint: 'batched quote cadence 15:14:30–15:33',
  },
]

const SOURCE_LABEL: Record<CasArSource, string> = {
  ui: 'saved',
  env: '.env',
  default: 'default',
}

type FormState = Record<Key, string>

function toForm(c: CasArConfig): FormState {
  return {
    threshold_pct: String(c.threshold_pct),
    max_positions: String(c.max_positions),
    capital_per_trade_inr: String(c.capital_per_trade_inr),
    poll_interval_s: String(c.poll_interval_s),
  }
}

function SettingsCard() {
  const qc = useQueryClient()
  const cfg = useQuery({
    queryKey: ['cas-ar', 'config'],
    queryFn: getCasArConfig,
    refetchInterval: 30_000,
  })
  const [form, setForm] = useState<FormState | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  useEffect(() => {
    if (cfg.data && form === null) setForm(toForm(cfg.data.effective))
  }, [cfg.data, form])

  const onSaved = (data: CasArConfigResponse, text: string) => {
    setForm(toForm(data.effective))
    setMsg({ ok: true, text })
    qc.invalidateQueries({ queryKey: ['cas-ar'] })
  }
  const onFailed = (e: unknown) => {
    const err = e as { response?: { data?: { message?: string } }; message?: string }
    setMsg({ ok: false, text: err.response?.data?.message ?? err.message ?? 'save failed' })
  }
  const save = useMutation({
    mutationFn: (values: Partial<Record<Key, number | null>>) => saveCasArConfig(values),
    onSuccess: (data) => onSaved(data, `Saved — applies at the ${data.applies_at}.`),
    onError: onFailed,
  })
  const reset = useMutation({
    mutationFn: () =>
      saveCasArConfig({
        threshold_pct: null,
        max_positions: null,
        capital_per_trade_inr: null,
        poll_interval_s: null,
      }),
    onSuccess: (data) =>
      onSaved(data, `Cleared — .env / defaults apply at the ${data.applies_at}.`),
    onError: onFailed,
  })

  if (cfg.isError) {
    return (
      <Card data-testid="cas-ar-settings-card">
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Settings2 className="h-4 w-4" /> Settings
          </CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-destructive">Could not load the config.</CardContent>
      </Card>
    )
  }
  if (cfg.isLoading || !form || !cfg.data) {
    return (
      <Card data-testid="cas-ar-settings-card">
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

  const data = cfg.data
  const submit = () => {
    setMsg(null)
    save.mutate({
      threshold_pct: Number(form.threshold_pct),
      max_positions: Number(form.max_positions),
      capital_per_trade_inr: Number(form.capital_per_trade_inr),
      poll_interval_s: Number(form.poll_interval_s),
    })
  }
  const busy = save.isPending || reset.isPending

  return (
    <Card data-testid="cas-ar-settings-card">
      <CardHeader className="flex flex-row items-center justify-between gap-2">
        <CardTitle className="text-base flex items-center gap-2">
          <Settings2 className="h-4 w-4" /> Settings
        </CardTitle>
        <span className="text-xs text-muted-foreground">
          applies at the {data.applies_at}
          {data.override?.updated_at
            ? ` · last saved ${new Date(`${data.override.updated_at}Z`).toLocaleString('en-IN')}`
            : ' · nothing saved yet'}
        </span>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {FIELDS.map((f) => {
            const b = data.bounds[f.key]
            return (
              <div key={f.key} className="space-y-1">
                <div className="flex items-center justify-between gap-2">
                  <Label htmlFor={`cas-ar-${f.key}`} className="text-xs">
                    {f.label}
                  </Label>
                  <Badge
                    variant={data.sources[f.key] === 'ui' ? 'default' : 'outline'}
                    className="text-[10px] px-1.5 py-0"
                    data-testid={`cas-ar-source-${f.key}`}
                  >
                    {SOURCE_LABEL[data.sources[f.key]]}
                  </Badge>
                </div>
                <Input
                  id={`cas-ar-${f.key}`}
                  data-testid={`cas-ar-${f.key}`}
                  type="number"
                  min={b.min}
                  max={b.max}
                  step={f.step}
                  className="h-8"
                  value={form[f.key]}
                  onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}
                />
                <p className="text-[11px] text-muted-foreground">
                  {b.min.toLocaleString('en-IN')}–{b.max.toLocaleString('en-IN')} · default{' '}
                  {data.defaults[f.key].toLocaleString('en-IN')} · {f.hint}
                </p>
              </div>
            )
          })}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" onClick={submit} disabled={busy} data-testid="cas-ar-save">
            {save.isPending && <Loader2 className="h-3 w-3 mr-1 animate-spin" />}
            Save
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setMsg(null)
              reset.mutate()
            }}
            disabled={busy || !data.override}
            data-testid="cas-ar-reset"
          >
            <RotateCcw className="h-3 w-3 mr-1" /> Clear saved values
          </Button>
          {msg && (
            <span
              className={`text-xs ${msg.ok ? 'text-emerald-600' : 'text-destructive'}`}
              data-testid="cas-ar-msg"
            >
              {msg.text}
            </span>
          )}
        </div>
        {data.day_config && (
          <p className="text-xs text-muted-foreground" data-testid="cas-ar-day-config">
            Today's arm ran with: threshold {data.day_config.threshold_pct}% · max{' '}
            {data.day_config.max_positions} · ₹
            {data.day_config.capital_per_trade_inr.toLocaleString('en-IN')}/trade · poll{' '}
            {data.day_config.poll_interval_s}s
          </p>
        )}
      </CardContent>
    </Card>
  )
}

function TodayCard() {
  const st = useQuery({
    queryKey: ['cas-ar', 'status'],
    queryFn: getCasArStatus,
    refetchInterval: 15_000,
  })
  if (st.isLoading || !st.data) {
    return (
      <Card>
        <CardContent className="p-4">
          <Skeleton className="h-12 w-full" />
        </CardContent>
      </Card>
    )
  }
  const s = st.data
  const cands = s.candidates ?? []
  const entries = s.entries ?? []
  const picked = cands.filter((c) => c.selected)
  return (
    <Card data-testid="cas-ar-today-card">
      <CardHeader className="pb-2">
        <CardTitle className="text-base flex items-center gap-2">
          <Activity className="h-4 w-4" /> Today
          <Badge variant="outline" className="ml-1">
            {s.mode}
          </Badge>
          {s.manual_pause && <Badge variant="destructive">paused</Badge>}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        <div className="flex flex-wrap gap-x-6 gap-y-1 text-muted-foreground">
          <span>
            armed: <b className="text-foreground">{s.armed ? 'yes' : 'no'}</b>
          </span>
          <span>
            universe: <b className="text-foreground">{s.universe}</b>
          </span>
          <span>
            polls: <b className="text-foreground">{s.polls ?? 0}</b>
          </span>
          <span>
            decided: <b className="text-foreground">{s.decided ? 'yes' : 'no'}</b>
          </span>
          {s.iep_feed && (
            <span>
              IEP moved: <b className="text-foreground">{s.iep_feed.moved}</b>/{s.iep_feed.with_iep}
            </span>
          )}
          <span>
            candidates: <b className="text-foreground">{cands.length}</b> · selected{' '}
            <b className="text-foreground">{picked.length}</b> · entries{' '}
            <b className="text-foreground">{entries.length}</b>
          </span>
        </div>
        {picked.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {picked.map((c) => (
              <Badge key={c.symbol} variant="secondary" className="font-mono">
                {c.symbol} {c.dislocation_pct.toFixed(2)}%
              </Badge>
            ))}
          </div>
        )}
        <p className="text-xs text-muted-foreground">
          arm {s.schedule.arm} · decide {s.schedule.decide} · fill {s.schedule.fill} · summary{' '}
          {s.schedule.summary} · T+1 exit {s.schedule.exit} (retry {s.schedule.exit_retry})
        </p>
      </CardContent>
    </Card>
  )
}

export function CasAuctionReversalCard() {
  return (
    <div className="space-y-4" data-testid="cas-ar-cards">
      <SettingsCard />
      <TodayCard />
    </div>
  )
}
