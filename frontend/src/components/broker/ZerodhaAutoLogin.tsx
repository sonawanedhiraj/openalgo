import { KeyRound, Loader2, LogIn, ShieldCheck, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { webClient } from '@/api/client'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

interface AutoLoginStatus {
  status: string
  broker: string
  enabled: boolean
  has_credentials: boolean
  has_totp: boolean
  user_id: string | null
  live: boolean
}

interface LoginResult {
  status: string
  ok: boolean
  message: string
}

/**
 * Headless Zerodha auto-login control (issue #654).
 *
 * When credentials are stored (encrypted, server-side) this shows a one-click
 * "Auto login" button that logs in with no browser step; otherwise it offers a
 * one-time user-id + password setup form. The password is never displayed or
 * returned by the API. This complements — never replaces — the manual Connect
 * Account flow below it.
 */
export function ZerodhaAutoLogin() {
  const [status, setStatus] = useState<AutoLoginStatus | null>(null)
  const [showSetup, setShowSetup] = useState(false)
  const [userId, setUserId] = useState('')
  const [password, setPassword] = useState('')
  const [saving, setSaving] = useState(false)
  const [loggingIn, setLoggingIn] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchStatus = async () => {
    try {
      const response = await webClient.get<AutoLoginStatus>(
        '/api/broker-auto-login/status?broker=zerodha'
      )
      setStatus(response.data)
      if (!response.data.has_credentials) setShowSetup(true)
    } catch {
      setStatus(null)
    }
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: mount-only status fetch
  useEffect(() => {
    fetchStatus()
  }, [])

  const handleSave = async () => {
    if (!userId.trim() || !password.trim()) return
    setSaving(true)
    setError(null)
    try {
      const response = await webClient.post<{ status: string; message?: string }>(
        '/api/broker-auto-login/credentials',
        { broker: 'zerodha', user_id: userId, password }
      )
      if (response.data.status === 'success') {
        setPassword('')
        setShowSetup(false)
        await fetchStatus()
      } else {
        setError(response.data.message || 'Failed to save credentials')
      }
    } catch (err) {
      setError(
        (err as { response?: { data?: { message?: string } } })?.response?.data?.message ||
          'Failed to save credentials'
      )
    } finally {
      setSaving(false)
    }
  }

  const handleAutoLogin = async () => {
    setLoggingIn(true)
    setError(null)
    try {
      const response = await webClient.post<LoginResult>('/api/broker-auto-login/login', {
        broker: 'zerodha',
      })
      if (response.data.ok) {
        window.location.href = '/dashboard'
      } else {
        setError(response.data.message || 'Auto-login failed — use manual Connect below.')
      }
    } catch (err) {
      setError(
        (err as { response?: { data?: { message?: string } } })?.response?.data?.message ||
          'Auto-login failed — use manual Connect below.'
      )
    } finally {
      setLoggingIn(false)
    }
  }

  const handleRemove = async () => {
    setError(null)
    try {
      await webClient.delete('/api/broker-auto-login/credentials?broker=zerodha')
      setShowSetup(true)
      await fetchStatus()
    } catch {
      setError('Failed to remove credentials')
    }
  }

  if (status === null) return null

  const canAutoLogin = status.has_credentials && status.has_totp

  return (
    <div className="rounded-lg border p-3 space-y-2">
      {status.has_credentials && !showSetup ? (
        <>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <ShieldCheck className="h-4 w-4 text-emerald-500" />
              Auto login
              {status.user_id && <span className="font-mono text-xs">({status.user_id})</span>}
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="text-xs text-muted-foreground underline"
                onClick={() => setShowSetup(true)}
              >
                Replace
              </button>
              <button
                type="button"
                className="text-xs text-muted-foreground underline flex items-center gap-1"
                onClick={handleRemove}
              >
                <Trash2 className="h-3 w-3" />
                Remove
              </button>
            </div>
          </div>
          <Button
            type="button"
            variant="secondary"
            className="w-full"
            onClick={handleAutoLogin}
            disabled={!canAutoLogin || loggingIn}
          >
            {loggingIn ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Logging in…
              </>
            ) : (
              <>
                <LogIn className="mr-2 h-4 w-4" />
                Auto login now
              </>
            )}
          </Button>
          {!status.has_totp && (
            <p className="text-xs text-amber-500">
              Set up the Zerodha TOTP key above to enable auto-login.
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            {status.enabled
              ? 'Automatic re-login is ON — the app also re-logs-in on its own if the session expires.'
              : 'One-click login. Automatic re-login is off (set BROKER_AUTO_LOGIN_ENABLED to turn it on).'}
          </p>
        </>
      ) : (
        <>
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <KeyRound className="h-4 w-4" />
            {status.has_credentials
              ? 'Replace Zerodha login credentials'
              : 'Set up auto-login (optional)'}
          </div>
          <Input
            type="text"
            placeholder="Kite user ID (e.g. AB1234)"
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            autoComplete="off"
          />
          <div className="flex gap-2">
            <Input
              type="password"
              placeholder="Kite login password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
            />
            <Button type="button" size="sm" onClick={handleSave} disabled={saving}>
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Save'}
            </Button>
            {status.has_credentials && (
              <Button type="button" variant="ghost" size="sm" onClick={() => setShowSetup(false)}>
                Cancel
              </Button>
            )}
          </div>
          <p className="text-xs text-muted-foreground">
            Stored encrypted on this server and never shown again. Needs the TOTP key above too. The
            manual Connect Account button always works regardless.
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
