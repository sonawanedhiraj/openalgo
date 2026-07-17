# futures_follow_cap50 — entry cap counts the soon-to-exit carried lot (#405)

**Date:** 2026-07-14
**Issue:** [#405](https://github.com/sonawanedhiraj/openalgo/issues/405)
**Harness:** `outputs/2026-07-14_futures_entry_cap_carry/run_carry.py` (byte-identical
signal reproduction via `sm_core`; read-only on `historify.duckdb`)
**Window:** 2024-01-01 → 2026-06-12 (canonical), ₹10L, C1(sector≥1.5)×W2+E4 K5, NIFTY-only CAP50

## Question

`run_entry` sizes the 15:20 entry against the 50% margin cap seeding `lots_filled =
self.lots_held()` — which **includes the prior-day position that is still open** (it
exits at 15:25, five minutes *after* the entry). Does counting that soon-to-be-freed
lot against the cap cost return?

## Key structural finding

The **validating backtest already sizes each day against a fresh 50% cap** — in
`run.py:run_variant`, `cum_margin` resets per entry day and never counts the carried
cohort. So the canonical **14.44% CAGR / 1.27 Sharpe** number is effectively
**"option (a)"** (fresh daily cap). **Production is the *more conservative* variant**
and therefore under-performs its own backtest.

## Results (four-way)

| Variant | Trades | Win% | Net ₹ | CAGR% | Sharpe | MaxDD% | Peak overlap |
|---|---|---|---|---|---|---|---|
| **CONTROL** carry-counted (= current production) | 137 | 52.6 | 351,880 | **13.12** | **1.19** | −7.11 | 49.8% |
| **OPTION_A** fresh daily cap (= canonical, = prod after fix) | 149 | 52.3 | 390,519 | **14.44** | **1.27** | −8.01 | **98.7%** ⚠️ |
| **OPTION_B** same-minute @15:20 (exit moved early) | 149 | 51.0 | 370,269 | **13.75** | **1.21** | −8.98 | 49.8% |
| **OPTION_C** same-minute @15:25 (exit kept, entry moved late) | 149 | 51.7 | 386,840 | **14.31** | **1.26** | −8.75 | 49.8% ✅ |

- Fidelity: OPTION_A reproduces the canonical 14.44% / 1.27 / −8.0% exactly.
- **OPTION_A − CONTROL: +1.32pp CAGR, +₹38,639, +0.08 Sharpe** (12 trades recovered across
  12 carry-binding days in 2.4y) — but peak overlap **98.7%**.
- **OPTION_B − OPTION_A: −0.69pp** (pure cost of exiting the carried lot at 15:20 vs 15:25).
- **OPTION_C − OPTION_A: −0.13pp** (pure cost of entering at 15:25 vs 15:20).
- **OPTION_C − CONTROL: +1.19pp CAGR, +₹34,960, +0.07 Sharpe**, peak overlap **49.8%**.

## The decisive finding: entry timing is cheap, exit timing is expensive

Both B and C keep margin ≤50% (sell-first, no overlap). They differ only in *which* leg
moves 5 minutes:

- **Move the EXIT earlier** (B, 15:25→15:20): **−0.69pp** — the last 5 min of the T+1 hold
  carries real edge, so giving it up is costly.
- **Move the ENTRY later** (C, 15:20→15:25): **−0.13pp** — the 5-min entry shift is nearly
  free.

So doing **both legs in the same 15:25 minute** (OPTION_C) captures **90% of the gain**
(+1.19pp of +1.32pp) at Sharpe 1.26 ≈ 1.27, while **never exceeding 50% margin** — no
overlap spike, no live rejection risk. It is the best of both worlds. (Slightly deeper DD
than A, −8.75 vs −8.01, but Sharpe is essentially identical.)

OPTION_A's +1.32pp is marginally higher but buys a transient **98.7% margin** (both cohorts
held 15:20–15:25); a live broker could reject the 15:20 entry without near-full free margin.
Sandbox (₹1Cr book) is unaffected.

## Recommendation (revised after the four-way run)

- **Sandbox → OPTION_A** (simplest; full +1.32pp; margin irrelevant on the ₹1Cr book).
  Aligns production with its own validated 14.44% backtest.
- **Live → OPTION_C** — the winner. Sell the carried lot and buy the new lot in the **same
  15:25 minute** (sell-first). +1.19pp / +0.07 Sharpe over current production, **margin
  never above 50%**, no rejection risk. Do NOT use option (b) (loses the exit edge, worst DD).

**Lesson:** the two 5-minute shifts are NOT symmetric — exit timing carries the edge, entry
timing does not. The user's "same minute" instinct is right *at 15:25*, wrong at 15:20.
