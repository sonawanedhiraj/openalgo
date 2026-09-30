# R70 — Is there a closing-hour twin of the open15 window? (issue #750)

**Date:** 2026-09-30 · **Verdict: REJECT.** No 15-minute window near the close has
opening-like volatility. The open15 rule has no gross edge in any closing window,
and no other closing-window predictor clears costs.
**Harness:** `backtest/close15/` (untracked, local; `data/bars.duckdb` is a
read-only extract of historify).

## Question

The operator's question: open15 A-grade trades win. Is there a 15-minute window in
the closing hours where volatility matches the first 15 minutes? If so, does an
open15-style (or any other) rule work there? Map what works and what doesn't.

## Data and method

- historify 1m bars, 212 F&O stocks and NIFTY, **2024-01-01 → 2026-09-30**
  (683 days). 641 days are pre-CAS; 42 are post-CAS (from 2026-08-03, when cash
  continuous trading in F&O names ends at 15:14).
- **Entry is always at the bar close. The trigger level is never used as an entry
  price** (the R58 look-ahead rule). The tick calibration below shows why this is
  the faithful model, not just the conservative one.
- Costs: 0.10% round trip (the `mis_round_trip_charges` value at ~₹60k notional).
- Every cell is split into two halves by date.

## 1. The volatility profile: the close is about 1/3 of the open

Median over symbol-days, per 15-minute bucket:

| Bucket | median abs(ret) | median range | 1m realised vol | share of day's volume |
|---|---:|---:|---:|---:|
| **09:15–09:30** (pre-CAS) | **0.53%** | **1.23%** | 0.60% | 9.0% |
| 09:30–09:45 | 0.28% | 0.65% | 0.43% | 5.0% |
| 14:30–14:45 | 0.14% | 0.33% | 0.23% | 3.0% |
| 14:45–15:00 | 0.13% | 0.33% | 0.23% | 3.5% |
| **15:00–15:15** (pre-CAS) | **0.17%** | **0.40%** | 0.28% | 7.2% |
| 15:15–15:30 (pre-CAS; window no longer exists) | 0.16% | 0.43% | 0.28% | 8.9% |
| **09:15–09:30** (post-CAS) | 0.49% | 1.06% | 0.50% | 8.6% |
| **15:00–15:14** (post-CAS) | 0.16% | 0.42% | 0.28% | **10.9%** |

