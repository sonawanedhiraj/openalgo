# R34 — Portfolio BLEND Meta-Backtest

**Date:** 2026-06-08 · **Window (OOS):** 2024-01 → 2025-11 (23 months) · **Type:** META-backtest on existing monthly return streams. **No new signals.**
Artifacts: `outputs/r34_portfolio_blend_2026-06-08/`

## Method & the one assumption that matters

Four validated components' monthly streams were blended six ways. Component A
(sector-rotation ETF, variant 26d) was regenerated as **real % returns** from
`historify.duckdb` (read-only). B/C/D are rupee P&L (`net_1_88`), each
**risk-budgeted to A's standalone OOS vol (12.3%/yr)** — a per-sleeve scalar that
preserves each sleeve's own Sharpe and green% and only sets the % scale for the
max-month/MaxDD gates. A missing month = no trade = 0%. **This vol-normalization
drives the %-based gates and is the load-bearing assumption.**

## 1. Variant matrix (all gates)

| Variant | green% | Sharpe | maxM | maxQ | payoff | MaxDD | CAGR | Verdict |
|---|---|---|---|---|---|---|---|---|
| **V_BLD_B** inv-vol | **70%** | **1.41** | 9.1% | 8.6% | **1.67** | -3.2% | 13.1% | **DEPLOY_CANDIDATE** |
| V_BLD_A equal-wt | 78% | 1.79 | 5.1% | 7.1% | 1.27 | -3.4% | 11.2% | PROMISING (payoff<1.3) |
| V_BLD_E vol-target | 78% | 1.79 | 8.5% | 11.8% | 1.27 | -5.6% | 18.9% | PROMISING |
| V_BLD_F regime | 74% | 1.45 | 4.0% | 6.0% | 1.08 | -4.7% | 8.8% | PROMISING |
| V_BLD_C 2-lowest-corr | 56% | 0.71 | 5.5% | 4.0% | 1.22 | -4.4% | 4.9% | MARGINAL |
| V_BLD_D risk-parity ERC | 44% | 0.15 | 5.1% | 7.1% | 1.46 | -7.6% | 0.7% | REJECT* |

Gate bar = green≥65, Sharpe≥1.3, maxM≤20, maxQ≤30, payoff≥1.3, MaxDD≤-15.
*ERC degenerates on sparse sleeves: flat (0-return) months read as ~0 vol, so ERC
piles weight into the inactive sleeve. Not a real risk-parity failure — an artifact
of zero-padding. Equal-weight and inverse-vol are the robust schemes.

## 2. The bar WAS reached — and why no single component did

| Component (standalone, vol-norm) | green% | Sharpe | payoff | Fails on |
|---|---|---|---|---|
| A sector-ETF | 78% | 1.52 | 0.84 | **payoff** |
| B stock-vs-sector pair | 48% | 0.73 | 1.85 | green, Sharpe |
| C V9-F Supertrend swing | 17% | 0.09 | 3.55 | green, Sharpe |
| D NIFTY Connors-RSI | 44% | 1.19 | 2.06 | green |

**Every single component fails ≥1 DEPLOY gate. V_BLD_B passes all six.** The
mechanism is real and specific: **A supplies hit-rate (78% green, payoff 0.84 —
frequent small wins), the lumpy sleeves supply payoff asymmetry (1.85–3.55), and
because they are near-uncorrelated the blend keeps both** — green stays 70% while
payoff rises to 1.67. This is the portfolio-layer lever R30–R33 never tested.

## 3. Correlation matrix (the whole game)

|  | A | B | C | D |
|---|---|---|---|---|
| **A** sector-ETF | 1.00 | -0.30 | 0.31 | 0.27 |
| **B** pair | -0.30 | 1.00 | -0.34 | -0.08 |
| **C** v9-F | 0.31 | -0.34 | 1.00 | 0.08 |
| **D** nifty-rsi | 0.27 | 0.08* | 0.08 | 1.00 |

Average pairwise corr ≈ **-0.01**. Equal-weight blend therefore cuts vol roughly
in half (12.3%→~6%/yr) with return intact → Sharpe 1.52→1.79. B's strongest
diversifier is its **-0.30/-0.34** to A and C. No instrument overlap: A = sector
ETFs (BANKBEES…), B = single stocks, C = sector-index futures, D = NIFTY index
options — they cannot conflict on positions.

## 4. Verdict: **DEPLOY_CANDIDATE (V_BLD_B) — with three honest caveats**

The strict cash-flow bar is **reachable**, contradicting the R30–R33 "structurally
unreachable" prior — but lean on it carefully:

1. **Window luck for A.** A's standalone Sharpe is 1.52 here vs 1.11 on full
   2022–2026; this OOS window dodges A's -7% 2026-H1 drawdown. The blend inherits that.
2. **Vol-normalization assumes capital scalability.** D traded ~₹800 index-option
   swings; risk-budgeting it to 12% vol implies large option positions whose
   slippage/liquidity isn't modeled here.
3. **Low correlations are partly zero-padding.** C and D are flat several months;
   flat months mechanically depress measured correlation. Always-on deployment may
   show higher stress-month correlation. N=23 is small.

## 5. Deployment notes (if pursued — sandbox first)

- **Components needed:** all four. Dropping C/D (the high-payoff lumpy sleeves)
  collapses payoff back below 1.3 → A reverts to PROMISING. The lumpy sleeves are
  load-bearing, not garnish.
- **Weighting:** inverse-vol (rolling 3M realized vol, monthly rebal), equal-weight
  fallback when a sleeve's window is flat. Avoid ERC (degenerate here).
- **Capital:** four parallel pools — ETF cash (A), stock margin (B), futures SPAN (C),
  option premium (D). Size each to equal ~12%/yr standalone vol, then inverse-vol blend.
- **Rebalance:** monthly, aligned to A's month-start sector rotation.
- **Next step:** paper-trade the blend alongside the existing 2026-07-01 sector-ETF
  sandbox rebalance; verify the lumpy sleeves' real fills match the vol-budget before
  trusting the 1.67 payoff.

**Bottom line:** Blending does NOT smooth lumpy sleeves into consistency. It works
because ONE consistent sleeve (sector-ETF) carries green%, the lumpy sleeves carry
payoff, and ~zero correlation lets the blend bank both. Real, but window- and
assumption-dependent — promote to sandbox, not live.
