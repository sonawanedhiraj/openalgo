import { useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Wallet } from 'lucide-react'
import { useState } from 'react'
import {
  type AccountPnlRow,
  type AccountsPnlResponse,
  getAccountsPnl,
  type PnlWindow,
} from '@/api/strategies-dashboard'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'

// Per-account realized P&L (issue #700). Answers one question for every
// account running the strategy — is it in profit or loss, net of charges? —
// one row per account (Primary + each child), a strategy total and a verdict
// that is simply the sign of the total. No individual trades.
//
// Conventions the page relies on (see services/strategy_accounts_pnl.py):
// - The Primary row is the SAME row set as the Performance table's Live column.
// - Children come from their own fills; a day with mirrors but no captured
//   row is "missing" and counted — it is never rendered as ₹0.

const WINDOWS: { label: string; value: PnlWindow }[] = [
  { label: '1D', value: '1d' },
  { label: '1W', value: '1w' },
  { label: '1M', value: '1m' },
  { label: 'All', value: 'all' },
]

function inr(v: number | null | undefined, opts: { sign?: boolean } = {}): string {
  if (v === null || v === undefined) return '—'
  const abs = Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })
  const sign = v < 0 ? '−' : opts.sign && v > 0 ? '+' : ''
  return `${sign}₹${abs}`
}

function pnlClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return 'text-muted-foreground'
  if (v > 0) return 'text-emerald-600 dark:text-emerald-400'
  if (v < 0) return 'text-red-600 dark:text-red-400'
  return ''
}

function captureBadge(row: AccountPnlRow) {
  const src =
    row.charges_source === 'broker'
      ? 'broker charges'
      : row.charges_source === 'mixed'
        ? 'mixed charges'
        : 'modelled charges'
  switch (row.capture) {
    case 'journal':
      return <Badge variant="outline">own journal · {src}</Badge>
    case 'final':
      return <Badge className="bg-emerald-600 hover:bg-emerald-600">final · {src}</Badge>
    case 'provisional':
      return <Badge variant="secondary">provisional · {src}</Badge>
    case 'missing':
      return (
        <Badge variant="destructive" data-testid={`acct-pnl-missing-${row.account_id}`}>
          not captured today
        </Badge>
      )
    default:
      return <Badge variant="outline">no trades today</Badge>
  }
}

function MiniBars({ daily }: { daily: [string, number][] }) {
  // Last 10 days' NET (de-cumulated from the cumulative series).
  const nets: number[] = []
  for (let i = 0; i < daily.length; i++) {
    nets.push(daily[i][1] - (i > 0 ? daily[i - 1][1] : 0))
  }
  const last = nets.slice(-10)
  if (last.length === 0) return <span className="text-xs text-muted-foreground">—</span>
  const max = Math.max(...last.map((n) => Math.abs(n)), 1)
  return (
    <span className="inline-flex items-end gap-[2px] h-5" aria-hidden="true">
      {last.map((n, i) => {
        const h = Math.max(2, Math.round((Math.abs(n) / max) * 18))
        const cls =
          n > 0 ? 'bg-emerald-500' : n < 0 ? 'bg-red-500 self-start' : 'bg-muted-foreground/40'
        return (
          <i
            key={`${i}-${n}`}
            className={`block w-[5px] rounded-[1px] ${cls}`}
            style={{ height: h }}
          />
        )
      })}
    </span>
  )
}

