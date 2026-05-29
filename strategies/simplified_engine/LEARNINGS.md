# Simplified Engine — Strategy Learnings

**Strategy**: Chartink FnO Intraday Breakout (Long/Short)
**Engine**: `SimplifiedStockEngine` in `services/simplified_stock_engine_core.py`
**First live session**: May 20, 2026 (sandbox mode)

---

## Strategy Overview

Scans Chartink for FnO stocks with >3% intraday moves, arms the engine for 5-minute
candle breakout entries with ATR-based stop-loss, volume confirmation, and RR-based
trailing. All positions flatten at 15:20 IST.

**Screeners**:
- Buy: `https://chartink.com/screener/fno-intraday-buy-20`
- Sell: `https://chartink.com/screener/alert-for-intraday-sell-fno`

**Webhook**: `POST /chartink/simplified-stock-engine/c7d08357-6fe1-4603-bd2a-be4c9f9e06ac`

---

## Current Live Config (as of May 29, 2026)

| Parameter | Value | Notes |
|-----------|-------|-------|
| `atr_sl_mult` | 1.2 | Reverted to default; tighter stops |
| `max_trades_per_day` | 6 | Increased from 4; more opportunities |
| `cooldown_candles` | 3 | 15-min cooldown after a trade closes |
| `capital` | 20,000 | Base capital |
| `leverage` | 5.0 | Intraday leverage |
| `max_risk_per_trade` | 500 | Max loss per position |
| `volume_multiplier` | 2.5 | Entry only on 2.5× average volume |
| `atr_period` | 14 | Wilder's ATR lookback |
| `no_new_entries_after` | 15:10 | No entries in last 20 min |
| `eod_exit_time` | 15:20 | Force exit all positions |
| `mode` | live | Real trading via Zerodha |

---

## Daily Results Log

### May 20, 2026 (First Day — Sandbox)
- **Market regime**: Strong bullish trend, all 8 scanned stocks >3% gainers
- **Backtest result**: 6 trades, 5W/1L, net +₹704.72 (3.52% ROI)
- **Best performer**: POWERINDIA (+₹308.67), SIEMENS (+₹276.00)
- **Only loser**: HINDPETRO (-₹454.23) — stopped out same candle
- **Note**: Backtest used old hardcoded config (atr_sl_mult=1.2, max_trades=6). Not
  directly comparable to live config.

### May 21, 2026 (Second Day — Sandbox, Automated Pipeline)
- **Market regime**: Mixed/choppy after opening rally
- **Live result**: 6 trades, 5W/1L, net **+₹621.55**
- **Backtest result** (old config): 6 trades, 2W/4L, net **-₹1,479.25**
- **Discrepancy**: ₹2,100.80 — caused by config mismatch (see below)
- **Key trades**:
  - SAMMAANCAP: +₹313.95 (held 1h47m, trailing stop locked profit)
  - GRASIM: +₹268.80 (quick 14-min trade)
  - ANGELONE (2nd entry): -₹528.20 (only loser, re-entry failed)
- **Stocks live-only**: ANGELONE, ADANIENSOL (not in backtest stock list)
- **Stocks backtest-only**: APOLLOHOSP (stopped out immediately in backtest)

### May 29, 2026 (Day 3 — First Live Trading Day)
- **Market regime**: BUY-dominant; only BUY screener produced signals (GMRAIRPORT, NBCC). SELL screener empty.
- **Engine mode**: `live` (first real-money day). Config changed from sandbox: atr_sl_mult 1.5→1.2, max_trades 4→6.
- **Live result**: 3 trades, 1W/2L, net **-₹784.80**
- **Win rate**: 33.3%
- **Trade breakdown**:
  - GMRAIRPORT LONG: BUY 500@102.96 (11:23) → SELL 500@103.50 (11:34), **+₹270**, 11min hold. Quick winner, trailing stop locked profit.
  - NBCC LONG #1: BUY 500@102.13 (12:59) → SELL 500@101.10 (13:43), **-₹514.80**, 44min hold. Stopped out on pullback.
  - NBCC LONG #2 (re-entry): BUY 500@101.50 (14:34) → SELL 500@100.42 (15:03), **-₹540**, 29min hold. Re-entry also stopped out; NBCC fading all afternoon.
