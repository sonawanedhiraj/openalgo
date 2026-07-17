import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, ClipboardList, Clock } from 'lucide-react'
import { useState } from 'react'

import {
  type EntryBreakdown,
  type EntryBreakdownSummary,
  intradayPullbackApi,
  type PickEvaluation,
} from '@/api/intraday-pullback'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'

const pct = (n: number | null | undefined) =>
  n == null ? '—' : `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`

function diagLine(d: Record<string, number> | null): string {
  if (!d) return ''
  const parts: string[] = []
  if (d.candles) parts.push(`${d.candles} candles`)
  parts.push(`${d.ref_formed ?? 0} refs`)
  parts.push(`${d.breakouts ?? 0} breakouts`)
  if (d.gate_blocked) parts.push(`${d.gate_blocked} gate-blocked`)
  if (d.no_slot) parts.push(`${d.no_slot} no-slot`)
  return parts.join(' · ')
}

function statusBadge(p: PickEvaluation) {
  if (p.position === 'open') return <Badge className="bg-blue-500/15 text-blue-600">open</Badge>
  if (p.position === 'closed') return <Badge variant="secondary">traded</Badge>
  return <Badge variant="outline">no entry</Badge>
}

/** dd MMM for the history table's date column. */
function fmtDayLabel(isoDate: string) {
  const d = new Date(`${isoDate}T00:00:00`)
  if (Number.isNaN(d.getTime())) return isoDate
  return d.toLocaleDateString(undefined, { day: '2-digit', month: 'short' })
}

/**
 * How far a day got before it stopped, as one line. This is the whole point of the record: the
 * strategy trades ~0.7 times/day, so a zero-trade day is normal — what matters is telling "no
 * breakout ever came" apart from "breakouts came but the gate blocked them".
 */
function summaryDiagLine(s: EntryBreakdownSummary): string {
  if (!s.selected) return 'no selection'
  if (s.n_picks === 0) return 'no stock qualified at 09:30'
  const d = s.diag
  const parts = [`${d.ref_formed} refs`, `${d.breakouts} breakouts`]
  if (d.gate_blocked) parts.push(`${d.gate_blocked} gate-blocked`)
  if (d.no_slot) parts.push(`${d.no_slot} no-slot`)
  return parts.join(' · ')
}

function sideBadge(side: 'L' | 'S' | null) {
  if (side === 'L') return <Badge className="bg-green-500/15 text-green-600">long</Badge>
  if (side === 'S') return <Badge className="bg-red-500/15 text-red-600">short</Badge>
  return <span className="text-muted-foreground">—</span>
}

/**
 * Trades-per-day strip. Makes a run of zero-trade days visible at a glance — pair it with the
 * "How far it got" column to tell a quiet market from a day that never got a breakout.
 */
function TradeStrip({ rows, pending }: { rows: EntryBreakdownSummary[]; pending: boolean }) {
  const maxTrades = Math.max(1, ...rows.map((r) => r.n_trades))
  // Oldest -> newest, so the strip reads left-to-right like a calendar.
  const chrono = [...rows].reverse()
  return (
    <div className="px-4 pb-3">
      <div className="flex items-end gap-[3px] h-8">
        {chrono.map((r) => (
          <div
            key={r.eval_date}
            title={`${r.eval_date} · ${r.n_trades} trade${r.n_trades !== 1 ? 's' : ''}`}
            className={
              r.n_trades > 0
                ? 'w-4 rounded-t-sm bg-green-500/70 dark:bg-green-500/60'
                : 'w-4 rounded-t-sm bg-muted-foreground/30'
            }
            style={{ height: r.n_trades > 0 ? `${(r.n_trades / maxTrades) * 100}%` : '3px' }}
          />
        ))}
        {pending && (
          <div
            title="Today · pending"
            className="w-4 h-8 rounded-t-sm border border-dashed border-muted-foreground/40 border-b-0"
          />
        )}
      </div>
      <p className="text-[11px] text-muted-foreground mt-1.5">Trades per day · hover for detail</p>
    </div>
  )
}

