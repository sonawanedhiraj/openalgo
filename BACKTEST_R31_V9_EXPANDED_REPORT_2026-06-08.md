# R31 — V9 Supertrend Sector-Swing on Expanded 141-Stock Universe

**Date:** 2026-06-08 · **Window:** OOS 2024-01-01 → 2025-11-30 @ 1.88 bps · 2026 = sanity
**Engine:** `r31_engine.py` (daily event-driven, true multi-leg pyramiding, per-leg FIFO CNC costs). Read-only on `historify.duckdb`.

## 1. Universe verification
141 sector-tagged F&O stocks (UNKNOWN dropped), **all 141 have 1m data** 2024-01-01→2026-06-08 (daily derived from 1m, cached). 16 rankable sectors (≥2 names). Sector momentum ranked on a **synthetic equal-weight constituent index per sector** (uniform across all sectors, including the INFRA/DEFENCE/TELECOM/PSE baskets that have no NSE index series). Distribution: FINSERVICE 21, ENERGY 15, INFRA 14, AUTO 12, BANK 11, CONSRDURBL 10, METAL/FMCG 9, PHARMA/IT 8, PSUBANK 7, DEFENCE 6, TELECOM/REALTY/HEALTHCARE 3, PSE 2.

## 2. Variant matrix (OOS, 1.88 bps)

| Var | mechanic | N | WR | payoff | Sharpe | green mo | max-mo % | net @1.88 | 2026 | verdict |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|---|
| **A** | baseline (control) | 36 | 0.33 | 2.68 | 0.45 | 44% | 128% | ₹52,810 | −27,755 | REJECT |
| **B** | cut losers (5d stop + BE) | 55 | 0.16 | 7.17 | 0.38 | 27% | 145% | ₹46,596 | +905 | REJECT |
| **C** | pyramid (+50%@1R,+25%@2R) | 36 | 0.28 | 4.29 | 0.60 | 37% | 101% | **₹107,266** | −36,263 | REJECT |
| **D** | cut + pyramid | 58 | 0.12 | 9.18 | 0.20 | 18% | 319% | ₹33,908 | −12,287 | REJECT |
| **E** | aggressive (2×/3×, ST10,2, 3d) | 92 | 0.10 | 8.26 | −0.16 | 25% | — | −₹28,143 | −77,807 | REJECT |
| **F** | C + breadth (top3/max5) | 62 | 0.24 | 4.34 | 0.48 | 26% | 98% | **₹110,198** | +10,533 | REJECT |

**All six REJECT** — every variant fails the ≥60% green-months and ≤25% concentration gates.

## 3. Winning variant detail — V9-C (highest Sharpe; F = C+breadth, highest net)
- **Edge is real but tiny-N and lumpy.** ₹107k OOS net, payoff 4.29, but only 36 trades over 19 months (~1.9/mo — *identical* to R30 despite 4.7× more stocks).
- **Per-sector net:** NIFTYMETAL ₹102.8k, NIFTYAUTO ₹72.6k, FINSERVICE ₹33.8k; **8 of 12 traded sectors net-negative**.
- **Per-symbol:** TOP NATIONALUM ₹108.2k, M&M ₹52.8k, LTF ₹33.8k, HEROMOTOCO ₹30.1k, KAYNES ₹10.0k. BOT APOLLOHOSP −16.3k, COALINDIA −10.4k, TMPV −9.7k, PREMIERENE −9.6k, ICICIBANK −8.9k.
- **Single-name concentration = 101%** (NATIONALUM alone > total net). Strip the top 2 names → strategy is net-negative.
- **Hand-validated trade #39 NATIONALUM (3 legs):** BUY 448@221.54 (stamp ₹14.89) + 224@243.91 (₹8.20) + 112@266.23 (₹4.47); SELL 784@373.02. buy ₹183,704 / sell ₹292,448 → stamp 27.56, STT 476.15, exch 16.43, SEBI 0.48, GST 3.04, **charges ₹523.65**, gross ₹108,740.61, **net ₹108,216.96** — matches trades.csv exactly. Per-leg stamp sums correctly (14.89+8.20+4.47=27.56).

## 4. Pyramiding analysis
- Avg **1.56 legs/trade**; **33% of trades pyramided** (12/36).
- **Pyramided trades: mean net +₹21,268, all 12 exited via trailing-stop (none cut early).** Single-leg trades (never reached +1R): mean net **−₹6,165**.
- Pyramiding *is* the entire edge — the 12 trades that proved out carry the book; the 24 that never reach +1R bleed. Asymmetry materializes, but the base rate of reaching +1R (33%) is too low to smooth months.

## 5. Cross-variant pattern — what moved the needle
- **Pyramiding helped profit most:** A→C lifted net +103% (₹52.8k→107.3k), payoff 2.68→4.29, Sharpe 0.45→0.60.
- **Cutting losers HURT:** time-stops (B) raised N (36→55) but crushed WR (33%→16%) and *dropped* green-months (44%→27%) — they chop winners-in-waiting and bank whipsaw losers. Combined (D) was destructive (Sharpe 0.20, green 18%): time-stops cut trades *before* they reach the +1R pyramid trigger.
- **Aggression backfired:** E (ST10,2 + 3-day stop) went net-negative — sensitive trail + early time-stop kill trends before payoff.
- **Breadth sweep (V9-C mechanic):** top2→top8 raised N (36→212) but net **collapsed** (+107k → −119k); Sharpe positive only at top2/top3. The edge lives *exclusively* in the top-2/3 ranked sectors; diluting to hit trade-count destroys it.

## 6. Verdict & recommendation — **REJECT (do not deploy)**
The R30 hypothesis — "widen the universe to fix monthly consistency" — is **falsified**. With 4.7× more stocks, trade frequency stayed at ~1.9/mo because the binding constraint is the *funnel* (top-2 sectors × fresh Supertrend flip × max-3 concurrent), not universe size. The fundamental tension is structural:
- **Narrow (top-2/3):** real edge (payoff >4, +₹107–110k) but ~2 trades/mo → green-months ≤37%, 101% single-name concentration.
- **Wide (top-4+):** enough trades for monthly cadence but the edge turns **net-negative** — lower-ranked sectors are losers.

There is **no breadth setting where this archetype is both profitable and monthly-consistent.** Green-months never exceed 44%.

**Recommendation:**
1. **Abandon the monthly-consistency mandate for this archetype.** V9-C/F is a legitimate *low-frequency, high-payoff trend sleeve* — judge it on annual return and payoff, not monthly green-rate. As an annual sleeve, V9-F (+₹110k OOS, +₹10.5k 2026, payoff 4.34) is the keeper.
2. **Keep pyramiding, drop all loser-cutting.** Pyramiding is the entire edge; every time-stop variant degraded it.
3. For genuine monthly consistency, a different engine is required (mean-reversion / higher-frequency / options-income) — Supertrend-flip sector rotation cannot get there at any breadth.