The busiest closing bucket moves about as much as 09:30–09:45, and about 1/3 of
the opening 15 minutes. Its volume matches the open or exceeds it (post-CAS
15:00–15:14 carries 10.9% of the day's volume). The price move does not follow.

**The closing surge is volume without price discovery.** The open prices
overnight information. The close is position squaring, index and MOC flow, and
now the auction book. None of that is new information.

## 2. Tick calibration: a live entry fills at the bar close

The open15 trigger (`Open15Core.on_tick` logic: running cum-vol-in-minute ≥
1.5× the mean of completed minutes, price beyond the 09:15 level) was replayed
on all 46 recorded mornings in `tick_logs/open15` and compared with the 1m-bar
harness on the same (symbol, day, side):

- Where the entry sits between the level (0) and the bar close (1):
  **median α = 1.00**, IQR 0.72–1.13, n = 3,317.
- Trigger minute is identical in 93% of cases.
- Mean gross return to 09:30: **tick 0.023%, bar-close 0.004%, level 0.134%.**

A real-time tick entry does not capture the intra-bar burst. It pays the same
price as the bar close. **The bar-close harness is therefore the faithful model
of live execution.** Level pricing is look-ahead that adds about 0.13% per trade
of fictional edge. So bar-close numbers can be read as "what open15 would do
live" in any window.

## 3. The open15 rule, unchanged, in each window

Rule: top-3 by day return at t0 go long, bottom-3 go short. Level = t0 bar
high/low, vol gate 1.5×, entries through t0+14, exit at window end. At the open,
the ranking uses the gap instead (the control).

| Window | n | win | gross | net | H1 / H2 net |
|---|---:|---:|---:|---:|---:|
| **09:15 open (control)** | 643 | 38.7% | −0.017% | −0.117% | −0.176 / −0.058 |
| 14:30–14:45 | 1,994 | 28.1% | −0.022% | −0.122% | −0.131 / −0.113 |
| 14:45–15:00 | 1,980 | 26.2% | −0.007% | −0.107% | −0.111 / −0.104 |
| 15:00–15:15 | 1,916 | 32.4% | +0.017% | −0.083% | −0.050 / −0.116 |
| 15:15–15:30 (pre-CAS only) | 1,492 | 22.3% | −0.132% | −0.232% | −0.301 / −0.164 |
| 14:55–15:10 (CAS-safe) | 2,244 | 31.1% | +0.014% | −0.086% | −0.089 / −0.083 |

With the bar-compatible **A grade** (trigger ≤ t0+7, universe median > −0.30%;
the tick-only ratio < 1.55× has no bar analogue):

| Window | n | win | gross | net |
|---|---:|---:|---:|---:|
| 09:15 open | 310 | 42.9% | +0.024% | −0.076% |
| 14:30–14:45 | 1,335 | 32.7% | −0.027% | −0.127% |
| 14:45–15:00 | 1,256 | 30.0% | −0.013% | −0.113% |
| 15:00–15:15 | 1,235 | 36.9% | +0.026% | −0.074% |
| 15:15–15:30 | 1,190 | 24.2% | −0.139% | −0.239% |
| 14:55–15:10 | 1,643 | 36.2% | +0.030% | −0.070% |

Even the look-ahead **level upper bound** is net-negative in every closing window
for long+short combined. The one exception is a pre-CAS 15:00 long cell
(+0.011% net), and it goes negative in H2 and post-CAS.

**The vol gate is not selective at the close.** Volume ramps into the close, so
a window-local baseline is beaten on almost every name: the gate fires on
roughly 3 picks per side per day. At the open it fires on about half.

**The 15:15–15:30 window was the worst of all:** −0.13% gross, both halves.
That is the reversal toward the VWAP close price that settled the pre-CAS close.
The window no longer exists.

## 4. Any other closing-window predictor?

Cross-sectional quintile spreads (Q5−Q1 mean return, t over days) were computed
for day return, intraday return, gap, first-30-min return, rest-of-day return,
last-30-min return, range position, relative-to-NIFTY return and relative
volume. They were run against 15:00→15:14, 15:00→15:29 (pre-CAS), 14:45→15:10
and 14:45→15:14.

- **Every spread is below 0.07%**, against a 0.10% cost per leg.
- The strongest is the pre-CAS **15:00→15:29 reversal** on last-30-min return
  (Q5−Q1 −0.073%, t −10.6, both halves). Again, this belongs to the retired
  VWAP-close window.
- **Extreme days don't help.** Top/bottom-3 movers, |day ret| ≥ 3% and ≥ 5%:
  all at or below 0.10% gross.
- **NIFTY intraday momentum** (sign of 09:45→14:44 → 14:45→15:15), the
  Gao-Han-Li-Zhou effect:
  - Pre-CAS: corr +0.21, sign-follow **+0.029% per day**, win 54.5%, both halves
    positive. That is about 7 NIFTY points, roughly the ~0.03% round-trip
    futures cost.
  - Largest |mid| buckets: still at most +0.03%.
  - **Post-CAS: corr +0.05 (n = 42), gone.**
- One post-CAS hint: shorting the bottom-3 day losers at 14:45 made +0.031% net
  over 126 trades / 42 days. **It is noise:** 5% of pre-CAS 42-day stretches
  look at least this good, and the full pre-CAS result is −0.143% net, both
  halves.

## What works / what doesn't

| Idea | Result |
|---|---|
| Find a closing 15m with opening-like volatility | **None exists.** Best is 1/3 of the open. |
| open15 rule at 14:30 / 14:45 / 15:00 / 14:55 / 15:15 | Gross ≈ 0, net −0.07 to −0.23% per trade. REJECT. |
| A-grade filter at the close | Changes nothing. The A premise (early trigger + healthy tape) doesn't transfer. |
| Momentum or reversal of day movers into the close | Sub-cost in both directions. |
| NIFTY intraday momentum into the close | Real pre-CAS (t ≈ 5) but ≈ cost, and gone post-CAS. |
| 15:15–15:30 VWAP-close reversal | The only large effect, and CAS removed the window. |

## Side finding: the premise itself is thin

Over the full 683 days, open15 at the **open** under the tick-faithful
bar-close model is also net-negative in equity terms. Gross is −0.017% for
top-3 and +0.024% for the A grade (A = trigger ≤ 09:22 with universe median
above −0.30%).

The live A-grade win rate rests on a short window. The 46-day tick replay
shows a 70.6% win rate on 17 A-ish trades, with H1 +0.60% and H2 +0.06%. That
is consistent with the R63 note that A was 9/9 in August and 4/8 in September.

This does not change the R63 pre-registered decision, which is already on
track. But **"A-grade wins" is not yet established beyond these two months.**
Any extension of the idea (to the close or elsewhere) inherits that
uncertainty.

Caveats:
- Equity returns, not the options open15 trades live. Options lever the move,
  but they don't change its sign, and they add spread cost.
- There are no ticks after 09:30, so the α = 1 calibration is assumed to hold
  at the close. Given that the close is lower-volatility and more liquid,
  that is conservative.
- The post-CAS sample is only 42 days.

## Not tested (and why)

- **Stock options 15:15–15:40.** The underlying is in the auction, so there is
  no continuous spot, and Kite does not serve expired option bars. The index
  version is already being recorded by `cas_320_expiry_straddle` (R69).
- **Opening-auction analogue at the close** (trading the CAS itself): that is
  R69's territory.
