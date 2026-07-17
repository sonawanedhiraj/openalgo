# R55 — OTM put overnight hedge on futures_follow_cap50 · 2026-07-13

**Question (operator):** the R54 stop-loss round showed a stop can't catch the
~19% overnight-gap portion of a war-news loss. A defined-risk **long PUT** held
alongside the future is the only structure that can. Does an overnight put hedge
make the sleeve safer *without* destroying the return?

**Verdict: REJECT.** With real NIFTY bhavcopy premiums, every put-hedge variant
is a net cost and lowers **both** CAGR (−3 to −8.5pp) and Sharpe (1.35 → 0.88–1.23).
The hedge does bound the worst single day (−₹33k → −₹19–26k) but barely moves
MaxDD, and it is **negatively correlated with the sleeve's own edge** — you buy
puts on bullish-signal days and pay to bet against the long NIFTY drift the
strategy monetises. Not deployable as an always-on hedge.

Issue: [#398](https://github.com/sonawanedhiraj/openalgo/issues/398) ·
Harness: `outputs/2026-07-13_futures_put_hedge/run_puthedge.py`

---

## Method — real bhavcopy premiums, EOD close-to-close (R42-validated proxy)

Synthetic Black-Scholes is known-optimistic for index-option buying on this
project (R33/R36; memory `synthetic-bs-optimistic-index-options`), so this round
uses **real** premiums from `index_options_eod` (NIFTY PE bhavcopy).

- **Signal set:** canonical C1(sector≥1.5)×W2+E4 K5 NIFTY-only CAP50, byte-identical
  via `sm_core`. The hedge changes only P&L, never selection.
- **Futures leg:** index cEOD(D) → cEOD(D+1) [close-to-close ~24h], the R42 proxy.
- **Hedge leg:** for each futures lot, BUY 1 NIFTY PUT at D's EOD close premium,
  SELL at D+1's EOD close (same strike + expiry). 1 put : 1 future (matched lot
  size → notional-matched). Strike = nearest available ≤ spot·(1−otm); expiry =
  nearest monthly with (expiry−D).days ≥ min_dte. Real Zerodha option charges
  (₹20/leg, STT 0.1% sell premium, txn 0.035%, GST 18%, stamp/SEBI).

**Data caveats:** NIFTY PE data ends **2026-06-04**, so the window is
2024-01-01…2026-06-04 — the 2026-07-07 war day itself is **not priceable**.
Expiries are monthly-only (no weeklies in the table). **Hedge coverage was 100%**
(every trade found a live OTM put on both D and D+1).

**Fidelity control — UNHEDGED reproduces the R42 long control (close-to-close):**

| Metric | R42 long control | This UNHEDGED |
|---|---|---|
| CAGR | 15.48% | **15.5%** |
| Sharpe | 1.27 | 1.35 |
| MaxDD | −6.05% | −8.05% |
| Worst day | −₹45,639 | −₹33,143 |

CAGR near-exact; Sharpe/DD within the window-difference noise (06-04 cutoff, K5
selection). The overlay is trustworthy.

## Results (₹10L, NIFTY-only CAP50, 2024-01-01 → 2026-06-04, 147 trades)

| Variant | Win% | CAGR% | Sharpe | MaxDD% | Worst day ₹ | Hedge P&L ₹ | Cover |
|---|---:|---:|---:|---:|---:|---:|---:|
| **UNHEDGED (control)** | 51.7 | **15.50** | **1.35** | −8.05 | −33,143 | — | — |
| PUT 0.5% OTM | 46.3 | 7.04 | 0.88 | −7.38 | −19,333 | −238,590 | 100% |
| PUT 1.0% OTM | 46.9 | 8.50 | 1.00 | −7.65 | −21,107 | −199,418 | 100% |
| PUT 1.5% OTM | 47.6 | 9.45 | 1.07 | −7.74 | −22,326 | −173,217 | 100% |
| PUT 2.0% OTM | 47.6 | 10.14 | 1.10 | −7.98 | −23,315 | −154,366 | 100% |
| PUT 3.0% OTM | 47.6 | 11.44 | 1.18 | −8.17 | −26,100 | −117,790 | 100% |
| PUT 2.0% OTM ½-notional | 47.6 | 12.74 | 1.23 | −8.09 | −28,300 | −80,652 | 100% |
| PUT 1.5% OTM dte≥7 | 47.6 | 9.22 | 1.06 | −7.60 | −22,326 | −179,660 | 100% |
| PUT 2.0% OTM dte≥7 | 47.6 | 9.98 | 1.09 | −7.89 | −23,315 | −158,685 | 100% |

Farther-OTM = cheaper drag but less protection; nearer-ATM = more protection but
brutal cost. **No variant beats unhedged on Sharpe.** The least-bad is the
½-notional 2% OTM (Sharpe 1.23) — still below 1.35, and it pays ~2.8pp of CAGR
for a ₹5k worst-day reduction.

## Why the hedge fails — it is short the sleeve's own edge

Decomposing the 2% OTM hedge P&L by whether NIFTY rose or fell overnight:

| Day type | n | Futures net ₹ | **Hedge net ₹** |
|---|---:|---:|---:|
| NIFTY UP (sleeve wins) | 80 | +782,216 | **−184,736** |
| NIFTY DOWN (sleeve loses) | 67 | −364,290 | **+30,370** |

The hedge is profitable on only **24%** of days. It costs ₹185k on the good days
to save ₹30k on the bad ones. This is not merely theta — it is **structural**:
the sleeve is honestly labelled *leveraged long-NIFTY beta*, it makes money from
the +0.17%/day drift on bullish-signal days, and a put is a bet against exactly
that drift. You pay premium every session to short your own edge; the put only
pays on the rare overnight gap-down. And because it is OTM, even on the biggest
gap-downs it recovers only ~25–30% of the futures loss (e.g. 2025-05-07: futures
−₹19,672, hedge +₹6,244 → still −₹13,428).

This is the third confirmation of the same structural fact on this signal family:
option **buying** loses to the IV floor / theta (R33/R36; the sleeve's own weekly-CALL
return-vehicle test), and now option buying as a **hedge** loses because it fights
the long-drift edge. The only wrapper that works is being **long delta-1 overnight**.

## Conclusion & recommendation

An always-on put hedge is **rejected**: negative-EV insurance that lowers CAGR and
Sharpe while only weakly bounding the tail. Keep the sleeve **unhedged**.

If the operator's goal is purely a hard cap on single-day loss (a risk-preference,
not a return decision), the ½-notional 2% OTM is the least-bad option — but
**sizing down is strictly better**: it cuts the tail linearly with zero theta drag
and zero edge-fighting, whereas the hedge cuts the tail sub-linearly while paying
premium against the strategy's direction. The evidence-backed tail levers remain:

1. **Size down** — linear tail reduction, no drag (preferred).
2. **Fix the 3% kill switch's T+1 blind spot** — it read ₹0 while the pilot lost
   ₹34k overnight.

A *conditional* hedge (buy puts only when a gap is likely) is the only thing that
could theoretically work, but it has no foundation here: the sleeve has **no**
directional predictive power (hit-rate 53%, corr 0.30 — the leveraged-beta caveat),
so it cannot time when to hedge, and regime-gating on this sleeve was already shown
value-destroying (memory `regime-gate-futures-value-destroying`). Not pursued.

Registry cross-cutting finding: *option buying — as a return vehicle OR as a hedge
— rejects on the sector_follow overnight family; a put hedge is structurally short
the sleeve's long-drift edge (loses on 76% of days). The edge is long delta-1
overnight and is best de-risked by sizing, not by options.*
