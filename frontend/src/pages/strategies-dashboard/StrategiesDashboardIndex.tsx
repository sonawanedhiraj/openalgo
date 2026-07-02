import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Bot,
  CheckCircle2,
  Clock,
  Loader2,
  PauseCircle,
  Power,
  RefreshCw,
  ShieldCheck,
  TrendingDown,
  TrendingUp,
} from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  type FlipModeOutcome,
  type LLMHealth,
  type LLMMode,
  type StrategyHealth,
  type StrategySummary,
  strategiesDashboardApi,
} from '@/api/strategies-dashboard'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { showToast } from '@/utils/toast'

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

// LLM control badge (issue #266 Phase 2) — a compact indicator of the current
// per-strategy LLM mode. 'off' renders nothing to keep the card uncluttered.
function LLMBadge({ llmMode }: { llmMode: LLMMode }) {
  if (llmMode === 'veto')
    return (
      <Badge
        className="bg-indigo-100 text-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-300 gap-1"
        title="LLM veto enabled"
      >
        <ShieldCheck className="h-3 w-3" /> Veto
      </Badge>
    )
  if (llmMode === 'delegate')
    return (
      <Badge
        className="bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300 gap-1"
        title="LLM delegate"
      >
        <Bot className="h-3 w-3" /> Delegate
      </Badge>
    )
  return null
}

// ---------------------------------------------------------------------------
// LLM health chip (issue #297)
// ---------------------------------------------------------------------------
//
// A single, install-global liveness indicator for the shared `claude` CLI that
// every strategy's Stage-1 veto calls. Because the probe spawns a real
// `claude -p` subprocess server-side (seconds, consumes tokens), this is
// MANUAL-ONLY: the query is `enabled: false` with no refetchInterval, so it
// runs solely when the operator clicks the chip's own refresh icon.

function llmUnreachableHint(reason: LLMHealth['reason']): string {
  switch (reason) {
    case 'not_logged_in':
      return 'run claude login'
    case 'cli_missing':
      return 'claude CLI not found'
    case 'timeout':
      return 'timed out'
    default:
      return 'error'
  }
}

