import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, Plus, RefreshCw, Trash2, Users } from 'lucide-react'
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

function StrategyChecklist({
  account,
  knownStrategies,
}: {
  account: ChildAccount
  knownStrategies: string[]
}) {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<string[]>(account.strategies)

  const saveMutation = useMutation({
    mutationFn: () => brokerAccountsApi.setStrategies(account.id, selected),
    onSuccess: () => {
      toast.success(`Strategies saved for ${account.display_name}`)
      queryClient.invalidateQueries({ queryKey: ['broker-accounts'] })
    },
    onError: () => toast.error('Failed to save strategies'),
  })

  return (
    <div className="mt-2 rounded-md border border-dashed p-3">
      <p className="text-muted-foreground text-xs mb-2">
        Select which strategies this account mirrors. No selection = no mirror orders.
      </p>
      {knownStrategies.map((name) => (
        <label key={name} className="flex items-center gap-2 py-1 text-sm">
          <Checkbox
            checked={selected.includes(name)}
            onCheckedChange={(checked) =>
              setSelected((prev) => (checked ? [...prev, name] : prev.filter((n) => n !== name)))
            }
          />
          {name}
        </label>
      ))}
      <Button
        size="sm"
        className="mt-2"
        onClick={() => saveMutation.mutate()}
        disabled={saveMutation.isPending}
      >
        Save selection
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

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['broker-accounts'],
    queryFn: brokerAccountsApi.overview,
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

      {data && !data.multi_account_enabled && (
        <div className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm text-amber-600 dark:text-amber-400">
          Mirror trading is disabled (<code>MULTI_ACCOUNT_ENABLED=false</code>). Accounts can be set
          up and logged in, but no orders will be mirrored until the operator enables the flag
          (order fan-out ships in Phase 2).
        </div>
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
                      {account.capital_inr.toLocaleString('en-IN')}
                    </div>
                  </div>
                  {account.connected ? (
                    <Badge className="bg-emerald-600">Connected</Badge>
                  ) : (
                    <Badge variant="destructive">Login needed</Badge>
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
