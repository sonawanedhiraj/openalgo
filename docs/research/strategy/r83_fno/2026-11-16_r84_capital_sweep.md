# R84: capital sweep of R83's frozen F&O strategies (#789)

**Date:** 2026-11-16

**Scope.** A re-costing of R83's frozen strategies (`backtest/r83_fno/sweep.py`), not a new search.
- Signals are computed once per (strategy, date) and reused at every capital level. Only sizing, whole-share rounding, idle cash, DP charges and statutory costs change with capital.
- Capital levels: ₹20k / 50k / 1L / 2L / 5L / 10L / 25L.
- Slippage: 5 bps per side, plus a zero-slip bound (in `data/r84_sweep.csv`).

**Windows.**
- B and C: TRAIN 2019–22, VALIDATE 2023-01 → 2024-06, R83's sealed HOLDOUT 2024-07 → 2026-11-13.
- A: in-sample 2022-08 → 2026-06 (where the spec was found); holdout 2019-01 → 2022-07 plus 2026-07 → 11.

**Rule.** A strategy that failed R83's TRAIN or VALIDATE is not resurrected by a capital level. A profitable result needs t ≥ 2, positive excess on every window, and positivity at two or more adjacent capital levels.

## Answer

**Nothing turns profitable at any capital level.**

**The hypothesis: A, sector ETF rotation.** Now modelled with whole ETF units at real ETF prices (index scaled to the ETF's price) and the DP charge.

| Capital | Holdout 2019-22: excess (t) | Forward 2026: excess (t) | In-sample: excess (t) | DP drag/yr | Absolute CAGR, Sharpe, MaxDD (holdout) |
|---|---|---|---|---|---|
| ₹20k | −0.34% (−0.07) | +0.97% (0.16) | −1.26% (−0.48) | 2.27% | 14.3%, 0.67, −29.8% |
| ₹50k | +0.89% (0.18) | +2.01% (0.33) | +0.11% (0.04) | 0.93% | 15.5%, 0.72, −29.3% |
| ₹1L | +1.39% (0.28) | +2.33% (0.39) | +0.53% (0.20) | 0.46% | 16.0%, 0.75, −28.9% |
| ₹10L | +1.77% (0.35) | +2.68% (0.44) | +0.92% (0.35) | 0.05% | 16.4%, 0.76, −28.7% |
| ₹25L | +1.80% (0.36) | +2.70% (0.45) | +0.94% (0.36) | 0.02% | 16.4%, 0.77, −28.7% |

- The hypothesis is half right. At ₹20k the cost drag is about 2.3% a year, not 4–6%: A holds a few cheap ETFs, while 4–6% was the drag on the 10-stock CNC books.
- Removing that drag does turn A's excess positive from ₹50k on every window.
- But the excess is +1–3% a year, against 15–20% annual volatility of the excess. t peaks at 0.45. Proving an edge that size would need decades of data.
- What A really is: about 16% CAGR, Sharpe 0.76, max drawdown −29%. That is sector beta, and an equal-weight sector basket earns essentially the same.

## Other strategies: best capital level

| Id | Failed in R83 at | Best capital | Holdout excess there (t) | Its TRAIN / VALIDATE there |
|---|---|---|---|---|
| B1 6-1 momentum | holdout | ₹25L | −16.4% (−1.39) | +7.5 / +9.0 |
| B2 low-vol | validate | ₹10L | +3.1% (0.50) | −5.0 / −11.7 (t −2.1): not resurrectable |
| B3 risk-parity momentum + low-vol | train | ₹25L | −3.3% (−0.56) | −0.6 / −5.4 |
| B4 sectors → stocks | train | ₹2L | −0.8% (−0.08) | −8.2 / +6.3 |
| B5 weekly reversal | train | ₹25L | −18.5% (−2.57) | −13.7 / −14.4 |
| B6 futures long/short momentum | holdout | not viable at ≤ ₹25L | −19.4% (−1.18); returns don't depend on capital | +13.2 / +13.6 |
| C1 residual momentum | holdout | ₹5L | −18.7% (−1.75) | +6.3 / +9.2 |
| C2 52-week high | train | ₹25L | +2.3% (0.30) | −3.7 / −0.9: not resurrectable |
| C3 smooth momentum | train | ₹25L | −5.8% (−0.83) | +0.5 / −4.1 |
| C4 quarterly risk parity | validate | ₹5L | −4.4% (−0.90) | +1.8 / −7.7 |
| C5 F&O additions | train | ₹20k | +11.9% (1.07) | −12.1 / −9.0: not resurrectable |

## Capital each strategy needs to be viable

"Viable" means at least 95% of the intended names are held at each rebalance and DP drag is below 0.5% a year. This is separate from profitability.

| Strategy | Needs | At ₹20k |
|---|---|---|
| A (ETFs) | ₹50k (DP 0.9%/yr; ₹1L for DP < 0.5%) | all names held, but DP 2.3%/yr |
| B1, C1 momentum | ₹2L | 62% of names held, 47% idle cash, DP 3.8%/yr |
| B2 low-vol | ₹2L | 68% of names held |
| B3, C4 (two legs, 20 names) | ₹2L–₹5L | 43% of names held, 63% idle cash |
| B4 | ₹1L | 83% of names held |
| B5 weekly | ₹5L (DP); costs still 15%/yr at ₹25L | DP 30%/yr |
| C2, C3 | ₹2L | 61–69% of names held |
| C5 | ₹1L | 81% of names held |
| B6 stock futures, 10 long + 10 short, 1 lot each | **≈ ₹29L** at holdout prices (median; p10 ₹26L, p90 ₹31L) | not viable; not viable at ₹25L either. Current lot ≈ ₹6.2L notional, 20% margin + 10% buffer |

## Disclosures

- **B6 margin** uses current master-contract lot sizes (historical lot sizes are not on disk) and a 20% margin rate. Real SPAN + exposure margin for stock futures is 18–35%, so ₹29L is a lower bound.
- **A's ETF unit prices** are the index scaled by the median ETF/index ratio. FINNIFTY and OIL & GAS have no ETF in the spec and are scaled to a ~₹100 unit.
- **The zero-slippage bound** changes no verdict: see the CSV.
- **Concurrent positions:** CNC books are fully cash-funded by construction, so they never exceed capital.
- **Raw outputs:** `backtest/r83_fno/data/r84_sweep.csv`, `r84_b6_capital.csv`.
