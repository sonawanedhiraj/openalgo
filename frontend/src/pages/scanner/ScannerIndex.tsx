import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, Copy, RefreshCw, ScanLine, TrendingDown, TrendingUp } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { type ScanDefinitionSummary, scannerApi } from '@/api/scanner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
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
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { ParamForm } from './ParamForm'

const REFRESH_MS = 30_000
const WS_HEALTH_POLL_MS = 15_000

type WsStatus = 'healthy' | 'degraded' | 'down'

interface WsProxyHealth {
  status: WsStatus
  last_tick_age_sec: number | null
  thread_count: number
  subscribed_symbols: number | null
}

async function fetchWsHealth(): Promise<WsProxyHealth> {
  const res = await fetch('/health/ws_proxy')
  if (!res.ok) throw new Error(`ws_proxy health ${res.status}`)
  return res.json()
}

const LED_CLS: Record<WsStatus, string> = {
  healthy: 'bg-green-500',
  degraded: 'bg-amber-400',
  down: 'bg-red-500',
}

const LED_LBL: Record<WsStatus, string> = {
  healthy: 'WS healthy',
  degraded: 'WS degraded',
  down: 'WS down',
}

function WsHealthLed() {
  const { data, isError } = useQuery<WsProxyHealth>({
    queryKey: ['ws-proxy-health'],
    queryFn: fetchWsHealth,
    refetchInterval: WS_HEALTH_POLL_MS,
    retry: 1,
  })

  const status: WsStatus = isError ? 'down' : (data?.status ?? 'down')
  const dotCls = LED_CLS[status]

  const tipLines: string[] = [`Status: ${status}`]
  if (data) {
    if (data.last_tick_age_sec !== null) tipLines.push(`Last tick: ${data.last_tick_age_sec}s ago`)
    else tipLines.push('Last tick: unknown')
    if (data.subscribed_symbols !== null)
      tipLines.push(`Subscribed: ${data.subscribed_symbols} symbols`)
    tipLines.push(`Threads: ${data.thread_count}`)
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={LED_LBL[status]}
          className="flex items-center gap-1.5 cursor-default focus:outline-none"
        >
          <span
            className={`inline-block h-2.5 w-2.5 rounded-full ${dotCls} ${status === 'healthy' ? 'animate-pulse' : ''}`}
          />
          <span className="text-xs text-muted-foreground">{LED_LBL[status]}</span>
        </button>
      </TooltipTrigger>
      <TooltipContent side="bottom">
        <div className="space-y-0.5">
          {tipLines.map((l) => (
            <p key={l}>{l}</p>
          ))}
        </div>
      </TooltipContent>
    </Tooltip>
  )
}

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
// Clone dialog
// ---------------------------------------------------------------------------

interface CloneDialogProps {
  open: boolean
  onOpenChange: (v: boolean) => void
  source: ScanDefinitionSummary
}

function CloneDialog({ open, onOpenChange, source }: CloneDialogProps) {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const [params, setParams] = useState<Record<string, number>>({})
  const [error, setError] = useState<string | null>(null)

  // reset state when dialog opens
  useEffect(() => {
    if (open) {
      setName(`${source.name}_custom`)
      setParams({})
      setError(null)
    }
  }, [open, source.name])

  const cloneMutation = useMutation({
    mutationFn: () =>
      scannerApi.cloneDefinition(source.id, {
        name,
        parameters_json: Object.keys(params).length > 0 ? params : null,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['scanner-definitions'] })
      onOpenChange(false)
    },
    onError: (err: unknown) => {
      const msg =
        err instanceof Error
          ? err.message
          : ((err as { response?: { data?: { message?: string } } })?.response?.data?.message ??
            'Clone failed')
      setError(msg)
    },
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Clone &quot;{source.name}&quot;</DialogTitle>
        </DialogHeader>
        <div className="space-y-4 py-2">
          <div className="space-y-1.5">
            <Label htmlFor="clone-name">New name</Label>
            <Input
              id="clone-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. fno_intraday_buy_tight_gap"
              disabled={cloneMutation.isPending}
            />
          </div>
          <div className="space-y-2">
            <p className="text-sm font-medium">Parameter overrides (leave at default to inherit)</p>
            <ParamForm
              screenerType={source.screener_type}
              value={params}
              onChange={setParams}
              disabled={cloneMutation.isPending}
            />
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={cloneMutation.isPending}
          >
            Cancel
          </Button>
          <Button
            onClick={() => cloneMutation.mutate()}
            disabled={cloneMutation.isPending || !name.trim()}
          >
            {cloneMutation.isPending ? 'Cloning…' : 'Clone'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ---------------------------------------------------------------------------
// Definition card with toggle
// ---------------------------------------------------------------------------

function DefinitionCard({ def }: { def: ScanDefinitionSummary }) {
  const queryClient = useQueryClient()
  const [optimisticEnabled, setOptimisticEnabled] = useState(def.enabled)
  const [cloneOpen, setCloneOpen] = useState(false)

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
    <>
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
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    onClick={() => setCloneOpen(true)}
                    aria-label={`Clone ${def.name}`}
                  >
                    <Copy className="h-3.5 w-3.5" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="top">Clone definition</TooltipContent>
              </Tooltip>
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
      <CloneDialog open={cloneOpen} onOpenChange={setCloneOpen} source={def} />
    </>
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
          <WsHealthLed />
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
