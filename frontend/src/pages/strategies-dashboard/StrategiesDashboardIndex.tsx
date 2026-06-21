import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Clock,
  PauseCircle,
  RefreshCw,
  TrendingDown,
  TrendingUp,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import {
  type StrategyHealth,
  type StrategySummary,
  strategiesDashboardApi,
} from '@/api/strategies-dashboard'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'

const REFRESH_MS = 30_000

// ---------------------------------------------------------------------------
// Health LED
// ---------------------------------------------------------------------------

function HealthLed({ health }: { health: StrategyHealth }) {
  if (health === 'healthy')
    return (
      <span title="Healthy">
        <CheckCircle2 className="h-4 w-4 text-green-500" />
      </span>
    )
  if (health === 'paused')
    return (
      <span title="Paused / Override active">
        <PauseCircle className="h-4 w-4 text-yellow-500" />
      </span>
    )
  if (health === 'scaffold')
    return (
      <span title="Scaffold — not trading">
        <Clock className="h-4 w-4 text-muted-foreground" />
      </span>
    )
  return (
    <span title="Unknown">
      <AlertTriangle className="h-4 w-4 text-orange-400" />
    </span>
  )
}

// ---------------------------------------------------------------------------
// Mode badge
// ---------------------------------------------------------------------------

function ModeBadge({ mode, deployable }: { mode: string; deployable: boolean }) {
  if (!deployable || mode.includes('scaffold'))
    return (
      <Badge variant="outline" className="text-muted-foreground">
        Scaffold
      </Badge>
    )
  if (mode === 'live')
    return <Badge className="bg-green-600 text-white hover:bg-green-700">Live</Badge>
  if (mode === 'sandbox')
    return (
      <Badge
        variant="secondary"
        className="text-amber-700 bg-amber-100 dark:text-amber-300 dark:bg-amber-900/30"
      >
        Sandbox
      </Badge>
    )
  return <Badge variant="outline">{mode}</Badge>
}

// ---------------------------------------------------------------------------
// P&L display
// ---------------------------------------------------------------------------

function PnlDisplay({ pnl }: { pnl: number | null | undefined }) {
  if (pnl == null) return <span className="text-muted-foreground text-sm">—</span>
  const isPos = pnl >= 0
  return (
    <span
      className={`font-mono text-sm font-medium ${isPos ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}
    >
      {isPos ? '+' : ''}
      {pnl.toLocaleString('en-IN', {
        style: 'currency',
        currency: 'INR',
        maximumFractionDigits: 0,
      })}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Strategy card
// ---------------------------------------------------------------------------

function StrategyCard({ s }: { s: StrategySummary }) {
  const hasOverride = s.active_overrides.length > 0

  return (
    <Card className="border hover:shadow-md transition-shadow">
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <HealthLed health={s.health} />
            <CardTitle className="text-base truncate">{s.display_name}</CardTitle>
          </div>
          <ModeBadge mode={s.mode} deployable={s.deployable} />
        </div>
        <p className="text-xs text-muted-foreground font-mono">
          {s.name} · v{s.version}
        </p>
      </CardHeader>

      <CardContent className="space-y-3">
        {/* Stats row */}
        <div className="grid grid-cols-3 gap-2 text-center">
          <div className="rounded-md bg-muted/40 py-2 px-1">
            <p className="text-xs text-muted-foreground">Open</p>
            <p className="text-lg font-semibold tabular-nums">{s.open_positions}</p>
          </div>
          <div className="rounded-md bg-muted/40 py-2 px-1">
            <p className="text-xs text-muted-foreground">Trades</p>
            <p className="text-lg font-semibold tabular-nums">{s.today_trade_count}</p>
          </div>
          <div className="rounded-md bg-muted/40 py-2 px-1">
            <p className="text-xs text-muted-foreground">Today P&L</p>
            <div className="flex items-center justify-center">
              {s.today_net_pnl != null ? (
                s.today_net_pnl >= 0 ? (
                  <TrendingUp className="h-3 w-3 text-green-500 mr-0.5" />
                ) : (
                  <TrendingDown className="h-3 w-3 text-red-500 mr-0.5" />
                )
              ) : null}
              <PnlDisplay pnl={s.today_net_pnl} />
            </div>
          </div>
        </div>

        {/* Active overrides warning */}
        {hasOverride && (
          <div className="flex items-center gap-1.5 text-xs text-yellow-700 dark:text-yellow-400 bg-yellow-50 dark:bg-yellow-900/20 rounded-md px-2 py-1.5">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            <span>{s.active_overrides.map((o) => o.type.replace('_', ' ')).join(', ')} active</span>
          </div>
        )}

        {/* Last trade */}
        {s.last_trade_at && (
          <p className="text-xs text-muted-foreground truncate">
            Last trade:{' '}
            {new Date(s.last_trade_at + 'Z').toLocaleString('en-IN', {
              dateStyle: 'short',
              timeStyle: 'short',
            })}
          </p>
        )}

        <Link to={`/strategies/${s.name}`}>
          <Button variant="outline" size="sm" className="w-full gap-1.5">
            View details
            <ArrowRight className="h-3.5 w-3.5" />
          </Button>
        </Link>
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Skeleton
// ---------------------------------------------------------------------------

function CardSkeleton() {
  return (
    <Card className="border">
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Skeleton className="h-4 w-4 rounded-full" />
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-5 w-16 ml-auto" />
        </div>
        <Skeleton className="h-3 w-32 mt-1" />
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-3 gap-2">
          <Skeleton className="h-12 rounded-md" />
          <Skeleton className="h-12 rounded-md" />
          <Skeleton className="h-12 rounded-md" />
        </div>
        <Skeleton className="h-8 w-full" />
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function StrategiesDashboardIndex() {
  const { data, isLoading, isError, error, refetch, isFetching, dataUpdatedAt } = useQuery({
    queryKey: ['strategies-list'],
    queryFn: () => strategiesDashboardApi.listStrategies(),
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

  return (
    <div className="p-4 md:p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3">
          <Activity className="h-6 w-6 text-primary" />
          <div>
            <h1 className="text-2xl font-semibold">Strategies</h1>
            <p className="text-sm text-muted-foreground">
              Live strategy status · read-only · auto-refreshes every 30s
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

      {/* Error */}
      {isError && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          Failed to load strategies: {error instanceof Error ? error.message : 'Unknown error'}
        </div>
      )}

      {/* Loading */}
      {isLoading && (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {[1, 2, 3, 4].map((i) => (
            <CardSkeleton key={i} />
          ))}
        </div>
      )}

      {/* Empty */}
      {!isLoading && !isError && data?.length === 0 && (
        <div className="flex flex-col items-center justify-center py-16 gap-3 text-muted-foreground">
          <Activity className="h-12 w-12 opacity-30" />
          <p className="text-sm">No strategies found under strategies/ directory.</p>
        </div>
      )}

      {/* Cards */}
      {!isLoading && data && data.length > 0 && (
        <>
          <p className="text-sm text-muted-foreground">
            {data.length} strateg{data.length !== 1 ? 'ies' : 'y'}
          </p>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {data.map((s) => (
              <StrategyCard key={s.name} s={s} />
            ))}
          </div>
        </>
      )}
    </div>
  )
}
