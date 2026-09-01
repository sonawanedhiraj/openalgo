import { AlertTriangle, Power } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { fetchCSRFToken } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'

interface SystemStatus {
  status: string
  pid: number
  uptime_s: number
  branch: string
  commit: string
  live_jobs: number | null
  schedulers: number | null
  market_guard_active: boolean
}

const CONFIRM_PHRASE = 'SHUTDOWN'

function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

export default function SystemCard() {
  const [status, setStatus] = useState<SystemStatus | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [confirmText, setConfirmText] = useState('')
  const [overrideMarket, setOverrideMarket] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [positionHints, setPositionHints] = useState<Record<string, number> | null>(null)
  const [shutDownAt, setShutDownAt] = useState<string | null>(null)

  const fetchStatus = useCallback(async () => {
    try {
      const response = await fetch('/system/api/status', { credentials: 'include' })
      if (!response.ok) return
      setStatus(await response.json())
    } catch {
      // status card degrades to nothing; the dashboard must not break on it
    }
  }, [])

  useEffect(() => {
    fetchStatus()
  }, [fetchStatus])

  const marketGuard = status?.market_guard_active ?? false
  const canConfirm = confirmText === CONFIRM_PHRASE && (!marketGuard || overrideMarket) && !busy

  const openDialog = () => {
    setConfirmText('')
    setOverrideMarket(false)
    setError(null)
    setPositionHints(null)
    setDialogOpen(true)
    fetchStatus() // refresh the guard state at the moment it matters
  }

  const doShutdown = async () => {
    setBusy(true)
    setError(null)
    try {
      const csrfToken = await fetchCSRFToken()
      const response = await fetch('/system/api/shutdown', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({
          confirm: confirmText,
          override_market_hours: overrideMarket,
        }),
      })
      const data = await response.json()
      if (response.ok && data.status === 'shutting_down') {
        setShutDownAt(data.at || '')
        setDialogOpen(false)
        return
      }
      if (response.status === 409) {
        setPositionHints(data.open_position_hints || {})
        setError(data.message || 'Refused during market hours.')
        return
      }
      setError(data.message || `Shutdown failed (HTTP ${response.status})`)
    } catch {
      setError('Shutdown request failed — is the server still reachable?')
    } finally {
      setBusy(false)
    }
  }

  if (shutDownAt !== null) {
    return (
      <div>
        <h2 className="text-xl md:text-2xl font-semibold mb-4 md:mb-6">System</h2>
        <Card className="border-destructive/40">
          <CardContent className="pt-6 text-center space-y-2">
            <p className="font-semibold">
              <Power className="inline h-4 w-4 mr-2 text-destructive" />
              OpenAlgo is shutting down{shutDownAt ? ` (requested ${shutDownAt})` : ''}
            </p>
            <p className="text-sm text-muted-foreground">
              This page will stop responding in a moment. To start again, run{' '}
              <code className="font-mono bg-muted px-1 rounded">uv run app.py</code> in the repo on
              the host machine.
            </p>
          </CardContent>
        </Card>
      </div>
    )
  }

  return (
    <div>
      <div className="flex items-center gap-3 mb-4 md:mb-6">
        <h2 className="text-xl md:text-2xl font-semibold">System</h2>
        {status && (
          <Badge
            variant="outline"
            className="border-green-500/50 text-green-600 dark:text-green-400"
          >
            <span className="w-2 h-2 rounded-full bg-green-500 mr-1.5" />
            Running
          </Badge>
        )}
      </div>
      <Card>
        <CardContent className="pt-6">
          <div className="flex flex-col sm:flex-row sm:items-center gap-4 sm:gap-6">
            <div className="flex-1 min-w-0">
              <p className="text-xs uppercase tracking-wide text-muted-foreground">Instance</p>
              <p className="text-sm mt-1 font-mono">
                {status ? (
                  <>
                    {status.branch} @ {status.commit} · PID {status.pid} · up{' '}
                    {formatUptime(status.uptime_s)}
                  </>
                ) : (
                  '…'
                )}
              </p>
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-xs uppercase tracking-wide text-muted-foreground">
                In-process right now
              </p>
              <p className="text-sm mt-1">
                {status?.live_jobs != null
                  ? `${status.live_jobs} scheduled jobs · ${status.schedulers ?? '?'} schedulers`
                  : '…'}
              </p>
            </div>
            <Button variant="destructive" onClick={openDialog} className="shrink-0">
              <Power className="h-4 w-4 mr-2" />
              Shut down OpenAlgo
            </Button>
          </div>
        </CardContent>
      </Card>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-destructive" />
              Shut down OpenAlgo?
            </DialogTitle>
            <DialogDescription>
              The process exits and everything inside it stops immediately:
            </DialogDescription>
          </DialogHeader>

          <ul className="text-sm text-muted-foreground space-y-1.5 list-disc pl-5">
            <li>
              All strategy schedulers —{' '}
              <span className="font-medium text-foreground">
                entries, T+1 exits, EOD watchdogs and square-off backstops
              </span>
            </li>
            <li>The WebSocket feed, scanner and tick-liveness watchdog</li>
            <li>Broker auto-login watcher and all Telegram alerting</li>
            <li>The web UI itself — this page goes dark until you restart</li>
          </ul>

          {marketGuard && (
            <div className="rounded-md border border-yellow-500/60 bg-yellow-500/10 p-3 text-sm space-y-2">
              <p>
                <AlertTriangle className="inline h-4 w-4 mr-1 text-yellow-600 dark:text-yellow-400" />
                <span className="font-semibold text-yellow-700 dark:text-yellow-400">
                  Market hours (09:00–15:35 IST).
                </span>{' '}
                Shutting down now leaves any open position unmanaged until restart.
              </p>
              {positionHints && Object.keys(positionHints).length > 0 && (
                <p className="font-mono text-xs">
                  Open rows:{' '}
                  {Object.entries(positionHints)
                    .map(([k, v]) => `${k}: ${v}`)
                    .join(' · ')}
                </p>
              )}
              <label className="flex items-start gap-2 cursor-pointer">
                <Checkbox
                  checked={overrideMarket}
                  onCheckedChange={(v) => setOverrideMarket(v === true)}
                  className="mt-0.5"
                />
                <span>I understand — shut down during market hours anyway</span>
              </label>
            </div>
          )}

          <div className="space-y-1.5">
            <label
              htmlFor="shutdown-confirm"
              className="text-xs uppercase tracking-wide text-muted-foreground"
            >
              Type {CONFIRM_PHRASE} to confirm
            </label>
            <Input
              id="shutdown-confirm"
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              placeholder={CONFIRM_PHRASE}
              autoComplete="off"
              className="font-mono"
            />
          </div>

          {error && <p className="text-sm text-destructive">{error}</p>}

          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)} disabled={busy}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={doShutdown} disabled={!canConfirm}>
              {busy ? 'Shutting down…' : 'Shut down'}
            </Button>
          </DialogFooter>
          <p className="text-xs text-muted-foreground">
            Restart is manual by design: run{' '}
            <code className="font-mono bg-muted px-1 rounded">uv run app.py</code> in the repo on
            the host. A shutdown is logged and Telegram-alerted, so it is never silent.
          </p>
        </DialogContent>
      </Dialog>
    </div>
  )
}
