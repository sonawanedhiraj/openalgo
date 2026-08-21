import { AlertTriangle, CalendarClock, Check, Lock, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { adminApi } from '@/api/admin'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import type {
  DaemonThreadRow,
  ScheduledJobRow,
  SchedulersResponse,
  SchedulerTier,
} from '@/types/admin'

const GROUP_LABELS: Record<string, string> = {
  'strategy:futures_follow_cap50': 'futures_follow_cap50',
  'strategy:sector_follow_cap5_vol': 'sector_follow_cap5_vol',
  'strategy:open15_vol_breakout': 'open15_vol_breakout',
  'strategy:intraday_pullback_top2': 'intraday_pullback_top2',
  'strategy:simplified_engine': 'simplified_engine',
  data_feed: 'Data feed',
  reports: 'Reports and ops',
  sandbox: 'Sandbox',
  python_strategy_host: 'Python strategy host',
  user: 'User-defined jobs',
  loop: 'Recurring loops',
  transport: 'Feed and transport',
  poller: 'Bot pollers',
  boot: 'Boot one-shots',
  unregistered: 'Not in the catalog',
}

const THREAD_GROUP_HINTS: Record<string, string> = {
  loop: 'Cron jobs in everything but name. Cadence is declared, heartbeat is measured.',
  transport: 'Feed and socket pumps. Never disableable — everything downstream depends on them.',
  poller: 'One poller per bot token; only one may own the token at a time.',
  boot: 'Run once at startup and exit. "Completed" is the healthy state.',
  unregistered: 'Alive but missing from the catalog — worth adding.',
}

function tierBadge(tier: SchedulerTier) {
  if (tier === 'protected') {
    return (
      <Badge variant="outline" className="border-red-500/40 text-red-600 dark:text-red-400 gap-1">
        <Lock className="h-3 w-3" />
        protected
      </Badge>
    )
  }
  if (tier === 'guarded') {
    return (
      <Badge variant="outline" className="border-amber-500/40 text-amber-700 dark:text-amber-400">
        guarded
      </Badge>
    )
  }
  return (
    <Badge variant="outline" className="text-muted-foreground">
      free
    </Badge>
  )
}

function stateDot(color: string) {
  return <span className={`h-2 w-2 shrink-0 rounded-full ${color}`} />
}

function jobDot(row: ScheduledJobRow) {
  const status = row.last_run?.status
  if (status === 'error') return stateDot('bg-red-500')
  if (status === 'missed') return stateDot('bg-amber-500')
  if (row.state === 'registered') return stateDot('bg-green-600')
  if (row.state === 'unregistered') return stateDot('bg-sky-500')
  return stateDot('bg-muted-foreground/40')
}

function threadDot(row: DaemonThreadRow) {
  if (row.state === 'dead') return stateDot('bg-red-500')
  if (row.state === 'stale') return stateDot('bg-amber-500')
  if (row.state === 'running') return stateDot('bg-green-600')
  if (row.state === 'completed') return stateDot('bg-sky-500')
  return stateDot('bg-muted-foreground/40')
}

function formatAge(seconds: number | null): string | null {
  if (seconds === null || seconds === undefined) return null
  if (seconds < 90) return `${Math.round(seconds)}s ago`
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`
  return `${Math.round(seconds / 3600)} h ago`
}

function formatCadence(seconds: number | null): string | null {
  if (!seconds) return null
  if (seconds < 90) return `every ${Math.round(seconds)}s`
  return `every ${Math.round(seconds / 60)} min`
}

function formatNextRun(iso: string | null): string | null {
  if (!iso) return null
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return null
  return parsed.toLocaleString(undefined, {
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function jobSubtitle(row: ScheduledJobRow): string {
  const parts: string[] = []
  if (row.schedule) parts.push(row.schedule)

  if (row.state === 'not_registered') {
    parts.push(
      row.env_flag
        ? `not registered · ${row.env_flag}=${row.env_flag_value ?? 'unset'}`
        : 'not registered'
    )
  } else if (row.state === 'unregistered') {
    parts.push('not in the catalog')
  } else {
    const next = formatNextRun(row.next_run_time)
    if (next) parts.push(`next ${next}`)
  }

  const last = row.last_run
  if (last?.status) {
    const duration = last.duration_ms ? ` ${(last.duration_ms / 1000).toFixed(1)}s` : ''
    parts.push(`last ${last.status}${duration}`)
  }
  return parts.join(' · ')
}

function threadSubtitle(row: DaemonThreadRow): string {
  const parts: string[] = []
  const cadence = formatCadence(row.cadence_sec)
  if (cadence) parts.push(cadence)
  if (row.window) parts.push(row.window)

  if (row.state === 'not_started') {
    parts.push(
      row.env_flag
        ? `not started · ${row.env_flag}=${row.env_flag_value ?? 'unset'}`
        : 'not started'
    )
  } else if (row.state === 'completed') {
    parts.push('ran and exited')
  } else if (row.state === 'dead') {
    parts.push('beat, then vanished')
  } else {
    const age = formatAge(row.heartbeat_age_sec)
    if (age) parts.push(`beat ${age}`)
    else if (row.alive) parts.push('alive')
  }
  return parts.join(' · ')
}

function StatTile({
  label,
  value,
  tone,
}: {
  label: string
  value: number | string
  tone?: 'danger' | 'warning'
}) {
  const toneClass =
    tone === 'danger'
      ? 'text-red-600 dark:text-red-400'
      : tone === 'warning'
        ? 'text-amber-600 dark:text-amber-400'
        : ''
  return (
    <div className="rounded-lg bg-muted/50 px-3 py-2">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`text-xl font-semibold ${toneClass}`}>{value}</div>
    </div>
  )
}

function GroupCard({
  group,
  hint,
  children,
}: {
  group: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div className="space-y-1">
      <div className="px-1">
        <div className="text-xs font-medium text-muted-foreground">
          {GROUP_LABELS[group] ?? group}
        </div>
        {hint && <div className="text-xs text-muted-foreground/70">{hint}</div>}
      </div>
      <div className="rounded-xl border divide-y overflow-hidden">{children}</div>
    </div>
  )
}

function Row({
  dot,
  title,
  subtitle,
  tier,
  muted,
  note,
  testId,
}: {
  dot: React.ReactNode
  title: string
  subtitle: string
  tier: SchedulerTier
  muted?: boolean
  note?: string | null
  testId?: string
}) {
  return (
    <div className="flex items-center gap-3 px-4 py-3" data-testid={testId}>
      {dot}
      <div className="min-w-0 flex-1">
        <div className={`text-sm font-medium truncate ${muted ? 'text-muted-foreground' : ''}`}>
          {title}
        </div>
        <div className="text-xs text-muted-foreground truncate">{subtitle}</div>
        {note && <div className="text-xs text-muted-foreground/70 mt-0.5">{note}</div>}
      </div>
      <div className="shrink-0">{tierBadge(tier)}</div>
    </div>
  )
}

export default function Schedulers() {
  const [data, setData] = useState<SchedulersResponse | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setIsLoading(true)
    try {
      const response = await adminApi.getSchedulers()
      setData(response)
      setError(null)
    } catch {
      setError('Could not load the scheduler inventory.')
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const jobGroups = useMemo(() => {
    const grouped = new Map<string, ScheduledJobRow[]>()
    for (const row of data?.jobs ?? []) {
      const list = grouped.get(row.group) ?? []
      list.push(row)
      grouped.set(row.group, list)
    }
    return [...grouped.entries()]
  }, [data])

  const threadGroups = useMemo(() => {
    const grouped = new Map<string, DaemonThreadRow[]>()
    for (const row of data?.threads ?? []) {
      const list = grouped.get(row.group) ?? []
      list.push(row)
      grouped.set(row.group, list)
    }
    return [...grouped.entries()]
  }, [data])

  if (isLoading && !data) {
    return (
      <div className="flex items-center justify-center py-16">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
      </div>
    )
  }

  const jobs = data?.jobs_summary ?? {}
  const threads = data?.threads_summary ?? {}

  return (
    <div className="py-6 space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <CalendarClock className="h-6 w-6" />
            Schedulers
          </h1>
          <p className="text-muted-foreground mt-1">
            Every scheduled job and long-lived thread in this process. Read-only — controls land in
            a later phase.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={load} disabled={isLoading}>
          <RefreshCw className={`h-4 w-4 mr-2 ${isLoading ? 'animate-spin' : ''}`} />
          Refresh
        </Button>
      </div>

      {error && (
        <div className="rounded-lg border border-red-500/40 bg-red-500/5 px-4 py-3 text-sm text-red-600 dark:text-red-400">
          {error}
        </div>
      )}

      {(data?.sources_failed?.length ?? 0) > 0 && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 px-4 py-3 text-sm text-amber-700 dark:text-amber-400 flex items-start gap-2">
          <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
          <span>
            Partial data: {data?.sources_failed.join(', ')} could not be read. The rest of the page
            is still accurate.
          </span>
        </div>
      )}

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Scheduled jobs</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3" data-testid="jobs-summary">
            <StatTile label="Catalogued" value={jobs.total ?? 0} />
            <StatTile label="Registered" value={jobs.registered ?? 0} />
            <StatTile label="Not registered" value={jobs.not_registered ?? 0} />
            <StatTile label="Last-run errors" value={jobs.last_run_error ?? 0} tone="danger" />
            <StatTile label="Missed" value={jobs.last_run_missed ?? 0} tone="warning" />
          </div>

          {jobGroups.map(([group, rows]) => (
            <GroupCard key={group} group={group}>
              {rows.map((row) => (
                <Row
                  key={`${row.scheduler}-${row.job_id}`}
                  testId={`job-${row.job_id}`}
                  dot={jobDot(row)}
                  title={`${row.label} — ${row.job_id}`}
                  subtitle={jobSubtitle(row)}
                  tier={row.tier}
                  muted={row.state !== 'registered'}
                  note={row.tier === 'protected' ? row.safety_note : null}
                />
              ))}
            </GroupCard>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Daemon threads</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3" data-testid="threads-summary">
            <StatTile label="Expected" value={threads.expected ?? 0} />
            <StatTile label="Alive" value={threads.alive ?? 0} />
            <StatTile label="Stale heartbeat" value={threads.stale ?? 0} tone="warning" />
            <StatTile label="Dead" value={threads.dead ?? 0} tone="danger" />
            <StatTile label="Not started" value={threads.not_started ?? 0} />
          </div>

          {threadGroups.map(([group, rows]) => (
            <GroupCard key={group} group={group} hint={THREAD_GROUP_HINTS[group]}>
              {rows.map((row) => (
                <Row
                  key={row.thread_name}
                  testId={`thread-${row.thread_name}`}
                  dot={threadDot(row)}
                  title={`${row.label} — ${row.thread_name}`}
                  subtitle={threadSubtitle(row)}
                  tier={row.tier}
                  muted={row.state === 'not_started'}
                  note={row.owner}
                />
              ))}
            </GroupCard>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">How to read this page</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground space-y-2">
          <p>
            <strong>Not registered</strong> means the job is declared in the catalog but is absent
            from its scheduler — usually an env flag turned it off before registration. That row
            cannot be discovered by inspecting the scheduler alone, which is why the catalog exists.
          </p>
          <p>
            <strong>Stale heartbeat</strong> means a loop thread is still alive but has not
            completed a tick within three times its declared cadence. A thread wedged on a socket
            read reports as alive forever, so liveness alone proves nothing.
          </p>
          <p>
            <strong>Not started</strong> is routinely legitimate — no broker session, outside the
            strategy window, flag off, bot not configured. These never raise an alert.
          </p>
          <p className="flex items-start gap-2">
            <Check className="h-4 w-4 mt-0.5 shrink-0" />
            <span>
              Tiers are recorded but not yet enforced. <strong>Protected</strong> jobs — exits, EOD
              flattens, square-offs — will never get a toggle: disabling one strands real positions.
            </span>
          </p>
        </CardContent>
      </Card>
    </div>
  )
}
