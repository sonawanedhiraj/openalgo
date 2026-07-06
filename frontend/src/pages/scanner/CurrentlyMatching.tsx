import { useQuery } from '@tanstack/react-query'
import { Radio, TrendingDown, TrendingUp } from 'lucide-react'
import { type CurrentlyMatchingDefinition, scannerApi } from '@/api/scanner'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'

// Same cadence as the rest of the /scanner page (ScannerIndex/ScannerDetail).
const REFRESH_MS = 30_000

// Reuses the same run_at-parsing convention as ScannerIndex/ScannerDetail:
// render the IST timestamp straight from the string (it already carries
// +05:30) rather than routing through Date()/Intl, whose output depends on
// the browser's local timezone.
function fmtTime(ts: string): string {
  const m = ts.match(/(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/)
  return m ? `${m[1]} ${m[2]}` : ts
}

// %-change column (issue #348) — matches the app's existing green/red +sign
// convention (see Positions.tsx: text-green-600 / text-red-600, "+" prefix on
// non-negative, 2dp) with an em-dash for a null (missing prev_close/last_price
// enrichment) rather than a client-side fallback value.
function PctChangeCell({ pct }: { pct: number | null }) {
  if (pct === null) {
    return <span className="text-muted-foreground">—</span>
  }
  const isPositive = pct >= 0
  return (
    <span className={isPositive ? 'text-green-600' : 'text-red-600'}>
      {isPositive ? '+' : ''}
      {pct.toFixed(2)}%
    </span>
  )
}

function DefinitionBlock({ def }: { def: CurrentlyMatchingDefinition }) {
  const isBuy = def.screener_type === 'buy'
  const Icon = isBuy ? TrendingUp : TrendingDown

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Icon className={`h-4 w-4 shrink-0 ${isBuy ? 'text-green-500' : 'text-red-500'}`} />
        <span className="text-sm font-medium truncate">{def.name}</span>
        <Badge variant={isBuy ? 'default' : 'destructive'} className="uppercase text-xs">
          {def.screener_type}
        </Badge>
        <Badge variant="outline" className="ml-auto shrink-0 tabular-nums">
          {def.symbols.length} active
        </Badge>
      </div>

      {def.symbols.length === 0 ? (
        <p className="text-xs text-muted-foreground italic pl-6">No symbols currently matching.</p>
      ) : (
        <div className="rounded-md border overflow-hidden">
          <table className="w-full text-xs">
            <thead>
              <tr className="bg-muted/40 text-muted-foreground">
                <th className="text-left font-medium px-3 py-1.5">Symbol</th>
                <th className="text-right font-medium px-3 py-1.5">% chg</th>
                <th className="text-left font-medium px-3 py-1.5">Since</th>
                <th className="text-left font-medium px-3 py-1.5">Last confirmed</th>
              </tr>
            </thead>
            <tbody>
              {/* Order comes straight from the API (buy: pct desc / sell: pct
                  asc, nulls last, tie alphabetical) — no client-side sort. */}
              {def.symbols.map((s) => (
                <tr key={s.symbol} className="border-t">
                  <td className="px-3 py-1.5 font-mono font-medium">{s.symbol}</td>
                  <td className="px-3 py-1.5 text-right tabular-nums">
                    <PctChangeCell pct={s.pct_change} />
                  </td>
                  <td className="px-3 py-1.5 tabular-nums text-muted-foreground">
                    {fmtTime(s.first_seen_at)}
                  </td>
                  <td className="px-3 py-1.5 tabular-nums text-muted-foreground">
                    {fmtTime(s.last_confirmed_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function CurrentlyMatchingSkeleton() {
  return (
    <div className="space-y-4">
      {[1, 2].map((i) => (
        <div key={i} className="space-y-2">
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-8 w-full" />
        </div>
      ))}
    </div>
  )
}

/**
 * "Currently matching" — the live list, symbols drop off when the screener
 * conditions stop holding (issue #342). Backed by GET
 * /scanner/api/currently-matching, which projects scan_results through a
 * TTL (SCANNER_ACTIVE_TTL_MIN, default 12 min) rather than tracking new
 * scanner state: the scan rules re-fire every 5m bar close while conditions
 * hold, so a symbol ages out of this list within about a TTL window of the
 * conditions no longer holding.
 *
 * This section is purely additive and sits ABOVE the existing signal
 * history (By Definition / By Symbol tabs) on the page — it never filters,
 * mutates, or truncates scan_results. History remains the permanent audit
 * trail of every fired signal.
 */
export function CurrentlyMatching() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['scanner-currently-matching'],
    queryFn: () => scannerApi.getCurrentlyMatching(),
    refetchInterval: REFRESH_MS,
  })

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Radio className="h-4 w-4 text-primary" />
          <CardTitle className="text-base">Currently matching</CardTitle>
          <span className="text-xs text-muted-foreground">
            · live list, auto-refreshes every 30s
            {data ? ` · TTL ${data.ttl_minutes}m` : ''}
          </span>
        </div>
      </CardHeader>
      <CardContent className="space-y-5">
        {isError && (
          <p className="text-sm text-destructive">
            Failed to load currently-matching symbols:{' '}
            {error instanceof Error ? error.message : 'Unknown error'}
          </p>
        )}

        {isLoading && <CurrentlyMatchingSkeleton />}

        {!isLoading && !isError && (data?.definitions.length ?? 0) === 0 && (
          <p className="text-sm text-muted-foreground italic">No enabled scan definitions.</p>
        )}

        {!isLoading &&
          data &&
          data.definitions.length > 0 &&
          data.definitions.map((def) => <DefinitionBlock key={def.id} def={def} />)}
      </CardContent>
    </Card>
  )
}