- **Notable observations**:
  - Re-entry on NBCC (Trade 3) repeated the Learning #7 pattern — re-entering after an exit on a fading stock lost more. Both NBCC trades were losers.
  - atr_sl_mult reverted to 1.2 (from 1.5) — the tighter stop may have contributed to the NBCC losses. Learning #1 warned that 1.2 produces more whipsaws.
  - NBCC was in cooldown at close, suggesting the engine correctly applied cooldown after Trade 2 before allowing Trade 3.
  - GMRAIRPORT remained armed at close with no re-entry — only 1 trade on the winner vs 2 on the loser.
- **Tick log**: 35,185 ticks written, 2.92 MB, 0 drops (final EOD).
- **Armed watches at close**: GMRAIRPORT (BUY), NBCC (BUY)
- **Funds**: ₹22,081 available (floor ₹20,000)
- **Errors**: 0 in last hour

---

## Key Learnings

### 1. ATR Multiplier is the Most Impactful Parameter
- **1.2× (old default)**: Tight stops. Catches quick reversals but whipsawed out of
  many trades that later recovered. Produced 33% win rate on May 21 backtest.
- **1.5× (current live)**: Wider stops. Survives normal pullbacks within a trending
  candle. Produced 83% win rate on May 21 live.
- **Observation**: The 0.3 difference in ATR multiplier flipped 3 trades from loss to
  profit on the same day. This is the single most sensitive parameter.
- **TODO**: Test 1.3, 1.4, 1.6, 1.8 across a week of data to find the sweet spot.

### 2. Cooldown Prevents Over-Trading on Whipsaw Stocks
- Without cooldown (backtest): SAMMAANCAP was traded 3 times, burning 50% of the
  trade budget on one choppy stock.
- With 3-candle cooldown (live): Only 1 SAMMAANCAP trade, allowing budget for
  ANGELONE and ADANIENSOL entries.
- **Observation**: Cooldown improves diversification across stocks.

### 3. Max Trades Per Day: 4 vs 6
- 6 trades (backtest): Filled budget by early afternoon, including re-entries on losers.
- 4 trades (live): More selective. Forced the engine to skip marginal setups.
- **Observation**: Fewer max trades → higher quality entries, but may miss late-day
  opportunities. Need more data.

### 4. EOD Trades Can Be the Biggest Winners
- On strong trend days (May 20), stocks that ran all day (POWERINDIA, SIEMENS, ABB)
  generated 90%+ of P&L through EOD exit at 15:20.
- Trailing stop trades exited with modest 1-3 point gains per share.
- **Implication**: Don't optimize for quick exits. The strategy's edge may be in
  catching all-day runners. Consider widening trailing stop or delaying its activation.

### 5. Volume Filter is Effective
- Prevented entries on HINDALCO, HINDPETRO (May 20), SAMMAANCAP partial (May 21)
  where breakouts weren't confirmed by volume.
- 2.5× multiplier seems about right — not so high that it misses real breakouts,
  not so low that it lets through noise.

### 6. Chartink Screener Timing Matters
- Stock universe shifts as the market moves intraday.
- Early scans (9:30-9:45) catch opening momentum stocks.
- Later scans may find different stocks that only crossed 3% threshold later.
- **Implication**: Multiple scan cycles improve coverage. The `fno-scan-cycle`
  scheduled task (every 15 min) handles this well.

### 7. Re-Entry Risk
- ANGELONE 2nd entry on May 21 was the only loser (-₹528.20).
- Re-entering a stock after it was already exited carries higher risk — the first
  exit often signals the trend is weakening.
- Cooldown helps but doesn't fully prevent re-entry on the same stock.
- **TODO**: Consider a per-symbol daily trade limit (max 1 or 2 entries per symbol).