function VerdictBar({ data }: { data: AccountsPnlResponse }) {
  const total = data.total.net_inr
  const verdict = data.verdict
  const tone =
    verdict === 'profit'
      ? 'bg-emerald-50 dark:bg-emerald-950/40'
      : verdict === 'loss'
        ? 'bg-red-50 dark:bg-red-950/40'
        : 'bg-muted/40'
  const pill =
    verdict === 'profit'
      ? 'bg-emerald-600 text-white'
      : verdict === 'loss'
        ? 'bg-red-600 text-white'
        : 'bg-muted text-muted-foreground'
  const primary = data.accounts.find((a) => a.role === 'primary')
  const children = data.accounts.filter((a) => a.role === 'child')
  const childNet = children.some((c) => c.net_inr !== null)
    ? children.reduce((s, c) => s + (c.net_inr ?? 0), 0)
    : null
  const windowLabel = WINDOWS.find((w) => w.value === data.window)?.label ?? data.window
  return (
    <div
      className={`mx-4 mb-3 rounded-md px-4 py-3 flex flex-wrap items-center gap-x-6 gap-y-2 ${tone}`}
      data-testid="acct-pnl-verdict"
    >
      <span className={`text-xs font-bold tracking-wider uppercase px-3 py-1.5 rounded ${pill}`}>
        {verdict === 'flat' ? 'Flat' : verdict}
      </span>
      <div>
        <div className={`font-mono text-2xl font-semibold tabular-nums ${pnlClass(total)}`}>
          {inr(total, { sign: true })}
        </div>
        <div className="text-xs text-muted-foreground">
          net of charges · {data.total.n_accounts} account{data.total.n_accounts === 1 ? '' : 's'} ·{' '}
          {data.window === 'all' && data.since ? `since ${data.since}` : windowLabel}
          {data.total.days_missing > 0 && (
            <span className="text-red-600 dark:text-red-400">
              {' '}
              · excludes {data.total.days_missing} uncaptured child-day
              {data.total.days_missing === 1 ? '' : 's'}
            </span>
          )}
        </div>
      </div>
      <div className="ml-auto flex gap-6 text-right">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Primary</div>
          <div
            className={`font-mono text-sm font-semibold tabular-nums ${pnlClass(primary?.net_inr)}`}
          >
            {inr(primary?.net_inr, { sign: true })}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">Children</div>
          <div className={`font-mono text-sm font-semibold tabular-nums ${pnlClass(childNet)}`}>
            {inr(childNet, { sign: true })}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
            Days traded
          </div>
          <div className="font-mono text-sm font-semibold tabular-nums">
            {data.total.days_traded}
          </div>
        </div>
      </div>
    </div>
  )
}

