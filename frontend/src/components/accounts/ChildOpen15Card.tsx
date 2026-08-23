import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ReactNode, useState } from 'react'
import { toast } from 'sonner'
import {
  brokerAccountsApi,
  type Open15AccountStatus,
  type Open15Position,
} from '@/api/broker-accounts'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

// Child open15 verification card (issue #663): per child account — today's
// open15_vol_breakout mirror attempts, whether every traded symbol is flat in
// the CHILD's own broker book once the exit time (default 09:30 IST) has
// passed, and the child's broker-sourced day P&L. The Square off button is the
// manual backstop; its success is a broker ACK, not a fill (the fill
// reconcile verifies afterwards).

function pnlSpan(value: number | null, testId?: string) {
  if (value === null) return <span className="text-muted-foreground">—</span>
  const cls = value > 0 ? 'text-emerald-600' : value < 0 ? 'text-red-600' : ''
  const sign = value > 0 ? '+' : ''
  return (
    <span className={`font-medium ${cls}`} data-testid={testId}>
      {sign}₹{value.toLocaleString('en-IN')}
    </span>
  )
}

function tradeStatusBadge(status: string, errorText: string | null) {
  if (status === 'placed') return <Badge className="bg-emerald-600">placed</Badge>
  if (status.startsWith('skipped')) {
    return <Badge variant="secondary">{status.replace('skipped_', 'skipped: ')}</Badge>
  }
  return (
    <Badge variant="destructive" title={errorText ?? undefined}>
      {status}
    </Badge>
  )
}

