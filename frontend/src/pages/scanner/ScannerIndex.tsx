import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, RefreshCw, ScanLine, TrendingDown, TrendingUp } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { type ScanDefinitionSummary, scannerApi } from '@/api/scanner'
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

const REFRESH_MS = 30_000

function fmtTime(ts: string, todayDate?: string): string {
  const date = new Date(ts)

  if (!todayDate) {
    const m = ts.match(/T(\d{2}:\d{2}:\d{2})/)
    return m ? m[1] : ts
  }

  const signalDate = ts.split('T')[0]
  if (signalDate === todayDate) {
    const m = ts.match(/T(\d{2}:\d{2}:\d{2})/)
    return m ? m[1] : ts
  }

  return new Intl.DateTimeFormat('en-IN', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}

function todayStr(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

// ---------------------------------------------------------------------------
// Definition card with toggle
// ---------------------------------------------------------------------------

function DefinitionCard({ def }: { def: ScanDefinitionSummary }) {
  const queryClient = useQueryClient()
  const [optimisticEnabled, setOptimisticEnabled] = useState(def.enabled)

  useEffect(() => {
    setOptimisticEnabled(def.enabled)
  }, [def.enabled])

  const toggleMutation = useMutation({
    mutationFn: () => scannerApi.toggleDefinition(def.id),
    onMutate: () => {
      setOptimisticEnabled((prev) => !prev)
    },
    onSuccess: (result) => {
      setOptimisticEnabled(result.enabled)
      queryClient.invalidateQueries({ queryKey: ['scanner-definitions'] })
    },
    onError: () => {
      setOptimisticEnabled(def.enabled)
    },
  })

  const isBuy = def.screener_type === 'buy'
  const Icon = isBuy ? TrendingUp : TrendingDown
  const accent = optimisticEnabled
    ? isBuy
      ? 'border-green-500/30 bg-green-500/5'
      : 'border-red-500/30 bg-red-500/5'
    : 'border-muted bg-muted/20 opacity-60'
  const badgeVariant = isBuy ? 'default' : 'destructive'

  return (
    <Card className={`border ${accent} hover:shadow-md transition-shadow`}>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <Icon
              className={`h-5 w-5 shrink-0 ${isBuy ? 'text-green-500' : 'text-red-500'} ${!optimisticEnabled ? 'opacity-50' : ''}`}
            />
            <CardTitle className="text-base truncate">{def.name}</CardTitle>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <Switch
              checked={optimisticEnabled}
              onCheckedChange={() => toggleMutation.mutate()}
              disabled={toggleMutation.isPending}
              aria-label={`Toggle ${def.name}`}
            />
            <Badge variant={badgeVariant} className="uppercase text-xs">
              {def.screener_type}
            </Badge>
            <Badge variant="outline" className="tabular-nums">
              {def.today_hit_count} today
            </Badge>
          </div>
        </div>
        {!optimisticEnabled && (
          <p className="text-xs text-muted-foreground mt-1 italic">Disabled — not scanning</p>
        )}
        {def.rule_module && optimisticEnabled && (
          <p className="text-xs text-muted-foreground mt-1 truncate">
            Rule: {def.rule_module.replace('services.scan_rules.', '')}
          </p>
        )}
      </CardHeader>

      <CardContent className="space-y-3">
        {def.latest_signals.length === 0 ? (
          <p className="text-sm text-muted-foreground italic">No signals yet</p>
        ) : (
          <div className="space-y-1.5">
            <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
              Latest signals
            </p>
            {def.latest_signals.map((sig) => (
              <div
                key={sig.id}
                className="flex items-start gap-2 text-xs border rounded-md px-2 py-1.5 bg-muted/30"
              >
                <span className="text-muted-foreground shrink-0 tabular-nums">
                  {fmtTime(sig.run_at, todayStr())}
                </span>
                <span className="truncate">
                  {sig.symbols.length > 0 ? sig.symbols.join(', ') : '—'}
                </span>
                <Badge variant="outline" className="ml-auto shrink-0 text-xs">
                  {sig.symbols.length}
                </Badge>
              </div>
            ))}
          </div>
        )}

        <div className="pt-1">
          <Link to={`/scanner/${def.id}`}>
            <Button variant="outline" size="sm" className="w-full gap-1.5">
              View history
              <ArrowRight className="h-3.5 w-3.5" />
            </Button>
          </Link>
        </div>
      </CardContent>
    </Card>
  )
}

function DefinitionCardSkeleton() {
  return (
    <Card className="border">
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Skeleton className="h-5 w-5 rounded" />
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-5 w-16 ml-auto" />
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        <Skeleton className="h-7 w-full" />
        <Skeleton className="h-7 w-full" />
        <Skeleton className="h-7 w-3/4" />
        <Skeleton className="h-8 w-full mt-1" />
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// "Today's hits by symbol" tab
// ---------------------------------------------------------------------------

function HitsBySymbolTable() {
  const [date, setDate] = useState(todayStr())

  const { data, isLoading, isError } = useQuery({
    queryKey: ['scanner-hits-by-symbol', date],
    queryFn: () => scannerApi.getHitsBySymbol(date),
    refetchInterval: REFRESH_MS,
  })

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <label className="text-sm font-medium" htmlFor="hbs-date">
          Date
        </label>
        <input
          id="hbs-date"
          type="date"
          value={date}
          max={todayStr()}
          onChange={(e) => setDate(e.target.value)}
          className="h-9 rounded-md border border-input bg-background px-3 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        />
        <Button variant="ghost" size="sm" onClick={() => setDate(todayStr())}>
          Today
        </Button>
      </div>

      {isError && <p className="text-sm text-destructive">Failed to load hits. Try again.</p>}

      <div className="rounded-lg border overflow-hidden">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Symbol</TableHead>
              <TableHead className="text-center">Hits</TableHead>
              <TableHead>Definitions</TableHead>
              <TableHead className="text-right">Latest hit</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading &&
              [1, 2, 3].map((i) => (
                <TableRow key={i}>
                  <TableCell>
                    <Skeleton className="h-4 w-24" />
                  </TableCell>
                  <TableCell>
                    <Skeleton className="h-4 w-8 mx-auto" />
                  </TableCell>
                  <TableCell>
                    <Skeleton className="h-4 w-32" />
                  </TableCell>
                  <TableCell>
                    <Skeleton className="h-4 w-24 ml-auto" />
                  </TableCell>
                </TableRow>
              ))}
            {!isLoading && (data?.symbols.length ?? 0) === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="text-center py-10 text-muted-foreground">
                  No hits found for {date}.
                </TableCell>
              </TableRow>
            )}
            {!isLoading &&
              data?.symbols.map((row) => (
                <TableRow key={row.symbol}>
                  <TableCell className="font-mono font-medium">{row.symbol}</TableCell>
                  <TableCell className="text-center tabular-nums">
                    <Badge variant="secondary">{row.hit_count}</Badge>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {row.definitions.join(', ')}
                  </TableCell>
                  <TableCell className="text-right tabular-nums text-xs text-muted-foreground">
                    {fmtTime(row.latest_hit, date)}
                  </TableCell>
                </TableRow>
              ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function ScannerIndex() {
  const { data, isLoading, isError, error, refetch, isFetching, dataUpdatedAt } = useQuery({
    queryKey: ['scanner-definitions'],
    queryFn: () => scannerApi.getDefinitions(),
    refetchInterval: REFRESH_MS,
  })

  const lastUpdated = dataUpdatedAt
    ? new Date(dataUpdatedAt).toLocaleTimeString('en-IN', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      })
    : null

  const enabledCount = data ? data.filter((d) => d.enabled).length : 0
  const totalCount = data ? data.length : 0

  return (
    <div className="p-4 md:p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3">
          <ScanLine className="h-6 w-6 text-primary" />
          <div>
            <h1 className="text-2xl font-semibold">In-House Scanner</h1>
            <p className="text-sm text-muted-foreground">
              Scan definitions · auto-refreshes every 30s
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
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

      {/* Error state */}
      {isError && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          Failed to load definitions: {error instanceof Error ? error.message : 'Unknown error'}
        </div>
      )}

      <Tabs defaultValue="definitions">
        <TabsList>
          <TabsTrigger value="definitions">By Definition</TabsTrigger>
          <TabsTrigger value="by-symbol">By Symbol</TabsTrigger>
        </TabsList>

        {/* By Definition tab */}
        <TabsContent value="definitions" className="mt-4 space-y-4">
          {isLoading && (
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {[1, 2, 3].map((i) => (
                <DefinitionCardSkeleton key={i} />
              ))}
            </div>
          )}

          {!isLoading && !isError && totalCount === 0 && (
            <div className="flex flex-col items-center justify-center py-16 gap-3 text-muted-foreground">
              <ScanLine className="h-12 w-12 opacity-30" />
              <p className="text-sm">No scan definitions found.</p>
            </div>
          )}

          {!isLoading && data && totalCount > 0 && (
            <>
              <p className="text-sm text-muted-foreground">
                {enabledCount} of {totalCount} definition{totalCount !== 1 ? 's' : ''} enabled
              </p>
              <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {data.map((def) => (
                  <DefinitionCard key={def.id} def={def} />
                ))}
              </div>
            </>
          )}
        </TabsContent>

        {/* By Symbol tab */}
        <TabsContent value="by-symbol" className="mt-4">
          <HitsBySymbolTable />
        </TabsContent>
      </Tabs>
    </div>
  )
}
