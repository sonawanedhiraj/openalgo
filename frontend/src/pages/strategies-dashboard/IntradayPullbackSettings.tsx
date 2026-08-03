import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, RotateCcw, Save } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { intradayPullbackApi } from '@/api/intraday-pullback'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import { showToast } from '@/utils/toast'

const SIZING = [
  {
    key: 'fixed',
    label: 'Fixed',
    hint: 'Sizes off the base every day — resets after profit or loss.',
  },
  {
    key: 'compound',
    label: 'Compounding',
    hint: 'Sizes off net capital carried forward — grows on wins, shrinks on losses.',
  },
  {
    key: 'capped',
    label: 'Capped',
    hint: 'Shrinks on losses, never levers above the base capital.',
  },
] as const

// issue #509. The long and short books are mutually exclusive by the 09:30
// NIFTY day gate, so excluding a side does NOT move its days to the other book
// — it means no trading at all on those days. The hints say so explicitly.
const TRADE_SIDES = [
  {
    key: 'both',
    label: 'Both',
    hint: 'Long book on NIFTY-up days, short book on NIFTY-down days. This is the backtested configuration.',
  },
  {
    key: 'long_only',
    label: 'Longs only',
    hint: 'Runs the long book on NIFTY-up days and does NOT TRADE AT ALL on NIFTY-down days — those days are given up, not switched to longs. Backtest contribution: 155 trades, PF 1.72, +₹44,202.',
  },
  {
    key: 'short_only',
    label: 'Shorts only',
    hint: 'Runs the deep-loser short book on NIFTY-down days and does NOT TRADE AT ALL on NIFTY-up days. This is the unvalidated, most slippage-fragile leg. Backtest contribution: 80 trades, PF 1.40, +₹14,362.',
  },
] as const

const inr = (n: number) => `₹${Math.round(n).toLocaleString('en-IN')}`

