import {
  Activity,
  ArrowDownCircle,
  ArrowUpCircle,
  Pause,
  Play,
  RefreshCw,
  Webhook,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  type EngineDirection,
  type SimplifiedEngineStatus,
  simplifiedEngineApi,
} from '@/api/simplified-engine'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { showToast } from '@/utils/toast'

interface DirectionCardProps {
  direction: EngineDirection
  enabled: boolean
  symbols: string[]
  onToggle: (next: boolean) => void
  toggling: boolean
}

function DirectionCard({
  direction,
  enabled,
  symbols,
  onToggle,
  toggling,
}: DirectionCardProps) {
  const isBuy = direction === 'BUY'
  const Icon = isBuy ? ArrowUpCircle : ArrowDownCircle
  const accentClass = isBuy
    ? 'border-green-500/40 bg-green-500/5'
    : 'border-red-500/40 bg-red-500/5'
  const iconClass = isBuy ? 'text-green-500' : 'text-red-500'
  const description = isBuy
    ? 'Arms long entries on the lowest-volume RED candle breakout above its open.'
    : 'Arms short entries on the lowest-volume GREEN candle breakdown below its open.'

  return (
    <Card className={`border-2 ${accentClass}`}>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-3">
            <Icon className={`h-7 w-7 ${iconClass}`} />
            <div>
              <CardTitle className="text-xl">{direction} Strategy</CardTitle>
              <CardDescription className="mt-1">{description}</CardDescription>
            </div>
          </div>
          <Badge variant={enabled ? 'default' : 'secondary'}>
            {enabled ? (
              <>
                <Play className="h-3 w-3 mr-1" /> Running
              </>
            ) : (
              <>
                <Pause className="h-3 w-3 mr-1" /> Paused
              </>
            )}
          </Badge>
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        <div className="flex items-center justify-between rounded-md border p-3">
          <div>
            <div className="text-sm font-medium">Enable {direction} entries</div>
            <div className="text-xs text-muted-foreground">
              When off, webhooks for this direction are rejected. Existing positions
              are not closed.
            </div>
          </div>
          <Switch
            checked={enabled}
            disabled={toggling}
            onCheckedChange={onToggle}
            aria-label={`Toggle ${direction}`}
          />
        </div>

        <div>
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-medium">Armed symbols</span>
            <Badge variant="outline">{symbols.length}</Badge>
          </div>
          {symbols.length === 0 ? (
            <div className="text-xs text-muted-foreground">
              No symbols armed yet. Send a Chartink webhook with a {isBuy ? 'BUY' : 'SELL'}-flavoured scan name.
            </div>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {symbols.map((s) => (
                <Badge key={s} variant="secondary" className="font-mono text-xs">
                  {s}
                </Badge>
              ))}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

function ModeBanner({ status }: { status: SimplifiedEngineStatus }) {
  const tone =
    status.mode === 'live'
      ? 'destructive'
      : status.mode === 'analyze'
        ? 'default'
        : 'default'
  const label =
    status.mode === 'live'
      ? 'LIVE TRADING'
      : status.mode === 'analyze'
        ? 'ANALYZER (PAPER)'
        : 'DRY RUN (NO ORDERS)'
  return (
    <Alert variant={tone === 'destructive' ? 'destructive' : 'default'}>
      <Activity className="h-4 w-4" />
      <AlertTitle>{label}</AlertTitle>
      <AlertDescription>
        Trades today: <strong>{status.trades_today}</strong> /{' '}
        {status.max_trades_per_day}. Quote subscriptions:{' '}
        <strong>{status.subscribed_symbols.length}</strong>.
      </AlertDescription>
    </Alert>
  )
}

function PositionsTable({ status }: { status: SimplifiedEngineStatus }) {
  const symbols = Object.keys(status.positions)
  if (symbols.length === 0) {
    return (
      <div className="text-sm text-muted-foreground py-6 text-center">
        No open positions.
      </div>
    )
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase text-muted-foreground border-b">
          <tr>
            <th className="py-2 pr-4">Symbol</th>
            <th className="py-2 pr-4">Side</th>
            <th className="py-2 pr-4">Qty</th>
            <th className="py-2 pr-4">Entry</th>
            <th className="py-2 pr-4">Stop Loss</th>
            <th className="py-2 pr-4">Risk/Share</th>
          </tr>
        </thead>
        <tbody>
          {symbols.map((sym) => {
            const p = status.positions[sym]
            return (
              <tr key={sym} className="border-b last:border-b-0">
                <td className="py-2 pr-4 font-mono">{sym}</td>
                <td className="py-2 pr-4">
                  <Badge
                    variant={p.side === 'LONG' ? 'default' : 'destructive'}
                    className="font-mono"
                  >
                    {p.side}
                  </Badge>
                </td>
                <td className="py-2 pr-4 font-mono">{p.qty}</td>
                <td className="py-2 pr-4 font-mono">{p.entry_price.toFixed(2)}</td>
                <td className="py-2 pr-4 font-mono">{p.stop_loss.toFixed(2)}</td>
                <td className="py-2 pr-4 font-mono">{p.risk_per_share.toFixed(2)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export default function SimplifiedEngine() {
  const [status, setStatus] = useState<SimplifiedEngineStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [togglingDirection, setTogglingDirection] = useState<EngineDirection | null>(
    null
  )

  const fetchStatus = useCallback(async () => {
    try {
      setError(null)
      const data = await simplifiedEngineApi.getStatus()
      setStatus(data)
    } catch (err) {
      const message =
        err instanceof Error ? err.message : 'Failed to load engine status'
      setError(message)
      showToast.error(message, 'chartink')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchStatus()
    const interval = window.setInterval(fetchStatus, 5000)
    return () => window.clearInterval(interval)
  }, [fetchStatus])

  const handleToggle = useCallback(
    async (direction: EngineDirection, next: boolean) => {
      try {
        setTogglingDirection(direction)
        const flags = await simplifiedEngineApi.toggleDirection(direction, next)
        setStatus((prev) =>
          prev ? { ...prev, direction_enabled: { ...prev.direction_enabled, ...flags } } : prev
        )
        showToast.success(
          `${direction} strategy ${next ? 'enabled' : 'paused'}`,
          'chartink'
        )
      } catch (err) {
        const message =
          err instanceof Error ? err.message : 'Failed to toggle strategy'
        showToast.error(message, 'chartink')
      } finally {
        setTogglingDirection(null)
      }
    },
    []
  )

  const buyEnabled = status?.direction_enabled?.BUY ?? false
  const sellEnabled = status?.direction_enabled?.SELL ?? false
  const buySymbols = useMemo(() => status?.buy_symbols ?? [], [status])
  const sellSymbols = useMemo(() => status?.sell_symbols ?? [], [status])

  if (loading) {
    return (
      <div className="container mx-auto py-6 space-y-6">
        <Skeleton className="h-8 w-72" />
        <div className="grid gap-4 md:grid-cols-2">
          <Skeleton className="h-72" />
          <Skeleton className="h-72" />
        </div>
        <Skeleton className="h-48" />
      </div>
    )
  }

  if (error && !status) {
    return (
      <div className="container mx-auto py-6 space-y-4">
        <Alert variant="destructive">
          <AlertTitle>Engine status unavailable</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
        <Button onClick={fetchStatus} variant="outline">
          <RefreshCw className="h-4 w-4 mr-2" />
          Retry
        </Button>
      </div>
    )
  }

  if (!status) return null

  return (
    <div className="container mx-auto py-6 space-y-6">
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Simplified Stock Engine</h1>
          <p className="text-muted-foreground">
            Two intraday strategies sharing one engine. Toggle either independently.
          </p>
        </div>
        <Button onClick={fetchStatus} variant="outline" size="sm">
          <RefreshCw className="h-4 w-4 mr-2" />
          Refresh
        </Button>
      </div>

      <ModeBanner status={status} />

      <Alert>
        <Webhook className="h-4 w-4" />
        <AlertTitle>How to arm symbols</AlertTitle>
        <AlertDescription>
          Create a Chartink strategy and POST screener alerts to{' '}
          <code className="bg-muted px-1 mx-1 rounded">
            /chartink/simplified-stock-engine/&lt;webhook_id&gt;
          </code>
          . The engine reads <strong>scan_name</strong>: contains "BUY" → arms a long
          watch; contains "SELL" / "SHORT" / "COVER" → arms a short watch.
        </AlertDescription>
      </Alert>

      <div className="grid gap-4 md:grid-cols-2">
        <DirectionCard
          direction="BUY"
          enabled={buyEnabled}
          symbols={buySymbols}
          toggling={togglingDirection === 'BUY'}
          onToggle={(v) => handleToggle('BUY', v)}
        />
        <DirectionCard
          direction="SELL"
          enabled={sellEnabled}
          symbols={sellSymbols}
          toggling={togglingDirection === 'SELL'}
          onToggle={(v) => handleToggle('SELL', v)}
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Open positions</CardTitle>
          <CardDescription>
            Live positions tracked by the engine. Pending entries:{' '}
            {status.pending_entries.length}; pending exits: {status.pending_exits.length}.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <PositionsTable status={status} />
        </CardContent>
      </Card>
    </div>
  )
}