function AccountSection({
  account,
  exitTime,
  afterExitTime,
}: {
  account: Open15AccountStatus
  exitTime: string
  afterExitTime: boolean
}) {
  const queryClient = useQueryClient()
  const [confirmTarget, setConfirmTarget] = useState<Open15Position | null>(null)

  const squareOffMutation = useMutation({
    mutationFn: (position: Open15Position) =>
      brokerAccountsApi.squareOff(account.account_id, {
        symbol: position.symbol,
        exchange: position.exchange,
        product: position.product,
      }),
    onSuccess: (result) => {
      toast.success(result.message || 'Square-off placed — broker ACK, verifying fill')
      setConfirmTarget(null)
      queryClient.invalidateQueries({ queryKey: ['open15-child-status'] })
      queryClient.invalidateQueries({ queryKey: ['mirror-orders'] })
    },
    onError: (e: unknown) => {
      const message =
        (e as { response?: { data?: { message?: string } } })?.response?.data?.message ??
        'Square-off failed'
      toast.error(message)
      setConfirmTarget(null)
      queryClient.invalidateQueries({ queryKey: ['open15-child-status'] })
    },
  })

  const openPositions = account.positions.filter((p) => p.open_qty !== 0)
  const hasTrades = account.trades.length > 0

  let statusBadge: ReactNode
  if (!account.connected) {
    statusBadge = <Badge variant="secondary">not connected</Badge>
  } else if (!hasTrades) {
    statusBadge = <Badge variant="outline">no trades today</Badge>
  } else if (!account.positions_readable) {
    statusBadge = <Badge variant="destructive">positions unreadable</Badge>
  } else if (account.open_after_exit) {
    statusBadge = (
      <Badge variant="destructive" data-testid={`open15-open-${account.account_id}`}>
        OPEN past {exitTime}
      </Badge>
    )
  } else if (openPositions.length > 0) {
    statusBadge = <Badge variant="secondary">position open (before {exitTime})</Badge>
  } else {
    statusBadge = (
      <Badge className="bg-emerald-600" data-testid={`open15-closed-${account.account_id}`}>
        All squared off
      </Badge>
    )
  }

  return (
    <div
      className={`rounded-lg border p-3 ${account.open_after_exit ? 'border-red-500' : ''}`}
      data-testid={`open15-account-${account.account_id}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{account.display_name}</span>
        {statusBadge}
        <span className="ml-auto text-xs text-muted-foreground">Day P&L</span>
        {pnlSpan(account.day_pnl, `open15-pnl-${account.account_id}`)}
      </div>

      {hasTrades && (
        <table className="w-full text-sm mt-2">
          <thead>
            <tr className="text-xs text-muted-foreground text-left">
              <th className="font-normal py-1">Time</th>
              <th className="font-normal">Symbol</th>
              <th className="font-normal">Side</th>
              <th className="font-normal">Qty</th>
              <th className="font-normal">Status</th>
              <th className="font-normal text-right">Position</th>
            </tr>
          </thead>
          <tbody>
            {account.trades.map((trade) => {
              const position = account.positions.find(
                (p) => p.symbol === trade.symbol && p.exchange === trade.exchange
              )
              return (
                <tr key={trade.id} className="border-t">
                  <td className="py-1 text-xs text-muted-foreground">
                    {trade.created_at ? trade.created_at.slice(11, 19) : ''}
                  </td>
                  <td>{trade.symbol}</td>
                  <td>
                    <Badge variant={trade.action === 'BUY' ? 'default' : 'outline'}>
                      {trade.action}
                    </Badge>
                  </td>
                  <td>{trade.child_qty}</td>
                  <td>{tradeStatusBadge(trade.status, trade.error_text)}</td>
                  <td className="text-right">
                    {trade.status !== 'placed' || !account.positions_readable ? (
                      <span className="text-muted-foreground text-xs">—</span>
                    ) : position && position.open_qty !== 0 ? (
                      <span className="text-red-600 font-medium">{position.open_qty} OPEN</span>
                    ) : (
                      <span className="text-muted-foreground text-xs">0 (closed)</span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}

      {account.trades.some((t) => t.error_text) && (
        <div className="mt-1 space-y-0.5">
          {account.trades
            .filter((t) => t.error_text)
            .map((t) => (
              <p key={t.id} className="text-xs text-muted-foreground">
                {t.symbol}: {t.error_text}
              </p>
            ))}
        </div>
      )}

      {afterExitTime &&
        account.positions_readable &&
        openPositions.map((position) => (
          <div
            key={`${position.symbol}|${position.product}`}
            className="flex flex-wrap items-center gap-2 mt-2"
          >
            <Button
              size="sm"
              variant="destructive"
              onClick={() => setConfirmTarget(position)}
              disabled={squareOffMutation.isPending}
              data-testid={`squareoff-${account.account_id}-${position.symbol}`}
            >
              Square off {Math.abs(position.open_qty)} {position.symbol}
            </Button>
            <span className="text-xs text-muted-foreground">
              MARKET {position.open_qty > 0 ? 'sell' : 'buy'} · qty re-read from the broker book at
              click
            </span>
          </div>
        ))}

      <Dialog
        open={confirmTarget !== null}
        onOpenChange={(open) => !open && setConfirmTarget(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Square off {confirmTarget?.symbol}?</DialogTitle>
          </DialogHeader>
          <p className="text-sm">
            Places a MARKET {confirmTarget && confirmTarget.open_qty > 0 ? 'SELL' : 'BUY'} for{' '}
            {account.display_name}&apos;s actual held quantity (currently{' '}
            {confirmTarget ? Math.abs(confirmTarget.open_qty) : ''}, re-checked at placement). This
            cannot be undone.
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmTarget(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => confirmTarget && squareOffMutation.mutate(confirmTarget)}
              disabled={squareOffMutation.isPending}
              data-testid="squareoff-confirm"
            >
              {squareOffMutation.isPending ? 'Placing…' : 'Square off'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default function ChildOpen15Card() {
  const { data } = useQuery({
    queryKey: ['open15-child-status'],
    queryFn: () => brokerAccountsApi.open15Status(),
    refetchInterval: 30000,
    retry: false,
  })

  if (!data || data.accounts.length === 0) return null

  return (
    <Card data-testid="open15-child-card">
      <CardHeader>
        <CardTitle className="text-base flex flex-wrap items-center gap-2">
          Child trades — open15_vol_breakout
          <span className="text-xs font-normal text-muted-foreground">
            today · exit {data.exit_time} IST
            {data.after_exit_time ? '' : ` · window open until ${data.exit_time}`}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {data.accounts.map((account) => (
          <AccountSection
            key={account.account_id}
            account={account}
            exitTime={data.exit_time}
            afterExitTime={data.after_exit_time}
          />
        ))}
        <p className="text-xs text-muted-foreground">
          Positions and P&L are read from each child&apos;s own broker book. A square-off success is
          a broker ACK — the fill reconcile verifies it afterwards.
        </p>
      </CardContent>
    </Card>
  )
}
