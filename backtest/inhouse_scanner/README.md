# In-house scanner → simplified engine backtest (R56)

Feeds historical bars to the **real** in-house Chartink-mirror scanner rules,
collects the timed BUY/SELL signals, then runs them through the **real**
`SimplifiedStockEngine`. End-to-end production code, not a re-implementation.

Full write-up + results:
[`docs/research/strategy/screener/2026-07-16_r56_inhouse_scanner_engine_backtest.md`](../../docs/research/strategy/screener/2026-07-16_r56_inhouse_scanner_engine_backtest.md).

## Two stages

```bash
# Stage 1 — replay the REAL scanner rules over history → signals.parquet
uv run python -m backtest.inhouse_scanner.replay_signals --start 2025-06-20 --end 2026-07-06

# Stage 2 — run the signals through the real engine → trades.parquet + summary.json
uv run python -m backtest.inhouse_scanner.replay_engine --signals signals.parquet
```

Helpful flags: `--symbols A,B,C` and `--limit-symbols N` (Stage 1) for quick
smoke runs; `--suffix _x` on both to avoid clobbering the canonical outputs.

## How it stays faithful to live

- **Stage 1** calls `services/scan_rules/fno_intraday_{buy,sell}_chartink.py`
  `rule()` directly. Point-in-time causal frames: continuous 5m/15m from 1m
  (rolling windows like `ScannerService._append_bar`), daily/weekly settled only
  through D-1 with a `timestamp` column (production `derive_today_and_yest` Path
  B). The rule's `datetime.now(IST)` is frozen to each simulated 5m-bar-close.
  Gap % / price band / SMA / ATR / RSI / Supertrend params resolve from the same
  env the live scanner reads. A day-constant open-gate + gate-1 gap pre-filter
  (strict necessary conditions) makes it tractable without dropping any real fire.
- **Stage 2** arms the real `SimplifiedStockEngine` (`mode=disabled`,
  `config_from_env`) at each fire time and replays that day's 5m candles; one
  engine per day shares the global risk state (`max_trades_per_day`, cooldown,
  same-day stop-out block, global profit lock) exactly like the live singleton.

## Data

Reads the app-independent `outputs/tod_volume_gate/prices.duckdb` (broker-sourced
1m + daily) — `db/historify.duckdb` is held open read-write by OpenAlgo and cannot
be read by an external process. 1m coverage 2025-06-20 → 2026-07-06 (258 trading
days, 211 symbols); daily from 2024-06 (full SMA200 depth). Read-only; writes only
this directory's parquet/json.

## Result (R56): REJECT

3,878 signals → 1,177 trades, net **−₹251,526**, **0/14 green months**. The scanner
reproduces Chartink selection (validated 10/10 on 2026-07-03) but the signals have
no exploitable *intraday* edge — they fire late (median ~13:45 IST) after the move;
the only measured edge is a small **BUY overnight drift** (+0.106%/T+1) an intraday
engine can't capture. SELL is anti-predictive overnight. Charges deepen a gross
loss. See the report for the full breakdown.
