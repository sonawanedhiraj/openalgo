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
  type LivePerf,
  type LLMFlipOutcome,
  type LLMMode,
  type PnlWindow,
  type RecentTrade,
  type SideSplit,
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
import { IntradayPullbackEvalCard } from './IntradayPullbackEvalCard'

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

// mode = the strategy's toggle; effectiveRouting = the per-strategy dispatch
// verdict right now (issue #440) — 'Live (held)' when Analyze mode gates a
// live toggle down to sandbox routing.
function ModeBadge({
  mode,
  deployable,
  effectiveRouting,
}: {
  mode: string
  deployable: boolean
  effectiveRouting?: 'live' | 'sandbox'
}) {
  if (!deployable || mode.includes('scaffold'))
    return (
      <Badge variant="outline" className="text-muted-foreground">
        Scaffold-only
      </Badge>
    )
  if (mode === 'live') {
    if (effectiveRouting === 'sandbox')
      return (
        <Badge
          variant="secondary"
          className="text-amber-700 bg-amber-100 dark:text-amber-300 dark:bg-amber-900/30"
          title="Toggle is LIVE but orders route to sandbox — Analyze mode is ON (navbar)"
        >
          Live (held: Analyze on)
        </Badge>
      )
    return (
      <Badge
        className="bg-green-600 text-white hover:bg-green-700"
        title="Orders route to the REAL broker"
      >
        Live
      </Badge>
    )
  }
  return (
    <Badge
      variant="secondary"
      className="text-amber-700 bg-amber-100 dark:text-amber-300 dark:bg-amber-900/30"
      title="Orders route to the sandbox book"
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

// A cell value with an optional ATM-options sub-value rendered beneath it
// (issue #455). `opt` stays undefined for strategies without option pricing.
// `tone` colors the main value (long/short P&L sub-rows, issue #458).
// `title` surfaces the backend's `notes` as a hover tooltip so a '—' can say why
// it is blank ("Sharpe needs >=20 trading days (7 so far)") instead of reading as
// broken (issue #568).
type PairedCell = { main: string; opt?: string; tone?: 'pos' | 'neg'; title?: string }

function PerfCell({ cell, sub }: { cell: PairedCell; sub?: boolean }) {
  const toneCls =
    cell.tone === 'pos'
      ? 'text-green-600 dark:text-green-400'
      : cell.tone === 'neg'
        ? 'text-red-600 dark:text-red-400'
        : ''
  return (
    <td
      title={cell.title}
      className={`px-4 text-right tabular-nums font-mono align-top ${sub ? 'py-1 text-xs' : 'py-2'}`}
    >
      <span className={toneCls}>{cell.main}</span>
      {cell.opt != null && (
        <div className={`text-blue-600 dark:text-blue-400 ${sub ? 'text-[11px]' : 'text-xs'}`}>
          {cell.opt}
        </div>
      )}
    </td>
  )
}

// '75% (6/8)' for a side with trades; '—' for an empty side (never 0%).
// Realized-metric cells for the Sandbox/Live columns (issue #568). These used to
// be a hardcoded '—' with no backend behind them. A null metric keeps the dash
// but carries `notes` as a tooltip, so "not enough history yet" is visibly
// different from "broken".
function metricCell(
  p: LivePerf | null | undefined,
  key: 'cagr_pct' | 'sharpe',
  suffix = ''
): PairedCell {
  if (!p) return { main: '—' }
  return { main: fmt(p[key], suffix), title: p.notes || undefined }
}

// CAGR cell for the Sandbox/Live columns (issue #573).
//
// CAGR is withheld until there are enough trading days to annualise honestly
// (60 — below that, simplified_engine's 41 days of +Rs8,220 on Rs20,000 prints
// a >700% "CAGR"). Rather than leave the cell blank, fall back to the window
// return the backend already computes, tagged with the trading-day count so it
// can never be read as an annualised figure. Once `cagr_pct` exists it takes
// over and the day-count marker disappears — that is the signal that the number
// changed meaning.
function cagrOrReturnCell(p: LivePerf | null | undefined): PairedCell {
  if (!p) return { main: '—' }
  if (p.cagr_pct != null) {
    return { main: fmt(p.cagr_pct, '%'), title: p.notes || undefined }
  }
  if (p.roc_pct == null) return { main: '—', title: p.notes || undefined }
  const days = p.trading_days
  return {
    main: fmt(p.roc_pct, '%'),
    opt: days ? `${days}d` : undefined,
    title:
      `Window return over ${days ?? '?'} trading days — NOT annualised. ` +
      `${p.notes || ''}`.trim(),
  }
}

// Charges are a modelled figure, not a broker-reported one — no Indian broker
// exposes per-order charges via API, so the label says so rather than implying
// these were billed (issue #579, same convention as open15).
const CHARGES_NOTE =
  'Modelled Zerodha MIS intraday charges (brokerage, STT, exchange, SEBI, stamp, GST). ' +
  'Deducted from Gross to give Net. No broker exposes per-order charges via API.'

function pnlTone(v: number | null | undefined): 'pos' | 'neg' | undefined {
  if (v == null) return undefined
  return v >= 0 ? 'pos' : 'neg'
}

function chargesCell(p: LivePerf | null | undefined): string {
  if (!p || p.charges_inr == null) return '—'
  const base = fmtPnl(-Math.abs(p.charges_inr))
  // A closed trade with no modelled charge contributes at gross, so the net
  // above is optimistic by that much. Say so rather than quietly rounding it in.
  return p.uncosted_trades ? `${base} (${p.uncosted_trades} uncosted)` : base
}

function maxDdCell(p: LivePerf | null | undefined): PairedCell {
  if (!p) return { main: '—' }
  const notional = p.capital_basis_is_notional
    ? ' % is of the declared capital, which is a per-trade risk-sizing base, not a compounding book.'
    : ''
  return {
    main: p.max_dd_inr != null ? fmtPnl(p.max_dd_inr) : '—',
    opt: p.max_dd_pct != null ? fmt(p.max_dd_pct, '%') : undefined,
    title: `${p.notes || ''}${notional}`.trim() || undefined,
  }
}

function fmtSideRate(s: SideSplit | null | undefined): string {
  if (!s || !s.n_trades) return '—'
  // A split that carries trade count + P&L but no per-side win rate (a backtest
  // whose source report never published one) renders '—' rather than a
  // half-formed 'undefined/155'. issue #508.
  if (s.win_rate_pct == null || s.wins == null) return '—'
  return `${fmt(s.win_rate_pct, '%')} (${s.wins}/${s.n_trades})`
}

function fmtSidePnl(s: SideSplit | null | undefined): string {
  if (!s || !s.n_trades) return '—'
  return fmtPnl(s.net_pnl_inr)
}

function sideTone(s: SideSplit | null | undefined): 'pos' | 'neg' | undefined {
  if (!s || !s.n_trades || s.net_pnl_inr == null) return undefined
  return s.net_pnl_inr >= 0 ? 'pos' : 'neg'
}

// '15 (8L / 7S)' when the split exists, else the plain count.
function withSideCounts(
  main: string,
  long: SideSplit | null | undefined,
  short: SideSplit | null | undefined
): string {
  if (main === '—' || !long || !short) return main
  return `${main} (${long.n_trades}L / ${short.n_trades}S)`
}

function PerfTable({ data }: { data: StrategyDetail }) {
  const bt = data.performance.backtest
  const sb = data.performance.sandbox
  const lv = data.performance.live
  const btOpt = bt.options
  const sbOpt = sb?.options
  const lvOpt = lv?.options
  const hasOptions = Boolean(btOpt || sbOpt || lvOpt)

  // Option-mode-unaffordable signals are visible as the stock-vs-options trade
  // count gap (fit-to-capital sizing can price out a high-premium contract).
  const btOptTrades =
    btOpt?.n_trades != null
      ? bt.n_trades != null && bt.n_trades > btOpt.n_trades
        ? `${btOpt.n_trades} (${bt.n_trades - btOpt.n_trades} skip)`
        : String(btOpt.n_trades)
      : undefined

  // Long/short sub-rows render only when some column carries the split
  // (open15_vol_breakout today; other strategies are unaffected). issue #458.
  const hasSideSplit = Boolean(
    bt.long || bt.short || sb?.long || sb?.short || lv?.long || lv?.short
  )

  const sideRateRow = (label: string, key: 'long' | 'short') => ({
    label,
    sub: true,
    bt: {
      main: fmtSideRate(bt[key]),
      opt: btOpt?.[key]?.n_trades ? fmtSideRate(btOpt[key]) : undefined,
    },
    sb: {
      main: fmtSideRate(sb?.[key]),
      opt: sbOpt?.[key]?.n_trades ? fmtSideRate(sbOpt[key]) : undefined,
    },
    lv: {
      main: fmtSideRate(lv?.[key]),
      opt: lvOpt?.[key]?.n_trades ? fmtSideRate(lvOpt[key]) : undefined,
    },
  })

  const sidePnlRow = (label: string, key: 'long' | 'short') => ({
    label,
    sub: true,
    bt: {
      main: fmtSidePnl(bt[key]),
      tone: sideTone(bt[key]),
      opt: btOpt?.[key]?.n_trades ? fmtSidePnl(btOpt[key]) : undefined,
    },
    sb: {
      main: fmtSidePnl(sb?.[key]),
      tone: sideTone(sb?.[key]),
      opt: sbOpt?.[key]?.n_trades ? fmtSidePnl(sbOpt[key]) : undefined,
    },
    lv: {
      main: fmtSidePnl(lv?.[key]),
      tone: sideTone(lv?.[key]),
      opt: lvOpt?.[key]?.n_trades ? fmtSidePnl(lvOpt[key]) : undefined,
    },
  })

  const rows: { label: string; sub?: boolean; bt: PairedCell; sb: PairedCell; lv: PairedCell }[] = [
    {
      // Sandbox/Live fall back to the window return until CAGR is annualisable
      // (issue #573), so the row covers both — the '(Nd)' marker distinguishes
      // them. Backtest is always a true CAGR or '—'.
      label: 'CAGR / Return',
      bt: { main: fmt(bt.cagr_pct, '%') },
      sb: cagrOrReturnCell(sb),
      lv: cagrOrReturnCell(lv),
    },
    {
      label: 'Sharpe',
      bt: { main: fmt(bt.sharpe) },
      sb: metricCell(sb, 'sharpe'),
      lv: metricCell(lv, 'sharpe'),
    },
    {
      // Sandbox/Live show drawdown in rupees as the primary figure — it is the
      // honest one. The %-of-capital reading is the paired `opt` sub-value and
      // is caveated in the tooltip whenever the basis is notional (issue #568).
      label: 'Max DD',
      bt: { main: fmt(bt.max_dd_pct, '%'), opt: btOpt ? fmt(btOpt.max_dd_pct, '%') : undefined },
      sb: maxDdCell(sb),
      lv: maxDdCell(lv),
    },
    // Backtest win-rate is the window figure; Sandbox/Live show the *running*
    // win-rate over closed trades so far (issue #323).
    {
      label: 'Win Rate',
      bt: {
        main: fmt(bt.win_rate_pct, '%'),
        opt: btOpt ? fmt(btOpt.win_rate_pct, '%') : undefined,
      },
      sb: {
        main: fmt(sb?.win_rate_pct, '%'),
        opt: sbOpt ? fmt(sbOpt.win_rate_pct, '%') : undefined,
      },
      lv: {
        main: fmt(lv?.win_rate_pct, '%'),
        opt: lvOpt ? fmt(lvOpt.win_rate_pct, '%') : undefined,
      },
    },
    ...(hasSideSplit ? [sideRateRow('Long', 'long'), sideRateRow('Short', 'short')] : []),
    // Backtest N is the window trade count; Sandbox/Live show closed trades so
    // far, the denominator behind the running win-rate + cumulative P&L.
    {
      label: 'N Trades',
      bt: { main: withSideCounts(fmt(bt.n_trades), bt.long, bt.short), opt: btOptTrades },
      sb: {
        main: withSideCounts(
          sb?.closed_trades != null ? String(sb.closed_trades) : '—',
          sb?.long,
          sb?.short
        ),
        opt: sbOpt?.n_trades != null ? String(sbOpt.n_trades) : undefined,
      },
      lv: {
        main: withSideCounts(
          lv?.closed_trades != null ? String(lv.closed_trades) : '—',
          lv?.long,
          lv?.short
        ),
        opt: lvOpt?.n_trades != null ? String(lvOpt.n_trades) : undefined,
      },
    },
    {
      label: 'Open Pos',
      bt: { main: '—' },
      sb: { main: sb?.open_positions != null ? String(sb.open_positions) : '—' },
      lv: { main: lv?.open_positions != null ? String(lv.open_positions) : '—' },
    },
    // Cumulative realized P&L: backtest window total vs since-inception per
    // mode. Gross and charges are shown as sub-rows above the net headline
    // (issue #579) — this row previously displayed the GROSS journal sum under
    // a "Net P&L" label, which reported +Rs8,740 on trades whose true net was
    // -Rs8,868. Showing the decomposition makes that class of error visible
    // instead of silent.
    ...(sb?.gross_pnl_inr != null || lv?.gross_pnl_inr != null
      ? [
          {
            label: 'Gross P&L',
            sub: true,
            bt: { main: '—' },
            sb: { main: fmtPnl(sb?.gross_pnl_inr), tone: pnlTone(sb?.gross_pnl_inr) },
            lv: { main: fmtPnl(lv?.gross_pnl_inr), tone: pnlTone(lv?.gross_pnl_inr) },
          },
          {
            label: 'Charges (modelled)',
            sub: true,
            bt: { main: '—' },
            sb: { main: chargesCell(sb), title: CHARGES_NOTE },
            lv: { main: chargesCell(lv), title: CHARGES_NOTE },
          },
        ]
      : []),
    {
      label: 'Net P&L',
      bt: {
        main: fmtPnl(bt.net_pnl_inr),
        opt: btOpt ? fmtPnl(btOpt.net_pnl_inr) : undefined,
      },
      sb: {
        main: fmtPnl(sb?.cum_net_pnl),
        tone: pnlTone(sb?.cum_net_pnl),
        opt: sbOpt ? fmtPnl(sbOpt.net_pnl_inr) : undefined,
      },
      lv: {
        main: fmtPnl(lv?.cum_net_pnl),
        tone: pnlTone(lv?.cum_net_pnl),
        opt: lvOpt ? fmtPnl(lvOpt.net_pnl_inr) : undefined,
      },
    },
    ...(hasSideSplit ? [sidePnlRow('Long', 'long'), sidePnlRow('Short', 'short')] : []),
    {
      label: 'Today P&L',
      bt: { main: '—' },
      sb: { main: fmtPnl(sb?.today_net_pnl) },
      lv: { main: fmtPnl(lv?.today_net_pnl) },
    },
  ]

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-sm flex items-center gap-2">
            <FileBarChart2 className="h-4 w-4" /> Performance Comparison
          </CardTitle>
          {hasOptions && (
            <span className="text-xs text-muted-foreground">
              per cell: stock ·{' '}
              <span className="text-blue-600 dark:text-blue-400 font-medium">options</span>
            </span>
          )}
        </div>
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
              {rows.map((r, i) => (
                <tr
                  key={`${r.label}-${i}`}
                  className={`border-b last:border-0 hover:bg-muted/20 ${r.sub ? 'bg-muted/20' : ''}`}
                >
                  <td
                    className={
                      r.sub
                        ? 'pl-8 pr-4 py-1 text-xs text-muted-foreground'
                        : 'px-4 py-2 text-muted-foreground'
                    }
                  >
                    {r.sub ? `↳ ${r.label}` : r.label}
                  </td>
                  <PerfCell cell={r.bt} sub={r.sub} />
                  <PerfCell cell={r.sb} sub={r.sub} />
                  <PerfCell cell={r.lv} sub={r.sub} />
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {hasOptions && (
          <p className="px-4 py-2 text-xs text-muted-foreground border-t">
            Options basis differs by column: backtest = fit-to-capital lots (₹30k slot);
            sandbox/live = 1-lot ATM shadow per trade — directionally comparable, not
            rupee-for-rupee.
          </p>
        )}
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

// "BAJAJ-AUTO28JUL2610700CE" -> "10700CE 28JUL" (compact contract label for the
// Opt Entry cell). Falls back to the raw symbol if the format is unexpected.
function optContractLabel(optSymbol: string): string {
  const m = optSymbol.match(/^(.+?)(\d{2}[A-Z]{3})(\d{2})(\d+(?:\.\d+)?)(CE|PE)$/)
  return m ? `${m[4]}${m[5]} ${m[2]}` : optSymbol
}

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
  // the sector_follow / simplified views stay compact. charges_inr alone does NOT
  // qualify — open15 journals charges but renders them in its entry/exit block.
  const hasFinancials = trades.some((t) => t.gross_pnl != null || t.margin_inr != null)
  // open15: rows carry the mid-bar trigger time + flatten timestamp — render the
  // Entry/Exit price+time (+ charges) block for those (issue #433).
  const hasEntryExit = trades.some((t) => t.trigger != null || t.exit_ts != null)
  // open15: ATM option shadow trade columns (issue #435, research-only).
  const hasOptShadow = trades.some((t) => t.opt_entry_premium != null)
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
                  {hasEntryExit && (
                    <th
                      className="text-right px-3 py-2 font-medium text-muted-foreground"
                      title="LTP at the legal mid-bar trigger moment"
                    >
                      Entry Price
                    </th>
                  )}
                  {hasEntryExit && (
                    <th
                      className="text-right px-3 py-2 font-medium text-muted-foreground"
                      title="Mid-bar trigger time (IST)"
                    >
                      Entry Time
                    </th>
                  )}
                  {hasEntryExit && (
                    <th
                      className="text-right px-3 py-2 font-medium text-muted-foreground"
                      title="Last tick at the 09:30 flatten"
                    >
                      Exit Price
                    </th>
                  )}
                  {hasEntryExit && (
                    <th
                      className="text-right px-3 py-2 font-medium text-muted-foreground"
                      title="Flatten timestamp (IST)"
                    >
                      Exit Time
                    </th>
                  )}
                  {hasEntryExit && (
                    <th
                      className="text-right px-3 py-2 font-medium text-muted-foreground"
                      title="Modelled MIS round-trip charges (brokerage + STT + txn + stamp + GST); Net P&L deducts these"
                    >
                      Charges
                    </th>
                  )}
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
                  {hasOptShadow && (
                    <th
                      className="text-right px-3 py-2 font-medium text-muted-foreground"
                      title="ATM option shadow trade (research, no orders): premium at entry — open of the minute after the trigger"
                    >
                      Opt Entry
                    </th>
                  )}
                  {hasOptShadow && (
                    <th
                      className="text-right px-3 py-2 font-medium text-muted-foreground"
                      title="Premium at the 09:30 flatten (bar open)"
                    >
                      Opt Exit
                    </th>
                  )}
                  {hasOptShadow && (
                    <th
                      className="text-right px-3 py-2 font-medium text-muted-foreground"
                      title="Contract lot size from the master contract"
                    >
                      Lot Size
                    </th>
                  )}
                  {hasOptShadow && (
                    <th
                      className="text-right px-3 py-2 font-medium text-muted-foreground"
                      title="Modelled option round-trip charges for 1 lot (brokerage + txn + STT + GST)"
                    >
                      Opt Charges
                    </th>
                  )}
                  {hasOptShadow && (
                    <th
                      className="text-right px-3 py-2 font-medium text-muted-foreground"
                      title="Net premium P&L for 1 lot (after modelled charges)"
                    >
                      Opt P&L / lot
                    </th>
                  )}
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
                        {hasEntryExit && (
                          <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        )}
                        {hasEntryExit && (
                          <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        )}
                        {hasEntryExit && (
                          <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        )}
                        {hasEntryExit && (
                          <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        )}
                        {hasEntryExit && (
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
                        {hasFinancials && (
                          <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        )}
                        <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        {hasOptShadow && (
                          <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        )}
                        {hasOptShadow && (
                          <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        )}
                        {hasOptShadow && (
                          <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        )}
                        {hasOptShadow && (
                          <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        )}
                        {hasOptShadow && (
                          <td className="px-3 py-1.5 text-right text-muted-foreground">—</td>
                        )}
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
                      {hasEntryExit && (
                        <td className="px-3 py-1.5 text-right tabular-nums font-mono">
                          {t.entry_price != null ? (
                            fmtPrice(t.entry_price)
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </td>
                      )}
                      {hasEntryExit && (
                        <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground whitespace-nowrap">
                          {t.trigger ?? '—'}
                        </td>
                      )}
                      {hasEntryExit && (
                        <td className="px-3 py-1.5 text-right tabular-nums font-mono">
                          {t.exit_price != null ? (
                            fmtPrice(t.exit_price)
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </td>
                      )}
                      {hasEntryExit && (
                        <td className="px-3 py-1.5 text-right tabular-nums text-muted-foreground whitespace-nowrap">
                          {/* exit_ts is IST-offset ISO — chars 11-18 are HH:MM:SS in IST,
                              locale/timezone-stable (no Date parsing) */}
                          {t.exit_ts ? t.exit_ts.slice(11, 19) : '—'}
                        </td>
                      )}
                      {hasEntryExit && (
                        <td className="px-3 py-1.5 text-right tabular-nums font-mono text-muted-foreground">
                          {t.charges_inr != null ? fmtInr(t.charges_inr) : '—'}
                        </td>
                      )}
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
                      {hasOptShadow && (
                        <td className="px-3 py-1.5 text-right tabular-nums">
                          {t.opt_entry_premium != null ? (
                            <>
                              <span className="font-mono">{fmtPrice(t.opt_entry_premium)}</span>
                              {t.opt_symbol && (
                                <div className="text-[11px] text-muted-foreground whitespace-nowrap">
                                  {optContractLabel(t.opt_symbol)}
                                </div>
                              )}
                            </>
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </td>
                      )}
                      {hasOptShadow && (
                        <td className="px-3 py-1.5 text-right tabular-nums font-mono align-top">
                          {t.opt_exit_premium != null ? (
                            fmtPrice(t.opt_exit_premium)
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </td>
                      )}
                      {hasOptShadow && (
                        <td className="px-3 py-1.5 text-right tabular-nums font-mono align-top">
                          {t.opt_lot_size != null ? (
                            t.opt_lot_size
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </td>
                      )}
                      {hasOptShadow && (
                        <td className="px-3 py-1.5 text-right tabular-nums font-mono text-muted-foreground align-top">
                          {t.opt_charges_inr != null ? fmtInr(t.opt_charges_inr) : '—'}
                        </td>
                      )}
                      {hasOptShadow && (
                        <td className="px-3 py-1.5 text-right tabular-nums font-mono align-top">
                          {t.opt_pnl != null ? (
                            <span
                              className={
                                t.opt_pnl >= 0
                                  ? 'text-green-600 dark:text-green-400'
                                  : 'text-red-600 dark:text-red-400'
                              }
                            >
                              {fmtPnl(t.opt_pnl)}
                            </span>
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </td>
                      )}
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
  // opens on a blank panel while today is still pending. `openDate` is null
  // until the operator touches a row, then '' means "explicitly closed" — the
  // ?? is load-bearing (|| would treat '' as untouched and re-open the default).
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
            <ModeBadge
              mode={data.mode}
              deployable={data.deployable}
              effectiveRouting={data.effective_routing}
            />
            <LLMModeBadge llmMode={data.llm_mode} />
          </div>
          <p className="text-sm text-muted-foreground font-mono pl-7">
            {data.name} · v{data.version}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {data.console_url && (
            <a href={data.console_url}>
              <Button variant="default" size="sm" className="gap-1.5">
                Decision log &amp; settings
              </Button>
            </a>
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

      {/* Today's per-pick evaluation — why entries did/didn't fire (issue #412) */}
      {data.name === 'intraday_pullback_top2' && <IntradayPullbackEvalCard />}

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
