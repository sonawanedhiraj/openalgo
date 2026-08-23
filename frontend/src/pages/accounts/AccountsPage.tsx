import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, Pencil, Plus, RefreshCw, Trash2, Users } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import { type AddAccountPayload, brokerAccountsApi, type ChildAccount } from '@/api/broker-accounts'
import ChildOpen15Card from '@/components/accounts/ChildOpen15Card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'

// Multi-account child accounts page (issue #468, Phase 1).
// Children mirror the primary's strategy orders (fan-out ships in Phase 2);
// this page manages setup, daily login, TOTP and strategy selection.

function TotpCell({ account }: { account: ChildAccount }) {
  const { data } = useQuery({
    queryKey: ['broker-accounts', account.id, 'totp'],
    queryFn: () => brokerAccountsApi.totpCode(account.id),
    enabled: account.has_totp_secret,
    refetchInterval: 5000,
    retry: false,
  })
  if (!account.has_totp_secret) {
    return <span className="text-muted-foreground text-xs">not enrolled</span>
  }
  if (!data) return <span className="text-muted-foreground text-xs">…</span>
  // The code is copied UNSPACED — Kite's 2FA field rejects the display spacing.
  const copyCode = () => {
    navigator.clipboard.writeText(data.code)
    toast.success('TOTP code copied')
  }
  return (
    <span className="flex items-center gap-1" data-testid={`totp-${account.id}`}>
      <span className="font-mono text-amber-500">
        {data.code.slice(0, 3)} {data.code.slice(3)}
        <span className="text-muted-foreground text-xs ml-1">({data.seconds_remaining}s)</span>
      </span>
      <Button
        variant="ghost"
        size="sm"
        className="h-6 px-1"
        title="Copy TOTP code"
        aria-label={`Copy TOTP code for ${account.display_name}`}
        onClick={copyCode}
        data-testid={`copy-totp-${account.id}`}
      >
        <Copy className="h-3 w-3" />
      </Button>
    </span>
  )
}

// Max concurrent positions per strategy (display-only, for the worst-case
// exposure line): open15 6 slots, sector_follow 5 positions, futures ~2 lots
// under the 50% cap, simplified engine ~3 trades/day.
const STRATEGY_MAX_CONCURRENT: Record<string, number> = {
  open15_vol_breakout: 6,
  sector_follow_cap5_vol: 5,
  futures_follow_cap50: 2,
  simplified_engine: 3,
}

function StrategyChecklist({
  account,
  knownStrategies,
}: {
  account: ChildAccount
  knownStrategies: string[]
}) {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<string[]>(account.strategies)
  const [perTrade, setPerTrade] = useState<Record<string, number | null>>(() =>
    Object.fromEntries(
      account.strategy_settings.map((s) => [s.strategy_name, s.capital_per_trade_inr])
    )
  )

  const saveMutation = useMutation({
    mutationFn: () => brokerAccountsApi.setStrategies(account.id, selected, perTrade),
    onSuccess: () => {
      toast.success(`Strategies saved for ${account.display_name}`)
      queryClient.invalidateQueries({ queryKey: ['broker-accounts'] })
    },
    onError: (e: unknown) => {
      const message =
        (e as { response?: { data?: { message?: string } } })?.response?.data?.message ??
        'Failed to save strategies'
      toast.error(message)
    },
  })

  const preview = (name: string) => {
    const value = perTrade[name]
    if (!value || value <= 0) {
      return '\u26a0 not set \u2014 mirrors for this strategy will be SKIPPED until a \u20b9/trade is set'
    }
    const maxConcurrent = STRATEGY_MAX_CONCURRENT[name]
    if (!maxConcurrent) return `\u20b9${value.toLocaleString('en-IN')} per trade`
    const worstCase = value * maxConcurrent
    return `\u20b9${value.toLocaleString('en-IN')} per trade \u00d7 up to ${maxConcurrent} concurrent = \u20b9${worstCase.toLocaleString('en-IN')} worst case`
  }

  return (
    <div className="mt-2 rounded-md border border-dashed p-3">
      <p className="text-muted-foreground text-xs mb-2">
        Tick a strategy to mirror it, then set the ₹ this account deploys PER TRADE of that strategy
        — quantity is computed from this capital and the live price at mirror time. Unset = mirrors
        skipped (loudly).
      </p>
      {knownStrategies.map((name) => {
        const isSelected = selected.includes(name)
        return (
          <div
            key={name}
            className="flex flex-wrap items-center gap-2 py-1.5 text-sm border-b last:border-b-0"
          >
            <label className="flex items-center gap-2 min-w-52">
              <Checkbox
                checked={isSelected}
                onCheckedChange={(checked) =>
                  setSelected((prev) =>
                    checked ? [...prev, name] : prev.filter((n) => n !== name)
                  )
                }
              />
              {name}
            </label>
            {isSelected && (
              <>
                <Input
                  className="w-28 h-8"
                  type="number"
                  placeholder="\u20b9 per trade"
                  value={perTrade[name] ?? ''}
                  onChange={(e) =>
                    setPerTrade((prev) => ({
                      ...prev,
                      [name]: e.target.value === '' ? null : Number(e.target.value),
                    }))
                  }
                  data-testid={`pertrade-${account.id}-${name}`}
                />
                <span className="text-muted-foreground text-xs">{preview(name)}</span>
              </>
            )}
          </div>
        )
      })}
      <Button
        size="sm"
        className="mt-2"
        onClick={() => saveMutation.mutate()}
        disabled={saveMutation.isPending}
      >
        Save
      </Button>
    </div>
  )
}

