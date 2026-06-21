import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ArrowUpDown, RefreshCw, ScanLine, TrendingDown, TrendingUp } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { type ScanSignal, scannerApi } from '@/api/scanner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

const REFRESH_MS = 30_000

type SortField = 'run_at' | 'symbol_count'
type SortDir = 'asc' | 'desc'

function fmtDateTime(ts: string): string {
  const m = ts.match(/(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/)
  if (!m) return ts
  return `${m[1]} ${m[2]}`
}

function todayStr(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function daysAgo(n: number): string {
  const d = new Date()
  d.setDate(d.getDate() - n)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function toISTIso(dateStr: string, endOfDay = false): string {
  const time = endOfDay ? 'T23:59:59' : 'T00:00:00'
  return `${dateStr}${time}+05:30`
}

// ---------------------------------------------------------------------------
// Hit-density bar chart
// ---------------------------------------------------------------------------

interface HourBucket {
  hour: number
  label: string
  count: number
}

function HitDensityChart({
  signals,
  activeHour,
  onHourClick,
}: {
  signals: ScanSignal[]
  activeHour: number | null
  onHourClick: (h: number | null) => void
}) {
  const buckets = useMemo<HourBucket[]>(() => {
    const counts = new Array(24).fill(0)
    for (const sig of signals) {
      const m = sig.run_at.match(/T(\d{2}):/)
      if (m) counts[parseInt(m[1], 10)] += 1
    }
    return counts
      .map((count, hour) => ({
        hour,
        label: `${String(hour).padStart(2, '0')}:00`,
        count,
      }))
      .filter((b) => b.count > 0)
  }, [signals])

  if (buckets.length === 0) return null

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wide">
          Hits per hour
          {activeHour !== null && (
            <span className="ml-2 normal-case text-primary">
              · filtered to {String(activeHour).padStart(2, '0')}:xx
              <button
                type="button"
                className="ml-2 underline text-xs"
                onClick={() => onHourClick(null)}
              >
                clear
              </button>
            </span>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="h-36 pt-0">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={buckets} margin={{ top: 4, right: 8, left: -20, bottom: 0 }}>
            <XAxis dataKey="label" tick={{ fontSize: 10 }} />
            <YAxis allowDecimals={false} tick={{ fontSize: 10 }} />
            <Tooltip
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              formatter={(value: any) => [value, 'signals']}
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              labelFormatter={(label: any) => `Hour ${label}`}
            />
            <Bar
              dataKey="count"
              radius={[3, 3, 0, 0]}
              cursor="pointer"
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              onClick={(entry: any) => {
                const h = typeof entry?.hour === 'number' ? entry.hour : null
                if (h !== null) onHourClick(activeHour === h ? null : h)
              }}
            >
              {buckets.map((b) => (
                <Cell
                  key={b.hour}
                  fill={
                    activeHour === null || activeHour === b.hour
                      ? 'hsl(var(--primary))'
                      : 'hsl(var(--muted))'
                  }
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Signal row
// ---------------------------------------------------------------------------

function SignalRow({ sig, isNew }: { sig: ScanSignal; isNew: boolean }) {
  return (
    <TableRow className={isNew ? 'bg-primary/5 animate-pulse-once' : ''}>
      <TableCell className="tabular-nums text-xs text-muted-foreground whitespace-nowrap">
        {fmtDateTime(sig.run_at)}
      </TableCell>
      <TableCell>
        {sig.symbols.length === 0 ? (
          <span className="text-muted-foreground italic text-xs">—</span>
        ) : (
          <div className="flex flex-wrap gap-1">
            {sig.symbols.map((s) => (
              <Badge key={s} variant="secondary" className="text-xs font-mono">
                {s}
              </Badge>
            ))}
          </div>
        )}
      </TableCell>
      <TableCell className="text-center tabular-nums">{sig.symbols.length}</TableCell>
      <TableCell className="text-center">
        <Badge variant="outline" className="text-xs">
          {sig.source}
        </Badge>
      </TableCell>
      <TableCell className="text-center">
        {sig.posted_to_engine ? (
          <Badge variant="default" className="text-xs bg-green-600">
            Yes
          </Badge>
        ) : (
          <span className="text-muted-foreground text-xs">—</span>
        )}
      </TableCell>
    </TableRow>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function ScannerDetail() {
  const { id } = useParams<{ id: string }>()
  const definitionId = id ? parseInt(id, 10) : NaN
  const queryClient = useQueryClient()

  const [sortField, setSortField] = useState<SortField>('run_at')
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const [seenIds] = useState(() => new Set<number>())
  const [activeHour, setActiveHour] = useState<number | null>(null)

  // Date range — default last 7 days
  const [sinceDate, setSinceDate] = useState(daysAgo(7))
  const [untilDate, setUntilDate] = useState(todayStr())

  const sinceISO = toISTIso(sinceDate, false)
  const untilISO = toISTIso(untilDate, true)

  const [optimisticEnabled, setOptimisticEnabled] = useState<boolean | null>(null)

  const { data, isLoading, isError, error, refetch, isFetching, dataUpdatedAt } = useQuery({
    queryKey: ['scanner-signals', definitionId, sinceDate, untilDate],
    queryFn: () => scannerApi.getSignals(definitionId, sinceISO, untilISO),
    refetchInterval: REFRESH_MS,
    enabled: !Number.isNaN(definitionId),
  })

  // Sync toggle state with server data
  useEffect(() => {
    if (data?.definition?.enabled !== undefined) {
      setOptimisticEnabled(data.definition.enabled)
    }
  }, [data?.definition?.enabled])

  const toggleMutation = useMutation({
    mutationFn: () => scannerApi.toggleDefinition(definitionId),
    onMutate: () => {
      setOptimisticEnabled((prev) => (prev === null ? null : !prev))
    },
    onSuccess: (result) => {
      setOptimisticEnabled(result.enabled)
      queryClient.invalidateQueries({ queryKey: ['scanner-definitions'] })
      queryClient.invalidateQueries({ queryKey: ['scanner-signals', definitionId] })
    },
    onError: () => {
      setOptimisticEnabled(data?.definition?.enabled ?? null)
    },
  })

  const handleSort = useCallback(
    (field: SortField) => {
      if (sortField === field) {
        setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
      } else {
        setSortField(field)
        setSortDir('desc')
      }
    },
    [sortField]
  )

  const sorted = useMemo(() => {
    if (!data) return []
    return [...data.signals].sort((a, b) => {
      let cmp = 0
      if (sortField === 'run_at') {
        cmp = a.run_at < b.run_at ? -1 : a.run_at > b.run_at ? 1 : 0
      } else {
        cmp = a.symbols.length - b.symbols.length
      }
      return sortDir === 'asc' ? cmp : -cmp
    })
  }, [data, sortField, sortDir])

  const displayed = useMemo(() => {
    if (activeHour === null) return sorted
    return sorted.filter((sig) => {
      const m = sig.run_at.match(/T(\d{2}):/)
      return m ? parseInt(m[1], 10) === activeHour : false
    })
  }, [sorted, activeHour])

  const newIds = useMemo(() => {
    if (!data) return new Set<number>()
    const fresh = new Set<number>()
    for (const sig of data.signals) {
      if (!seenIds.has(sig.id)) {
        fresh.add(sig.id)
        seenIds.add(sig.id)
      }
    }
    return fresh
  }, [data, seenIds])

  const lastUpdated = dataUpdatedAt
    ? new Date(dataUpdatedAt).toLocaleTimeString('en-IN', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      })
    : null

  const defn = data?.definition
  const enabledState = optimisticEnabled ?? defn?.enabled ?? true
  const isBuy = defn?.screener_type === 'buy'
  const Icon = isBuy ? TrendingUp : TrendingDown

  return (
    <div className="p-4 md:p-6 space-y-6">
      {/* Back nav */}
      <Link
        to="/scanner"
        className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        All definitions
      </Link>

      {/* Header */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3 min-w-0">
          {defn ? (
            <Icon className={`h-6 w-6 shrink-0 ${isBuy ? 'text-green-500' : 'text-red-500'}`} />
          ) : (
            <ScanLine className="h-6 w-6 text-primary shrink-0" />
          )}
          <div className="min-w-0">
            {isLoading ? (
              <Skeleton className="h-7 w-48" />
            ) : (
              <h1 className="text-2xl font-semibold truncate">
                {defn?.name ?? `Definition ${definitionId}`}
              </h1>
            )}
            <p className="text-sm text-muted-foreground">
              Signal history · auto-refreshes every 30s
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          {defn && (
            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground">
                {enabledState ? 'Enabled' : 'Disabled'}
              </span>
              <Switch
                checked={enabledState}
                onCheckedChange={() => toggleMutation.mutate()}
                disabled={toggleMutation.isPending || isLoading}
                aria-label="Toggle definition"
              />
            </div>
          )}
          {lastUpdated && (
            <span className="text-xs text-muted-foreground">Updated {lastUpdated}</span>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => refetch()}
            disabled={isFetching}
            className="gap-1.5"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        </div>
      </div>

      {/* Metadata card */}
      {defn && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground uppercase tracking-wide">
              Parameters
            </CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="grid grid-cols-2 sm:grid-cols-4 gap-x-6 gap-y-3 text-sm">
              <div>
                <dt className="text-xs text-muted-foreground">Type</dt>
                <dd>
                  <Badge variant={isBuy ? 'default' : 'destructive'} className="uppercase">
                    {defn.screener_type}
                  </Badge>
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Status</dt>
                <dd>
                  <Badge variant={enabledState ? 'default' : 'secondary'}>
                    {enabledState ? 'Enabled' : 'Disabled'}
                  </Badge>
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Rule module</dt>
                <dd className="font-mono text-xs break-all">
                  {defn.rule_module ? defn.rule_module.replace('services.scan_rules.', '') : '—'}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-muted-foreground">Last updated</dt>
                <dd className="text-xs">{fmtDateTime(defn.updated_at)}</dd>
              </div>
            </dl>
          </CardContent>
        </Card>
      )}

      {/* Date range picker */}
      <Card>
        <CardContent className="pt-4">
          <div className="flex flex-wrap items-center gap-4">
            <div className="flex items-center gap-2">
              <label className="text-sm font-medium" htmlFor="since-date">
                From
              </label>
              <input
                id="since-date"
                type="date"
                value={sinceDate}
                max={untilDate}
                onChange={(e) => {
                  setSinceDate(e.target.value)
                  setActiveHour(null)
                }}
                className="h-9 rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              />
            </div>
            <div className="flex items-center gap-2">
              <label className="text-sm font-medium" htmlFor="until-date">
                To
              </label>
              <input
                id="until-date"
                type="date"
                value={untilDate}
                min={sinceDate}
                max={todayStr()}
                onChange={(e) => {
                  setUntilDate(e.target.value)
                  setActiveHour(null)
                }}
                className="h-9 rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              />
            </div>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setSinceDate(daysAgo(7))
                setUntilDate(todayStr())
                setActiveHour(null)
              }}
            >
              Reset to last 7 days
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Hit density chart */}
      {!isLoading && (data?.signals.length ?? 0) > 0 && (
        <HitDensityChart
          signals={data?.signals ?? []}
          activeHour={activeHour}
          onHourClick={setActiveHour}
        />
      )}

      {/* Error */}
      {isError && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          Failed to load signals: {error instanceof Error ? error.message : 'Unknown error'}
        </div>
      )}

      {/* Signal table */}
      <div className="rounded-lg border overflow-hidden">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>
                <Button
                  variant="ghost"
                  size="sm"
                  className="-ml-2 h-7 gap-1"
                  onClick={() => handleSort('run_at')}
                >
                  Time (IST)
                  <ArrowUpDown className="h-3.5 w-3.5 opacity-50" />
                </Button>
              </TableHead>
              <TableHead>Symbols</TableHead>
              <TableHead className="text-center">
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 gap-1"
                  onClick={() => handleSort('symbol_count')}
                >
                  Count
                  <ArrowUpDown className="h-3.5 w-3.5 opacity-50" />
                </Button>
              </TableHead>
              <TableHead className="text-center">Source</TableHead>
              <TableHead className="text-center">Posted</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading &&
              [1, 2, 3, 4, 5].map((i) => (
                <TableRow key={i}>
                  <TableCell>
                    <Skeleton className="h-4 w-28" />
                  </TableCell>
                  <TableCell>
                    <Skeleton className="h-4 w-48" />
                  </TableCell>
                  <TableCell>
                    <Skeleton className="h-4 w-8 mx-auto" />
                  </TableCell>
                  <TableCell>
                    <Skeleton className="h-4 w-16 mx-auto" />
                  </TableCell>
                  <TableCell>
                    <Skeleton className="h-4 w-8 mx-auto" />
                  </TableCell>
                </TableRow>
              ))}
            {!isLoading && displayed.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="text-center py-12 text-muted-foreground">
                  {activeHour !== null
                    ? `No signals in hour ${String(activeHour).padStart(2, '0')}:xx for this range.`
                    : `No signals from ${sinceDate} to ${untilDate}.`}
                </TableCell>
              </TableRow>
            )}
            {!isLoading &&
              displayed.map((sig) => (
                <SignalRow key={sig.id} sig={sig} isNew={newIds.has(sig.id)} />
              ))}
          </TableBody>
        </Table>
      </div>

      {data && (
        <p className="text-xs text-muted-foreground text-right">
          Showing {displayed.length} of {data.count} signal{data.count !== 1 ? 's' : ''} ·{' '}
          {sinceDate} to {untilDate}
        </p>
      )}
    </div>
  )
}
