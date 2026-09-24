/**
 * Machine-wide system health panel for /health (issue #744).
 *
 * - Unclean-exit banner: the previous run ended without a clean shutdown
 *   (e.g. the 2026-09-24 libzmq 10055 abort).
 * - System card: free RAM, non-paged pool (where socket buffers live),
 *   commit and TCP, with a free-RAM / non-paged chart and threshold lines.
 * - Free up memory: apps ranked by working set with how to free each.
 *   Recommend only — OpenAlgo never closes an app.
 */

import {
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  LineSeries,
  LineStyle,
  type UTCTimestamp,
} from 'lightweight-charts'
import { AlertTriangle, Cpu, Lock, Sparkles, X } from 'lucide-react'
import { useEffect, useRef } from 'react'
import type {
  HealthLevel,
  HistoricalMetric,
  SystemAppGroup,
  SystemHealth,
  UncleanExitReport,
} from '@/api/health'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

const LEVEL_TEXT: Record<HealthLevel, string> = {
  pass: 'text-green-500',
  warn: 'text-yellow-500',
  fail: 'text-red-500',
}

const LEVEL_BADGE: Record<HealthLevel, { label: string; className: string }> = {
  pass: { label: 'OK', className: 'bg-green-500/15 text-green-700 dark:text-green-400' },
  warn: { label: 'Warning', className: 'bg-yellow-500/15 text-yellow-700 dark:text-yellow-400' },
  fail: { label: 'Critical', className: 'bg-red-500/15 text-red-700 dark:text-red-400' },
}

