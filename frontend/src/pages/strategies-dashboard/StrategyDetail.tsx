import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  BookOpen,
  Bot,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Clock,
  FileBarChart2,
  GitCompare,
  History,
  Info,
  ListChecks,
  Loader2,
  PauseCircle,
  RefreshCw,
  ShieldCheck,
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
  type DataHealth,
  type EntryBreakdownOutcome,
  type EntryBreakdownPayload,
  type EntryBreakdownSummary,
  type EntryBreakdownSymbol,
  type LLMFlipOutcome,
  type LLMMode,
  type PnlWindow,
  type RecentTrade,
  type StrategyDetail,
  strategiesDashboardApi,
  type TradeLLMReview,
  type UnmatchedSkipDecision,
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

// Plain unsigned INR amount (for costs like charges and capital deployed), no
// leading +/-. `null` renders as an em-dash.
function fmtInr(v: number | null | undefined) {
  if (v == null) return '—'
  return v.toLocaleString('en-IN', {
    style: 'currency',
    currency: 'INR',
    maximumFractionDigits: 0,
  })
}

// Instrument price with 2 decimals (NIFTY futures trade in 0.05 ticks), ₹ prefix.
// `null` renders as an em-dash.
function fmtPrice(v: number | null | undefined) {
  if (v == null) return '—'
  return v.toLocaleString('en-IN', {
    style: 'currency',
    currency: 'INR',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

// Matches a trailing timezone designator: `Z`, or a numeric offset like
// `+05:30` / `-0800`. Timestamps WITH an offset (e.g. signal_decision.candidate_at
// = `datetime.now(Asia/Kolkata).isoformat()`) must be parsed as-is; only naive
// timestamps (assumed UTC) get a `Z` appended.
const TZ_SUFFIX = /(?:Z|[+-]\d{2}:?\d{2})$/

function fmtDate(iso: string | null | undefined) {
  if (!iso) return '—'
  const d = new Date(TZ_SUFFIX.test(iso) ? iso : iso + 'Z')
  // Invalid dates don't throw here — toLocaleString() would render the literal
  // string "Invalid Date". Guard explicitly and fall back to the raw value.
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('en-IN', { dateStyle: 'short', timeStyle: 'short' })
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

// Data-freshness tile (issue #237): surfaces the latest data_health_check state
// for the strategy's feed so "no signals" (quiet market) is distinguishable from
// "feed stale". Renders nothing for strategies without a feed check (e.g. the
// webhook-driven simplified engine).
function DataHealthBadge({ dataHealth }: { dataHealth: DataHealth }) {
  if (!dataHealth?.available) return null
  const at = dataHealth.check_at
    ? new Date(dataHealth.check_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : '—'
  if (dataHealth.overall_ok) {
    return (
      <Badge
        className="bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400 gap-1"
        title={`Feed fresh (checked ${at})${dataHealth.shared ? ` — shares ${dataHealth.feed} feed` : ''}`}
      >
        <CheckCircle2 className="h-3 w-3" /> Feed OK{dataHealth.shared ? ' (shared)' : ''}
      </Badge>
    )
  }
  const staleCount = dataHealth.stale_count ?? 0
  const staleList = dataHealth.stale_symbols?.length
    ? `: ${dataHealth.stale_symbols.join(', ')}`
    : ''
  return (
    <Badge
      className="bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400 gap-1"
      title={`${staleCount} stale symbol(s) as of ${at}${staleList}`}
    >
      <AlertTriangle className="h-3 w-3" /> Feed stale ({staleCount})
    </Badge>
  )
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

function LLMModeBadge({ llmMode }: { llmMode: LLMMode }) {
  if (llmMode === 'veto')
    return (
      <Badge className="bg-indigo-100 text-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-300 gap-1">
        <ShieldCheck className="h-3 w-3" /> LLM veto
      </Badge>
    )
  if (llmMode === 'delegate')
    return (
      <Badge className="bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300 gap-1">
        <Bot className="h-3 w-3" /> LLM delegate
      </Badge>
    )
  return (
    <Badge variant="outline" className="text-muted-foreground gap-1">
      <Bot className="h-3 w-3" /> LLM off
    </Badge>
  )
}

// ---------------------------------------------------------------------------
// LLM control (issue #266 Phase 2) — per-strategy off/veto segmented toggle
// (delegate shown but disabled) + decisions history + reachable health line.
// ---------------------------------------------------------------------------

const LLM_OPTIONS: { value: LLMMode; label: string; disabled?: boolean; hint?: string }[] = [
  { value: 'off', label: 'Off', hint: 'No LLM review — orders proceed unreviewed.' },
  {
    value: 'veto',
    label: 'Veto',
    hint: 'The LLM reviews every entry; a "skip" verdict blocks the order.',
  },
  {
    value: 'delegate',
    label: 'Delegate',
    disabled: true,
    hint: 'Coming soon — requires the LLM-decides engine path (a later phase).',
  },
]

export function LLMControlCard({ data }: { data: StrategyDetail }) {
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const current = data.llm_mode

  const flip = useMutation({
    mutationFn: (target: LLMMode) => strategiesDashboardApi.flipLLMMode(data.name, target),
    onSuccess: (outcome: LLMFlipOutcome) => {
      if (outcome.accepted) {
        setError(null)
        queryClient.invalidateQueries({ queryKey: ['strategy-detail', data.name] })
        queryClient.invalidateQueries({ queryKey: ['strategies-list'] })
      } else {
        setError(outcome.error_message ?? 'LLM mode change refused')
      }
    },
    onError: () => setError('LLM mode request failed — check server logs'),
  })

  const handleSelect = (target: LLMMode) => {
    if (target === current || flip.isPending) return
    // Confirm only when *enabling* enforcement (veto blocks real orders in
    // active mode). Turning off never needs a confirm.
    if (target === 'veto') {
      const ok = window.confirm(
        `Enable LLM VETO for ${data.display_name}?\n\n` +
          'The LLM will review every entry signal. In an enforcing mode a "skip" ' +
          'verdict will BLOCK the order. If the reviewer is unreachable it fails ' +
          'safe (the trade proceeds) and the decision is logged as review_failed.'
      )
      if (!ok) return
    }
    setError(null)
    flip.mutate(target)
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm flex items-center gap-2">
          <Bot className="h-4 w-4" /> LLM Control
          {!data.llm_veto_enabled && (
            <Badge variant="outline" className="ml-auto text-xs text-muted-foreground font-normal">
              veto not wired for this strategy
            </Badge>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* Segmented toggle */}
        <div className="inline-flex rounded-md border p-0.5 bg-muted/30">
          {LLM_OPTIONS.map((opt) => {
            const active = current === opt.value
            return (
              <button
                key={opt.value}
                type="button"
                title={opt.hint}
                disabled={opt.disabled || flip.isPending}
                onClick={() => handleSelect(opt.value)}
                className={[
                  'px-3 py-1.5 text-xs rounded-[5px] transition-colors flex items-center gap-1',
                  active
                    ? 'bg-background shadow-sm font-medium text-foreground'
                    : 'text-muted-foreground hover:text-foreground',
                  opt.disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer',
                ].join(' ')}
              >
                {flip.isPending && active && <Loader2 className="h-3 w-3 animate-spin" />}
                {opt.label}
                {opt.disabled && <span className="text-[10px] opacity-70">(soon)</span>}
              </button>
            )
          })}
        </div>
        <p className="text-xs text-muted-foreground">
          {LLM_OPTIONS.find((o) => o.value === current)?.hint ??
            'No LLM review configured for this strategy.'}
        </p>
        {!data.llm_veto_enabled && (
          <p className="text-xs text-muted-foreground italic">
            This strategy does not call the LLM veto today, so setting a mode has no runtime effect
            yet and its decisions history is empty.
          </p>
        )}
        {error && (
          <div className="rounded-md bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 p-2 text-xs text-red-700 dark:text-red-300 flex items-center gap-1">
            <AlertTriangle className="h-3 w-3" /> {error}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function DecisionBadge({ decision }: { decision: string }) {
  if (decision === 'take')
    return (
      <Badge className="bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400 text-xs py-0">
        take
      </Badge>
    )
  if (decision === 'skip')
    return (
      <Badge className="bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400 text-xs py-0">
        skip
      </Badge>
    )
  if (decision === 'review_failed')
    return (
      <Badge className="bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300 text-xs py-0 gap-1">
        <AlertTriangle className="h-3 w-3" /> review_failed
      </Badge>
    )
  return (
    <Badge variant="outline" className="text-xs py-0">
      {decision}
    </Badge>
  )
}

// Epoch millis for merged-table sorting. created_at (naive UTC) and
// candidate_at (IST offset) must be compared as instants, not as strings.
function parseTs(iso: string | null | undefined): number {
  if (!iso) return 0
  const d = new Date(TZ_SUFFIX.test(iso) ? iso : `${iso}Z`)
  return Number.isNaN(d.getTime()) ? 0 : d.getTime()
}

function ReasoningCell({ reasoning }: { reasoning: string | null | undefined }) {
  const [open, setOpen] = useState(false)
  const text = reasoning ?? ''
  if (!text) return <span className="text-muted-foreground">—</span>
  const truncated = text.length > 80
  return (
    <button
      type="button"
      className="text-left text-muted-foreground hover:text-foreground"
      onClick={() => setOpen(!open)}
    >
      <span className={open ? '' : 'line-clamp-1'}>{text}</span>
      {truncated && <span className="text-[10px] text-primary ml-1">{open ? 'less' : 'more'}</span>}
    </button>
  )
}

function LLMVerdictCell({ llm }: { llm: TradeLLMReview | null | undefined }) {
  if (!llm) return <span className="text-muted-foreground">—</span>
  return (
    <span className="flex items-center gap-1">
      <DecisionBadge decision={llm.decision} />
      {llm.confidence != null && (
        <span className="tabular-nums text-muted-foreground">{llm.confidence.toFixed(2)}</span>
      )}
    </span>
  )
}

function TradeStatusBadge({ status }: { status: string }) {
  if (status === 'veto_skip' || status === 'vetoed')
    return (
      <Badge className="bg-indigo-100 text-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-300 text-xs py-0">
        vetoed
      </Badge>
    )
  if (status === 'rejected' || status === 'exception' || status === 'failed')
    return (
      <Badge variant="destructive" className="text-xs py-0">
        {status}
      </Badge>
    )
  if (status === 'open')
    return (
      <Badge className="bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300 text-xs py-0">
        open
      </Badge>
    )
  return (
    <Badge variant={status === 'placed' ? 'default' : 'outline'} className="text-xs py-0">
      {status}
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
    // Backtest win-rate is the 2.5yr figure; Sandbox/Live show the *running*
    // win-rate over closed trades so far (issue #323).
    {
      label: 'Win Rate',
      bt: fmt(bt.win_rate_pct, '%'),
      sb: fmt(sb?.win_rate_pct, '%'),
      lv: fmt(lv?.win_rate_pct, '%'),
    },
    // Backtest N is the window trade count; Sandbox/Live show closed trades so
    // far, the denominator behind the running win-rate + cumulative P&L.
    {
      label: 'N Trades',
      bt: fmt(bt.n_trades),
      sb: sb?.closed_trades != null ? String(sb.closed_trades) : '—',
      lv: lv?.closed_trades != null ? String(lv.closed_trades) : '—',
    },
    {
      label: 'Open Pos',
      bt: '—',
      sb: sb?.open_positions != null ? String(sb.open_positions) : '—',
      lv: lv?.open_positions != null ? String(lv.open_positions) : '—',
    },
    // Cumulative realized P&L since the strategy started trading in that mode.
    {
      label: 'Cum P&L',
      bt: '—',
      sb: fmtPnl(sb?.cum_net_pnl),
      lv: fmtPnl(lv?.cum_net_pnl),
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
// Trades & LLM decisions — merged table (issue #358). One row per entry event:
// trade rows carry their matched veto verdict; enforced skips that never
// journaled render as pseudo-rows so "what the LLM blocked" sits next to
// "what actually traded".
// ---------------------------------------------------------------------------

type MergedRow =
  | { kind: 'trade'; ts: number; trade: RecentTrade }
  | { kind: 'skip'; ts: number; skip: UnmatchedSkipDecision }

export function TradesAndDecisionsCard({ data }: { data: StrategyDetail }) {
  const [sortAsc, setSortAsc] = useState(false)
  const trades = data.recent_trades
  const skips = data.llm_unmatched_skips ?? []

  // LLM health summary for the header (same source the old decisions card used;
  // rows are not needed — the table below is fed by the detail payload).
  const { data: decisions } = useQuery({
    queryKey: ['strategy-llm-decisions-summary', data.name],
    queryFn: () => strategiesDashboardApi.getLLMDecisions(data.name, 1, 0),
    enabled: data.llm_veto_enabled,
    refetchInterval: 30_000,
  })
  const summary = decisions?.summary
  const recentFailed = summary?.recent_review_failed ?? 0
  const reachable = recentFailed === 0

  const rows: MergedRow[] = [
    ...trades.map((t): MergedRow => ({ kind: 'trade', ts: parseTs(t.created_at), trade: t })),
    ...skips.map((s): MergedRow => ({ kind: 'skip', ts: parseTs(s.candidate_at), skip: s })),
  ].sort((a, b) => (sortAsc ? a.ts - b.ts : b.ts - a.ts))

  // Gross P&L / Charges / Capital are only populated for strategies that journal
  // them per leg (futures_follow_cap50). Show those columns only when present so
  // the sector_follow / simplified views stay compact.
  const hasFinancials = trades.some(
    (t) => t.gross_pnl != null || t.charges_inr != null || t.margin_inr != null
  )
  // LLM columns render for veto-wired strategies (or whenever a verdict/skip
  // actually exists) — sector_follow stays compact.
  const hasLLM = data.llm_veto_enabled || skips.length > 0 || trades.some((t) => t.llm)

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <CardTitle className="text-sm flex items-center gap-2">
            <History className="h-4 w-4" /> Trades &amp; LLM Decisions
            <span className="text-xs text-muted-foreground font-normal">
              last {trades.length} trade{trades.length !== 1 ? 's' : ''}
              {skips.length > 0 &&
                ` · ${skips.length} vetoed signal${skips.length !== 1 ? 's' : ''}`}
            </span>
          </CardTitle>
          {data.llm_veto_enabled && summary && summary.total > 0 && (
            <div
              className={[
                'flex items-center gap-1 text-xs rounded-md px-2 py-1',
                reachable
                  ? 'text-green-700 dark:text-green-400 bg-green-50 dark:bg-green-900/20'
                  : 'text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-900/20',
              ].join(' ')}
            >
              {reachable ? (
                <>
                  <CheckCircle2 className="h-3 w-3" /> LLM reachable
                </>
              ) : (
                <>
                  <AlertTriangle className="h-3 w-3" /> LLM unreachable (last {recentFailed} failed
                  — run <code className="font-mono">claude login</code>)
                </>
              )}
            </div>
          )}
        </div>
        {data.llm_veto_enabled && summary && summary.total > 0 && (
          <p className="text-xs text-muted-foreground">
            LLM decisions: take {summary.take} · skip {summary.skip} · review_failed{' '}
            {summary.review_failed} · {summary.total} total
          </p>
        )}
      </CardHeader>
      <CardContent className="p-0">
        {rows.length === 0 ? (
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
                  {hasFinancials && (
                    <th
                      className="text-right px-3 py-2 font-medium text-muted-foreground"
                      title="Entry (BUY) price of the NIFTY future"
                    >
                      Buy Price
                    </th>
                  )}
                  {hasFinancials && (
                    <th
                      className="text-right px-3 py-2 font-medium text-muted-foreground"
                      title="Exit (SELL) price of the NIFTY future"
                    >
                      Sell Price
                    </th>
                  )}
                  {hasFinancials && (
                    <th className="text-right px-3 py-2 font-medium text-muted-foreground">
                      Gross P&L
                    </th>
                  )}
                  {hasFinancials && (
                    <th
                      className="text-right px-3 py-2 font-medium text-muted-foreground"
                      title="Round-trip charges for both legs, deducted once on the exit"
                    >
                      Charges
                    </th>
                  )}
                  <th className="text-right px-3 py-2 font-medium text-muted-foreground">
                    Net P&L
                  </th>
                  {hasFinancials && (
                    <th
                      className="text-right px-3 py-2 font-medium text-muted-foreground"
                      title="SPAN margin committed by the entry (BUY); released on the T+1 exit"
                    >
                      Capital
                    </th>
                  )}
                  {hasLLM && (
                    <th
                      className="text-left px-3 py-2 font-medium text-muted-foreground"
                      title="Stage-1 LLM veto verdict for this entry (exits are never reviewed)"
                    >
                      LLM
                    </th>
                  )}
                  {hasLLM && (
                    <th className="text-left px-3 py-2 font-medium text-muted-foreground">
                      Reasoning
                    </th>
                  )}
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
                {rows.map((row) => {
                  if (row.kind === 'skip') {
                    const s = row.skip
                    // A vetoed entry: no order, no fill, no P&L — the verdict
                    // and reasoning ARE the event.
                    return (
                      <tr
                        key={`skip-${s.decision_id}`}
                        className="border-b last:border-0 hover:bg-muted/20 bg-indigo-50/30 dark:bg-indigo-900/5"
                      >
                        <td className="px-3 py-1.5">
                          <span className="font-medium text-muted-foreground">
                            {s.direction ?? '—'}
                          </span>
                        </td>
                        <td className="px-3 py-1.5 font-mono">{s.symbol}</td>
                        <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        {hasFinancials && (
                          <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        )}
                        {hasFinancials && (
                          <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        )}
                        {hasFinancials && (
                          <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        )}
                        {hasFinancials && (
                          <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        )}
                        <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        {hasFinancials && (
                          <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        )}
                        {hasLLM && (
                          <td className="px-3 py-1.5 whitespace-nowrap">
                            <LLMVerdictCell llm={s} />
                          </td>
                        )}
                        {hasLLM && (
                          <td className="px-3 py-1.5 max-w-[16rem]">
                            <ReasoningCell reasoning={s.reasoning} />
                          </td>
                        )}
                        <td className="px-3 py-1.5">
                          <Badge variant="outline" className="text-xs py-0">
                            {s.enforcement_mode}
                          </Badge>
                        </td>
                        <td className="px-3 py-1.5">
                          <TradeStatusBadge status="vetoed" />
                        </td>
                        <td className="px-3 py-1.5 text-right text-muted-foreground whitespace-nowrap">
                          {fmtDate(s.candidate_at)}
                        </td>
                      </tr>
                    )
                  }
                  const t = row.trade
                  const isBuy = t.side === 'BUY' || t.side === 'LONG'
                  const netPnl = t.net_pnl
                  return (
                    <tr key={`trade-${t.id}`} className="border-b last:border-0 hover:bg-muted/20">
                      <td className="px-3 py-1.5">
                        <span
                          className={`font-medium ${isBuy ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'}`}
                        >
                          {t.side}
                        </span>
                      </td>
                      <td className="px-3 py-1.5 font-mono">{t.symbol}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{t.quantity}</td>
                      {hasFinancials && (
                        <td className="px-3 py-1.5 text-right tabular-nums font-mono">
                          {t.entry_price != null ? (
                            fmtPrice(t.entry_price)
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </td>
                      )}
                      {hasFinancials && (
                        <td className="px-3 py-1.5 text-right tabular-nums font-mono">
                          {t.exit_price != null ? (
                            fmtPrice(t.exit_price)
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </td>
                      )}
                      {hasFinancials && (
                        <td className="px-3 py-1.5 text-right tabular-nums font-mono">
                          {t.gross_pnl != null ? (
                            <span
                              className={
                                t.gross_pnl >= 0
                                  ? 'text-green-600 dark:text-green-400'
                                  : 'text-red-600 dark:text-red-400'
                              }
                            >
                              {fmtPnl(t.gross_pnl)}
                            </span>
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </td>
                      )}
                      {hasFinancials && (
                        <td className="px-3 py-1.5 text-right tabular-nums font-mono text-muted-foreground">
                          {t.charges_inr != null ? fmtInr(t.charges_inr) : '—'}
                        </td>
                      )}
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
                      {hasFinancials && (
                        // Capital (SPAN margin) is committed by the entry (BUY) and
                        // released by the T+1 exit (SELL) — so show it on the entry
                        // leg only. On an exit the position is closing, and a merged
                        // multi-lot SELL carries just one leg's margin, which would
                        // under-report. P&L + round-trip charges live on the SELL.
                        <td className="px-3 py-1.5 text-right tabular-nums font-mono text-muted-foreground">
                          {t.side === 'BUY' && t.margin_inr != null ? fmtInr(t.margin_inr) : '—'}
                        </td>
                      )}
                      {hasLLM && (
                        <td className="px-3 py-1.5 whitespace-nowrap">
                          <LLMVerdictCell llm={t.llm} />
                        </td>
                      )}
                      {hasLLM && (
                        <td className="px-3 py-1.5 max-w-[16rem]">
                          <ReasoningCell reasoning={t.llm?.reasoning} />
                        </td>
                      )}
                      <td className="px-3 py-1.5">
                        <Badge variant="outline" className="text-xs py-0">
                          {t.mode}
                        </Badge>
                      </td>
                      <td className="px-3 py-1.5">
                        <TradeStatusBadge status={t.status} />
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
// Entry-evaluation breakdown card (issue #352) — futures_follow_cap50 only.
// Answers "why zero signals today" from the persisted 15:20 evaluation snapshot
// without reading logs. Data starts with the first post-deploy 15:20 run.
// ---------------------------------------------------------------------------

function fmtPct(v: number | null | undefined) {
  if (v == null) return '—'
  return `${(v * 100).toFixed(2)}%`
}

function OutcomeBadge({ outcome }: { outcome: EntryBreakdownOutcome }) {
  if (outcome === 'in_cap_placed')
    return (
      <Badge className="bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400 text-xs py-0">
        placed
      </Badge>
    )
  if (outcome === 'not_selected')
    return (
      <Badge className="bg-emerald-50 text-emerald-700 dark:bg-emerald-900/20 dark:text-emerald-400 text-xs py-0">
        passed (not selected)
      </Badge>
    )
  if (outcome === 'cap_skipped')
    return (
      <Badge className="bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300 text-xs py-0">
        cap skipped
      </Badge>
    )
  if (outcome === 'vetoed')
    return (
      <Badge className="bg-indigo-100 text-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-300 text-xs py-0">
        LLM vetoed
      </Badge>
    )
  if (outcome === 'placement_failed')
    return (
      <Badge variant="destructive" className="text-xs py-0">
        placement failed
      </Badge>
    )
  if (outcome === 'missing_data')
    return (
      <Badge className="bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400 text-xs py-0 gap-1">
        <AlertTriangle className="h-3 w-3" /> missing data
      </Badge>
    )
  return (
    <Badge variant="outline" className="text-xs py-0 text-muted-foreground">
      failed gate
    </Badge>
  )
}

function BreakdownSymbolRow({ row }: { row: EntryBreakdownSymbol }) {
  return (
    <tr className="border-b last:border-0 hover:bg-muted/20">
      <td className="px-3 py-1.5 font-mono">{row.symbol}</td>
      <td className="px-3 py-1.5 text-muted-foreground whitespace-nowrap">
        {row.sector_index ?? '—'}
      </td>
      <td className="px-3 py-1.5 text-right tabular-nums font-mono">{fmtPct(row.sector_ret)}</td>
      <td className="px-3 py-1.5 text-right tabular-nums font-mono">{fmtPct(row.stock_ret)}</td>
      <td className="px-3 py-1.5 text-right tabular-nums font-mono">
        {row.vol_ratio != null ? row.vol_ratio.toFixed(2) : '—'}
      </td>
      <td className="px-3 py-1.5">
        <OutcomeBadge outcome={row.outcome} />
      </td>
      <td
        className="px-3 py-1.5 text-muted-foreground max-w-[16rem] truncate"
        title={row.fail_reason ?? undefined}
      >
        {row.fail_reason ?? '—'}
      </td>
      <td className="px-3 py-1.5 text-muted-foreground">{row.intraday_source ?? '—'}</td>
    </tr>
  )
}

// Outcomes a symbol can only reach AFTER clearing all three gates — everything
// in run_entry's signal loop plus "passed but fell outside the K5 selection".
const PASSING_OUTCOMES: EntryBreakdownOutcome[] = [
  'in_cap_placed',
  'cap_skipped',
  'vetoed',
  'placement_failed',
  'not_selected',
]

/**
 * Renders ONE day's evaluation payload. Shared by every row of the history
 * table (issue #395) so today and any past day can never drift apart.
 */
function EntryBreakdownBody({ payload }: { payload: EntryBreakdownPayload }) {
  const [expanded, setExpanded] = useState(false)
  const [howOpen, setHowOpen] = useState(false)

  const gateFails = payload.per_gate_fail_counts
  const total = payload.symbols.length
  const passing = payload.symbols.filter((s) => PASSING_OUTCOMES.includes(s.outcome))
  // Per-gate counts are independent (one symbol can fail several gates);
  // missing-data symbols are never gate-evaluated.
  const gated = total - (gateFails?.missing_data ?? 0)

  return (
    <>
      {/* Symbols that cleared ALL three gates — the source of any signal.
                Shown on the row face so a signal day is self-explanatory
                without expanding the per-symbol table. */}
      {passing.length > 0 ? (
        <div className="px-4 pb-3 space-y-1">
          <p className="text-xs font-medium text-muted-foreground">
            Passed all 3 gates ({passing.length})
          </p>
          {passing.map((row) => (
            <div key={row.symbol} className="flex flex-wrap items-center gap-2 text-xs">
              <span className="font-mono font-medium">{row.symbol}</span>
              <span className="text-muted-foreground tabular-nums">
                sector {fmtPct(row.sector_ret)} · stock {fmtPct(row.stock_ret)} · vol{' '}
                {row.vol_ratio != null ? `${row.vol_ratio.toFixed(2)}x` : '—'}
              </span>
              <OutcomeBadge outcome={row.outcome} />
            </div>
          ))}
        </div>
      ) : (
        <p className="px-4 pb-3 text-xs text-muted-foreground">
          No symbol passed all 3 gates today.
        </p>
      )}
      {/* Per-gate summary — counts are independent per gate across all
                evaluated symbols (one symbol can fail several gates). */}
      <div className="flex flex-wrap gap-2 px-4 pb-3">
        <Badge
          variant="outline"
          className="text-xs text-muted-foreground"
          title="Symbols whose mapped sector index was up more than 1% intraday"
        >
          sector &gt;1%: {gated - (gateFails?.sector ?? 0)}/{gated} passed
        </Badge>
        <Badge
          variant="outline"
          className="text-xs text-muted-foreground"
          title="Symbols up more than 0.5% intraday"
        >
          stock &gt;0.5%: {gated - (gateFails?.stock ?? 0)}/{gated} passed
        </Badge>
        <Badge
          variant="outline"
          className="text-xs text-muted-foreground"
          title="Symbols with volume above 1x their 20-day average"
        >
          vol &gt;1x: {gated - (gateFails?.vol ?? 0)}/{gated} passed
        </Badge>
        {(gateFails?.missing_data ?? 0) > 0 && (
          <Badge className="bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400 text-xs">
            missing data: {gateFails?.missing_data}
          </Badge>
        )}
      </div>
      {/* How-this-works explainer — the strategy pipeline in one place,
                so the card is readable without the PLAN/registry docs. Static
                mechanism text; live numbers are in the Parameters card below. */}
      <button
        type="button"
        className="w-full flex items-center justify-between px-4 py-2 border-t text-xs text-muted-foreground hover:bg-muted/20"
        onClick={() => setHowOpen(!howOpen)}
      >
        <span className="flex items-center gap-1.5">
          <Info className="h-3.5 w-3.5" /> How this evaluation works (signals → sizing)
        </span>
        {howOpen ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
      </button>
      {howOpen && (
        <div className="px-4 pb-3 pt-1 space-y-2 text-xs text-muted-foreground">
          <p>
            <span className="font-medium text-foreground">1 · Signals (15:20 IST).</span> Each of
            the 30 locked universe stocks is a signal only if ALL three gates hold: its mapped
            sector index is up &gt;1% intraday, the stock itself is up &gt;0.5%, and today's volume
            is &gt;1× its 20-day average. Passing stocks are ranked by volume ratio; at most the top
            5 become signals.
          </p>
          <p>
            <span className="font-medium text-foreground">2 · What it buys.</span> The stock is only
            the trigger — the position is always ONE NIFTY near-month futures lot per signal (NFO,
            NRML, MARKET). This sleeve is leveraged NIFTY beta on bullish-breadth days, not stock
            selection: the stocks vote, NIFTY is the vehicle.
          </p>
          <p>
            <span className="font-medium text-foreground">3 · Position sizing.</span> Signals are
            taken greedily in vol-ratio order, one lot each, until the estimated overnight SPAN
            margin would exceed the hard cap of 50% of capital — later signals are "cap skipped"
            (never partially sized). Each in-cap signal must also clear the LLM review before
            placement (enforcing in sandbox). Current capital / per-lot margin / cap values are in
            the Parameters card below.
          </p>
          <p>
            <span className="font-medium text-foreground">4 · Exit &amp; risk.</span> Every lot is
            sold at MARKET the next trading day at 15:25 IST (watchdog retry 15:28). No stop loss —
            backtests showed hard stops are net-negative on this signal class; the backstop is a
            3%-of-capital daily-loss kill switch that halts new entries.
          </p>
        </div>
      )}
      {/* Expandable per-symbol table (sorted by closeness to passing) */}
      <button
        type="button"
        className="w-full flex items-center justify-between px-4 py-2 border-t text-xs text-muted-foreground hover:bg-muted/20"
        onClick={() => setExpanded(!expanded)}
      >
        <span>Per-symbol breakdown ({total} symbols, sorted by closeness to passing)</span>
        {expanded ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
      </button>
      {expanded && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b bg-muted/30">
                <th className="text-left px-3 py-2 font-medium text-muted-foreground">Symbol</th>
                <th className="text-left px-3 py-2 font-medium text-muted-foreground">
                  Sector Index
                </th>
                <th className="text-right px-3 py-2 font-medium text-muted-foreground">
                  Sector Ret
                </th>
                <th className="text-right px-3 py-2 font-medium text-muted-foreground">
                  Stock Ret
                </th>
                <th className="text-right px-3 py-2 font-medium text-muted-foreground">
                  Vol Ratio
                </th>
                <th className="text-left px-3 py-2 font-medium text-muted-foreground">Outcome</th>
                <th className="text-left px-3 py-2 font-medium text-muted-foreground">
                  Fail Reason
                </th>
                <th className="text-left px-3 py-2 font-medium text-muted-foreground">Source</th>
              </tr>
            </thead>
            <tbody>
              {payload.symbols.map((row) => (
                <BreakdownSymbolRow key={row.symbol} row={row} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

/** dd MMM for the history table's date column. */
function fmtDayLabel(isoDate: string) {
  const d = new Date(`${isoDate}T00:00:00`)
  if (Number.isNaN(d.getTime())) return isoDate
  return d.toLocaleDateString(undefined, { day: '2-digit', month: 'short' })
}

/**
 * Signals-per-day strip. Makes a run of zero-signal days visible at a glance —
 * pair it with the Source column to tell a quiet market from a degraded feed.
 */
function SignalStrip({ rows, pending }: { rows: EntryBreakdownSummary[]; pending: boolean }) {
  const maxSignals = Math.max(1, ...rows.map((r) => r.n_signals))
  // Oldest -> newest, so the strip reads left-to-right like a calendar.
  const chrono = [...rows].reverse()
  return (
    <div className="px-4 pb-3">
      <div className="flex items-end gap-[3px] h-8">
        {chrono.map((r) => (
          <div
            key={r.eval_date}
            title={`${r.eval_date} · ${r.n_signals} signal${r.n_signals !== 1 ? 's' : ''}`}
            className={
              r.n_signals > 0
                ? 'w-4 rounded-t-sm bg-green-500/70 dark:bg-green-500/60'
                : 'w-4 rounded-t-sm bg-muted-foreground/30'
            }
            style={{ height: r.n_signals > 0 ? `${(r.n_signals / maxSignals) * 100}%` : '3px' }}
          />
        ))}
        {pending && (
          <div
            title="Today · pending"
            className="w-4 h-8 rounded-t-sm border border-dashed border-muted-foreground/40 border-b-0"
          />
        )}
      </div>
      <p className="text-[11px] text-muted-foreground mt-1.5">Signals per day · hover for detail</p>
    </div>
  )
}

/**
 * One day in the history table. The full payload is fetched only when the row
 * is open — past days are immutable, so it is cached indefinitely.
 */
function EvalDayRow({
  summary,
  open,
  onToggle,
}: {
  summary: EntryBreakdownSummary
  open: boolean
  onToggle: () => void
}) {
  const { data: snapshot, isLoading } = useQuery({
    queryKey: ['futures-follow-entry-breakdown', summary.eval_date],
    queryFn: () => strategiesDashboardApi.getEntryBreakdown(summary.eval_date),
    enabled: open,
    staleTime: Number.POSITIVE_INFINITY,
  })

  const gp = summary.gates_passed
  const gf = summary.gates_failed
  const n = summary.evaluated_symbols

  return (
    <>
      <tr
        className={`border-b cursor-pointer hover:bg-muted/20 ${open ? 'bg-muted/20' : ''}`}
        onClick={onToggle}
      >
        <td className="px-3 py-2 whitespace-nowrap">
          {open ? (
            <ChevronDown className="h-3.5 w-3.5 inline mr-1 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 inline mr-1 text-muted-foreground" />
          )}
          <span className="font-mono">{fmtDayLabel(summary.eval_date)}</span>
        </td>
        <td
          className={`px-2 py-2 text-right tabular-nums ${
            summary.n_signals > 0 ? '' : 'text-muted-foreground'
          }`}
        >
          {summary.n_signals}
        </td>
        <td
          className={`px-2 py-2 text-right tabular-nums ${
            summary.placed > 0 ? '' : 'text-muted-foreground'
          }`}
        >
          {summary.placed}
        </td>
        <td
          className="px-2 py-2 text-muted-foreground tabular-nums"
          title={`Failed — sector ${gf.sector} · stock ${gf.stock} · vol ${gf.vol}`}
        >
          sector {gp.sector} · stock {gp.stock} · vol {gp.vol}
          {n > 0 && <span className="opacity-60"> of {n}</span>}
        </td>
        <td className="px-3 py-2 text-muted-foreground whitespace-nowrap">
          {summary.dominant_source} {summary.live_source_count}/{summary.total_symbols}
          {summary.missing_data > 0 && (
            <AlertTriangle className="h-3 w-3 inline ml-1 text-red-500" />
          )}
        </td>
      </tr>
      {open && (
        <tr className="border-b bg-muted/10">
          <td colSpan={5} className="p-0">
            {isLoading || !snapshot ? (
              <div className="p-4">
                <Skeleton className="h-24 w-full" />
              </div>
            ) : (
              <EntryBreakdownBody payload={snapshot.payload} />
            )}
          </td>
        </tr>
      )}
    </>
  )
}

/**
 * The 15:20 evaluation card (issue #395). Today is simply the newest row — it
 * shows a one-line pending state until the 15:20 IST run writes its snapshot,
 * and no pending row at all on a weekend / NSE holiday.
 */
export function EntryEvaluationCard() {
  const [limit, setLimit] = useState(30)
  const [openDate, setOpenDate] = useState<string | null>(null)

  const { data: history, isLoading } = useQuery({
    queryKey: ['futures-follow-eval-history', limit],
    queryFn: () => strategiesDashboardApi.getEntryBreakdownHistory(limit),
    // Only poll while today's snapshot is still expected; otherwise nothing can
    // change (past days are immutable) and the old 60s poll just fetched a null.
    refetchInterval: (q) => {
      const t = q.state.data?.today
      return t?.is_trading_day && !t.snapshot_exists ? 60_000 : false
    },
  })

  const rows = history?.rows ?? []
  const today = history?.today
  const showPending = Boolean(today?.is_trading_day && !today.snapshot_exists)
  // Default-open the newest day that actually has a snapshot, so the card never
  // opens on a blank panel while today is still pending.
  const defaultOpen = rows[0]?.eval_date ?? null
  const effectiveOpen = openDate ?? defaultOpen
  const signalDays = rows.filter((r) => r.n_signals > 0).length

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="text-sm flex items-center gap-2">
            <ListChecks className="h-4 w-4" /> 15:20 Evaluation
            {rows.length > 0 && (
              <span className="text-xs text-muted-foreground font-normal">
                {rows[0].mode && `mode ${rows[0].mode} · `}
                {rows.length} day{rows.length !== 1 ? 's' : ''} recorded · {signalDays} signal day
                {signalDays !== 1 ? 's' : ''}
              </span>
            )}
          </CardTitle>
          {(rows.length > 0 || history?.has_more) && (
            <select
              className="text-xs h-7 rounded-md border bg-background px-2"
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
            >
              <option value={30}>Last 30 days</option>
              <option value={90}>Last 90 days</option>
            </select>
          )}
        </div>
      </CardHeader>
      <CardContent className="p-0">
        {isLoading ? (
          <div className="p-4">
            <Skeleton className="h-24 w-full" />
          </div>
        ) : rows.length === 0 && !showPending ? (
          <p className="text-sm text-muted-foreground px-4 py-6 text-center italic">
            No evaluation recorded yet — the breakdown is captured at the next 15:20 IST entry run.
          </p>
        ) : (
          <>
            {rows.length > 0 && <SignalStrip rows={rows} pending={showPending} />}
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-y bg-muted/30 text-muted-foreground">
                    <th className="text-left px-3 py-2 font-medium">Date</th>
                    <th className="text-right px-2 py-2 font-medium">Signals</th>
                    <th className="text-right px-2 py-2 font-medium">Placed</th>
                    <th className="text-left px-2 py-2 font-medium">Gates passed</th>
                    <th className="text-left px-3 py-2 font-medium">Source</th>
                  </tr>
                </thead>
                <tbody>
                  {showPending && (
                    <tr className="border-b">
                      <td className="px-3 py-2 whitespace-nowrap text-muted-foreground">
                        <Clock className="h-3.5 w-3.5 inline mr-1" />
                        <span className="font-mono">Today</span>
                      </td>
                      <td colSpan={4} className="px-2 py-2 text-muted-foreground italic">
                        Runs at 15:20 IST
                      </td>
                    </tr>
                  )}
                  {rows.map((r) => (
                    <EvalDayRow
                      key={r.eval_date}
                      summary={r}
                      open={effectiveOpen === r.eval_date}
                      onToggle={() => setOpenDate(effectiveOpen === r.eval_date ? '' : r.eval_date)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
            {history?.has_more && limit < 90 && (
              <button
                type="button"
                className="w-full border-t px-4 py-2 text-xs text-muted-foreground hover:bg-muted/20"
                onClick={() => setLimit(90)}
              >
                Load more
              </button>
            )}
          </>
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
            <DataHealthBadge dataHealth={data.data_health} />
            <ModeBadge mode={data.mode} deployable={data.deployable} />
            <LLMModeBadge llmMode={data.llm_mode} />
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

      {/* LLM control (issue #266 Phase 2) */}
      <LLMControlCard data={data} />

      {/* Performance + P&L curve */}
      <div className="grid gap-4 xl:grid-cols-2">
        <PerfTable data={data} />
        <PnlCurve name={data.name} />
      </div>

      {/* 15:20 entry-evaluation breakdown + history (issues #352, #395) */}
      {data.name === 'futures_follow_cap50' && <EntryEvaluationCard />}

      {/* Trades + LLM decisions (merged, issue #358) */}
      <TradesAndDecisionsCard data={data} />

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
