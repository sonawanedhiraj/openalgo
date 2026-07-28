import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, Pencil, Plus, RefreshCw, Trash2, Users } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import { type AddAccountPayload, brokerAccountsApi, type ChildAccount } from '@/api/broker-accounts'
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
  return (
    <span className="font-mono text-amber-500" data-testid={`totp-${account.id}`}>
      {data.code.slice(0, 3)} {data.code.slice(3)}
      <span className="text-muted-foreground text-xs ml-1">({data.seconds_remaining}s)</span>
    </span>
  )
}

// Primary-side per-trade sizing used ONLY for the preview line (display, not
// stored): open15 ₹1.5L/slot, sector_follow ₹50k/position, futures 1 NIFTY
// lot ≈ ₹2.8L margin, simplified ₹20k capital base.
const STRATEGY_SLOT_INFO: Record<string, { slot: number; label: string }> = {
  open15_vol_breakout: { slot: 150000, label: 'per slot' },
  sector_follow_cap5_vol: { slot: 50000, label: 'per position' },
  futures_follow_cap50: { slot: 280000, label: 'per lot (margin)' },
  simplified_engine: { slot: 20000, label: 'per trade base' },
}

function StrategyChecklist({
  account,
  knownStrategies,
  primaryBookCapital,
}: {
  account: ChildAccount
  knownStrategies: string[]
  primaryBookCapital: number
}) {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<string[]>(account.strategies)
  const [overrides, setOverrides] = useState<Record<string, number | null>>(() =>
    Object.fromEntries(
      account.strategy_settings.map((s) => [s.strategy_name, s.capital_override_inr])
    )
  )

  const saveMutation = useMutation({
    mutationFn: () => brokerAccountsApi.setStrategies(account.id, selected, overrides),
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
    const capital = overrides[name] ?? account.capital_inr
    const factor = capital / primaryBookCapital
    const info = STRATEGY_SLOT_INFO[name]
    if (!info) return `factor ${factor.toFixed(3)}×`
    const perSlot = Math.round(info.slot * factor)
    if (name === 'futures_follow_cap50' && capital < info.slot) {
      return `factor ${factor.toFixed(3)}× — ⚠ below 1 lot (needs ≥ ₹${info.slot.toLocaleString('en-IN')}): every mirror skips`
    }
    return `factor ${factor.toFixed(3)}× → ≈ ₹${perSlot.toLocaleString('en-IN')} ${info.label}`
  }

  return (
    <div className="mt-2 rounded-md border border-dashed p-3">
      <p className="text-muted-foreground text-xs mb-2">
        Tick a strategy to mirror it. The Capital field appears only for selected strategies — blank
        uses the account's base capital (₹{account.capital_inr.toLocaleString('en-IN')}).
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
                  placeholder="base"
                  value={overrides[name] ?? ''}
                  onChange={(e) =>
                    setOverrides((prev) => ({
                      ...prev,
                      [name]: e.target.value === '' ? null : Number(e.target.value),
                    }))
                  }
                  data-testid={`override-${account.id}-${name}`}
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
  const [capitalDraft, setCapitalDraft] = useState<number | null>(null)
  const [editAccount, setEditAccount] = useState<ChildAccount | null>(null)
  const [editForm, setEditForm] = useState({
    display_name: '',
    broker_client_id: '',
    capital_inr: 0,
    api_key: '',
    api_secret: '',
  })

  const openEdit = (account: ChildAccount) => {
    setEditAccount(account)
    setEditForm({
      display_name: account.display_name,
      broker_client_id: account.broker_client_id ?? '',
      capital_inr: account.capital_inr,
      api_key: '',
      api_secret: '',
    })
  }

  const editMutation = useMutation({
    mutationFn: () =>
      brokerAccountsApi.update(editAccount?.id ?? 0, {
        display_name: editForm.display_name,
        broker_client_id: editForm.broker_client_id,
        capital_inr: editForm.capital_inr,
        ...(editForm.api_key ? { api_key: editForm.api_key } : {}),
        ...(editForm.api_secret ? { api_secret: editForm.api_secret } : {}),
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
      setCapitalDraft(null)
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
            <div className="flex items-center gap-2 text-sm">
              <Label className="text-muted-foreground">Primary book capital (₹)</Label>
              <Input
                className="w-32"
                type="number"
                value={capitalDraft ?? data.primary_book_capital}
                onChange={(e) => setCapitalDraft(Number(e.target.value))}
              />
              {capitalDraft !== null && capitalDraft !== data.primary_book_capital && (
                <Button
                  size="sm"
                  onClick={() => settingsMutation.mutate({ primary_book_capital: capitalDraft })}
                >
                  Save
                </Button>
              )}
            </div>
            <span className="text-muted-foreground text-xs">
              {data.multi_account_enabled
                ? 'LIVE strategy orders mirror to enabled child accounts, scaled by child capital ÷ primary book. Changes apply immediately.'
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
                      {account.capital_inr.toLocaleString('en-IN')} base
                      {account.strategy_settings
                        .filter((s) => s.capital_override_inr !== null)
                        .map(
                          (s) =>
                            ` · ${s.strategy_name.split('_')[0]}: ₹${(s.capital_override_inr as number).toLocaleString('en-IN')}`
                        )
                        .join('')}
                    </div>
                  </div>
                  {account.connected ? (
                    <Badge className="bg-emerald-600">Connected</Badge>
                  ) : (
                    <Badge variant="destructive">Login needed</Badge>
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
                  <StrategyChecklist
                    account={account}
                    knownStrategies={data.known_strategies}
                    primaryBookCapital={data.primary_book_capital}
                  />
                )}
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent>
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
        <DialogContent>
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
            <div>
              <Label>Replace API Key (optional)</Label>
              <Input
                value={editForm.api_key}
                onChange={(e) => setEditForm({ ...editForm, api_key: e.target.value })}
                placeholder="leave blank to keep current"
              />
            </div>
            <div>
              <Label>Replace API Secret (optional)</Label>
              <Input
                type="password"
                value={editForm.api_secret}
                onChange={(e) => setEditForm({ ...editForm, api_secret: e.target.value })}
                placeholder="leave blank to keep current"
              />
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