/** The per-pick evaluation table for one day — the card's original body. */
function EvalBody({ payload }: { payload: EntryBreakdown }) {
  if (!payload.selected) {
    return (
      <p className="text-sm text-muted-foreground px-4 py-6 text-center italic">
        No selection recorded — a book runs only when NIFTY is clearly up/down at 09:30 IST.
      </p>
    )
  }
  if (payload.evaluation.length === 0) {
    return (
      <p className="text-sm text-muted-foreground px-4 py-6 text-center italic">
        No stock qualified at 09:30 ({payload.side_today === 'L' ? 'long' : 'short'} book).
      </p>
    )
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b bg-muted/30 text-muted-foreground">
            <th className="text-left px-3 py-2 font-medium">Stock</th>
            <th className="text-left px-2 py-2 font-medium">Sector</th>
            <th className="text-right px-2 py-2 font-medium">09:30</th>
            <th className="text-left px-2 py-2 font-medium">Status</th>
            <th className="text-left px-3 py-2 font-medium">Why</th>
          </tr>
        </thead>
        <tbody>
          {payload.evaluation.map((e) => (
            <tr key={e.symbol} className="border-b last:border-b-0 align-top">
              <td className="px-3 py-2 font-medium whitespace-nowrap">{e.symbol}</td>
              <td className="px-2 py-2 text-muted-foreground whitespace-nowrap">
                {e.sector ?? '—'}
              </td>
              <td className="px-2 py-2 text-right font-mono tabular-nums whitespace-nowrap">
                {pct(e.gain_930_pct)}
              </td>
              <td className="px-2 py-2">{statusBadge(e)}</td>
              <td className="px-3 py-2">
                <div>{e.reason}</div>
                {e.diag && diagLine(e.diag) && (
                  <div className="text-[11px] text-muted-foreground mt-0.5">{diagLine(e.diag)}</div>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * One day in the history table. The full payload is fetched only when the row is open. Past days
 * are immutable so they are cached indefinitely; today's row is still being written by the 5-min
 * eval tick, so it keeps polling while open.
 */
function EvalDayRow({
  summary,
  isToday,
  open,
  onToggle,
}: {
  summary: EntryBreakdownSummary
  isToday: boolean
  open: boolean
  onToggle: () => void
}) {
  const { data: payload, isLoading } = useQuery({
    queryKey: ['intraday-pullback-entry-breakdown', summary.eval_date],
    queryFn: () => intradayPullbackApi.getEntryBreakdown(summary.eval_date),
    enabled: open,
    staleTime: isToday ? 0 : Number.POSITIVE_INFINITY,
    refetchInterval: isToday && open ? 60_000 : false,
  })

  return (
    <>
      <tr
        className={`border-b cursor-pointer hover:bg-muted/20 ${open ? 'bg-muted/20' : ''}`}
        onClick={onToggle}
      >
        <td className="px-3 py-2 whitespace-nowrap">
          {open ? (
            <ChevronDown className="h-3.5 w-3.5 inline mr-1 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 inline mr-1 text-muted-foreground" />
          )}
          <span className="font-mono">{isToday ? 'Today' : fmtDayLabel(summary.eval_date)}</span>
        </td>
        <td className="px-2 py-2">{sideBadge(summary.side_today)}</td>
        <td className="px-2 py-2 text-right font-mono tabular-nums whitespace-nowrap text-muted-foreground">
          {pct(summary.nifty_930_pct)}
        </td>
        <td
          className={`px-2 py-2 text-right tabular-nums ${
            summary.n_picks > 0 ? '' : 'text-muted-foreground'
          }`}
        >
          {summary.n_picks}
        </td>
        <td
          className={`px-2 py-2 text-right tabular-nums ${
            summary.n_trades > 0 ? '' : 'text-muted-foreground'
          }`}
        >
          {summary.n_trades}
          {summary.n_open > 0 && (
            <span className="text-[11px] text-blue-600 ml-1">({summary.n_open} open)</span>
          )}
        </td>
        <td className="px-3 py-2 text-muted-foreground">{summaryDiagLine(summary)}</td>
      </tr>
      {open && (
        <tr className="border-b bg-muted/10">
          <td colSpan={6} className="p-0">
            {isLoading || !payload ? (
              <div className="p-4">
                <Skeleton className="h-24 w-full" />
              </div>
            ) : (
              <EvalBody payload={payload} />
            )}
          </td>
        </tr>
      )}
    </>
  )
}

/**
 * The evaluation record (issues #394 / #414 / #422). Today is simply the newest row — it shows a
 * one-line pending state until the 09:30 IST selection writes its first snapshot, and no pending
 * row at all on a weekend / NSE holiday.
 */
export function IntradayPullbackEvalCard() {
  const [limit, setLimit] = useState(30)
  const [openDate, setOpenDate] = useState<string | null>(null)

  const { data: history, isLoading } = useQuery({
    queryKey: ['intraday-pullback-eval-history', limit],
    queryFn: () => intradayPullbackApi.getEntryBreakdownHistory(limit),
    // Poll while today's row is still being written by the 5-min eval tick; past days are
    // immutable, so on a weekend / NSE holiday nothing can change.
    refetchInterval: (q) => (q.state.data?.today?.is_trading_day ? 60_000 : false),
  })

  const rows = history?.rows ?? []
  const today = history?.today
  const showPending = Boolean(today?.is_trading_day && !today.snapshot_exists)
  // Default-open the newest recorded day, so the card never opens on a blank panel while today is
  // still pending. `openDate` is null until the operator touches a row, then '' means "explicitly
  // closed" — the ?? is load-bearing (|| would treat '' as untouched and re-open the default).
  const defaultOpen = rows[0]?.eval_date ?? null
  const effectiveOpen = openDate ?? defaultOpen
  const tradeDays = rows.filter((r) => r.n_trades > 0).length

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="text-sm flex items-center gap-2">
            <ClipboardList className="h-4 w-4" /> Evaluation record
            {rows.length > 0 && (
              <span className="text-xs text-muted-foreground font-normal">
                {rows[0].mode && `mode ${rows[0].mode} · `}
                {rows.length} day{rows.length !== 1 ? 's' : ''} recorded · {tradeDays} trade day
                {tradeDays !== 1 ? 's' : ''}
              </span>
            )}
          </CardTitle>
          {(rows.length > 0 || history?.has_more) && (
            <select
              className="text-xs h-7 rounded-md border bg-background px-2"
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
            >
              <option value={30}>Last 30 days</option>
              <option value={90}>Last 90 days</option>
            </select>
          )}
        </div>
      </CardHeader>
      <CardContent className="p-0">
        {isLoading ? (
          <div className="p-4">
            <Skeleton className="h-24 w-full" />
          </div>
        ) : rows.length === 0 && !showPending ? (
          <p className="text-sm text-muted-foreground px-4 py-6 text-center italic">
            No evaluation recorded yet — the day's picks are chosen at 09:30 IST (and a book runs
            only when NIFTY is clearly up/down at the open).
          </p>
        ) : (
          <>
            {rows.length > 0 && <TradeStrip rows={rows} pending={showPending} />}
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-y bg-muted/30 text-muted-foreground">
                    <th className="text-left px-3 py-2 font-medium">Date</th>
                    <th className="text-left px-2 py-2 font-medium">Book</th>
                    <th className="text-right px-2 py-2 font-medium">NIFTY 09:30</th>
                    <th className="text-right px-2 py-2 font-medium">Picks</th>
                    <th className="text-right px-2 py-2 font-medium">Trades</th>
                    <th className="text-left px-3 py-2 font-medium">How far it got</th>
                  </tr>
                </thead>
                <tbody>
                  {showPending && (
                    <tr className="border-b">
                      <td className="px-3 py-2 whitespace-nowrap text-muted-foreground">
                        <Clock className="h-3.5 w-3.5 inline mr-1" />
                        <span className="font-mono">Today</span>
                      </td>
                      <td colSpan={5} className="px-2 py-2 text-muted-foreground italic">
                        Picks are chosen at 09:30 IST
                      </td>
                    </tr>
                  )}
                  {rows.map((r) => (
                    <EvalDayRow
                      key={r.eval_date}
                      summary={r}
                      isToday={r.eval_date === today?.date}
                      open={effectiveOpen === r.eval_date}
                      onToggle={() => setOpenDate(effectiveOpen === r.eval_date ? '' : r.eval_date)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
            {history?.has_more && limit < 90 && (
              <button
                type="button"
                className="w-full border-t px-4 py-2 text-xs text-muted-foreground hover:bg-muted/20"
                onClick={() => setLimit(90)}
              >
                Load more
              </button>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