function fmtMb(mb: number | null | undefined): string {
  if (mb == null) return '—'
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb).toLocaleString()} MB`
}

function istTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return new Intl.DateTimeFormat('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(d)
}

function istDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return new Intl.DateTimeFormat('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(d)
}

function UncleanExitBanner({
  report,
  onDismiss,
}: {
  report: UncleanExitReport
  onDismiss: () => void
}) {
  const crash = report.crash
  return (
    <div
      data-testid="unclean-exit-banner"
      className="rounded-lg border border-yellow-500/50 bg-yellow-500/10 p-4 flex items-start gap-3"
    >
      <AlertTriangle className="h-5 w-5 text-yellow-600 dark:text-yellow-400 mt-0.5 shrink-0" />
      <div className="flex-1 space-y-1 text-sm">
        <p className="font-semibold">Previous run ended without a clean shutdown</p>
        {crash ? (
          <p>
            Windows crash record: {crash.application} {crash.exception_code} in {crash.module}
            {crash.meaning ? ` — ${crash.meaning}` : ''}
          </p>
        ) : (
          <p className="text-muted-foreground">
            No Windows crash record found yet (it's checked about 90 s after boot). None at all
            means it was killed, lost power, or the machine rebooted.
          </p>
        )}
        <p>
          Last sample {istTime(report.last_sample?.at)} IST: free RAM{' '}
          {fmtMb(report.last_sample?.available_mb)}, non-paged pool{' '}
          {fmtMb(report.last_sample?.nonpaged_mb)}
        </p>
        {report.min_available_mb != null && (
          <p>
            Lowest free RAM in its final 5 minutes: {fmtMb(report.min_available_mb)} at{' '}
            {istTime(report.min_available_at)} IST
          </p>
        )}
        {report.top_apps && report.top_apps.length > 0 && (
          <p className="text-muted-foreground">
            Top memory then:{' '}
            {report.top_apps
              .slice(0, 4)
              .map((a) => `${a.app} ${fmtMb(a.rss_mb)}`)
              .join(', ')}
          </p>
        )}
        <p className="text-xs text-muted-foreground">
          Detected {istDateTime(report.detected_at)} IST
        </p>
      </div>
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7"
        onClick={onDismiss}
        aria-label="Dismiss"
      >
        <X className="h-4 w-4" />
      </Button>
    </div>
  )
}

function Metric({
  label,
  value,
  note,
  level,
}: {
  label: string
  value: string
  note?: string
  level?: HealthLevel
}) {
  return (
    <div className="rounded-md bg-muted/50 p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={cn('text-xl font-bold', level && level !== 'pass' && LEVEL_TEXT[level])}>
        {value}
      </p>
      {note && <p className="text-xs text-muted-foreground">{note}</p>}
    </div>
  )
}

function SystemChart({ history, health }: { history: HistoricalMetric[]; health: SystemHealth }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const ramRef = useRef<ISeriesApi<'Line'> | null>(null)
  const npRef = useRef<ISeriesApi<'Line'> | null>(null)
  // Primitive deps: `health` is a new object on every 10 s refresh, and
  // depending on it would tear down and rebuild the chart each time.
  const warnMb = health.thresholds?.ram_warn_mb
  const failMb = health.thresholds?.ram_fail_mb

  useEffect(() => {
    if (!containerRef.current || chartRef.current) return
    const dark = document.documentElement.classList.contains('dark')
    const chart = createChart(containerRef.current, {
      height: 240,
      width: containerRef.current.clientWidth,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: dark ? '#9ca3af' : '#6b7280',
      },
      grid: { vertLines: { visible: false }, horzLines: { visible: false } },
      timeScale: {
        timeVisible: true,
        tickMarkFormatter: (time: number) =>
          istTime(new Date(time * 1000).toISOString()).slice(0, 5),
      },
      localization: {
        timeFormatter: (time: number) => istDateTime(new Date(time * 1000).toISOString()),
        priceFormatter: (p: number) => `${Math.round(p)} MB`,
      },
    })
    ramRef.current = chart.addSeries(LineSeries, {
      color: '#3b82f6',
      lineWidth: 2,
      priceLineVisible: false,
      title: 'Free RAM',
    })
    npRef.current = chart.addSeries(LineSeries, {
      color: '#a855f7',
      lineWidth: 1,
      priceLineVisible: false,
      title: 'Non-paged pool',
    })
    if (warnMb != null && failMb != null) {
      ramRef.current.createPriceLine({
        price: warnMb,
        color: '#eab308',
        lineStyle: LineStyle.Dashed,
        lineWidth: 1,
        axisLabelVisible: true,
        title: 'warn',
      })
      ramRef.current.createPriceLine({
        price: failMb,
        color: '#ef4444',
        lineStyle: LineStyle.Dashed,
        lineWidth: 1,
        axisLabelVisible: true,
        title: 'critical',
      })
    }
    chartRef.current = chart
    const onResize = () => {
      if (containerRef.current) chart.applyOptions({ width: containerRef.current.clientWidth })
    }
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      chart.remove()
      chartRef.current = null
      ramRef.current = null
      npRef.current = null
    }
  }, [warnMb, failMb])

  useEffect(() => {
    if (!ramRef.current || !npRef.current) return
    const step = history.length > 1200 ? Math.ceil(history.length / 1200) : 1
    const ram = []
    const np = []
    for (let i = 0; i < history.length; i += step) {
      const m = history[i]
      const time = Math.floor(new Date(m.timestamp).getTime() / 1000) as UTCTimestamp
      if (m.sys_available_mb != null) ram.push({ time, value: m.sys_available_mb })
      if (m.sys_nonpaged_mb != null) np.push({ time, value: m.sys_nonpaged_mb })
    }
    ramRef.current.setData(ram)
    npRef.current.setData(np)
  }, [history])

  return <div ref={containerRef} className="w-full" />
}

function AppRow({ g }: { g: SystemAppGroup }) {
  const tone =
    g.class === 'safe'
      ? 'bg-green-500/15 text-green-700 dark:text-green-400'
      : 'bg-yellow-500/15 text-yellow-700 dark:text-yellow-400'
  return (
    <div className="grid grid-cols-[minmax(0,1.3fr)_88px_minmax(0,1.7fr)] gap-3 border-t py-2 text-sm">
      <div>
        <p className="font-medium">{g.app}</p>
        <p className="text-xs text-muted-foreground">
          {g.processes} process{g.processes === 1 ? '' : 'es'}
        </p>
      </div>
      <span className={cn('h-fit w-fit rounded px-2 py-0.5 text-xs', tone)}>{fmtMb(g.rss_mb)}</span>
      <div>
        <p>{g.how}</p>
        {g.reaches_target && (
          <p className="text-xs text-green-700 dark:text-green-400">
            This alone reaches the target.
          </p>
        )}
      </div>
    </div>
  )
}

function FreeUpMemory({ health }: { health: SystemHealth }) {
  const rec = health.recommendations
  if (!rec) return null
  const safe = rec.safe
  const careful = [...rec.careful, ...rec.unknown]
  return (
    <Card data-testid="free-up-memory-card">
      <CardHeader className="pb-2">
        <CardTitle className="text-lg flex items-center gap-2">
          <Sparkles className="h-5 w-5 text-muted-foreground" />
          Free up memory
        </CardTitle>
        <CardDescription>
          Free RAM {fmtMb(rec.available_mb)} · target {fmtMb(rec.target_mb)}
          {rec.need_mb ? ` · need ${fmtMb(rec.need_mb)}` : ' · nothing needed right now'}. Ranked by
          RAM in use, which is what closing frees. OpenAlgo never closes apps itself.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {safe.length > 0 && (
          <>
            <p className="text-xs text-muted-foreground mb-1">Safe to close</p>
            {safe.map((g) => (
              <AppRow key={`s-${g.app}`} g={g} />
            ))}
          </>
        )}
        {careful.length > 0 && (
          <>
            <p className="text-xs text-muted-foreground mt-4 mb-1">
              Careful — check before closing
            </p>
            {careful.map((g) => (
              <AppRow key={`c-${g.app}`} g={g} />
            ))}
          </>
        )}
        <div className="mt-4 rounded-md bg-muted/50 p-3 text-sm">
          <p className="flex items-center gap-2 font-medium">
            <Lock className="h-4 w-4" />
            Never listed
          </p>
          <p className="text-muted-foreground">
            OpenAlgo itself and the terminal running it, Windows services (svchost, explorer, dwm),
            and Defender.
          </p>
        </div>
      </CardContent>
    </Card>
  )
}

export function SystemHealthPanel({
  health,
  history,
  onDismissUncleanExit,
}: {
  health: SystemHealth | null
  history: HistoricalMetric[]
  onDismissUncleanExit: () => void
}) {
  if (!health?.enabled) return null
  const r = health.readings
  const rules = health.rules
  const t = health.thresholds
  const status = health.status ?? 'pass'
  const growth = r?.nonpaged_growth_mb_per_h

  return (
    <div className="space-y-4">
      {health.unclean_exit && (
        <UncleanExitBanner report={health.unclean_exit} onDismiss={onDismissUncleanExit} />
      )}

      <Card data-testid="system-card">
        <CardHeader className="pb-2">
          <div className="flex items-center gap-2">
            <Cpu className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-lg">System</CardTitle>
            <Badge variant="outline" className={cn('border-0', LEVEL_BADGE[status].className)}>
              {LEVEL_BADGE[status].label}
            </Badge>
            {!health.alerts_enabled && (
              <Badge variant="outline" className="text-xs">
                Telegram alerts off (calibrating)
              </Badge>
            )}
          </div>
          <CardDescription>
            Machine-wide memory and connections. Socket buffers come from the non-paged pool, which
            must stay in physical RAM — running low is what aborted OpenAlgo on 24 Sep (libzmq
            10055). Updated {istTime(health.timestamp)} IST.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 grid-cols-2 lg:grid-cols-4">
            <Metric
              label="Free RAM"
              value={fmtMb(r?.available_mb)}
              note={t ? `warn < ${t.ram_warn_mb} MB · critical < ${t.ram_fail_mb} MB` : undefined}
              level={rules?.free_ram.level}
            />
            <Metric
              label="Non-paged pool"
              value={fmtMb(r?.nonpaged_mb)}
              note={
                r?.platform_supported === false
                  ? 'Windows only'
                  : growth != null
                    ? `${growth >= 0 ? '+' : ''}${Math.round(growth)} MB per hour`
                    : 'trend after 1 hour of samples'
              }
              level={rules?.nonpaged.level}
            />
            <Metric
              label="Memory commit"
              value={r?.commit_pct != null ? `${Math.round(r.commit_pct)}%` : '—'}
              note={
                r?.commit_mb != null && r?.commit_limit_mb != null
                  ? `${fmtMb(r.commit_mb)} of ${fmtMb(r.commit_limit_mb)}`
                  : undefined
              }
            />
            <Metric
              label="TCP (system / OpenAlgo)"
              value={`${r?.tcp_system ?? '—'} / ${r?.tcp_openalgo ?? '—'}`}
              note={t ? `warn > ${t.tcp_warn.toLocaleString()}` : undefined}
              level={rules?.tcp.level}
            />
          </div>
          <div>
            <p className="text-sm text-muted-foreground mb-1">
              Free RAM (blue) and non-paged pool (purple), last 6 hours
            </p>
            <SystemChart history={history} health={health} />
          </div>
        </CardContent>
      </Card>

      <FreeUpMemory health={health} />
    </div>
  )
}
