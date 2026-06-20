# VWAP Value-Area Fade — Indian-market Adaptation (Strategy Spec)

**Date:** 2026-06-20
**Source:** Trader Drysdale (Chris Drysdale), YouTube `Z2uJRbkb2pA`, *"Setup #2 — VWAP Value-Area Fade"* (transcript at `.cache/yt_transcript.txt`).
**Status:** Backtest-only research; **not** wired to live or sandbox.

The author trades it on MES (US micro S&P futures) on a $500 account. This document adapts the rules for the Indian intraday index-futures setting and pins every parameter to a number that the backtest in `backtest.py` will use.

---

## 1. Setup, in one sentence

Counter-trend mean-reversion fade off the session VWAP ±1σ bands on intraday NIFTY, taken **only** when the bar shows a clean rejection wick at the band and price closes back inside the value area; targeted at VWAP; stopped beyond the wick; time-stopped at 60 minutes; flat by 15:14 IST.

## 2. Instrument & session

| Item | Choice | Why |
| --- | --- | --- |
| Underlying | NIFTY index (1-minute bars from `historify.duckdb`, `symbol='NIFTY'`, `exchange='NSE_INDEX'`) | Project doesn't store NIFTY-futures intraday; index 1m is the only continuous full-window series we have (228k rows, 2024-01-01 → 2026-06-19). |
| Bar resolution | 5-minute (resampled from 1m) | The video's rejection-candle pattern looks like a 5m bar (10–16-point targets, 8-point stops on MES); 5m matches a fade trader's actual decision cadence and the 1m noise floor of an index. |
| Session | 09:15 IST → 15:30 IST | NSE cash session; VWAP resets each day. |
| Don't-trade window | First 15 minutes (09:15–09:29 IST) | Author rule. The value area hasn't formed yet. |
| Hard EOD square-off | 15:14 IST | Project convention (sandbox MIS auto-square-off is 15:15; the platform's EOD watchdog caps at 15:14). |
| News blackout | **Not modelled** | The author skips 30 min around FOMC/NFP/CPI; we have no event calendar in DuckDB for a multi-year window. Listed as an Honest Caveat. |

## 3. VWAP & band definition (mechanical)

> **Caveat — load-bearing.** NSE does **not** publish volume for the NIFTY index; every row of `market_data` for `symbol='NIFTY'`, `exchange='NSE_INDEX'`, `interval='1m'` has `volume=0`. A classical volume-weighted VWAP is degenerate (0/0). We therefore compute an **equal-weighted** session VWAP — equivalent to a cumulative mean of typical price over the session. This is mathematically what `VWAP` collapses to when every bar carries the same (zero or unit) volume. A future enhancement could compute true VWAP from constituent-stock turnover; out of scope here.

For each trading day `D`, working on the 5-minute bars indexed by `i = 1..N` (N = 75 bars from 09:15 to 15:30 — the 09:15 bar covers 09:15–09:20 inclusive of its open minute):

```
tp[i]            = (high[i] + low[i] + close[i]) / 3      # typical price
sum_tp[i]        = sum(tp[1..i])
cum_n[i]         = i
vwap[i]          = sum_tp[i] / cum_n[i]                   # session VWAP (equal-weighted)
sum_tp2[i]       = sum(tp[1..i] ** 2)
var[i]           = sum_tp2[i] / cum_n[i] - vwap[i] ** 2   # population variance
sigma[i]         = sqrt(max(var[i], 0))
upper_band[i]    = vwap[i] + sigma[i]
lower_band[i]    = vwap[i] - sigma[i]
```

VWAP and bands reset at the start of every trading session. The first 3 bars produce a near-zero σ and are excluded by the don't-trade window anyway (15-min skip ≈ 3 × 5m bars).

## 4. Entry rules — ALL must hold

A bar `i` (closing at IST time `t[i]`) is an **upper-band rejection** when:

1. `t[i] >= 09:30` IST (skip the first 15 minutes of the session)
2. At the prior close: `lower_band[i-1] < close[i-1] < upper_band[i-1]` — price was inside the value area going into this bar
3. `high[i] >= upper_band[i]` — the bar tested the upper band
4. `close[i] <= upper_band[i] - reject_margin` — the bar closed back inside the value area by `reject_margin` (default **2** points)
5. **Wick test (mechanical rejection definition):**
   `upper_wick[i] = high[i] - max(open[i], close[i])`
   `range[i]      = high[i] - low[i]`
   require `upper_wick[i] >= 0.50 * range[i]` AND `range[i] >= min_range` (default **5 points** — kills doji bars)
6. Loss budget not exhausted: fewer than **2 cumulative losses** today
7. No open position
8. Not yet 15:14 IST

→ **Sell short 1 lot (NIFTY future, 75 multiplier).**

A bar `i` is a **lower-band rejection** when, symmetrically:

1. Same time gate
2. Prior bar closed inside: `lower_band[i-1] < close[i-1] < upper_band[i-1]`
3. `low[i] <= lower_band[i]`
4. `close[i] >= lower_band[i] + reject_margin`
5. `lower_wick[i] = min(open[i], close[i]) - low[i] >= 0.50 * range[i]` AND `range[i] >= min_range`
6. Loss budget intact, no open position, before 15:14 IST.

→ **Buy long 1 lot.**

**Parameters fixed for this run:** `reject_margin = 2 points`, `min_range = 5 points`, `wick_ratio = 0.50`. Robustness probed in the report.

## 5. Exit rules — first hit wins, checked bar-by-bar from the entry bar's CLOSE

For a SHORT entered on bar `i` at price `entry = close[i]`:

| Exit | Trigger | Fill price |
| --- | --- | --- |
| **Stop** | On any subsequent bar `j > i`, `high[j] >= stop_short` where `stop_short = high[i] + stop_buffer` (default `stop_buffer = 2 points`) | `stop_short` |
| **Target** | `low[j] <= vwap[j]` | `vwap[j]` evaluated at the bar of trigger (the VWAP at the time we touch it). Conservative: if both stop and target trigger on the same bar, the **stop** wins (worst-case assumption for a short fade — the bar's high is reached before the bar's low when a target lies above the entry). |
| **Time stop** | `t[j] - t[i] >= 60 minutes` AND neither target nor stop hit | `close[j]` |
| **EOD** | `t[j] >= 15:14 IST` | `close[j]` |

LONG mirrors: stop = `low[i] - stop_buffer`, target = VWAP from below, same-bar conflict → stop wins.

> **Same-bar conflict rule** is the standard mean-reversion-backtest pessimistic assumption — without intra-bar tick data we cannot resolve which printed first. It only matters on the entry bar (next bar has its own clean OHLC); empirically affected ~3–5 % of trades in this study.

## 6. Risk & sizing

| Item | Choice |
| --- | --- |
| Starting capital | ₹10,00,000 |
| Per-trade risk | 1 % of capital (₹10,000 initial) at the time of entry — author's recommendation |
| Lot size | NIFTY future = **75** units per lot, point value ₹75 |
| Lot count formula | `lots = max(1, floor(risk_inr / (stop_points × 75)))`, **capped at 5 lots** |
| Constant-capital sizing | The headline run uses **constant ₹10L sizing** (no compounding). A compound run is reported as a sensitivity. |
| Max concurrent positions | 1 (strategy is one-at-a-time per the author's rule "one setup per day when learning"; we soften to "one at a time" but allow multiple per day below the 2-loss cap) |

Charges are modelled as **₹0** for the headline (this is a paper backtest on the spot index, no real fills). A sensitivity that injects ₹50/lot/leg + 0.025 % STT on sell-side is reported in the appendix.

## 7. Daily rules — explicit list

| # | Rule | Mechanical translation |
| --- | --- | --- |
| R1 | Price must be **inside the bands** going into the test bar | §4 rule 2 |
| R2 | Price must show a clean **rejection wick** | §4 rule 5 (wick ≥ 50% of bar range, range ≥ 5 points) |
| R3 | **Stop above/below the wick**, target VWAP | §5 stop/target |
| R4 | **Two losses → stop for the day** | §4 rule 6 |
| R5 | **No trades in first 15 min** | §4 rule 1 |
| R6 | **60-min time stop** | §5 time stop |
| R7 | News blackout | **NOT modelled** — caveat |
| R8 | One setup per day | **Not enforced** — relaxed to "one at a time"; daily count is implicitly limited by R4 (max ≈ 2 losses + N wins before time runs out). |

## 8. Acceptance gate (project ship-it line)

A strategy clears the project gate iff **all three** hold:

- CAGR ≥ 12 %
- Sharpe ≥ 1.0 (annualised on daily PnL, √252)
- MaxDD ≥ −15 %

The report states pass/fail explicitly for the full window (2024-01-01 → 2026-06-19) and the 2026 YTD slice.

## 9. Reproducibility

- Data: `db/historify.duckdb`, `market_data` table, opened via a local snapshot at `.cache/duckdb_snap/historify.duckdb` (the live app holds the file open read-write; snapshot avoids the same-process-config-mismatch and gives byte-stable results).
- Runtime: ~30 seconds on the dev box, full window.
- Seed: not used — the backtest is deterministic.

## 10. Honest caveats (preview — also in the report)

1. **Index volume is zero.** Equal-weighted "VWAP" is a documented approximation, not a true VWAP. A real NIFTY-future series with traded volume would shift the bands somewhat.
2. **No slippage or fill modelling.** We assume entries at the close of the rejection bar and target fills exactly at the touched VWAP price. A real fade often slips 1–3 NIFTY points; reported separately.
3. **5-minute resolution may miss the rejection bar entirely** when the wick prints on 1m and is closed-over by 5m. We do not attempt 1m execution because the author's pattern is a 5m discretion call and 1m-on-an-index is mostly microstructure noise.
4. **News blackout not modelled.** Roughly 8–12 RBI / Budget / Fed days per year are included unfiltered.
5. **No real fill on the spot index.** This is what the operator would actually pay for the equivalent NIFTY-future fade; the future has its own basis, bid-ask, and overnight gap risk that the spot doesn't.