const EMPTY_FORM: AddAccountPayload = {
  display_name: '',
  broker_client_id: '',
  api_key: '',
  api_secret: '',
  capital_inr: 0,
}

export default function AccountsPage() {
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [addOpen, setAddOpen] = useState(false)
  const [form, setForm] = useState<AddAccountPayload>(EMPTY_FORM)
  const [totpAccount, setTotpAccount] = useState<ChildAccount | null>(null)
  const [totpSecret, setTotpSecret] = useState('')
  const [expanded, setExpanded] = useState<number | null>(null)
  const [editAccount, setEditAccount] = useState<ChildAccount | null>(null)
  // Credential replacement is opt-in (issue #492): the fields do not exist in
  // the DOM until this is ticked, so the browser cannot autofill a saved login
  // into them and a routine capital edit can never overwrite working keys.
  const [replaceCreds, setReplaceCreds] = useState(false)
  const [editForm, setEditForm] = useState({
    display_name: '',
    broker_client_id: '',
    capital_inr: 0,
    api_key: '',
    api_secret: '',
    password: '',
  })

  const openEdit = (account: ChildAccount) => {
    setEditAccount(account)
    setReplaceCreds(false)
    setEditForm({
      display_name: account.display_name,
      broker_client_id: account.broker_client_id ?? '',
      capital_inr: account.capital_inr,
      api_key: '',
      api_secret: '',
      password: '',
    })
  }

  const editMutation = useMutation({
    mutationFn: () =>
      brokerAccountsApi.update(editAccount?.id ?? 0, {
        display_name: editForm.display_name,
        broker_client_id: editForm.broker_client_id,
        capital_inr: editForm.capital_inr,
        ...(replaceCreds && editForm.api_key ? { api_key: editForm.api_key } : {}),
        ...(replaceCreds && editForm.api_secret ? { api_secret: editForm.api_secret } : {}),
        ...(replaceCreds && editForm.password ? { password: editForm.password } : {}),
      }),
    onSuccess: () => {
      toast.success('Account updated')
      setEditAccount(null)
      queryClient.invalidateQueries({ queryKey: ['broker-accounts'] })
    },
    onError: (e: unknown) => {
      const message =
        (e as { response?: { data?: { message?: string } } })?.response?.data?.message ??
        'Failed to update account'
      toast.error(message)
    },
  })

  const autoToggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      brokerAccountsApi.update(id, { auto_login_enabled: enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['broker-accounts'] }),
    onError: (e: unknown) => {
      const message =
        (e as { response?: { data?: { message?: string } } })?.response?.data?.message ??
        'Failed to update auto-login'
      toast.error(message)
    },
  })

  const autoLoginMutation = useMutation({
    mutationFn: (id: number) => brokerAccountsApi.autoLogin(id),
    onSuccess: (result) => {
      if (result.ok) {
        toast.success('Auto-login succeeded')
        queryClient.invalidateQueries({ queryKey: ['broker-accounts'] })
      } else {
        toast.error(result.message || 'Auto-login failed — use Connect →')
      }
    },
    onError: (e: unknown) => {
      const message =
        (e as { response?: { data?: { message?: string } } })?.response?.data?.message ??
        'Auto-login failed — use Connect →'
      toast.error(message)
    },
  })

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['broker-accounts'],
    queryFn: brokerAccountsApi.overview,
  })

  const settingsMutation = useMutation({
    mutationFn: (payload: { enabled?: boolean; primary_book_capital?: number }) =>
      brokerAccountsApi.updateSettings(payload),
    onSuccess: (settings) => {
      toast.success(
        settings.enabled
          ? 'Mirror trading ENABLED — applies immediately'
          : 'Mirror trading disabled'
      )
      queryClient.invalidateQueries({ queryKey: ['broker-accounts'] })
    },
    onError: (e: unknown) => {
      const message =
        (e as { response?: { data?: { message?: string } } })?.response?.data?.message ??
        'Failed to update settings'
      toast.error(message)
    },
  })

  // Surface callback results (?connected=N / ?error=...) once, then clean the URL.
  useEffect(() => {
    const connected = searchParams.get('connected')
    const error = searchParams.get('error')
    if (connected) toast.success('Child account connected')
    if (error) toast.error(`Child account login failed: ${error}`)
    if (connected || error) setSearchParams({}, { replace: true })
  }, [searchParams, setSearchParams])

  const addMutation = useMutation({
    mutationFn: () => brokerAccountsApi.add(form),
    onSuccess: () => {
      toast.success('Account added (starts disabled)')
      setAddOpen(false)
      setForm(EMPTY_FORM)
      queryClient.invalidateQueries({ queryKey: ['broker-accounts'] })
    },
    onError: (e: unknown) => {
      const message =
        (e as { response?: { data?: { message?: string } } })?.response?.data?.message ??
        'Failed to add account'
      toast.error(message)
    },
  })

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      brokerAccountsApi.update(id, { is_enabled: enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['broker-accounts'] }),
    onError: () => toast.error('Failed to update account'),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: number) => brokerAccountsApi.remove(id),
    onSuccess: () => {
      toast.success('Account deleted')
      queryClient.invalidateQueries({ queryKey: ['broker-accounts'] })
    },
    onError: () => toast.error('Failed to delete account'),
  })

  const totpMutation = useMutation({
    mutationFn: () => brokerAccountsApi.update(totpAccount?.id ?? 0, { totp_secret: totpSecret }),
    onSuccess: () => {
      toast.success('TOTP secret saved')
      setTotpAccount(null)
      setTotpSecret('')
      queryClient.invalidateQueries({ queryKey: ['broker-accounts'] })
    },
    onError: (e: unknown) => {
      const message =
        (e as { response?: { data?: { message?: string } } })?.response?.data?.message ??
        'Failed to save TOTP secret'
      toast.error(message)
    },
  })

  const handleConnect = async (account: ChildAccount) => {
    try {
      const url = await brokerAccountsApi.loginUrl(account.id)
      window.open(url, '_blank', 'noopener')
    } catch {
      toast.error('Could not build the login URL')
    }
  }

  const copyRedirectUrl = (account: ChildAccount) => {
    navigator.clipboard.writeText(`${window.location.origin}${account.redirect_url_hint}`)
    toast.success('Redirect URL copied — paste it into the Kite Connect app settings')
  }

  return (
    <div className="container mx-auto p-4 space-y-4" data-testid="accounts-page">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <Users className="h-6 w-6" /> Accounts
        </h1>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => refetch()}>
            <RefreshCw className="h-4 w-4" />
          </Button>
          <Button size="sm" onClick={() => setAddOpen(true)} data-testid="add-account-btn">
            <Plus className="h-4 w-4 mr-1" /> Add Account
          </Button>
        </div>
      </div>

      {data && (
        <Card data-testid="mirror-settings-card">
          <CardContent className="pt-4 flex flex-wrap items-center gap-4">
            <div className="flex items-center gap-2">
              <Switch
                checked={data.multi_account_enabled}
                onCheckedChange={(checked) => settingsMutation.mutate({ enabled: checked })}
                data-testid="mirror-master-switch"
              />
              <span className="font-medium">
                Mirror trading {data.multi_account_enabled ? 'ENABLED' : 'disabled'}
              </span>
            </div>
            <span className="text-muted-foreground text-xs">
              {data.multi_account_enabled
                ? 'LIVE strategy orders mirror to enabled child accounts, sized by each child\u2019s \u20b9-per-trade setting at live prices. Changes apply immediately.'
                : 'No orders are mirrored while disabled. Accounts can still be set up and logged in.'}
            </span>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            Primary Account <Badge variant="outline">PRIMARY</Badge>
          </CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          The primary broker account runs the WebSocket feed, historical data, scanner and all
          strategy signals. It is configured via <code>.env</code> and the usual{' '}
          <a href="/broker" className="underline">
            /broker
          </a>{' '}
          login — not editable here. Child accounts below only mirror its strategy orders.
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Child Accounts</CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
          {data && data.accounts.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No child accounts yet. Add one to mirror strategy trades into another family account.
            </p>
          )}
          <div className="space-y-3">
            {data?.accounts.map((account) => (
              <div
                key={account.id}
                className="rounded-lg border p-3"
                data-testid={`account-${account.id}`}
              >
                <div className="flex flex-wrap items-center gap-3">
                  <div className="min-w-40">
                    <div className="font-medium">{account.display_name}</div>
                    <div className="text-xs text-muted-foreground">
                      {account.broker_client_id || '—'} · ₹
                      {account.capital_inr.toLocaleString('en-IN')} funds
                      {account.strategy_settings
                        .filter((s) => s.capital_per_trade_inr !== null)
                        .map(
                          (s) =>
                            ` · ${s.strategy_name.split('_')[0]}: ₹${(s.capital_per_trade_inr as number).toLocaleString('en-IN')}/trade`
                        )
                        .join('')}
                    </div>
                  </div>
                  {account.connected ? (
                    <Badge className="bg-emerald-600">Connected</Badge>
                  ) : (
                    <Badge variant="destructive">Login needed</Badge>
                  )}
                  {(!account.api_key_masked || !account.has_api_secret) && (
                    <Badge variant="destructive" data-testid={`no-creds-${account.id}`}>
                      credentials missing
                    </Badge>
                  )}
                  {account.today_mirrors &&
                    (account.today_mirrors.placed > 0 ||
                      account.today_mirrors.skipped > 0 ||
                      account.today_mirrors.failed > 0) && (
                      <span className="flex gap-1 text-xs" data-testid={`mirrors-${account.id}`}>
                        <Badge variant="outline">today: {account.today_mirrors.placed} ✓</Badge>
                        {account.today_mirrors.skipped > 0 && (
                          <Badge variant="secondary">{account.today_mirrors.skipped} skipped</Badge>
                        )}
                        {account.today_mirrors.failed > 0 && (
                          <Badge variant="destructive">{account.today_mirrors.failed} failed</Badge>
                        )}
                      </span>
                    )}
                  <TotpCell account={account} />
                  {!account.connected && (
                    <Button size="sm" onClick={() => handleConnect(account)}>
                      Connect →
                    </Button>
                  )}
                  {!account.connected && account.has_password && account.has_totp_secret && (
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => autoLoginMutation.mutate(account.id)}
                      disabled={autoLoginMutation.isPending}
                      data-testid={`auto-login-${account.id}`}
                    >
                      Auto login
                    </Button>
                  )}
                  {account.has_password && (
                    <div
                      className="flex items-center gap-1 text-xs text-muted-foreground"
                      title="Automatically re-login this child when its session expires (needs the master switch on /broker on)"
                    >
                      Auto
                      <Switch
                        checked={account.auto_login_enabled}
                        onCheckedChange={(checked) =>
                          autoToggleMutation.mutate({ id: account.id, enabled: checked })
                        }
                        data-testid={`auto-toggle-${account.id}`}
                      />
                    </div>
                  )}
                  <div className="flex items-center gap-1 text-xs text-muted-foreground">
                    Enabled
                    <Switch
                      checked={account.is_enabled}
                      onCheckedChange={(checked) =>
                        toggleMutation.mutate({ id: account.id, enabled: checked })
                      }
                    />
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => openEdit(account)}
                    data-testid={`edit-${account.id}`}
                  >
                    <Pencil className="h-3 w-3 mr-1" /> Edit
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setExpanded(expanded === account.id ? null : account.id)}
                  >
                    Strategies ({account.strategies.length})
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setTotpAccount(account)
                      setTotpSecret('')
                    }}
                  >
                    TOTP…
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => copyRedirectUrl(account)}>
                    <Copy className="h-3 w-3 mr-1" /> Redirect URL
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="text-destructive"
                    onClick={() => {
                      if (window.confirm(`Delete account "${account.display_name}"?`)) {
                        deleteMutation.mutate(account.id)
                      }
                    }}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
                {expanded === account.id && data && (
                  <StrategyChecklist account={account} knownStrategies={data.known_strategies} />
                )}
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      <ChildOpen15Card />

      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent className="max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Add Child Account</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <Label>Display name</Label>
              <Input
                value={form.display_name}
                onChange={(e) => setForm({ ...form, display_name: e.target.value })}
                placeholder="Dad — Zerodha"
              />
            </div>
            <div>
              <Label>Broker Client ID</Label>
              <Input
                value={form.broker_client_id}
                onChange={(e) => setForm({ ...form, broker_client_id: e.target.value })}
                placeholder="AB1234"
              />
            </div>
            <div>
              <Label>Kite Connect API Key</Label>
              <Input
                name="child-kite-api-key"
                autoComplete="off"
                value={form.api_key}
                onChange={(e) => setForm({ ...form, api_key: e.target.value })}
              />
              <p className="text-xs text-muted-foreground mt-1">
                From a new app under the OPERATOR's developer profile (developers.kite.trade) — the
                IP whitelist is profile-level. Stored encrypted, write-only.
              </p>
            </div>
            <div>
              <Label>Kite Connect API Secret</Label>
              <Input
                type="password"
                name="child-kite-api-secret"
                autoComplete="new-password"
                value={form.api_secret}
                onChange={(e) => setForm({ ...form, api_secret: e.target.value })}
              />
            </div>
            <div>
              <Label>Capital (₹)</Label>
              <Input
                type="number"
                value={form.capital_inr || ''}
                onChange={(e) => setForm({ ...form, capital_inr: Number(e.target.value) })}
                placeholder="250000"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setAddOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => addMutation.mutate()}
              disabled={addMutation.isPending}
              data-testid="save-account-btn"
            >
              Save (starts disabled)
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={editAccount !== null} onOpenChange={(open) => !open && setEditAccount(null)}>
        <DialogContent className="max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Edit — {editAccount?.display_name}</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div>
              <Label>Display name</Label>
              <Input
                value={editForm.display_name}
                onChange={(e) => setEditForm({ ...editForm, display_name: e.target.value })}
              />
            </div>
            <div>
              <Label>Broker Client ID</Label>
              <Input
                value={editForm.broker_client_id}
                onChange={(e) => setEditForm({ ...editForm, broker_client_id: e.target.value })}
              />
            </div>
            <div>
              <Label>Base capital (₹)</Label>
              <Input
                type="number"
                value={editForm.capital_inr || ''}
                onChange={(e) => setEditForm({ ...editForm, capital_inr: Number(e.target.value) })}
                data-testid="edit-capital"
              />
              <p className="text-xs text-muted-foreground mt-1">
                Default sizing for every mirrored strategy: factor = base capital ÷ primary book.
                Per-strategy overrides live in the Strategies panel.
              </p>
            </div>
            <div className="rounded-md border p-3 space-y-2">
              <div className="text-sm" data-testid="stored-credentials">
                <div>
                  <span className="text-muted-foreground">Stored API key: </span>
                  {editAccount?.api_key_masked ? (
                    <code className="font-mono">{editAccount.api_key_masked}</code>
                  ) : (
                    <span className="text-destructive">none on file</span>
                  )}
                </div>
                <div>
                  <span className="text-muted-foreground">Stored API secret: </span>
                  {editAccount?.has_api_secret ? (
                    <span>stored</span>
                  ) : (
                    <span className="text-destructive">none on file</span>
                  )}
                </div>
                <div>
                  <span className="text-muted-foreground">Login password: </span>
                  {editAccount?.has_password ? (
                    <span>stored</span>
                  ) : (
                    <span className="text-muted-foreground">not set (manual login)</span>
                  )}
                </div>
              </div>
              <label className="flex items-center gap-2 text-sm">
                <Checkbox
                  checked={replaceCreds}
                  onCheckedChange={(checked) => {
                    const on = checked === true
                    setReplaceCreds(on)
                    if (!on) setEditForm((prev) => ({ ...prev, api_key: '', api_secret: '' }))
                  }}
                  data-testid="replace-creds"
                />
                Replace API key / secret
              </label>
              {replaceCreds ? (
                <>
                  <div>
                    <Label>New API Key</Label>
                    <Input
                      name="child-kite-api-key"
                      autoComplete="off"
                      value={editForm.api_key}
                      onChange={(e) => setEditForm({ ...editForm, api_key: e.target.value })}
                      placeholder="from developers.kite.trade"
                      data-testid="edit-api-key"
                    />
                  </div>
                  <div>
                    <Label>New API Secret</Label>
                    <Input
                      type="password"
                      name="child-kite-api-secret"
                      autoComplete="new-password"
                      value={editForm.api_secret}
                      onChange={(e) => setEditForm({ ...editForm, api_secret: e.target.value })}
                      placeholder="from developers.kite.trade"
                      data-testid="edit-api-secret"
                    />
                  </div>
                  <div>
                    <Label>Kite login password (for auto-login)</Label>
                    <Input
                      type="password"
                      name="child-kite-password"
                      autoComplete="new-password"
                      value={editForm.password}
                      onChange={(e) => setEditForm({ ...editForm, password: e.target.value })}
                      placeholder="optional — enables headless auto-login"
                      data-testid="edit-password"
                    />
                    <p className="text-muted-foreground text-xs mt-1">
                      Stored encrypted, write-only. With this + the TOTP secret set, the child gets
                      an "Auto login" button and is re-logged-in automatically when
                      BROKER_AUTO_LOGIN_ENABLED is on.
                    </p>
                  </div>
                  <p className="text-muted-foreground text-xs">
                    All fields are optional — a blank field keeps the stored value. Type these
                    yourself; never accept a browser autofill here.
                  </p>
                </>
              ) : (
                <p className="text-muted-foreground text-xs">
                  Credentials are left untouched. Tick the box only when you actually want to
                  replace them.
                </p>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditAccount(null)}>
              Cancel
            </Button>
            <Button onClick={() => editMutation.mutate()} disabled={editMutation.isPending}>
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={totpAccount !== null} onOpenChange={(open) => !open && setTotpAccount(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>TOTP helper — {totpAccount?.display_name}</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              Paste the TOTP secret from this Zerodha account's external-2FA enrollment. The rolling
              6-digit code will show on the Accounts page. Write-only after save.
            </p>
            <div>
              <Label>TOTP secret (base32)</Label>
              <Input
                value={totpSecret}
                onChange={(e) => setTotpSecret(e.target.value)}
                placeholder="JBSWY3DPEHPK3PXP"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setTotpAccount(null)}>
              Cancel
            </Button>
            <Button onClick={() => totpMutation.mutate()} disabled={totpMutation.isPending}>
              Save secret
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
