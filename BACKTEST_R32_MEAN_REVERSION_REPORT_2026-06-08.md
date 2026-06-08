# R32 — Mean-Reversion Hunt on the 141-Stock F&O Universe

**Date:** 2026-06-08 · **Goal:** find a *high-frequency, low-payoff, narrow-distribution*
monthly cash-flow engine (structurally opposite to R31 V9). **Bar (strict):** OOS
2024-01-01→2025-11-30 @1.88 bps — Sharpe ≥1.5, ≥65% green months, ≤20% single-month
share, ≤15% single-name, WR ≥55%, N ≥100, 2026 positive, net+ @3.5 bps.

**Verdict: NO CANDIDATE. All six variants REJECT.** Mean reversion does not clear even
the MARGINAL bar (best monthly-Sharpe 0.64). The thesis is falsified on this universe.

## 1. Variant matrix (OOS @1.88 bps)

| V | Strategy | N | WR | Payoff | Sharpe(mo,ann) | Green% | MaxMo% | Net@1.88 | 2026 | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| A | Connors RSI(2) classic | 359 | 0.446 | 0.78 | -2.37 | 20% | — | -447,803 | + | REJECT |
| B | 3-day pullback in uptrend | 317 | 0.451 | 1.05 | -1.05 | 40% | — | -169,760 | + | REJECT |
| C | Bollinger Band fade | 130 | 0.492 | 1.24 | 0.58 | 57% | 99% | +113,138 | + | REJECT |
| D | Sector relative weakness (weekly) | 270 | 0.478 | 1.26 | 0.64 | 57% | 83% | +153,980 | + | REJECT |
| E | Connors + filters | 252 | 0.472 | 0.74 | -1.61 | 25% | — | -274,617 | + | REJECT |
| F | C + vol/streak risk scaling | 130 | 0.492 | 1.23 | 0.54 | 57% | 102% | +116,814 | + | REJECT |

(MaxMo% blank where OOS net ≤0.) **WR never reaches 50%** — the 55-70% the MR thesis
assumes never materialises on Indian F&O large caps.

## 2. "Least-bad" detail — D & C (both still REJECT)

**D (best Sharpe/net)** is a mirage. Per-sector OOS net: **INFRA_BASKET +₹114,836** and
**DEFENCE_BASKET +₹109,212** carry the whole result; every NSE-indexed sector
(BANK -63k, CONSRDURBL -50k, PHARMA -43k, ENERGY -30k, TELECOM -30k) loses money.
Top names: WAAREEENER +63k, BDL +60k, KAYNES +42k, POWERINDIA +41k, COCHINSHIP +39k —
the *hottest momentum stocks of 2024-25*. D is **momentum-dip-buying in two runaway
baskets**, not cross-sectional mean reversion. Strip the top-2 names and net falls to
+₹31,580; strip the two baskets and it is deeply negative.

**C (cleanest pure MR)**: OOS net +113k but **99.6% of it is a single month (2025-10)**;
ADANIGREEN contributes +34,805 from *one* trade. Month-by-month (full period) swings
-90,616 (Mar-26) ↔ +112,643 (Oct-25). net ex-top2 = +37,500 (barely positive). This is
the same lumpy, regime-dependent distribution as momentum — opposite skin, identical wall.

## 3. Cost-vs-gross — STT is **not** the killer

| V | gross (OOS) | friction | gross/cost | STT | STT %gross | avg win |
|---|---|---|---|---|---|---|
| C | +201k | 86k | **2.34** | 28k | 14% | ₹10,679 |
| D | +411k | 257k | 1.66 | — | — | ₹8,812 |

Every variant's avg win after costs is **₹4.5k-11k ≫ ₹500** — no STT-eats-edge flag fires.
**Why:** risk-sizing to ₹10k/trade builds ₹2-4 L notional positions, so STT (~0.2% RT) is
a small slice of ATR-scale exits (BB→SMA20, ~2σ ≈ 2-4% moves). The thesis worried STT
would eat 1% targets — but with risk-based sizing, MR exits are *not* 1% wins; they are
big-and-lumpy. The cost worry was real in principle and simply doesn't bind. The edge,
not friction, is the reason for failure.

## 4. Hand-validated trade (to the rupee)

**C, trade #2 — JSWENERGY**, entry 2024-10-29 @644.00, exit 2024-11-06 @684.95, qty 272.
- buy_value 175,168.00 · sell_value 186,306.40 · gross **+11,138.40**
- STT 0.1%×2 legs = 361.47 · exchange 0.00345% = 12.47 · SEBI 0.36 · GST 18%(exch+sebi) 2.31 · stamp 0.015% buy 26.28 → **total charges 402.89** ✓ (CSV 402.89)
- slippage 1.88 bps × turnover = 67.96 → **net@1.88 = 11,138.40 − 402.89 − 67.96 = 10,667.55** ✓ (CSV 10,667.55)

## 5. Verdict & recommendation

**Do not deploy any R32 variant.** None reaches MARGINAL. Recommendation: **abandon
single-name mean reversion on Indian F&O equity** as a monthly-consistency vehicle.

## 6. Why mean reversion is structurally wrong here

1. **WR is 44-49%, not 55-70%.** Indian F&O large caps gap and trend through support;
   they don't cleanly revert intraday/multi-day. The high-WR premise is false on this set.
2. **MR profits cluster in post-selloff rebound months** (Jun-25, Oct-25, Apr-26) and
   bleed in trending/falling-knife months (Dec-25, Jan-26, Mar-26). The distribution is as
   regime-dependent as momentum — the failure mode is *trending markets*, exactly as
   predicted, and those dominate 2024-26.
3. **Any "winner" is concentrated momentum in disguise** (D = defence/infra dip-buys), so
   the de-concentration test guts it.
4. **The cash-flow profile cannot be manufactured by sizing.** Fixed-risk sizing → ATR-scale
   P&L → lumpy months. Shrinking size for "many small wins" would raise the STT drag and
   shrink the rebound payoff that is the only thing keeping nets positive. Vol-scaling (F)
   made it *worse*: MR's best entries are in high-vol panics, exactly when it cuts size.

**Net:** R30/R31 hit the universe-breadth wall for momentum; R32 hits a deeper wall for
MR — there is no genuine high-WR reversion edge on F&O equity to begin with. The monthly
cash-flow engine is not in the equity-name reversion space. If pursued, look at
index/ETF-level reversion or pairs (market-neutral), not single F&O names.
