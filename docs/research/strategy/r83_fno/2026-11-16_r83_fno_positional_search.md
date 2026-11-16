# R83: search for a working strategy on NSE F&O stocks (#788)

**Date:** 2026-11-16
**Pre-registration:**
- batch A/B: `b853e39f7`
- batch C: `9606ae3a1`, committed before any batch-B result

**Freezes:**
- batch B: `f26e5d904`
- batch C: written before its holdout ran; the commit landed after, see Disclosures

**Harness:** `backtest/r83_fno/`

## Answer

**Nothing passes.** No candidate is profitable after real costs on the sealed holdout.

The two leads carried over from 2026-06-06 do not survive a fair benchmark:
- **Sector momentum / low-vol / risk-parity (R21b/R24/R26)** earns 11–16% a year. All of it is the equal-weighted sector basket; the momentum/low-vol selection adds −0.4%/yr (t −0.10) on 2019–22, the period it never saw. Those rounds were costed and benchmarked against NIFTY only, and were never deployed (verified in R81).
- **The momentum family on F&O stocks** (B1, B6 futures long/short, C1 residual momentum) passed TRAIN and VALIDATE, then lost 17–20%/yr against the universe on the Jul-2024 → Nov-2026 holdout. C1's loss is significant: t −2.0.

## Results

Excess = net strategy return minus the equal-weight eligible F&O universe (for A: the equal-weight of the same 11 sector series). Costs are Kite-verified, slippage 5 bps per side.

| Id | Strategy (vehicle) | Furthest stage | Result there |
|---|---|---|---|
| A | Sector ETF rotation, R26 spec (ETF CNC) | holdout | excess −0.4%/yr, t −0.10 (absolute CAGR 11.2%, Sharpe 0.62, MaxDD −31%) |
| B1 | 6-1 momentum, top 10 (CNC) | holdout (≥ ₹1L) | excess −16.8%/yr, t −1.6, placebo p 0.998 |
| B2 | Low-vol, bottom 10 (CNC) | validate | excess −24.7%/yr, t −3.4 |
| B3 | Risk-parity momentum + low-vol (CNC) | train | excess −12.6%/yr |
| B4 | Top-3 sectors → top-3 stocks each (CNC) | train | excess −14.2%/yr |
| B5 | Weekly 5-day-loser reversal (CNC) | train | excess −79%/yr (costs 68%/yr) |
| B6 | Market-neutral momentum, top/bottom 10 (stock futures) | holdout | −19.4%/yr, t −1.18, placebo p 0.98 |
| C1 | Residual (beta-adjusted) 6-1 momentum (CNC) | holdout (≥ ₹1L) | excess −19.9%/yr, t −2.0, placebo p 0.999 |
| C2 | 52-week-high proximity (CNC) | train | excess −12.0%/yr |
| C3 | Smooth momentum: low-vol among top-30 momentum (CNC) | train | excess −8.9%/yr |
| C4 | B3 rebalanced quarterly (CNC) | validate | excess −22.6%/yr |
| C5 | New F&O additions, 3-month hold (CNC) | train | excess −12.1%/yr |

**Capital.** At ₹20k every CNC candidate loses 4–6%/yr more than at ₹10L: the ₹15.34 DP charge per scrip sold and integer-share cash drag. B1 and C1 cleared TRAIN/VALIDATE only from ₹1L. B6 needs about ₹25L+ of margin (20 stock-futures lots).

## Method

- **Costs:** verified on 2026-11-16 against Kite `POST /charges/orders`, one leg at a time.
  - CNC: buy 11.9 bps, sell 10.4 bps + ₹15.34 DP per scrip.
  - ETF: ~2.4 bps round trip + DP.
  - Stock futures: 6.3 bps round trip at ₹7.2L, with STT 0.05% on the sell side; today's rate is applied to all history.
  - Slippage 0 / 5 / 15 bps per side; the 0 and 15 bps results are in the CSVs.
- **Universe:** point-in-time F&O membership from 95 NSE month-end lists (Dec 2018 → Oct 2026). Eligible names have ≥ 252 bars, price ≥ ₹50, and 20-day median traded value ≥ ₹10 cr.
- **Data:** Kite daily bars 2018-01 → 2026-11-13. 25 historical names have no Kite history (2.8% of symbol-months).
- **Execution:** signal at the period-end close, traded at the next open, integer shares, only the difference between old and new holdings traded. Simulated at ₹20k, ₹1L, ₹5L and ₹10L.
- **Periods:**
  - B and C: TRAIN 2019–22, VALIDATE 2023-01 → 2024-06, SEALED HOLDOUT 2024-07 → 2026-11-13.
  - A: in-sample 2022-08 → 2026-06 (where the spec was found); holdout 2019-01 → 2022-07 plus 2026-07 → 11.
- **Gates:** holdout excess t ≥ 2, absolute return > 0, TRAIN and VALIDATE positive, ≥ 7/10 half-years positive, VALIDATE plateau, and selection placebo p < 0.05 (1,000 random same-size portfolios).

Full ledger in CSV:
- `backtest/r83_fno/data/A.csv`
- `backtest/r83_fno/data/tv_B.csv`
- `backtest/r83_fno/data/tv_C.csv`
- `backtest/r83_fno/data/holdout.csv`
- `backtest/r83_fno/data/holdout_batch_C_to_holdout.csv`

## Disclosures

1. **`eligible()` bug before freezing.** The first B run keyed the PIT list on the signal month, which made it one month stale and left the first two TRAIN months empty. It was fixed to the pre-registered "trading month M uses the M−1 list" rule, and B was re-run before the freeze. The holdout had not been opened.
2. **Batch-C freeze commit.** It was rejected by the end-of-file hook, and the C1 holdout ran before the freeze was committed (`1936f00d5`/`616e099b6`). The freeze was mechanical: C1 was the only batch-C primary positive in both TRAIN and VALIDATE at any capital.
3. **Survivorship and dividends.** Survivorship bias (25 missing names) and excluded dividends both understate or flatter in ways that cannot rescue a −17%/yr holdout.
4. **Not run.** Option-based vehicles were not tested: there is no expired-contract history from Kite, and daily F&O bhavcopies would be needed.
