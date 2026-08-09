# R56 — In-house scanner → Simplified engine, full-history backtest

**Date:** 2026-07-16
**Issue:** [#416](https://github.com/sonawanedhiraj/openalgo/issues/416)
**Verdict:** **REJECT** as an intraday auto-trading system. The in-house scanner
faithfully reproduces Chartink selection, but its signals carry **no exploitable
intraday directional edge**; run through the intraday simplified engine they lose
gross, and Zerodha charges deepen the loss to a red month **every single month
(0/14 green).**

---

## What was tested (and why it is new)

Every prior simplified-engine backtest (`backtest/run_backtest.py`, Rounds 2–6)
took the entry universe **as given** — a fixed symbol list armed at day-start —
and only backtested the *execution/exit* engine. The in-house scanner (the
Chartink-mirror rules that decide *which* stocks to arm and *when*) was never in
the loop of a historical replay.

This round closes that gap: it feeds historical bars to the **real** scanner rule
functions, collects the timed BUY/SELL signals, and runs those through the **real**
`SimplifiedStockEngine`. End-to-end, both halves are the production code.

### Harness (`backtest/inhouse_scanner/`)

- **Stage 1 — `replay_signals.py`** drives the actual
  `services/scan_rules/fno_intraday_{buy,sell}_chartink.py` `rule()` functions
  (not a re-implementation, unlike `backtest/tod_volume_gate/replay.py`). Per
  symbol-day it builds **point-in-time causal frames**: continuous 5m/15m from 1m
  (rolling windows, matching `ScannerService._append_bar`), and daily/weekly
  settled only through D-1 with a `timestamp` column so `derive_today_and_yest`
  takes production Path B (today's running snapshot from today's 5m bars). The
  rule's internal `datetime.now(IST)` is frozen to each simulated 5m-bar-close via
  a monkeypatched `datetime` subclass, so evaluation is causal, not wall-clock.
  Records first-fire per (symbol, day, direction).
  - *Tractability:* gates 9/10 (`open > yest_close`, `open > pivot`) are
    day-constant, and gate 1 (gap) equals `today_close > yest_close×(1+gap%)` —
    both are **strict necessary conditions** of the real rule, so pre-filtering on
    them skips only bars the real rule would reject anyway. ~6.4 s/symbol.
- **Stage 2 — `replay_engine.py`** arms the real `SimplifiedStockEngine`
  (`mode=disabled`) at each fire time (history seeded to that bar), replays the
  day's 5m candles, and lets the engine's own breakout + ATR/volume entry, ATR
  stop, RR-trailing, same-day stop-out block, global `max_trades_per_day` cap, and
  EOD flatten run. One engine per day shares global risk state, exactly like the
  live singleton. Config from `config_from_env()` (the live `SIMPLIFIED_ENGINE_*`).

### Data & window

App-independent `outputs/tod_volume_gate/prices.duckdb` (broker-API-sourced —
OpenAlgo holds `db/historify.duckdb` open read-write, so an external process
cannot read it). **1m: 2025-06-20 → 2026-07-06 (258 trading days, 211 symbols);
daily from 2024-06-03 (full SMA200 depth).** The window is bounded by 1m
availability, not choice — intraday gates (5m/15m RSI, Supertrend, today-running
snapshot) require 1m, which does not reliably exist for the scanner universe
before mid-2025.

### Live config (faithful)

capital ₹20,000, leverage 5×, max_risk ₹500/trade, **max 6 trades/day (global)**,
ATR-SL 1.5×, volume-multiplier 2.5, cooldown 3, MIS intraday, gap 1.5%
(`CHARTINK_RULE_BUY_GAP_PCT` / `SELL`).

---

## Validation — the scanner replay reproduces Chartink

On **2026-07-03**, cross-checking the replay's BUY fires against the recorded
Chartink `fno-intraday-buy` list (`backtest/scans/2026-07-03_BUY.txt`), restricted
to a 15-symbol test set: **10 of 10** Chartink-flagged names reproduced, **zero
misses** (plus LODHA, a legitimate +3.4% gap name not on that day's Chartink list —
expected, since the in-house rule mirrors but is not byte-identical to Chartink).
The gate progression is sensible (early bars fail `gap_up`, then `vol_sma50` until
cumulative volume clears the daily SMA, then PASS). Two harness bugs were found and
fixed during validation — a µs/ns epoch-scaling error and a `datetime64` column
that forced the rule's `_daily_bar_date` into the known `np.int64` isinstance trap
(→ phantom `dbar_stale` on every bar). See the commit history.

---

## Results

### Signal generation (Stage 1)

**3,878 signals** over 258 trading days, 211 symbols — BUY 1,995, SELL 1,883
(~15/day). Median fire time **270 min into the session (~13:45 IST)** — the daily
volume-SMA gate only clears after volume accumulates, so fires are structurally
**late in the day**.

### Signal-level edge (gross, no engine, no costs)

Forward return from `fire_price`, LONG/SHORT-signed (positive = signal was right):

| Leg | n | ret→same-day close | win | ret→T+1 close | win |
|-----|---|--------------------|-----|---------------|-----|
| BUY  | 1995 | **+0.017%** | 47.2% | **+0.106%** | 50.6% |
| SELL | 1883 | −0.003% | 46.7% | **−0.093%** | 47.8% |
| ALL  | 3878 | +0.007% | 46.9% | +0.009% | 49.2% |

Two structural facts fall out:
1. **Intraday continuation edge is ≈ zero.** Same-day return from the fire is a
   coin-flip (mean ~0.00%, win ~47%) — because the scanner fires *late*, after the
   move that tripped the gap+volume+trend gates has largely happened.
2. **The only real edge is BUY overnight drift** (+0.106% to T+1, win 50.6%) — and
   **SELL is anti-predictive** (stocks *rise* after a sell fire; a short loses).
   This re-confirms the registry's "overnight edge is long-only"
   ([R42](../../../../strategies/STRATEGY_REGISTRY.md),
   short-mirror rejection; MIS-leveraged sector_follow rejection).

An **intraday** engine that exits at EOD cannot capture an overnight drift and is
exposed to the zero-edge same-day noise. That is the whole story.

### Engine execution (Stage 2)

| Metric | Value |
|--------|-------|
| Armed signals | 3,878 |
| Trades (fills) | 1,177 (30% of signals — rest never broke out, hit the 6-trade/day cap, or were blocked) |
| Win rate (net) | 42.8% |
| Gross P&L | **−₹162,042** |
| Charges | −₹89,484 |
| **Net P&L** | **−₹251,526** |
| Avg net / trade | −₹214 |
| Payoff (net avg win / avg loss) | ₹172 / −₹503 = **0.34** |
| Green months | **0 / 14** |

By leg: **LONG** 649 trades, gross −₹103k, net −₹152k, win 39.9%; **SHORT** 528
trades, gross −₹59k, net −₹100k, win 46.4%. **Both legs lose gross.** LONG is
worst despite the overnight BUY edge — because entering intraday, late, after the
run-up, and exiting same day converts that overnight drift into stop-outs.

By exit: 1,029 of 1,177 trades (87%) exit on a stop (`stop_loss` −₹130k over 225,
`stop_loss_intracandle` −₹111k over 804); `eod` −₹11k over 148. The profile is the
textbook double-loss: **win rate < 50% AND payoff < 1.**

> The −1258% "ROI on capital" the summary prints is misleading — ₹20,000 is the
> per-trade risk-sizing base, not a book that compounds. Read the result as **net
> −₹251k / 0-of-14 green months**, not as an account draw-down percentage.

---

## Why it fails (and why this was predictable)

This reproduces, on the *real* signal path, exactly what the R1 research sweep
found on a fixed universe: *"every variant loses gross AND net; ~all exits are
stops/EOD; win < 50%; avg loss ≫ avg win."* The new, load-bearing addition is
**why**: the scanner's gap+volume+trend gates fire **after** the intraday move,
leaving no same-day continuation to trade. The signal selection itself is sound
(it matches Chartink 10/10) — the mismatch is between a **late, momentum-exhausted
signal** and an **intraday hold**.

## What would be worth testing next (not this round)

- **T+1 overnight hold on BUY signals only** — the one measured edge (+0.106%/T+1,
  win 50.6%) is a CNC-style hold, the same shape that works for `sector_follow`
  and `futures_follow`. Whether +0.106% survives ~₹76/trade CNC costs is the open
  question (it is thin — likely marginal), but it is the only positive number here.
- **Drop the SELL leg entirely** — anti-predictive overnight, gross-negative
  intraday. Consistent with every prior short-mirror result.
- **Earlier-firing variant** — the late fire is caused by the full-day volume-SMA
  gate; a time-of-day-adjusted volume gate was already studied (R48) and found not
  to add net edge, so this is low-priority.

## Artifacts

- Harness: `backtest/inhouse_scanner/replay_signals.py`, `replay_engine.py`
- `backtest/inhouse_scanner/signals.parquet` (3,878 fires),
  `trades.parquet` (1,177), `summary.json`
- Data: `outputs/tod_volume_gate/prices.duckdb` (unchanged, read-only)