export default function IntradayPullbackSettings() {
  const qc = useQueryClient()
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['intraday-pullback-settings'],
    queryFn: () => intradayPullbackApi.getSettings(),
  })

  const [capital, setCapital] = useState('')
  const [sizing, setSizing] = useState<string>('fixed')
  const [ntStart, setNtStart] = useState('11:00')
  const [ntEnd, setNtEnd] = useState('13:00')
  const [afEnd, setAfEnd] = useState('15:00')
  const [tradeSide, setTradeSide] = useState<string>('both')

  useEffect(() => {
    if (data) {
      setCapital(String(data.base_capital))
      setSizing(data.sizing_mode)
      setTradeSide(data.trade_side ?? 'both')
      setNtStart(data.no_trade[0])
      setNtEnd(data.no_trade[1])
      setAfEnd(data.afternoon[1])
    }
  }, [data])

  const marginPerSlot = useMemo(() => {
    const c = Number(capital)
    return Number.isFinite(c) && c > 0 ? Math.round(c / 2) : 0
  }, [capital])

  const save = useMutation({
    mutationFn: () =>
      intradayPullbackApi.updateSettings({
        base_capital: Number(capital),
        sizing_mode: sizing,
        trade_side: tradeSide,
        no_trade_start: ntStart,
        no_trade_end: ntEnd,
        afternoon_start: ntEnd, // afternoon starts where the no-trade window ends (contiguous)
        afternoon_end: afEnd,
      }),
    onSuccess: () => {
      showToast.success('Settings saved', 'strategy')
      qc.invalidateQueries({ queryKey: ['intraday-pullback-settings'] })
    },
    onError: (e: unknown) =>
      showToast.error(e instanceof Error ? e.message : 'Failed to save settings', 'strategy'),
  })

  const reset = useMutation({
    mutationFn: () => intradayPullbackApi.resetSettings(),
    onSuccess: () => {
      showToast.success('Reset to defaults', 'strategy')
      qc.invalidateQueries({ queryKey: ['intraday-pullback-settings'] })
    },
    onError: () => showToast.error('Failed to reset', 'strategy'),
  })

  const activeSizing = SIZING.find((s) => s.key === sizing) ?? SIZING[0]
  const activeTradeSide = TRADE_SIDES.find((s) => s.key === tradeSide) ?? TRADE_SIDES[0]
  const deployable =
    sizing === 'fixed'
      ? Number(capital)
      : sizing === 'capped'
        ? Math.min(Number(capital) + (data?.realized_pnl_to_date ?? 0), Number(capital))
        : Number(capital) + (data?.realized_pnl_to_date ?? 0)

  return (
    <div className="p-4 md:p-6 max-w-3xl mx-auto space-y-4">
      <div className="flex items-center gap-3">
        <Link
          to="/strategies/intraday_pullback_top2"
          className="text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-5 w-5" />
        </Link>
        <div>
          <h1 className="text-lg font-medium">Intraday pullback — combined L/S</h1>
          <p className="text-sm text-muted-foreground">Trading settings</p>
        </div>
      </div>

      {isLoading ? (
        <Card>
          <CardContent className="space-y-4 pt-6">
            <Skeleton className="h-10 w-64" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-20 w-full" />
          </CardContent>
        </Card>
      ) : isError ? (
        <Card>
          <CardContent className="pt-6">
            <p className="text-sm text-destructive">
              Couldn&apos;t load settings — the strategy service may not be running.
            </p>
            <Button variant="outline" className="mt-3" onClick={() => refetch()}>
              Retry
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <CardTitle>Trading settings</CardTitle>
              <Badge variant="secondary">editable</Badge>
            </div>
            <CardDescription>Takes effect from the next daily reset (09:00 IST).</CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="space-y-2">
              <Label htmlFor="capital">
                Capital for trading <span className="text-muted-foreground">(base)</span>
              </Label>
              <div className="flex items-center gap-2 max-w-xs">
                <span className="text-muted-foreground">₹</span>
                <Input
                  id="capital"
                  type="number"
                  step={5000}
                  value={capital}
                  onChange={(e) => setCapital(e.target.value)}
                  className="w-40"
                />
                <span className="text-xs text-muted-foreground">
                  {data?.slots ?? 2} slots · {inr(marginPerSlot)} margin each
                </span>
              </div>
            </div>

            <div className="space-y-2">
              <Label>Sizing mode</Label>
              <div className="inline-flex rounded-md border overflow-hidden">
                {SIZING.map((s) => (
                  <button
                    key={s.key}
                    type="button"
                    onClick={() => setSizing(s.key)}
                    className={`px-4 py-2 text-sm border-l first:border-l-0 transition-colors ${
                      sizing === s.key
                        ? 'bg-primary text-primary-foreground'
                        : 'bg-transparent hover:bg-muted'
                    }`}
                  >
                    {s.label}
                  </button>
                ))}
              </div>
              <p className="text-xs text-muted-foreground">{activeSizing.hint}</p>
            </div>

            <div className="space-y-2">
              <Label>Trade side</Label>
              <div className="inline-flex rounded-md border overflow-hidden">
                {TRADE_SIDES.map((s) => (
                  <button
                    key={s.key}
                    type="button"
                    onClick={() => setTradeSide(s.key)}
                    className={`px-4 py-2 text-sm border-l first:border-l-0 transition-colors ${
                      tradeSide === s.key
                        ? 'bg-primary text-primary-foreground'
                        : 'bg-transparent hover:bg-muted'
                    }`}
                  >
                    {s.label}
                  </button>
                ))}
              </div>
              <p className="text-xs text-muted-foreground">{activeTradeSide.hint}</p>
              {tradeSide !== 'both' && (
                <p className="text-xs text-amber-600 dark:text-amber-500">
                  ⚠ One-sided. The two books are mutually exclusive by the 09:30 NIFTY day gate, so
                  this gives up every day the excluded book would have run — roughly half the
                  calendar. The published backtest figures are both-sides numbers.
                </p>
              )}
            </div>

            <div className="rounded-lg bg-muted/40 px-4 py-3 flex items-baseline justify-between gap-4 flex-wrap">
              <div>
                <p className="text-xs text-muted-foreground">
                  Today&apos;s deployable capital <span>(read-only)</span>
                </p>
                <p className="text-2xl font-medium tabular-nums">
                  {inr(Number.isFinite(deployable) ? deployable : Number(capital))}
                </p>
              </div>
              <p className="text-xs text-muted-foreground text-right max-w-[240px]">
                Realized P&amp;L to date:{' '}
                <span
                  className={
                    (data?.realized_pnl_to_date ?? 0) >= 0 ? 'text-green-600' : 'text-destructive'
                  }
                >
                  {(data?.realized_pnl_to_date ?? 0) >= 0 ? '+' : ''}
                  {inr(data?.realized_pnl_to_date ?? 0)}
                </span>
                {sizing === 'fixed' ? '. Banked as cash in fixed mode.' : '. Reinvested daily.'}
              </p>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
              <div className="space-y-2">
                <Label>No-trade period</Label>
                <div className="flex items-center gap-2">
                  <Input
                    type="time"
                    value={ntStart}
                    onChange={(e) => setNtStart(e.target.value)}
                    className="w-32"
                  />
                  <span className="text-muted-foreground">→</span>
                  <Input
                    type="time"
                    value={ntEnd}
                    onChange={(e) => setNtEnd(e.target.value)}
                    className="w-32"
                  />
                </div>
                <p className="text-xs text-muted-foreground">
                  No new entries in this mid-day window.
                </p>
              </div>
              <div className="space-y-2">
                <Label>Afternoon eval period</Label>
                <div className="flex items-center gap-2">
                  <Input type="time" value={ntEnd} disabled className="w-32" />
                  <span className="text-muted-foreground">→</span>
                  <Input
                    type="time"
                    value={afEnd}
                    onChange={(e) => setAfEnd(e.target.value)}
                    className="w-32"
                  />
                </div>
                <p className="text-xs text-muted-foreground">
                  Second entry window (starts where no-trade ends; short reuses 09:30 picks).
                </p>
              </div>
            </div>

            <Separator />
            <div className="flex gap-3">
              <Button onClick={() => save.mutate()} disabled={save.isPending}>
                <Save className="h-4 w-4 mr-2" />
                {save.isPending ? 'Saving…' : 'Save changes'}
              </Button>
              <Button variant="outline" onClick={() => reset.mutate()} disabled={reset.isPending}>
                <RotateCcw className="h-4 w-4 mr-2" />
                Reset to defaults
              </Button>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
