import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock,
  FileBarChart2,
  GitCompare,
  History,
  PauseCircle,
  RefreshCw,
  TrendingDown,
  TrendingUp,
} from 'lucide-react'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  type PnlWindow,
  type RecentTrade,
  type StrategyDetail,
  strategiesDashboardApi,
  type VersionLogEntry,
} from '@/api/strategies-dashboard'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmt(v: number | null | undefined, suffix = ''): string {
  if (v == null) return '—'
  return `${v}${suffix}`
}

function fmtPnl(v: number | null | undefined) {
  if (v == null) return '—'
  const sym = v >= 0 ? '+' : ''
  return (
    sym +
    v.toLocaleString('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 })
  )
}

function fmtDate(iso: string | null | undefined) {
  if (!iso) return '—'
  try {
    return new Date(iso + (iso.endsWith('Z') ? '' : 'Z')).toLocaleString('en-IN', {
      dateStyle: 'short',
      timeStyle: 'short',
    })
  } catch {
    return iso
  }
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function HealthBadge({ health }: { health: string }) {
  if (health === 'healthy')
    return (
      <Badge className="bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400 gap-1">
        <CheckCircle2 className="h-3 w-3" /> Healthy
      </Badge>
    )
  if (health === 'paused')
    return (
      <Badge className="bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-400 gap-1">
        <PauseCircle className="h-3 w-3" /> Paused
      </Badge>
    )
  if (health === 'scaffold')
    return (
      <Badge variant="outline" className="gap-1 text-muted-foreground">
        <Clock className="h-3 w-3" /> Scaffold
      </Badge>
    )
  return <Badge variant="outline">Unknown</Badge>
}

function ModeBadge({ mode, deployable }: { mode: string; deployable: boolean }) {
  if (!deployable || mode.includes('scaffold'))
    return (
      <Badge variant="outline" className="text-muted-foreground">
        Scaffold-only
      </Badge>
    )
  if (mode === 'live')
    return <Badge className="bg-green-600 text-white hover:bg-green-700">Live</Badge>
  return (
    <Badge
      variant="secondary"
      className="text-amber-700 bg-amber-100 dark:text-amber-300 dark:bg-amber-900/30"
    >
      Sandbox
    </Badge>
  )
}

// ---------------------------------------------------------------------------
// Performance comparison table
// ---------------------------------------------------------------------------

function PerfTable({ data }: { data: StrategyDetail }) {
  const bt = data.performance.backtest
  const sb = data.performance.sandbox
  const lv = data.performance.live

  const rows = [
    { label: 'CAGR', bt: fmt(bt.cagr_pct, '%'), sb: '—', lv: '—' },
    { label: 'Sharpe', bt: fmt(bt.sharpe), sb: '—', lv: '—' },
    { label: 'Max DD', bt: fmt(bt.max_dd_pct, '%'), sb: '—', lv: '—' },
    { label: 'Win Rate', bt: fmt(bt.win_rate_pct, '%'), sb: '—', lv: '—' },
    { label: 'N Trades', bt: fmt(bt.n_trades), sb: '—', lv: '—' },
    {
      label: 'Open Pos',
      bt: '—',
      sb: sb?.open_positions != null ? String(sb.open_positions) : '—',
      lv: lv?.open_positions != null ? String(lv.open_positions) : '—',
    },
    {
      label: 'Today P&L',
      bt: '—',
      sb: fmtPnl(sb?.today_net_pnl),
      lv: fmtPnl(lv?.today_net_pnl),
    },
  ]

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm flex items-center gap-2">
          <FileBarChart2 className="h-4 w-4" /> Performance Comparison
        </CardTitle>
        {bt.window && <p className="text-xs text-muted-foreground">Backtest window: {bt.window}</p>}
      </CardHeader>
      <CardContent className="p-0">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/30">
                <th className="text-left px-4 py-2 font-medium text-muted-foreground w-28">
                  Metric
                </th>
                <th className="text-right px-4 py-2 font-medium">Backtest</th>
                <th className="text-right px-4 py-2 font-medium">Sandbox</th>
                <th className="text-right px-4 py-2 font-medium">Live</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.label} className="border-b last:border-0 hover:bg-muted/20">
                  <td className="px-4 py-2 text-muted-foreground">{r.label}</td>
                  <td className="px-4 py-2 text-right tabular-nums font-mono">{r.bt}</td>
                  <td className="px-4 py-2 text-right tabular-nums font-mono">{r.sb}</td>
                  <td className="px-4 py-2 text-right tabular-nums font-mono">{r.lv}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// P&L curve
// ---------------------------------------------------------------------------

const WINDOWS: { label: string; value: PnlWindow }[] = [
  { label: '1D', value: '1d' },
  { label: '1W', value: '1w' },
  { label: '1M', value: '1m' },
  { label: 'All', value: 'all' },
]

function PnlCurve({ name }: { name: string }) {
  const [window, setWindow] = useState<PnlWindow>('all')

  const { data, isLoading } = useQuery({
    queryKey: ['strategy-pnl-curve', name, window],
    queryFn: () => strategiesDashboardApi.getPnlCurve(name, window),
  })

  const points = data?.points ?? []
  const cumulative = points.reduce(
    (acc, p, i) => {
      const prev = i > 0 ? acc[i - 1].cum : 0
      acc.push({ date: p.date, pnl: p.pnl, cum: prev + p.pnl })
      return acc
    },
    [] as { date: string; pnl: number; cum: number }[]
  )

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-sm flex items-center gap-2">
            <TrendingUp className="h-4 w-4" /> P&L Curve
          </CardTitle>
          <div className="flex items-center gap-1">
            {WINDOWS.map((w) => (
              <Button
                key={w.value}
                variant={window === w.value ? 'secondary' : 'ghost'}
                size="sm"
                className="h-6 px-2 text-xs"
                onClick={() => setWindow(w.value)}
              >
                {w.label}
              </Button>
            ))}
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : cumulative.length === 0 ? (
          <div className="h-40 flex items-center justify-center text-sm text-muted-foreground">
            No trade data yet for this strategy
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={180}>
            <AreaChart data={cumulative} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id="pnlGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="hsl(var(--primary))" stopOpacity={0.25} />
                  <stop offset="95%" stopColor="hsl(var(--primary))" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" className="stroke-muted/30" />
              <XAxis
                dataKey="date"
                tick={{ fontSize: 10 }}
                tickLine={false}
                axisLine={false}
                className="fill-muted-foreground"
              />
              <YAxis
                tick={{ fontSize: 10 }}
                tickLine={false}
                axisLine={false}
                className="fill-muted-foreground"
                tickFormatter={(v: number) => `₹${(v / 1000).toFixed(1)}k`}
              />
              <Tooltip
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                formatter={(value: any) => {
                  const n = typeof value === 'number' ? value : 0
                  return [
                    n.toLocaleString('en-IN', {
                      style: 'currency',
                      currency: 'INR',
                      maximumFractionDigits: 0,
                    }),
                    'Cumulative P&L',
                  ]
                }}
                labelClassName="text-xs"
                contentStyle={{ fontSize: '12px' }}
              />
              <Area
                type="monotone"
                dataKey="cum"
                stroke="hsl(var(--primary))"
                fill="url(#pnlGrad)"
                strokeWidth={2}
                dot={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Recent trades table
// ---------------------------------------------------------------------------

function RecentTradesTable({ trades }: { trades: RecentTrade[] }) {
  const [sortAsc, setSortAsc] = useState(false)

  const sorted = [...trades].sort((a, b) => {
    const ta = a.created_at ?? ''
    const tb = b.created_at ?? ''
    return sortAsc ? ta.localeCompare(tb) : tb.localeCompare(ta)
  })

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm flex items-center gap-2">
          <History className="h-4 w-4" /> Recent Trades
          <span className="ml-auto text-xs text-muted-foreground font-normal">
            last {trades.length}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        {trades.length === 0 ? (
          <p className="text-sm text-muted-foreground px-4 py-6 text-center italic">
            No trades yet
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b bg-muted/30">
                  <th className="text-left px-3 py-2 font-medium text-muted-foreground">Side</th>
                  <th className="text-left px-3 py-2 font-medium text-muted-foreground">Symbol</th>
                  <th className="text-right px-3 py-2 font-medium text-muted-foreground">Qty</th>
                  <th className="text-right px-3 py-2 font-medium text-muted-foreground">
                    Net P&L
                  </th>
                  <th className="text-left px-3 py-2 font-medium text-muted-foreground">Mode</th>
                  <th className="text-left px-3 py-2 font-medium text-muted-foreground">Status</th>
                  <th
                    className="text-right px-3 py-2 font-medium text-muted-foreground cursor-pointer select-none"
                    onClick={() => setSortAsc(!sortAsc)}
                  >
                    <span className="flex items-center justify-end gap-0.5">
                      Time{' '}
                      {sortAsc ? (
                        <ChevronUp className="h-3 w-3" />
                      ) : (
                        <ChevronDown className="h-3 w-3" />
                      )}
                    </span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((t) => {
                  const isBuy = t.side === 'BUY'
                  const netPnl = t.net_pnl
                  return (
                    <tr key={t.id} className="border-b last:border-0 hover:bg-muted/20">
                      <td className="px-3 py-1.5">
                        <span
                          className={`font-medium ${isBuy ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}
                        >
                          {t.side}
                        </span>
                      </td>
                      <td className="px-3 py-1.5 font-mono">{t.symbol}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{t.quantity}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums font-mono">
                        {netPnl != null ? (
                          <span
                            className={
                              netPnl >= 0
                                ? 'text-green-600 dark:text-green-400'
                                : 'text-red-600 dark:text-red-400'
                            }
                          >
                            {fmtPnl(netPnl)}
                          </span>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="px-3 py-1.5">
                        <Badge variant="outline" className="text-xs py-0">
                          {t.mode}
                        </Badge>
                      </td>
                      <td className="px-3 py-1.5">
                        <Badge
                          variant={t.status === 'placed' ? 'default' : 'destructive'}
                          className="text-xs py-0"
                        >
                          {t.status}
                        </Badge>
                      </td>
                      <td className="px-3 py-1.5 text-right text-muted-foreground whitespace-nowrap">
                        {fmtDate(t.created_at)}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Parameter snapshot
// ---------------------------------------------------------------------------

function ParamSnapshot({
  name,
  currentVersion,
  config,
}: {
  name: string
  currentVersion: string
  config: Record<string, unknown>
}) {
  const { data } = useQuery({
    queryKey: ['strategy-param-diff', name, 'prev'],
    queryFn: () => strategiesDashboardApi.getParametersDiff(name),
    staleTime: 60_000,
  })

  const changedKeys = new Set((data?.changed_keys ?? []).map((c) => c.key))

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm flex items-center gap-2">
          <GitCompare className="h-4 w-4" /> Parameters · {currentVersion}
        </CardTitle>
        {changedKeys.size > 0 && (
          <p className="text-xs text-muted-foreground">
            {changedKeys.size} key{changedKeys.size !== 1 ? 's' : ''} changed vs previous version
          </p>
        )}
      </CardHeader>
      <CardContent className="p-0">
        <div className="overflow-x-auto">
          <table className="w-full text-xs font-mono">
            <tbody>
              {Object.entries(config)
                .filter(([k]) => k !== 'parity_target' && k !== 'universe')
                .map(([k, v]) => (
                  <tr
                    key={k}
                    className={`border-b last:border-0 hover:bg-muted/20 ${changedKeys.has(k) ? 'bg-yellow-50 dark:bg-yellow-900/10' : ''}`}
                  >
                    <td className="px-4 py-1.5 text-muted-foreground w-1/2">{k}</td>
                    <td className="px-4 py-1.5 break-all">
                      {changedKeys.has(k) && (
                        <span className="inline-block w-2 h-2 rounded-full bg-yellow-400 mr-1.5 align-middle" />
                      )}
                      {JSON.stringify(v)}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Version log
// ---------------------------------------------------------------------------

function VersionLog({ entries }: { entries: VersionLogEntry[] }) {
  const [expanded, setExpanded] = useState<string | null>(entries[0]?.version ?? null)

  if (entries.length === 0) return null

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm flex items-center gap-2">
          <BookOpen className="h-4 w-4" /> Version Log
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 p-4 pt-0">
        {entries.map((e) => (
          <div key={e.version} className="border rounded-md">
            <button
              className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-muted/30 rounded-md"
              onClick={() => setExpanded(expanded === e.version ? null : e.version)}
            >
              <div className="flex items-center gap-2">
                <Badge variant="outline" className="font-mono text-xs">
                  {e.version}
                </Badge>
                <span className="text-xs text-muted-foreground">{e.date}</span>
              </div>
              {expanded === e.version ? (
                <ChevronUp className="h-3.5 w-3.5 text-muted-foreground" />
              ) : (
                <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
              )}
            </button>
            {expanded === e.version && (
              <div className="px-3 pb-3 pt-1">
                <pre className="text-xs text-muted-foreground whitespace-pre-wrap leading-relaxed">
                  {e.body}
                </pre>
              </div>
            )}
          </div>
        ))}
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Overrides banner
// ---------------------------------------------------------------------------

function OverridesBanner({ overrides }: { overrides: StrategyDetail['active_overrides'] }) {
  if (overrides.length === 0) return null
  return (
    <div className="flex flex-wrap gap-2">
      {overrides.map((o, i) => (
        <div
          key={i}
          className="flex items-center gap-2 text-sm text-yellow-800 dark:text-yellow-300 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-700 rounded-md px-3 py-2"
        >
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>
            <strong>{o.type.replace('_', ' ')}</strong>
            {o.reason ? ` — ${o.reason}` : ''}
            {o.expires_at ? ` · expires ${fmtDate(o.expires_at)}` : ''}
          </span>
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

function StrategyDetailSkeleton() {
  return (
    <div className="p-4 md:p-6 space-y-6">
      <Skeleton className="h-8 w-64" />
      <Skeleton className="h-6 w-48" />
      <div className="grid gap-4 md:grid-cols-2">
        <Skeleton className="h-56" />
        <Skeleton className="h-56" />
      </div>
      <Skeleton className="h-48" />
      <Skeleton className="h-64" />
    </div>
  )
}

export default function StrategyDetailPage() {
  const { name } = useParams<{ name: string }>()

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ['strategy-detail', name],
    queryFn: () => strategiesDashboardApi.getStrategy(name!),
    enabled: !!name,
    refetchInterval: 30_000,
  })

  if (isLoading) return <StrategyDetailSkeleton />

  if (isError || !data) {
    return (
      <div className="p-4 md:p-6">
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {isError
            ? `Failed to load strategy: ${error instanceof Error ? error.message : 'Unknown error'}`
            : 'Strategy not found'}
        </div>
      </div>
    )
  }

  return (
    <div className="p-4 md:p-6 space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div className="space-y-1">
          <div className="flex items-center gap-2 flex-wrap">
            <Link
              to="/strategies"
              className="text-muted-foreground hover:text-foreground transition-colors"
            >
              <ArrowLeft className="h-4 w-4" />
            </Link>
            <Activity className="h-5 w-5 text-primary" />
            <h1 className="text-2xl font-semibold">{data.display_name}</h1>
            <HealthBadge health={data.health} />
            <ModeBadge mode={data.mode} deployable={data.deployable} />
          </div>
          <p className="text-sm text-muted-foreground font-mono pl-7">
            {data.name} · v{data.version}
          </p>
        </div>
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

      {/* Active overrides */}
      <OverridesBanner overrides={data.active_overrides} />

      {/* Performance + P&L curve */}
      <div className="grid gap-4 xl:grid-cols-2">
        <PerfTable data={data} />
        <PnlCurve name={data.name} />
      </div>

      {/* Recent trades */}
      <RecentTradesTable trades={data.recent_trades} />

      {/* Params + Version log */}
      <div className="grid gap-4 xl:grid-cols-2">
        <ParamSnapshot
          name={data.name}
          currentVersion={data.version}
          config={data.config_snapshot}
        />
        <VersionLog entries={data.version_log} />
      </div>

      {/* Backtest references */}
      {data.backtest_refs.length > 0 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm flex items-center gap-2">
              <TrendingDown className="h-4 w-4" /> Backtest Reports
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="space-y-1">
              {data.backtest_refs.map((ref) => (
                <li key={ref} className="text-sm text-muted-foreground font-mono truncate">
                  {ref}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