function LLMHealthChip() {
  const { data, isFetching, isError, refetch, dataUpdatedAt } = useQuery({
    queryKey: ['llm-health'],
    queryFn: () => strategiesDashboardApi.getLLMHealth(),
    enabled: false, // manual-only — the probe spawns a claude subprocess
    refetchInterval: false,
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: Number.POSITIVE_INFINITY,
    retry: false,
  })

  const checkedAt =
    dataUpdatedAt > 0
      ? new Date(dataUpdatedAt).toLocaleTimeString('en-IN', {
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
          hour12: false,
        })
      : null

  let tone = 'text-muted-foreground bg-muted/60'
  let icon = <Bot className="h-3.5 w-3.5" />
  let label = 'LLM: not checked'

  if (isFetching) {
    icon = <Loader2 className="h-3.5 w-3.5 animate-spin" />
    label = 'Checking LLM…'
  } else if (isError) {
    tone = 'text-red-700 dark:text-red-300 bg-red-50 dark:bg-red-900/20'
    icon = <AlertTriangle className="h-3.5 w-3.5" />
    label = 'LLM check failed'
  } else if (data) {
    if (data.reachable) {
      tone = 'text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-900/20'
      icon = <CheckCircle2 className="h-3.5 w-3.5" />
      label = `LLM reachable · ${data.latency_ms}ms`
    } else {
      tone = 'text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-900/20'
      icon = <AlertTriangle className="h-3.5 w-3.5" />
      label = `LLM unreachable — ${llmUnreachableHint(data.reason)}`
    }
  }

  const title = data
    ? `${data.reason}${data.detail ? ` — ${data.detail}` : ''}${checkedAt ? ` (checked ${checkedAt})` : ''}`
    : 'Click the refresh icon to probe the LLM (spawns a claude subprocess — not auto-polled)'

  return (
    <div className={`flex items-center gap-1.5 rounded-md px-2 py-1 text-xs ${tone}`} title={title}>
      {icon}
      <span className="whitespace-nowrap">{label}</span>
      <button
        type="button"
        onClick={() => refetch()}
        disabled={isFetching}
        title="Check LLM reachability now"
        className="ml-0.5 rounded p-0.5 hover:bg-black/10 dark:hover:bg-white/10 disabled:opacity-50"
      >
        <RefreshCw className={`h-3 w-3 ${isFetching ? 'animate-spin' : ''}`} />
      </button>
    </div>
  )
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
// Mode toggle button (issue #162)
// ---------------------------------------------------------------------------
//
// One-click sandbox<->live flip routed through POST /strategies/api/<name>/mode.
// The server runs the preflight; on a 409 ("blocked"), the response carries a
// `blockers` list which we display to the operator so they know exactly what
// to fix before retrying — not silent.

function ModeToggleButton({ s }: { s: StrategySummary }) {
  const queryClient = useQueryClient()
  const [blockers, setBlockers] = useState<string[] | null>(null)

  // Scaffold-only strategies have no LIVE path — disable the toggle entirely.
  const isScaffold = !s.deployable || s.mode.includes('scaffold')
  const targetMode: 'live' | 'sandbox' = s.mode === 'live' ? 'sandbox' : 'live'

  const flip = useMutation({
    mutationFn: () => strategiesDashboardApi.flipMode(s.name, targetMode),
    onSuccess: (outcome: FlipModeOutcome) => {
      if (outcome.accepted) {
        setBlockers(null)
        showToast.success(`${s.display_name} → ${outcome.new_mode?.toUpperCase()}`, 'strategy')
        queryClient.invalidateQueries({ queryKey: ['strategies-list'] })
      } else {
        // Preflight refused — surface the blockers list to the operator.
        setBlockers(outcome.blockers)
        showToast.error(
          `Cannot enable ${targetMode.toUpperCase()} (${outcome.blockers.length} blocker${outcome.blockers.length === 1 ? '' : 's'})`,
          'strategy'
        )
      }
    },
    onError: () => {
      showToast.error('Mode flip request failed — check server logs', 'strategy')
    },
  })

  const handleClick = () => {
    if (isScaffold) return
    // Light confirm for the LIVE direction only; sandbox is always safe.
    if (targetMode === 'live') {
      const ok = window.confirm(
        `Enable LIVE mode for ${s.display_name}?\n\n` +
          'The server will run a preflight check first. If any condition is not met ' +
          '(broker session, data freshness, orphan trades, etc.) the flip will be ' +
          'refused with a clear blocker message.'
      )
      if (!ok) return
    }
    setBlockers(null)
    flip.mutate()
  }

  if (isScaffold) {
    return (
      <Button
        variant="ghost"
        size="sm"
        className="w-full text-xs text-muted-foreground"
        disabled
        title="Scaffold strategy — no LIVE path"
      >
        <Power className="h-3 w-3 mr-1.5" />
        Scaffold only
      </Button>
    )
  }

  return (
    <div className="space-y-1.5">
      <Button
        variant={targetMode === 'live' ? 'default' : 'outline'}
        size="sm"
        className="w-full text-xs"
        onClick={handleClick}
        disabled={flip.isPending}
        title={
          targetMode === 'live'
            ? 'Enable LIVE mode (preflight will refuse if not ready)'
            : 'Switch back to SANDBOX'
        }
      >
        {flip.isPending ? (
          <Loader2 className="h-3 w-3 mr-1.5 animate-spin" />
        ) : (
          <Power className="h-3 w-3 mr-1.5" />
        )}
        {flip.isPending ? 'Flipping…' : `Switch to ${targetMode.toUpperCase()}`}
      </Button>
      {blockers && blockers.length > 0 && (
        <div className="rounded-md bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 p-2 space-y-1">
          <p className="text-xs font-semibold text-red-700 dark:text-red-300 flex items-center gap-1">
            <AlertTriangle className="h-3 w-3" /> Cannot enable {targetMode.toUpperCase()}
          </p>
          <ul className="text-xs text-red-700 dark:text-red-300 space-y-0.5 ml-4 list-disc">
            {blockers.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
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
          <div className="flex items-center gap-1.5">
            <LLMBadge llmMode={s.llm_mode} />
            <ModeBadge mode={s.mode} deployable={s.deployable} />
          </div>
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

        {/* Mode flip toggle (issue #162) — gated by server-side preflight.
            On block, the blockers list is rendered below the button so the
            operator sees exactly why the flip was refused. */}
        <ModeToggleButton s={s} />

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
        <div className="flex items-center gap-2 flex-wrap">
          <LLMHealthChip />
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