export function AccountPnlCard({ name }: { name: string }) {
  const [window, setWindow] = useState<PnlWindow>('all')
  const queryClient = useQueryClient()
  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ['strategy-accounts-pnl', name, window],
    queryFn: () => getAccountsPnl(name, window),
    refetchInterval: 60000,
    retry: false,
  })

  // A strategy with neither a journal adapter nor any mirroring child has
  // nothing to say here — render nothing rather than an empty table.
  if (!isLoading && (!data || data.accounts.length === 0)) return null

  const recapture = async () => {
    await getAccountsPnl(name, window, true)
    await queryClient.invalidateQueries({ queryKey: ['strategy-accounts-pnl', name] })
    await refetch()
  }

  return (
    <Card data-testid="acct-pnl-card">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <CardTitle className="text-sm flex items-center gap-2">
            <Wallet className="h-4 w-4" /> Account P&L — is it making money?
          </CardTitle>
          <div className="flex items-center gap-1">
            {WINDOWS.map((w) => (
              <Button
                key={w.value}
                variant={window === w.value ? 'secondary' : 'ghost'}
                size="sm"
                className="h-6 px-2 text-xs"
                onClick={() => setWindow(w.value)}
                data-testid={`acct-pnl-window-${w.value}`}
              >
                {w.label}
              </Button>
            ))}
            <Button
              variant="outline"
              size="sm"
              className="h-6 px-2 text-xs gap-1 ml-2"
              onClick={recapture}
              disabled={isFetching}
              title="Re-read today's child fills from the broker (same-day only)"
            >
              <RefreshCw className={`h-3 w-3 ${isFetching ? 'animate-spin' : ''}`} />
              Recapture today
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        {isLoading || !data ? (
          <div className="p-4">
            <Skeleton className="h-24 w-full" />
          </div>
        ) : (
          <>
            <VerdictBar data={data} />
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/30 text-xs text-muted-foreground">
                    <th className="text-left px-4 py-2 font-medium">Account</th>
                    <th className="text-right px-3 py-2 font-medium">Net P&L</th>
                    <th className="text-right px-3 py-2 font-medium">Today</th>
                    <th className="text-right px-3 py-2 font-medium">Days</th>
                    <th className="text-right px-3 py-2 font-medium">Win days</th>
                    <th className="text-right px-3 py-2 font-medium">Max DD</th>
                    <th className="text-right px-3 py-2 font-medium">On capital</th>
                    <th className="text-right px-3 py-2 font-medium">Last 10 days</th>
                    <th className="text-left px-3 py-2 font-medium">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {data.accounts.map((row) => (
                    <tr
                      key={row.account_id ?? 'primary'}
                      className="border-b last:border-0 hover:bg-muted/20"
                      data-testid={`acct-pnl-row-${row.account_id ?? 'primary'}`}
                    >
                      <td className="px-4 py-2">
                        <div className="font-medium">{row.name}</div>
                        <div className="text-[11px] text-muted-foreground">
                          {row.role === 'primary' ? 'own journal' : 'own fills'}
                          {row.days_missing > 0 && (
                            <span className="text-red-600 dark:text-red-400">
                              {' '}
                              · {row.days_missing} day{row.days_missing === 1 ? '' : 's'} not
                              captured
                            </span>
                          )}
                        </div>
                      </td>
                      <td
                        className={`px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap font-semibold ${pnlClass(row.net_inr)}`}
                      >
                        {inr(row.net_inr, { sign: true })}
                      </td>
                      <td
                        className={`px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap ${pnlClass(row.today_net_inr)}`}
                      >
                        {inr(row.today_net_inr, { sign: true })}
                      </td>
                      <td className="px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap">
                        {row.days_traded}
                      </td>
                      <td className="px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap">
                        {row.win_days_pct === null ? '—' : `${row.win_days_pct}%`}
                      </td>
                      <td
                        className={`px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap ${pnlClass(row.max_dd_inr)}`}
                      >
                        {inr(row.max_dd_inr)}
                      </td>
                      <td className="px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap">
                        {row.return_pct === null ? (
                          '—'
                        ) : (
                          <>
                            <span className={pnlClass(row.return_pct)}>
                              {row.return_pct > 0 ? '+' : ''}
                              {row.return_pct}%
                            </span>
                            <div className="text-[10px] text-muted-foreground">
                              on {inr(row.capital_basis_inr)}
                              {row.role === 'primary' ? ' notional' : ''}
                            </div>
                          </>
                        )}
                      </td>
                      <td className="px-3 py-2 text-right">
                        <MiniBars daily={row.daily} />
                      </td>
                      <td className="px-3 py-2">{captureBadge(row)}</td>
                    </tr>
                  ))}
                  <tr className="border-t-2 bg-muted/20 font-semibold">
                    <td className="px-4 py-2">Strategy total</td>
                    <td
                      className={`px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap ${pnlClass(data.total.net_inr)}`}
                    >
                      {inr(data.total.net_inr, { sign: true })}
                    </td>
                    <td className="px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap">
                      {(() => {
                        const todays = data.accounts.filter((a) => a.today_net_inr !== null)
                        if (todays.length === 0) return '—'
                        const t = todays.reduce((s, a) => s + (a.today_net_inr ?? 0), 0)
                        return <span className={pnlClass(t)}>{inr(t, { sign: true })}</span>
                      })()}
                    </td>
                    <td className="px-3 py-2 text-right font-mono tabular-nums whitespace-nowrap">
                      {data.total.days_traded}
                    </td>
                    <td className="px-3 py-2" />
                    <td className="px-3 py-2" />
                    <td className="px-3 py-2" />
                    <td className="px-3 py-2" />
                    <td className="px-3 py-2">
                      {data.total.days_missing > 0 ? (
                        <Badge variant="secondary">{data.total.days_missing} day(s) excluded</Badge>
                      ) : null}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p className="px-4 py-3 text-[11px] text-muted-foreground">
              Primary = this page&apos;s Performance table Live figure (same rows, same net).
              Children = realized from each child&apos;s own broker fills, matched to mirror orders,
              net of charges (broker figure where available, modelled otherwise). A day a child was
              not captured is excluded and counted, never shown as ₹0. Whole-account P&amp;L, which
              would include manual trades, is not part of this table.
            </p>
          </>
        )}
      </CardContent>
    </Card>
  )
}
