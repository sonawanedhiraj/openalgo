import type { ScreenerType } from '@/api/scanner'

interface ParamField {
  key: string
  label: string
  defaultValue: number
  step: number
  min: number
  unit?: string
}

const BUY_FIELDS: ParamField[] = [
  { key: 'gap_pct', label: 'Gap %', defaultValue: 3.0, step: 0.1, min: 0.1, unit: '%' },
  { key: 'atr_pct', label: 'ATR % threshold', defaultValue: 5.0, step: 0.1, min: 0.1, unit: '%' },
  { key: 'vol_5m_mult', label: '5m volume mult', defaultValue: 2.0, step: 0.1, min: 0.1 },
  { key: 'rsi_threshold', label: 'RSI threshold', defaultValue: 50, step: 1, min: 1 },
  { key: 'supertrend_period', label: 'Supertrend period', defaultValue: 7, step: 1, min: 1 },
  { key: 'supertrend_mult', label: 'Supertrend mult', defaultValue: 3.0, step: 0.1, min: 0.1 },
  { key: 'price_min', label: 'Price min', defaultValue: 100, step: 1, min: 1 },
  { key: 'price_max', label: 'Price max', defaultValue: 5000, step: 1, min: 1 },
  { key: 'vol_sma_short', label: 'Vol SMA short', defaultValue: 50, step: 1, min: 1 },
  { key: 'vol_sma_long', label: 'Vol SMA long', defaultValue: 200, step: 1, min: 1 },
]

const SELL_FIELDS: ParamField[] = [
  { key: 'gap_pct', label: 'Gap %', defaultValue: 3.0, step: 0.1, min: 0.1, unit: '%' },
  { key: 'atr_pct', label: 'ATR % threshold', defaultValue: 5.0, step: 0.1, min: 0.1, unit: '%' },
  { key: 'rsi_threshold', label: 'RSI threshold', defaultValue: 50, step: 1, min: 1 },
  { key: 'supertrend_period', label: 'Supertrend period', defaultValue: 7, step: 1, min: 1 },
  { key: 'supertrend_mult', label: 'Supertrend mult', defaultValue: 3.0, step: 0.1, min: 0.1 },
  { key: 'price_min', label: 'Price min', defaultValue: 100, step: 1, min: 1 },
  { key: 'price_max', label: 'Price max', defaultValue: 5000, step: 1, min: 1 },
]

interface ParamFormProps {
  screenerType: ScreenerType
  value: Record<string, number>
  onChange: (params: Record<string, number>) => void
  disabled?: boolean
}

export function ParamForm({ screenerType, value, onChange, disabled = false }: ParamFormProps) {
  const fields = screenerType === 'buy' ? BUY_FIELDS : SELL_FIELDS

  function handleChange(key: string, raw: string) {
    const n = parseFloat(raw)
    if (!Number.isNaN(n)) {
      onChange({ ...value, [key]: n })
    }
  }

  return (
    <div className="grid grid-cols-2 gap-3">
      {fields.map((f) => (
        <div key={f.key} className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground" htmlFor={`param-${f.key}`}>
            {f.label}
            {f.unit ? ` (${f.unit})` : ''}
          </label>
          <input
            id={`param-${f.key}`}
            type="number"
            step={f.step}
            min={f.min}
            value={value[f.key] ?? f.defaultValue}
            onChange={(e) => handleChange(f.key, e.target.value)}
            disabled={disabled}
            className="w-full h-8 rounded-md border border-input bg-background px-2 py-1 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
          />
        </div>
      ))}
    </div>
  )
}
