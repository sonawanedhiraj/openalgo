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

### May 22, 2026 (Third Day — Sandbox)
- **Market regime**: Mixed — SELL screener produced signals (GLENMARK), BUY side had
  broad universe (DIXON, VBL, ASTRAL, SAMMAANCAP, KAYNES, MFSL)
- **Live result**: 4 trades (max hit), 3W/1L, net **+₹365.30**, win rate **75%**
- **Trade breakdown**:
  - DIXON (LONG): BUY 7 @ ₹11,822 → SELL @ ₹11,748 | **-₹518.00** | Only loser, large-cap SL hit
  - GLENMARK (SHORT #1): SELL 32 @ ₹2,293.10 → BUY @ ₹2,287.20 | **+₹188.80** | 44-min hold
  - GLENMARK (SHORT #2): SELL 38 @ ₹2,281.00 → BUY @ ₹2,270.80 | **+₹387.60** | ~1h51m hold, best trade
  - VBL (LONG): BUY 186 @ ₹537.25 → SELL @ ₹538.90 | **+₹306.90** | Quick 12-min scalp
- **SELL direction worked**: GLENMARK was the first productive SHORT trade — two wins
  from the sell screener accounting for ₹576.40 combined (157% of net P&L)
- **Tick logging active**: 80,468 ticks / 6.77 MB written, no drops (final EOD)
- **Cooldown**: VBL and GLENMARK both entered cooldown after exits
- **Armed at close**: BUY — DIXON, VBL, ASTRAL, SAMMAANCAP, KAYNES, MFSL; SELL — GLENMARK
- **Errors**: WebSocket DNS failures (`getaddrinfo failed`) around 14:17 — transient,
  auto-recovered. No impact on trading.

### May 26, 2026 (Fourth Trading Day — Live/Analyze Mode)
- **Market regime**: Mixed — both BUY and SELL screeners active. BUY side had PREMIERENE,
  ADANIPOWER, TMPV, JSWENERGY. SELL side had CONCOR, RVNL. Shorts outperformed longs.
- **Live result**: 4 trades (max hit), 2W/2L, net **+₹164.15**, win rate **50%**
- **Trade breakdown**:
  - CONCOR (SHORT): SELL 124 @ ₹484.65 → BUY @ ₹476.50 | **+₹1,010.60** | 5h21m hold, best trade — held nearly all day, exited at 15:15 (likely EOD flatten)
  - RVNL (SHORT): SELL 379 @ ₹263.75 → BUY @ ₹263.30 | **+₹170.55** | 51-min hold, small scalp
  - PREMIERENE (LONG): BUY 85 @ ₹1,018.00 → SELL @ ₹1,012.00 | **-₹510.00** | 1h13m hold, stopped out
  - ADANIPOWER (LONG): BUY 300 @ ₹245.25 → SELL @ ₹243.56 | **-₹507.00** | 18-min hold, stopped out
- **SHORT direction dominated**: Both SHORT trades were winners (+₹1,181.15 combined), both
  LONG trades were losers (-₹1,017.00 combined). Net +₹164.15 entirely from shorts.
- **CONCOR was a standout**: Held for 5+ hours with only ₹4.01 risk/share, captured ₹8.15/share
  (>2R profit). This is the type of all-day runner that Learning #4 identifies as the strategy's edge.
- **Tick logging active**: 83,951 ticks / 7.13 MB written, 0 drops
- **Armed at close**: BUY — PREMIERENE, ADANIPOWER, TMPV, JSWENERGY; SELL — CONCOR, RVNL
- **Errors**:
  - Pre-login auth error at 08:33 (benign, before Zerodha session started).
  - **EOD flatten failure at 15:20**: Engine tried to exit CONCOR SHORT at `eod_exit_time=15:20`
    but got rejected: "MIS orders cannot be placed after square-off time (15:15 IST)." The
    position was already closed at 15:15:01 (broker auto-square-off), so no financial impact.
    However, this reveals a config bug: `eod_exit_time` (15:20) is *after* the broker's MIS
    cutoff (15:15). Should be changed to 15:10 or 15:12 to ensure the engine exits before
    the broker forces a market-price square-off.
  - **Action needed**: Update `eod_exit_time` from 15:20 → 15:10 in engine config.

### May 27, 2026 (Fifth Trading Day — Live Mode)
- **Market regime**: BUY-dominated — 5 stocks on buy screener (ADANIENSOL, CGPOWER,
  JSWENERGY, ADANIPOWER, SWIGGY), only 1 on sell (COALINDIA). No SHORT trades fired
  despite COALINDIA being armed.
- **Live result**: 3 trades (of 4 max), 2W/1L, net **-₹158.43**, win rate **66.7%**
- **Trade breakdown**:
  - ADANIENSOL (LONG): BUY 31 @ ₹1,533.70 → SELL @ ₹1,541.00 | **+₹226.30** | 3h33m hold, best trade — patient hold rewarded
  - SWIGGY (LONG): BUY 368 @ ₹271.55 → SELL @ ₹270.10 | **-₹533.60** | 10-min hold, SL hit — late entry (14:58) on a fading move
  - ADANIPOWER (LONG): BUY 402 @ avg ₹248.68 → SELL @ ₹249.05 | **+₹148.87** | 3-min hold, quick scalp at 15:09 (just before no_new_entries_after cutoff)
- **Late entries underperformed**: Both SWIGGY (14:58) and ADANIPOWER (15:09) entered
  in the last ~15 minutes before the entry cutoff. SWIGGY was a clear loser; ADANIPOWER
  barely scraped a profit. ADANIENSOL, entered at 11:09, was the only meaningful winner.
- **No SHORT trades**: COALINDIA was armed for SELL but never triggered. All 3 trades
  were LONG. This is the first day with zero SHORT trades since SELL direction was enabled.
- **Tick logging active**: 122,158 ticks / 10.59 MB written, 0 drops
- **Armed at close**: BUY — ADANIENSOL, CGPOWER, JSWENERGY, ADANIPOWER, SWIGGY; SELL — COALINDIA
- **Symbols in cooldown at close**: SWIGGY, ADANIPOWER
- **Funds**: Available cash ₹22,392.70 (floor ₹20,000)
- **Errors**: Pre-login WebSocket 403s at 06:53–06:56 IST (benign, before Zerodha session).
  No trading-hour errors.

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

### June 1, 2026 (Day 4 — Live, Monday)
- **Market regime**: Mixed. BUY screener produced signals (NBCC seen in scans). SELL screener quiet. Engine ran in live mode with atr_sl_mult=1.5.
- **Engine result**: **0 engine-managed trades**. Engine shows trades_today=0 at EOD — engine never subscribed to any symbols (tick log: 0 ticks, 0 bytes, 0 drops).
- **Broker tradebook** (non-engine fills, likely from signal_review or manual):
  - TCS LONG: BUY 42 @ ~2331.8 (12:09) → SELL 42 @ 2322.0 (13:24), **-₹411**, ~1h15m hold
  - NBCC LONG: BUY 500 @ 104.94 (14:38) → SELL 500 @ 104.43 (15:25), **-₹255**, ~47m hold
- **Net P&L (tradebook)**: **-₹666** (0W/2L, 0% win rate)
- **Notable observations**:
  - Engine had 0 trades despite being in live mode — scanned symbols didn't meet entry criteria (volume/breakout conditions), or scan cycles didn't successfully arm the engine.
  - TCS and NBCC trades came from outside the engine (signal_review_service or manual). Both losers.
  - NBCC continues its losing streak from Day 3 — now 3 consecutive losing trades across 2 days. Strong signal to avoid re-entering NBCC in current regime.
  - `signal_review_service` repeatedly failed to persist decisions due to missing `signal_decision` table (DB migration gap, known since last session).
  - `get_funds` API raised an error — engine failed open (continued without funds check).
  - atr_sl_mult confirmed at 1.5 (matching Learning #8).
- **Armed watches at close**: None (buy_symbols=[], sell_symbols=[])
- **Tick log**: 0 ticks written, 0 bytes, 0 drops
- **Errors**:
  - `signal_decision` table missing (DB migration needed) — recurring, many entries today
  - `get_funds` raised error in simplified_stock_engine_service — needs investigation
  - Telegram bot placeholder token errors (known config issue)

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

### 9. SHORT Re-Entry Can Work (Unlike LONG Re-Entry)
- May 21: ANGELONE LONG re-entry was the only loser (-₹528.20) — re-entering a
  weakening uptrend was risky.
- May 22: GLENMARK SHORT re-entry worked — both entries were winners (+₹188.80,
  +₹387.60). A stock that keeps falling often has sustained selling pressure.
- **Observation**: Re-entry risk may be directional. Shorts on persistent losers
  may tolerate re-entry better than longs on fading momentum stocks. Sample size
  is tiny (n=2) — need more data before making a rule.

### 10. SHORT Trades Are Consistently Profitable (Early Signal)
- May 22: GLENMARK SHORT — 2 trades, 2W, +₹576.40
- May 26: CONCOR SHORT +₹1,010.60, RVNL SHORT +₹170.55 — 2 trades, 2W, +₹1,181.15
- May 27: No SHORT trades (COALINDIA armed but never triggered)
- **Cumulative SHORT record**: 4 trades, 4W/0L, +₹1,757.55 (100% win rate)
- **Cumulative LONG record** (May 22–27): DIXON -₹518, VBL +₹306.90, PREMIERENE -₹510,
  ADANIPOWER(26) -₹507, ADANIENSOL(27) +₹226.30, SWIGGY -₹533.60, ADANIPOWER(27) +₹148.87
  = 7 trades, 3W/4L, -₹1,386.53 (43% win rate)
- **Observation**: Small sample (n=11 total), but the directional asymmetry persists.
  SHORT entries from the sell screener may have a stronger edge because stocks falling
  >3% intraday often have sustained selling pressure (institutional unwinding, stop
  cascades), while stocks rising >3% may face profit-taking resistance.
- **Caution**: This could be regime-dependent — a strong bull market may flip the pattern.
  Continue tracking per-direction stats before adjusting max_trades allocation.

### 11. Late Entries (After 14:30) Underperform
- May 27: SWIGGY entered at 14:58 → lost ₹533.60 (SL hit in 10 min). ADANIPOWER
  entered at 15:09 → scraped +₹148.87 in 3 min. Only ADANIENSOL (entered 11:09,
  held 3.5h) was a meaningful winner.
- **Observation**: Entries in the last hour face compressed time for trends to develop,
  and proximity to EOD flatten reduces the strategy's edge of catching all-day runners
  (Learning #4). The `no_new_entries_after=15:10` cutoff may be too late — consider
  tightening to 14:30 or adding a separate late-entry risk multiplier.
- **Sample size**: Only 1 day of data — need to track late vs early entry performance
  over more sessions before changing config.

### 12. Reverting atr_sl_mult to 1.2 on Live Hurts (May 29 Evidence)
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

### 13. Config Mismatch is Dangerous
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

## Cost model corrections (June 7, 2026)

### Brokerage cap was per round trip, should be per order (Fixed)
`compute_zerodha_intraday_charges` (`services/simplified_stock_engine_core.py`)
capped brokerage at ₹20 for the whole round trip — but Zerodha caps ₹20 **per
order**. NBCC reconciliation exposed it: model ₹20.00 vs Kite actual ₹32.15
(37.8% under-reported). Each leg is now charged `min(₹20, 0.03% of that leg)`,
rounded to paise the way Kite's `/charges/orders` reports each order, and summed
(16.15 buy + 16.00 sell = 32.15, matching Kite exactly; model total ₹57.37 vs
Kite ₹57.27, within ₹0.5 — residual is the exchange/SEBI rate approximations,
not the cap). Regression test: `test/test_simplified_stock_engine_charges.py`.

### Capturing LTP at signal time for slippage validation (New)
Added `ltp_at_signal` (nullable REAL) to `trade_journal`. The engine writes the
decision-time reference price at entry (`_journal_record_entry`), pinned even
after `update_entry_fill` overwrites `entry_price` with the real fill. This lets
the nightly loop compute `realized_slippage = (fill_price − ltp_at_signal) /
ltp_at_signal` once live fills accumulate — directly addressing "No slippage"
under Backtest Limitations above. Column evolves at boot via guarded
`ALTER TABLE` in `trade_journal_db.init_db`.

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
- [x] ~~Does the SELL direction (shorting top losers) work at all?~~ **Yes** — May 22
      produced GLENMARK shorts: 2 trades, both winners, +₹576.40 combined. Sell screener
      needs a bearish stock, not a bearish market. Keep SELL enabled.
- [ ] Is there a market regime detector that could switch parameters dynamically?
- [ ] Per-symbol daily trade limit — would it improve or hurt?
- [x] ~~Enable tick logging and compare tick-replay vs candle-replay results.~~
      Tick replay is implemented. Enable with `SIMPLIFIED_ENGINE_TICK_LOG_ENABLED=true`.
      Filename/field mismatch fixed May 22.
- [ ] Test with `mode=live` after 2 weeks of profitable sandbox results.
- [ ] Save the engine's armed stock list to a daily log file so past days can be
      replayed exactly even after the engine resets.
