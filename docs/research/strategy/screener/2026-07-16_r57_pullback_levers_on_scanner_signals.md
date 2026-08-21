# R57 — Pullback levers on the scanner signals: a thin but real edge

**Date:** 2026-07-16
**Issue:** [#416](https://github.com/sonawanedhiraj/openalgo/issues/416) (follow-on to R56)
**Verdict:** **PROMISING (not deployable).** Applying `intraday_pullback_top2`'s
validated learnings — long-only, mid-strength band cap, afternoon window,
hold-to-EOD execution — to the R56 scanner signals **isolates a genuine,
split-half-stable directional edge** that R56's aggregate and the simplified
engine both destroyed. But it is **thin (+0.17%/trade) and slippage-fragile**:
it survives ~0.05% round-trip slippage and breaks by ~0.10% — the exact
open-risk the parent strategy carries.

---

## The question

R56 concluded the scanner signals have ~zero *aggregate* intraday edge. But
`intraday_pullback_top2` (R53: Sharpe 3.42, PF 1.68) works on the *same kind* of
signal by **selection discipline**, not a different entry. So: does applying its
learnings as an iteration ladder extract edge from a disciplined *subset* of the
scanner signals? The simplified engine already *is* a pullback-breakout entry, so
the levers to port are the selection filters + the exit rule, not the entry.

Harness: `backtest/inhouse_scanner/iterate_pullback.py` (signal-forward-return
ladder, split-half) and `pullback_exec.py` (pullback-style execution + slippage
sweep). LONG-signed forward returns; H1 = 2025-06..2025-12, H2 = 2026-01..2026-07.

## Iteration ladder (signal-level, ret to same-day close)

| Iter | Lever | mean/trade | win% | H1 / H2 | n |
|------|-------|-----------|------|---------|---|
| I0 | all signals (R56) | +0.007% | 46.9% | +0.092 / −0.038 | 3878 |
| I1 | long-only | +0.017% | 47.2% | +0.056 / −0.004 | 1995 |
| I2 | + band [1.5,2.5)% | +0.040% | 48.2% | **+0.040 / +0.040** | 733 |
| I2 | band [3.0%+) (extended) | **−0.032%** | 45.3% | +0.059 / −0.069 | 989 |
| I3 | + window 13:00–15:00 | +0.089% | 54.3% | +0.142 / +0.065 | 658 |
| I3 | window 11:00–13:00 | −0.083% | 44.1% | −0.055 / −0.099 | 485 |
| **I4** | **band [1.5,2.5) ∩ afternoon** | **+0.172%** | **58.7%** | **+0.159 / +0.180** | 259 |
| I6 | band [1.5,3.0) ∩ afternoon | +0.155% | 58.3% | +0.186 / +0.138 | 369 |

Two structural facts, both matching the parent strategy independently:
1. **Mid-strength is the edge; extended gainers mean-revert.** Band [1.5,2.5)% is
   positive in BOTH halves; [3%+] is negative, [5%+] strongly negative
   (−0.164%, win 39%). R56's ~zero aggregate was mid-band edge + extended-band
   drag cancelling.
2. **The good window is 13:00–15:00; 11:00–13:00 is dead** — mirroring
   `intraday_pullback_top2`'s own `no_trade [11:00,13:00]` + `afternoon
   [13:00,15:00]` windows. (The scanner fires late by construction — its
   volume-SMA gate only clears mid-afternoon — so its edge lives in the afternoon,
   not the pullback strategy's morning slot.)

The joint cut **I4** (long + mid-band + afternoon) is +0.172%/trade to close,
win **58.7%**, both halves consistent, ~1 signal/day.

## Execution matters more than selection

The **same I4 signals through the simplified engine lose** (net −₹15,422, win
42.3%, 1/14 green) — **60% of trades die on `stop_loss_intracandle`.** The
engine's tight ATR stop + RR-trailing shakes trades out of the very pullbacks the
edge is built on. This is `intraday_pullback_top2`'s learning verbatim (*"exit
management ALL rejected; hold full-size to EOD"*).

Swapping in **pullback execution** (enter at signal, hold to EOD, wide/no stop)
recovers it (₹50k notional/trade, real MIS charges):

| Execution (I4) | net | win | green months | H1 / H2 |
|---|---|---|---|---|
| engine (ATR stop + trail) | −₹15,422 | 42.3% | 1/14 | — |
| hold-to-close, no stop | +₹8,409 | 52.1% | 9/14 | +2,620 / +5,788 |
| **hold-to-EOD, 1.0% stop** | **+₹10,133** | 52.1% | **10/14** | +2,962 / +7,171 |

I6 (wider band) is directionally identical (+₹11,734, 10/14 green, both halves +).
Wide stops are ~neutral-to-slightly-helpful; tight stops hurt — again matching the
parent's "wide stops aren't intrinsically bad" finding.

## Honesty: the slippage cliff (the binding constraint)

Entry is modeled at the fire-bar close with **no slippage** — an upper bound. The
slippage sweep at the best config (I4, 1.0% stop) is decisive:

| Slippage / side | net | green months | halves |
|---|---|---|---|
| 0 bps | +₹10,133 | 10/14 | both + |
| 2.5 bps (0.05% r/t) | +₹3,791 | 9/14 | both + |
| **5.0 bps (0.10% r/t)** | **−₹2,623** | 6/14 | **both −** |
| 7.5 bps | −₹9,031 | 5/14 | both − |

**The edge survives ~0.05% round-trip slippage and breaks by ~0.10%.** This is
precisely `intraday_pullback_top2`'s open-risk #1 (*"~0.05%/side the combined PF
likely drifts… treat as an upper bound"*). At ~₹39/trade net (0 slip) on ~1
trade/day, the margin for slippage is small.

## Verdict & next iteration

**Edge found — real, not noise** (coherent band×window structure, split-half
stable, independently corroborated by the parent strategy). **Not deployable
as-is:** thin and slippage-fragile; needs real fill measurement.

The one un-tested lever is the R53 **PRIMARY** one: **`nf_mom` + NIFTY-regime +
sector-green gates**, which lifted the parent book most. It needs NIFTY/sector 1m
that `prices.duckdb` deliberately excludes (a broker fetch). If regime gating
removes the negative-slippage-margin days (as it did for the parent), it could
push this from "marginal" to "tradeable." **Recommended next round:** fetch
NIFTY + sector 1m, add `nf_mom`/regime/sector-green to I4/I6, and measure whether
the slippage cliff moves out past ~0.10% r/t.

## Artifacts

- `backtest/inhouse_scanner/iterate_pullback.py` (ladder), `pullback_exec.py`
  (execution + slippage sweep)
- `signals_i4.parquet` (259), `signals_i6.parquet` (369)
