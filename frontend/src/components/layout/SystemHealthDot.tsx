/**
 * Navbar system-health indicator (issue #744).
 *
 * Green / amber / red dot for machine-wide health (free RAM, non-paged pool,
 * TCP) that links to /health. Polls /health/api/system every 30 s with a
 * plain fetch that never redirects, and renders nothing when the endpoint is
 * unavailable, so it can never disrupt the page it sits on.
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getSystemHealth, type SystemHealth } from '@/api/health'
import { cn } from '@/lib/utils'

const POLL_MS = 30000

const STYLES = {
  pass: { dot: 'bg-green-500', label: 'System: OK', chip: 'text-muted-foreground' },
  warn: {
    dot: 'bg-yellow-500',
    label: 'System: warning',
    chip: 'border-yellow-500/50 bg-yellow-500/10 text-yellow-700 dark:text-yellow-400',
  },
  fail: {
    dot: 'bg-red-500',
    label: 'System: critical',
    chip: 'border-red-500/50 bg-red-500/10 text-red-700 dark:text-red-400',
  },
} as const

export function SystemHealthDot() {
  const [health, setHealth] = useState<SystemHealth | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      if (document.hidden) return
      const data = await getSystemHealth()
      if (!cancelled) setHealth(data)
    }
    load()
    const timer = setInterval(load, POLL_MS)
    document.addEventListener('visibilitychange', load)
    return () => {
      cancelled = true
      clearInterval(timer)
      document.removeEventListener('visibilitychange', load)
    }
  }, [])

  if (!health?.enabled || !health.status) return null

  const style = STYLES[health.status]
  const avail = health.readings?.available_mb
  const crashed = Boolean(health.unclean_exit)
  const title = [
    style.label,
    avail != null ? `free RAM ${Math.round(avail).toLocaleString()} MB` : null,
    crashed ? 'previous run ended without a clean shutdown' : null,
  ]
    .filter(Boolean)
    .join(' · ')

  return (
    <Link
      to="/health"
      title={title}
      aria-label={title}
      data-testid="system-health-dot"
      className={cn(
        'flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md border px-2 py-1 text-xs transition-colors hover:bg-muted',
        health.status === 'pass' ? 'border-transparent' : style.chip
      )}
    >
      <span className={cn('h-2 w-2 rounded-full', style.dot)} />
      <span className={cn('hidden lg:inline', health.status === 'pass' && 'sr-only')}>
        {style.label}
      </span>
      {crashed && <span className="hidden lg:inline">· crash</span>}
    </Link>
  )
}
