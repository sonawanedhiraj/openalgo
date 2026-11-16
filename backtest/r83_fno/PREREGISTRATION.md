# R83 pre-registration — find a working strategy on NSE F&O stocks (#788)

Committed BEFORE any candidate result is computed. Specs are fixed here; nothing is tuned after.

## Costs (verified 2026-11-16 against Kite `POST /charges/orders`, single-leg)
- **CNC delivery:** buy = STT 0.1% + stamp 0.015% + exchange 0.00307% + SEBI 0.0001% + GST 18% on (exchange + SEBI) ≈ 11.87 bps. Sell ≈ 10.37 bps + **DP ₹15.34 per scrip per sell day** (Zerodha, incl. GST). Brokerage 0.
- **ETF (CNC):** STT 0.001% on sell only, stamp 0.015% on buy, exchange + SEBI + GST ≈ 2.4 bps round trip + DP ₹15.34 per scrip per sell.
- **Stock futures (NRML):** brokerage min(₹20, 0.03%)/order, **STT 0.05% sell** (in force since 2026-04), stamp 0.002% buy, exchange 0.00183%, SEBI, GST ≈ 6.3 bps RT for a ₹7.2L lot. Current rates are applied to ALL history (conservative).
- **Slippage:** 5 bps per side (primary), 0 bps (zero-slip bound), 15 bps (stress) — reported for all.

## Data and universe
- Daily Kite bars 2018-01-01 → 2026-11-13 (split/bonus-adjusted by Kite; dividends excluded → long returns understated). Sources: `backtest/r81_entry/data/bars_r81.duckdb` and `backtest/r82_sizing/data/bars_fwd.duckdb`.
- **Point-in-time F&O membership:** month M = FUTSTK list of NSE's month-end bhavcopy of M−1 (95 lists, 2018-12 → 2026-10). 25 historical names (2.8% of symbol-months, mainly 2019–22) have no Kite history → survivorship bias, disclosed.
- **Eligibility at each rebalance:** in the PIT list, ≥ 252 prior daily bars, close ≥ ₹50, 20-day median traded value ≥ ₹10 cr.
- Single-day |return| > 35% is treated as an unadjusted corporate action → return set to 0, count reported.

## Periods
- **F&O stock candidates (B):** TRAIN 2019-01 → 2022-12, VALIDATE 2023-01 → 2024-06, **SEALED HOLDOUT 2024-07 → 2026-11-13**.
- **Sector ETF rotation (A, frozen in R21b/R24/R26 on 2022-08 → 2026-06):** that window is in-sample. **HOLDOUT = the windows it never saw:** 2019-01 → 2022-07 (backward) + 2026-07 → 2026-11-13 (forward).

## Execution
Signals on the last trading day of the period at the close; trades at the NEXT trading day's open ± slippage. Integer shares; cash left uninvested. Only the difference between old and new holdings is traded. Simulated at capital ₹20k (primary), ₹1L, ₹5L, ₹10L.

## Candidates (all long-only CNC unless stated; equal weight within a leg)
| Id | Rule | Neighbours (plateau) |
|---|---|---|
| A | Sector ETF rotation R26 spec: 11 sector series (9 ETFs + FINNIFTY + OIL&GAS proxies, on the underlying index); momentum top-3 by 126d return; low-vol bottom-3 by 60d daily-return vol (annualised); risk-parity inverse-vol between legs (leg vol = mean member vol); monthly | 63d / 252d momentum; top-2 / top-4 |
| B1 MOM | Top 10 F&O stocks by 126d return skipping the last 21d (6-1) | 3-1, 12-1, N=5, N=20 |
| B2 LOWVOL | Bottom 10 by 60d daily-return std | 20d, 120d, N=5, N=20 |
| B3 RP | B1 + B2 legs, risk-parity inverse-vol between legs | 50/50; N=5; N=20 |
| B4 SECSTK | Top-3 sector indices by 126d return; in each, the top-3 F&O stocks by own 6-1 momentum (sector map `strategies/intraday_pullback_top2/sector_map.json`) | top-2 sectors; 2 stocks; 63d sector lookback |
| B5 REV | Weekly: buy the 10 eligible stocks with the lowest 5-day return at the Friday close, hold one week | 10-day lookback; N=5; N=20 |
| B6 MOMLS | Market-neutral: long top-10 / short bottom-10 by 6-1 momentum via **stock futures**, monthly roll. Modelled on spot returns (no expired-futures history exists); futures costs on both legs every month (roll) | 3-1, 12-1, N=5 |

## Primary metric and gates (holdout)
- **Excess return:** monthly net strategy return − equal-weight eligible-universe return (same execution, no costs). For A the benchmark is the equal-weight of the 11 sector series. For B6 the return itself (market-neutral).
- **PASS requires ALL of:**
  - holdout excess > 0 with **t ≥ 2** (monthly; ≥ 2.58 = confirmed), AND absolute net return > 0
  - TRAIN and VALIDATE excess > 0 (B); for A, the in-sample period positive
  - **≥ 7/10 half-years positive** excess in 2019–2023 (B)
  - **plateau:** every neighbour's excess > 0 on VALIDATE (A: on the holdout's backward window)
  - **placebo:** holdout excess beats ≥ 95% of 1,000 random same-size portfolios drawn from the same eligible set each period (p < 0.05)
  - all at **5 bps slippage**, reported at 0 and 15 bps too
- Capital: gates are computed at ₹20k. If a candidate fails only because of the ₹20k frictions (DP charges / integer shares) the smallest capital in {₹1L, ₹5L, ₹10L} at which it passes is reported. Capital is a friction axis, not a signal parameter.
- Only candidates whose TRAIN and VALIDATE excess are both > 0 are run on the holdout; that list is frozen by commit (`data/frozen.json`) before the holdout opens.

## Rationale (economic, stated before testing)
- **Momentum (B1, B4, B6):** under-reaction to information and slow institutional flows; documented in Indian equities.
- **Low volatility (B2):** leverage-constrained investors overpay for high-beta names, so low-vol stocks earn more per unit of risk.
- **Short-term reversal (B5):** compensation for providing liquidity to forced or impatient sellers.
- **Risk parity (A, B3):** momentum and low-vol pay off in different regimes, so combining them lowers drawdown.