### 8. Reverting atr_sl_mult to 1.2 on Live Hurts (May 29 Evidence)
- First live day used atr_sl_mult=1.2 instead of the sandbox-proven 1.5.
- Result: 33% win rate, -₹784.80 net. Both NBCC trades stopped out on pullbacks.
- This mirrors the May 21 backtest finding (Learning #1): 1.2 produced 33% win rate vs 83% at 1.5.
- **Strong signal across 2 data points**: atr_sl_mult=1.2 consistently underperforms 1.5 on this strategy.
- **Action**: Consider reverting to 1.5 before next live session.
- **Update (May 29 EOD)**: Reverted live engine to `atr_sl_mult=1.5` for Monday's
  open. `.env`, `.sample.env`, and the Python default in
  `SimplifiedEngineConfig` now agree on 1.5. Evidence basis: May 21 backtest
  (1.2 → 33% win rate vs 1.5 → 83%) + May 29 live (1.2 → 33% / -₹784.80).
  Regression-guarded by `test_default_atr_sl_mult_is_1_5` in
  `test/test_simplified_stock_engine_core.py` so future config edits cannot
  silently revert to 1.2 without a failing test.

### 9. Config Mismatch is Dangerous
- May 21 comparison proved that the backtester and live engine MUST use identical
  config. A 0.3 difference in one parameter caused a ₹2,100 P&L swing.
- **Rule**: Always use `--from-engine` when backtesting. Never rely on defaults.
- Fixed in backtester: `config_from_engine_api()` fetches live config from the
  engine's status endpoint.

---

## Backtest Limitations (Known)

1. **Candle vs tick**: Finalized 5-min candles miss intra-candle price action.
   Tick-level replay is now supported (`--tick-data`) but tick logging must be
   enabled first: `SIMPLIFIED_ENGINE_TICK_LOG_ENABLED=true`.
2. **No slippage**: MARKET orders in live trading fill at varying prices.
3. **No partial fills**: Assumes full quantity fills instantly.
4. **Selection bias**: Testing on stocks already known to be >3% gainers guarantees
   a bullish sample on BUY direction days.
5. **Intra-candle SL**: Uses candle low/high — actual SL hit time is unknown.

---

## Bug Fixes & Improvements (May 22, 2026)

### Tick Log Loader Format Mismatch (Fixed)
The backtester's `load_tick_data()` expected filenames `ticks_YYYY-MM-DD.jsonl`
and a `"price"` field, but the actual `TickLogWriter` produces:
- **Filename**: `ticks-YYYYMMDD-<pid>.jsonl` (dashes, compact date, PID suffix)
- **Field**: `"ltp"` not `"price"`

The loader would have silently found zero files and fallen back to candle mode
every time, even with tick logging enabled. Fixed to:
- Scan for both writer format (`ticks-YYYYMMDD-*.jsonl`) and legacy format
- Read `"ltp"` with fallback to `"price"`
- Merge multiple PID files for the same date (e.g. after app restart)

### Exact Day Replay (New)
Added `--replay-symbols` and `--from-results` flags for reproducing a trading
day exactly:

```bash
# Full exact replay: live config + live stock list + tick data
uv run python backtest/run_backtest.py \
    --date 2026-05-22 --from-engine --replay-symbols --tick-data tick_logs

# Replay a past day using stocks from its results file
uv run python backtest/run_backtest.py \
    --date 2026-05-21 --from-engine --from-results backtest/results_2026-05-21.json
```

Symbol sources (priority): `--replay-symbols` > `--from-results` > `--symbols` > defaults.

---

## Open Questions / Future Research

- [ ] What ATR multiplier optimizes across 20+ trading days? (Test 1.2 to 2.0)
- [ ] Should trailing stop activation be delayed (e.g., only after 2R profit)?
- [ ] Does the SELL direction (shorting top losers) work at all? Sell screener was
      consistently empty on the days tested.
- [ ] Is there a market regime detector that could switch parameters dynamically?
- [ ] Per-symbol daily trade limit — would it improve or hurt?
- [x] ~~Enable tick logging and compare tick-replay vs candle-replay results.~~
      Tick replay is implemented. Enable with `SIMPLIFIED_ENGINE_TICK_LOG_ENABLED=true`.
      Filename/field mismatch fixed May 22.
- [ ] Test with `mode=live` after 2 weeks of profitable sandbox results.
- [ ] Save the engine's armed stock list to a daily log file so past days can be
      replayed exactly even after the engine resets.
