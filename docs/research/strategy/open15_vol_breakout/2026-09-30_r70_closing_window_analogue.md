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

## Side finding (CORRECTED by R70b below)

The first pass said "open15 at the open is net-negative under the faithful
model". That holds for the bar harness, but **the bar harness cannot express the
two things that make open15 work**:

- the A grade's tick-only `ratio < 1.55×`;
- the rolling top-mover watch list.

R70b replays both on ticks: the opening edge is real on the post-CAS sample.

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


---

# R70b addendum (2026-10-04): tick-exact replay, operator follow-up

> "Testing using the minute closing entry does not test correctly — there are
> large moves mid-minute. Check if we have tick logs."

## Tick data found

- `tick_logs/ticks-YYYYMMDD-*.jsonl` is the simplified-engine ticklog: all day,
  about 210 F&O symbols, about 1 tick per second per symbol, 2026-09-01 →
  10-01.
  - It has **17 usable post-CAS days** covering 14:25–15:16. Three files had no
    closing ticks.
  - The morning is mostly absent from this log.
- `tick_logs/open15/` covers 09:14:58–09:30 for every universe symbol,
  2026-07-22 → 10-01. That gives 37 post-CAS days for the opening control.
- Extractor: `backtest/close15/extract_ticks.py` →
  `data/ticks/<day>.parquet`.

## 1. Entry: mid-minute moves do not change the entry price

`replay_close_ticks.py` mirrors `Open15Core.on_tick` on each closing window.
In the trade's direction, the bar close is only **0.003–0.010% worse** than the
trigger tick, and the next tick only 0.003–0.006% worse.

All closing windows stay net-negative with tick entries:

| Window (17 days) | All triggers, net | Top-3/side, net |
|---|---:|---:|
| 14:30–14:45 | −0.106% (n = 4,017) | −0.126% |
| 14:45–15:00 | −0.119% | −0.065% |
| 14:55–15:10 | −0.098% | −0.105% |
| 15:00–15:14:55 | −0.119% | −0.210% |

This matches the opening calibration (α = 1.00).

## 2. Exit: tick-exact targets / stops / trails do not help

`exit_grid_ticks.py` tested 30 first-touch target × stop combinations and 3
trailing stops, filled at the touching tick.

- The close simply does not move. Median MFE is 0.13–0.17%, and **only 6–18%
  of closing trades ever reach +0.4%, vs 36% at the open.**
- **0 of 33 rules are positive in both halves in any closing window.** The best
  closing cell is −0.035% net.

## 3. What actually makes open15 work, and that it does not transfer

Live journal (`live_vs_stock.py`): 58 real fills. The underlying move from the
trigger tick to 09:30:

| Grade | n | stock move | win | journal net |
|---|---:|---:|---:|---:|
| **A** | 23 | **+0.50%** | 83% | **+₹90,954** |
| B | 17 | −0.13% | 47% | −₹28,924 |
| C | 18 | −0.05% | 28% | −₹28,320 |

**The edge is in the underlying, not the options overlay.**

Tick replay (`mover_A.py`, post-CAS): full A grade (trigger ≤ t0+7, tick ratio
< 1.55×, universe tape > −0.30%) × a **rolling top-N mover watch list**
(best day-return rank ≤ N at any minute before the trigger, additive like
#529). Gross % per trade, equity, cost 0.10%:

| Window | Top-5 A long | Top-5 A short | Top-10 A long / short | All-symbol A long / short |
|---|---|---|---|---|
| **OPEN 09:15–09:30** | **75.7% win, +0.43 (n 37), H1 +0.46 / H2 +0.21 net** | **75%, +0.44 (n 20), H1 +0.61 / H2 +0.08** | +0.26 / +0.30 | +0.02 / +0.08 |
| 14:30–14:45 | 22%, −0.17 (n 9) | 56%, +0.06 (n 9) | −0.13 / +0.04 | −0.05 / +0.05 |
| 14:45–15:00 | 25%, −0.14 (n 4) | 83%, +0.09 (n 12) | −0.09 / +0.03 | −0.05 / 0.00 |
| 14:55–15:10 | 44%, −0.05 (n 9) | 70%, 0.00 (n 10) | −0.08 / +0.01 | −0.05 / +0.06 |
| 15:00–15:14:55 | 8%, −0.22 (n 12) | 53%, 0.00 (n 15) | −0.11 / −0.01 | −0.03 / +0.01 |

- **At the open, the effect weakens as the watch list widens** (top-5 > top-10
  > all), and both sides and both halves are positive.
- **At the close, the same filter picks LOSING longs.** The day's top gainers
  that break their window-open high on a clean volume burst give the move back
  into the close. Shorts are flat.

The two halves of the open15 edge are both opening-specific:

- **Mover selection:** the overnight gap is new information that keeps
  repricing. A 14:30 day-mover's move is already done.
- **A grade:** an early, clean volume burst in a healthy tape is price
  discovery at 09:16. Late in the day, the same burst is flow.

## Final verdict R70 / R70b: REJECT the closing analogue (tick-confirmed)

**What works (opening, keep):**
- rolling top-5 movers × A grade, both sides;
- a wider watch list dilutes it, so keep `rolling_top_n` small.

**What does not work (closing, every variant tested):**
- the open15 rule in 4 closing windows (5 pre-CAS with bars);
- the A grade on its own;
- top-3/5/10 rolling movers × A;
- 33 tick-exact exit rules;
- quintile predictors;
- NIFTY intraday momentum;
- extreme-day subsets.

**Untestable:** the CAS-safe end of the window is ~15:10. Zerodha squares off
MIS in CAS stocks at 15:12, and 15:15–15:40 is auction or index-option
territory (R69).

Caveats:
- The opening edge rests on 37 post-CAS days (n = 57 top-5 A). Keep the R63
  pre-registered check.
- The closing sample is 17 days, but the closing result agrees with 683 days of
  bars.
