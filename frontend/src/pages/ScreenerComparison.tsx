import { useQuery } from '@tanstack/react-query'
import { ArrowDownCircle, ArrowUpCircle, CheckCircle2, RefreshCw, XCircle } from 'lucide-react'
import { useMemo } from 'react'
import {
  type ChartinkTimelineEvent,
  type InhouseTimelineEvent,
  type ScannerComparisonResponse,
  type ScannerComparisonSideSummary,
  type ScreenerSide,
  scannerComparisonApi,
} from '@/api/scanner-comparison'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

const REFRESH_MS = 30_000

function fmtJaccard(j: number | null): string {
  if (j === null || j === undefined || Number.isNaN(j)) return '—'
  return j.toFixed(2)
}

function fmtRatio(r: number | null): string {
  if (r === null || r === undefined || Number.isNaN(r)) return '—'
  return `${Math.round(r * 100)}%`
}

function fmtTime(ts: string): string {
  // ts is ISO-8601 with IST offset (e.g. 2026-06-19T13:19:02.018722+05:30).
  // Strip the offset and seconds for the compact column.
  const m = ts.match(/T(\d{2}:\d{2}:\d{2})/)
  return m ? m[1] : ts
}

interface SummaryStripProps {
  side: ScreenerSide
  m: ScannerComparisonSideSummary
}

