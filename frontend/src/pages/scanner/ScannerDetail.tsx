import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, ArrowUpDown, RefreshCw, ScanLine, TrendingDown, TrendingUp } from 'lucide-react'
import { useCallback, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { type ScanSignal, scannerApi } from '@/api/scanner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
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

type SortField = 'run_at' | 'symbol_count'
type SortDir = 'asc' | 'desc'

function fmtDateTime(ts: string): string {
  const m = ts.match(/(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/)
  if (!m) return ts
  return `${m[1]} ${m[2]}`
}

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

export default function ScannerDetail() {
  const { id } = useParams<{ id: string }>()
  const definitionId = id ? parseInt(id, 10) : NaN

  const [sortField, setSortField] = useState<SortField>('run_at')
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const [seenIds] = useState(() => new Set<number>())

  const { data, isLoading, isError, error, refetch, isFetching, dataUpdatedAt } = useQuery({
    queryKey: ['scanner-signals', definitionId],
    queryFn: () => scannerApi.getSignals(definitionId),
    refetchInterval: REFRESH_MS,
    enabled: !Number.isNaN(definitionId),
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
              Signal history (last 24h) · auto-refreshes every 30s
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
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
                  <Badge variant={defn.enabled ? 'default' : 'secondary'}>
                    {defn.enabled ? 'Enabled' : 'Disabled'}
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
            {!isLoading && sorted.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="text-center py-12 text-muted-foreground">
                  No signals in the last 24 hours.
                </TableCell>
              </TableRow>
            )}
            {!isLoading &&
              sorted.map((sig) => <SignalRow key={sig.id} sig={sig} isNew={newIds.has(sig.id)} />)}
          </TableBody>
        </Table>
      </div>

      {data && (
        <p className="text-xs text-muted-foreground text-right">
          Showing {data.count} signal{data.count !== 1 ? 's' : ''} since {fmtDateTime(data.since)}
        </p>
      )}
    </div>
  )
}
