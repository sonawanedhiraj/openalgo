import { useQuery } from '@tanstack/react-query'
import { brokerAccountsApi, type MirrorOrder } from '@/api/broker-accounts'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

// Multi-account Phase 3 (issue #476): today's child mirror attempts, grouped
// by parent order. Renders NOTHING when there is no mirror activity — the
// card is invisible for single-account installs.

function statusBadge(order: MirrorOrder) {
  switch (order.status) {
    case 'placed':
      return (
        <Badge className="bg-emerald-600">
          {order.account_name} · {order.child_qty} ✓
        </Badge>
      )
    case 'skipped_no_session':
      return <Badge variant="destructive">{order.account_name} · skipped: no session</Badge>
    case 'skipped_zero_qty':
      return <Badge variant="secondary">{order.account_name} · skipped: qty 0</Badge>
    default:
      return (
        <Badge variant="destructive">
          {order.account_name} · {order.status}: {order.error_text ?? '—'}
        </Badge>
      )
  }
}

export default function MirrorOrdersCard() {
  const { data } = useQuery({
    queryKey: ['mirror-orders'],
    queryFn: () => brokerAccountsApi.mirrorOrders(),
    refetchInterval: 30000,
    retry: false,
  })

  if (!data || data.length === 0) return null

  // Group child attempts under their parent order (parent_orderid + symbol).
  const groups = new Map<string, MirrorOrder[]>()
  for (const order of data) {
    const key = `${order.parent_orderid ?? '?'}|${order.symbol}|${order.action}`
    const list = groups.get(key)
    if (list) list.push(order)
    else groups.set(key, [order])
  }

  return (
    <Card data-testid="mirror-orders-card">
      <CardHeader>
        <CardTitle className="text-base">Mirror Orders — today</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="space-y-2">
          {Array.from(groups.entries()).map(([key, orders]) => {
            const first = orders[0]
            return (
              <div key={key} className="flex flex-wrap items-center gap-2 border-b pb-2 text-sm">
                <span className="text-muted-foreground text-xs">
                  {first.created_at ? first.created_at.slice(11, 19) : ''}
                </span>
                <span className="font-medium">{first.symbol}</span>
                <Badge variant={first.action === 'BUY' ? 'default' : 'outline'}>
                  {first.action}
                </Badge>
                <span className="text-muted-foreground text-xs">
                  {first.strategy_name} · parent {first.parent_qty}
                </span>
                <span className="flex flex-wrap gap-1">{orders.map((o) => statusBadge(o))}</span>
              </div>
            )
          })}
        </div>
        <p className="text-muted-foreground text-xs mt-2">
          Exit quantities come from each child's actual held position. Child P&L lives in the
          child's own broker book.
        </p>
      </CardContent>
    </Card>
  )
}