function SummaryStrip({ side, m }: SummaryStripProps) {
  const isBuy = side === 'BUY'
  const Icon = isBuy ? ArrowUpCircle : ArrowDownCircle
  const accent = isBuy ? 'border-green-500/40 bg-green-500/5' : 'border-red-500/40 bg-red-500/5'
  const iconClass = isBuy ? 'text-green-500' : 'text-red-500'

  return (
    <Card className={`border-2 ${accent}`}>
      <CardHeader className="pb-3">
        <div className="flex items-center gap-3">
          <Icon className={`h-6 w-6 ${iconClass}`} />
          <div>
            <CardTitle className="text-lg">{side}</CardTitle>
            <CardDescription className="mt-0.5">In-house vs Chartink screener</CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          <div>
            <div className="text-xs text-muted-foreground">In-house</div>
            <div className="text-2xl font-semibold tabular-nums">{m.inhouse_count}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Chartink</div>
            <div className="text-2xl font-semibold tabular-nums">{m.chartink_count}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Intersection</div>
            <div className="text-2xl font-semibold tabular-nums">{m.intersection_count}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Jaccard</div>
            <div className="text-2xl font-semibold tabular-nums">{fmtJaccard(m.jaccard)}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Recall</div>
            <div className="text-2xl font-semibold tabular-nums">{fmtRatio(m.ratio)}</div>
          </div>
        </div>
        {m.tuning_suggestion ? (
          <div className="text-sm text-muted-foreground border-l-2 border-muted pl-3">
            {m.tuning_suggestion}
          </div>
        ) : null}
        {m.false_negatives.length > 0 || m.false_positives.length > 0 ? (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 text-xs">
            {m.false_negatives.length > 0 ? (
              <div>
                <span className="font-medium text-red-500">Chartink-only</span>:{' '}
                <span className="text-muted-foreground">
                  {m.false_negatives.slice(0, 12).join(', ')}
                  {m.false_negatives.length > 12 ? ` (+${m.false_negatives.length - 12})` : ''}
                </span>
              </div>
            ) : null}
            {m.false_positives.length > 0 ? (
              <div>
                <span className="font-medium text-amber-500">In-house-only</span>:{' '}
                <span className="text-muted-foreground">
                  {m.false_positives.slice(0, 12).join(', ')}
                  {m.false_positives.length > 12 ? ` (+${m.false_positives.length - 12})` : ''}
                </span>
              </div>
            ) : null}
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}

function PostedBadge({ posted }: { posted: boolean }) {
  return posted ? (
    <Badge variant="default" className="gap-1">
      <CheckCircle2 className="h-3 w-3" /> posted
    </Badge>
  ) : (
    <Badge variant="secondary" className="gap-1">
      <XCircle className="h-3 w-3" /> skipped
    </Badge>
  )
}

function SideBadge({ side }: { side: ScreenerSide }) {
  const isBuy = side === 'BUY'
  return (
    <Badge
      variant="outline"
      className={
        isBuy
          ? 'border-green-500/40 text-green-600 dark:text-green-400'
          : 'border-red-500/40 text-red-600 dark:text-red-400'
      }
    >
      {side}
    </Badge>
  )
}

interface NormalizedRow {
  key: string
  ts: string
  side: ScreenerSide
  symbols: string[]
  count: number
  posted: boolean
}

function normalizeChartink(rows: ChartinkTimelineEvent[]): NormalizedRow[] {
  return rows.map((r) => ({
    key: `ch-${r.cycle_id}-${r.side}`,
    ts: r.ts,
    side: r.side,
    symbols: r.symbols,
    count: r.count,
    posted: r.posted,
  }))
}

function normalizeInhouse(rows: InhouseTimelineEvent[]): NormalizedRow[] {
  return rows.map((r) => ({
    key: `in-${r.result_id}`,
    ts: r.ts,
    side: r.side,
    symbols: r.symbols,
    count: r.count,
    posted: r.posted,
  }))
}

interface TimelinePanelProps {
  title: string
  description: string
  emptyHint: string
  rows: NormalizedRow[]
}

function TimelinePanel({ title, description, emptyHint, rows }: TimelinePanelProps) {
  const totalSymbols = rows.reduce((acc, r) => acc + r.count, 0)

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle className="text-base">{title}</CardTitle>
            <CardDescription className="mt-1">{description}</CardDescription>
          </div>
          <Badge variant="outline">
            {rows.length} events · {totalSymbols} symbols
          </Badge>
        </div>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <div className="text-sm text-muted-foreground py-6 text-center">
            No events yet today. {emptyHint}
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-[78px]">Time</TableHead>
                <TableHead className="w-[60px]">Side</TableHead>
                <TableHead>Symbols</TableHead>
                <TableHead className="w-[96px] text-right">Engine</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((r) => (
                <TableRow key={r.key}>
                  <TableCell className="tabular-nums font-mono text-xs">{fmtTime(r.ts)}</TableCell>
                  <TableCell>
                    <SideBadge side={r.side} />
                  </TableCell>
                  <TableCell className="text-sm">
                    <div className="flex flex-wrap gap-1">
                      {r.symbols.slice(0, 12).map((s) => (
                        <span key={s} className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
                          {s}
                        </span>
                      ))}
                      {r.symbols.length > 12 ? (
                        <span className="text-xs text-muted-foreground">
                          +{r.symbols.length - 12}
                        </span>
                      ) : null}
                    </div>
                  </TableCell>
                  <TableCell className="text-right">
                    <PostedBadge posted={r.posted} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}

export default function ScreenerComparison() {
  const query = useQuery<ScannerComparisonResponse>({
    queryKey: ['scanner-comparison', 'today'],
    queryFn: () => scannerComparisonApi.getToday(),
    refetchInterval: REFRESH_MS,
    refetchOnWindowFocus: true,
  })

  const data = query.data
  const lastUpdated = useMemo(() => {
    const t = query.dataUpdatedAt
    if (!t) return null
    const d = new Date(t)
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  }, [query.dataUpdatedAt])

  return (
    <div className="py-6 space-y-6">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Screener Comparison</h1>
          <p className="text-muted-foreground mt-1">
            Live side-by-side: in-house tick-driven scanner vs the Chartink screener posted via
            webhook.{' '}
            {data ? <span className="font-mono">{data.date}</span> : <span>Today (IST).</span>}
          </p>
        </div>
        <div className="flex items-center gap-3">
          {lastUpdated ? (
            <div className="text-xs text-muted-foreground">
              Refreshed <span className="font-mono">{lastUpdated}</span> · auto every 30s
            </div>
          ) : null}
          <Button
            variant="outline"
            size="sm"
            onClick={() => query.refetch()}
            disabled={query.isFetching}
          >
            <RefreshCw className={`mr-2 h-4 w-4 ${query.isFetching ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        </div>
      </div>

      {query.isError ? (
        <Alert variant="destructive">
          <AlertTitle>Couldn't load comparison</AlertTitle>
          <AlertDescription>
            {query.error instanceof Error ? query.error.message : 'Unknown error'}
          </AlertDescription>
        </Alert>
      ) : null}

      {query.isLoading || !data ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Skeleton className="h-40 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <SummaryStrip side="BUY" m={data.summary.BUY} />
            <SummaryStrip side="SELL" m={data.summary.SELL} />
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <TimelinePanel
              title="In-house scanner"
              description="Live tick-driven matches recorded as the engine ran (scan_results · source=inhouse)."
              emptyHint="Either no rules matched, or the live tick feed hasn't surfaced any candidates yet."
              rows={normalizeInhouse(data.timeline.inhouse)}
            />
            <TimelinePanel
              title="Chartink screener"
              description="Lists posted by Chartink to the webhook (scan_cycle · cycle_kind=chartink). 'posted' means the simplified engine accepted the cycle."
              emptyHint="Either Chartink hasn't fired yet today, or the webhook wasn't reachable."
              rows={normalizeChartink(data.timeline.chartink)}
            />
          </div>
        </>
      )}
    </div>
  )
}
