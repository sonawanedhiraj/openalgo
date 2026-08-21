import { useQuery } from '@tanstack/react-query'
import { ClipboardList } from 'lucide-react'

import { intradayPullbackApi, type PickEvaluation } from '@/api/intraday-pullback'
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

export function IntradayPullbackEvalCard() {
  const { data, isLoading } = useQuery({
    queryKey: ['intraday-pullback-entry-breakdown'],
    queryFn: () => intradayPullbackApi.getEntryBreakdown(),
    refetchInterval: (q) => {
      const d = q.state.data
      // poll while the day is live (selected but not everything has traded)
      return d?.selected && d.n_trades_today < (d.picks?.length ?? 0) ? 60_000 : false
    },
  })

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm flex items-center gap-2">
          <ClipboardList className="h-4 w-4" /> Today's evaluation
          {data && (
            <span className="text-xs text-muted-foreground font-normal">
              {data.date}
              {data.side_today && ` · ${data.side_today === 'L' ? 'long' : 'short'} book`}
              {data.nifty_930_pct != null && ` · NIFTY ${pct(data.nifty_930_pct)}`}
              {` · ${data.n_trades_today} trade${data.n_trades_today === 1 ? '' : 's'}`}
            </span>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        {isLoading ? (
          <div className="p-4">
            <Skeleton className="h-20 w-full" />
          </div>
        ) : !data || !data.selected ? (
          <p className="text-sm text-muted-foreground px-4 py-6 text-center italic">
            No selection yet — the day's picks are chosen at 09:30 IST (or a book runs only when
            NIFTY is clearly up/down at the open).
          </p>
        ) : data.evaluation.length === 0 ? (
          <p className="text-sm text-muted-foreground px-4 py-6 text-center italic">
            No stock qualified at 09:30 today ({data.side_today === 'L' ? 'long' : 'short'} book).
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-y bg-muted/30 text-muted-foreground">
                  <th className="text-left px-3 py-2 font-medium">Stock</th>
                  <th className="text-left px-2 py-2 font-medium">Sector</th>
                  <th className="text-right px-2 py-2 font-medium">09:30</th>
                  <th className="text-left px-2 py-2 font-medium">Status</th>
                  <th className="text-left px-3 py-2 font-medium">Why</th>
                </tr>
              </thead>
              <tbody>
                {data.evaluation.map((e) => (
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
                        <div className="text-[11px] text-muted-foreground mt-0.5">
                          {diagLine(e.diag)}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
