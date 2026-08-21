import { Check, Copy, KeyRound, Loader2, ShieldCheck } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { webClient } from '@/api/client'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

interface TotpStatusResponse {
  status: string
  broker: string
  configured: boolean
}

interface TotpCurrentResponse {
  status: string
  broker: string
  code: string
  seconds_remaining: number
  interval: number
}

interface ZerodhaTotpHelperProps {
  /** Reports the latest code upward so the Connect button can auto-copy it. */
  onCodeChange?: (code: string | null) => void
}

/**
 * Live external-2FA TOTP code for the Zerodha Kite login page.
 *
 * When a secret is stored (encrypted, server-side) this shows the current
 * 6-digit code with a countdown and a copy button; otherwise it offers a
 * one-time setup input. The secret itself is never displayed or returned by
 * the API — only the derived code.
 */
export function ZerodhaTotpHelper({ onCodeChange }: ZerodhaTotpHelperProps) {
  const [configured, setConfigured] = useState<boolean | null>(null)
  const [code, setCode] = useState<string | null>(null)
  const [secondsLeft, setSecondsLeft] = useState(0)
  const [interval, setIntervalLength] = useState(30)
  const [copied, setCopied] = useState(false)
  const [showSetup, setShowSetup] = useState(false)
  const [secretInput, setSecretInput] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const fetchCurrent = useCallback(async () => {
    try {
      const response = await webClient.get<TotpCurrentResponse>(
        '/api/broker-totp/current?broker=zerodha'
      )
      if (response.data.status === 'success') {
        setCode(response.data.code)
        setSecondsLeft(response.data.seconds_remaining)
        setIntervalLength(response.data.interval)
        onCodeChange?.(response.data.code)
        // Refetch right after this window rolls over.
        if (refreshTimer.current) clearTimeout(refreshTimer.current)
        refreshTimer.current = setTimeout(
          fetchCurrent,
          response.data.seconds_remaining * 1000 + 500
        )
      }
    } catch {
      setError('Failed to fetch TOTP code')
    }
  }, [onCodeChange])

  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const response = await webClient.get<TotpStatusResponse>(
          '/api/broker-totp/status?broker=zerodha'
        )
        setConfigured(response.data.configured)
        if (response.data.configured) {
          fetchCurrent()
        }
      } catch {
        setConfigured(false)
      }
    }
    fetchStatus()
    return () => {
      if (refreshTimer.current) clearTimeout(refreshTimer.current)
    }
  }, [fetchCurrent])

  // Local 1s countdown between refetches.
  useEffect(() => {
    if (!code) return
    const tick = setInterval(() => {
      setSecondsLeft((s) => (s > 0 ? s - 1 : 0))
    }, 1000)
    return () => clearInterval(tick)
  }, [code])

  const handleCopy = async () => {
    if (!code) return
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      setError('Clipboard copy failed')
    }
  }

  const handleSaveSecret = async () => {
    if (!secretInput.trim()) return
    setSaving(true)
    setError(null)
    try {
      const response = await webClient.post<{ status: string; message?: string }>(
        '/api/broker-totp',
        { broker: 'zerodha', secret: secretInput }
      )
      if (response.data.status === 'success') {
        setSecretInput('')
        setShowSetup(false)
        setConfigured(true)
        fetchCurrent()
      } else {
        setError(response.data.message || 'Failed to save TOTP secret')
      }
    } catch (err) {
      const message =
        (err as { response?: { data?: { message?: string } } })?.response?.data?.message ||
        'Failed to save TOTP secret'
      setError(message)
    } finally {
      setSaving(false)
    }
  }

  if (configured === null) return null

  return (
    <div className="rounded-lg border p-3 space-y-2">
      {configured && !showSetup ? (
        <>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <ShieldCheck className="h-4 w-4 text-emerald-500" />
              Kite 2FA code
            </div>
            <button
              type="button"
              className="text-xs text-muted-foreground underline"
              onClick={() => setShowSetup(true)}
            >
              Replace key
            </button>
          </div>
          <div className="flex items-center gap-3">
            <span className="font-mono text-2xl tracking-widest" data-testid="totp-code">
              {code ? `${code.slice(0, 3)} ${code.slice(3)}` : '--- ---'}
            </span>
            <Button type="button" variant="outline" size="sm" onClick={handleCopy}>
              {copied ? (
                <Check className="h-4 w-4 text-emerald-500" />
              ) : (
                <Copy className="h-4 w-4" />
              )}
            </Button>
            <span className="text-xs text-muted-foreground tabular-nums">{secondsLeft}s</span>
          </div>
          <div className="h-1 w-full rounded bg-muted">
            <div
              className="h-1 rounded bg-primary transition-all duration-1000 ease-linear"
              style={{ width: `${(secondsLeft / interval) * 100}%` }}
            />
          </div>
          <p className="text-xs text-muted-foreground">
            Code is auto-copied when you click Connect — paste it on Kite's 2FA screen.
          </p>
        </>
      ) : (
        <>
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <KeyRound className="h-4 w-4" />
            {configured ? 'Replace Zerodha TOTP key' : 'Set up Zerodha TOTP (optional)'}
          </div>
          <div className="flex gap-2">
            <Input
              type="password"
              placeholder="Base32 secret from Zerodha external TOTP setup"
              value={secretInput}
              onChange={(e) => setSecretInput(e.target.value)}
              autoComplete="off"
            />
            <Button type="button" size="sm" onClick={handleSaveSecret} disabled={saving}>
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Save'}
            </Button>
            {configured && (
              <Button type="button" variant="ghost" size="sm" onClick={() => setShowSetup(false)}>
                Cancel
              </Button>
            )}
          </div>
          <p className="text-xs text-muted-foreground">
            Stored encrypted on this server and never shown again — only the 6-digit code is.
          </p>
        </>
      )}
      {error && (
        <Alert variant="destructive">
          <AlertDescription className="text-xs">{error}</AlertDescription>
        </Alert>
      )}
    </div>
  )
}
