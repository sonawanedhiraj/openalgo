<!-- migrated from outputs/2026-06-13_sector_rotation_backtest_report.md on 2026-06-13 | summary: Sector Rotation ETF â€” Backtest Re-Validation (2026-06-13) -->

# Sector Rotation ETF â€” Backtest Re-Validation (2026-06-13)

**Strategy:** `sector_rotation_etf` (variant **26d**, risk-parity inverse-vol) â€”
the newly-scaffolded monthly long-only ETF rotation. Disambiguation: this is the
strategy added in the recent commits (`d1033b55f` scaffold, `22d38ecea`/`4ab695c75`
docs), **not** the older intraday `sector_follow_cap5_vol`.

**Method:** re-ran the canonical Round-26 harness
(`outputs/backtest_round26_etf_combined_2026-06-06/run_etf_combined.py`) verbatim to
reproduce the published headline, then ran an extended harness
(`outputs/2026-06-13_sector_rotation_extended.py`) that reuses the **identical** logic
(same universe, friction, lookbacks, weighting) and adds Sortino, monthly hit rate,
best/worst months, drawdown episodes, and a recent-12-month cut. Read-only on
`historify.duckdb` â€” no DB writes, no orders, no harness changes.

## Window & rationale

**2022-08-01 â†’ 2026-06-04, 47 monthly rebalances.** This is the exact window of the
published Round-26 report â€” chosen for apples-to-apples comparison. It is also the
**data-bound** window: liquid sector-ETF NAVs in `historify.duckdb` only start
~2022. (METALIETF launched 2024-08-20 and is index-shadowed before launch.) A
2020-2026 extension is **not possible without splicing more index-proxy history**,
so it was not run; instead I add a **recent-12-month** cut to test regime
sensitivity.

## Universe & parameters

| | |
|---|---|
| Tradeable ETFs (9) | BANKBEES, ITBEES, PSUBNKBEES, PHARMABEES, METALIETF, PVTBANIETF, FMCGIETF, AUTOBEES, HEALTHIETF |
| Index proxies (2) | FINNIFTY, NIFTYOILANDGAS (no liquid ETF) |
| Momentum sleeve | top-3 by trailing **126d (~6M)** return |
| Low-vol sleeve | bottom-3 by trailing **60d** vol (annualized) |
| Weighting | risk-parity inverse-vol on the union (â‰¤6 holdings) |
| Rebalance | first trading day each month, long-only, unleveraged |
| Friction | ETF buy 0.17% / sell 0.27% + 0.25%/yr expense drag; index-proxy 0.12/0.22 |

## Headline â€” reproduced exactly

| Metric | 26d strat | NIFTY 50 | Published (R26) |
|---|---:|---:|---:|
| Total return | **+69.85%** | +35.0% | +69.8% âœ“ |
| CAGR | **14.79%** | 8.14% | 14.8% âœ“ |
| Sharpe | **1.17** | 0.69 | 1.17 âœ“ |
| Sortino | **1.49** | 0.95 | â€” (new) |
| Max drawdown | âˆ’16.94% | âˆ’15.77% | âˆ’16.9% âœ“ |
| Daily win rate | 57.9% | 54.0% | â€” |
| **Monthly hit rate** | **72.3%** | 55.3% | â€” (new) |
| Total-return alpha | **+34.8 pp** | â€” | +34.8 pp âœ“ |

Every published figure reproduces to the decimal. The new metrics reinforce the
thesis: **Sortino 1.49** (downside-only) sits above the Sharpe, and the strategy was
**green in 72% of months** vs NIFTY's 55%.

## Equity curve (growth of â‚¹1, year-end points)

```
 1.79 |                                  ........26d
 1.70 |                          ,-'''-.,'      *(1.70 end)
 1.59 |                     _,-'        '
 1.40 |                 _,-'            NIFTY 1.35*
 1.26 |          _,,--'        ____....----'''
 1.08 |     _,-''   ____....----'''
 1.00 |*__.-''----'''
      +----------------------------------------------
       2022H2   2023    2024    2025    2026YTD
  26d : 1.000  1.085  1.258  1.590  1.786  1.700
 NIFTY: 1.000  1.044  1.253  1.363  1.506  1.350
```
(PNG skipped â€” matplotlib not installed in the env. Metrics in
`outputs/2026-06-13_sector_rotation_metrics.json`.)

## Per-year breakdown vs NIFTY

| Year | 26d | NIFTY | Alpha |
|---|---:|---:|---:|
| 2022 H2 | +8.5% | +4.4% | +4.1 pp |
| 2023 | +15.9% | +20.0% | **âˆ’4.1 pp** |
| 2024 | +26.4% | +8.8% | **+17.6 pp** |
| 2025 | +12.3% | +10.5% | +1.7 pp |
| 2026 YTD | âˆ’4.8% | âˆ’10.4% | +5.6 pp |

**The entire edge is concentrated in 2024** (+17.6 pp). In 2023 the strategy
*underperformed* a strong broad-market year â€” characteristic of a rotation strategy
that lags when everything rallies and wins when dispersion is high.

## Recent 12 months (regime check)

| Metric | 26d | NIFTY |
|---|---:|---:|
| Total | +3.5% | âˆ’4.6% |
| Sharpe | 0.32 | âˆ’0.30 |
| Sortino | 0.38 | âˆ’0.42 |
| Monthly hit | 61.5% | 38.5% |
| Max DD | âˆ’16.9% | âˆ’15.2% |

Last 12 months are **soft in absolute terms** (Sharpe 0.32 vs full-window 1.17) but
the strategy still beat a falling NIFTY by ~8 pp and stayed positive. The full-window
Sharpe is propped up by 2024; recent realized Sharpe is much lower.

## Top / bottom months

**Best:** 2026-04 +10.6%, 2024-07 +8.0%, 2025-03 +7.5%, 2023-12 +6.7%, 2024-06 +5.6%
**Worst:** 2026-03 **âˆ’15.9%**, 2025-02 âˆ’5.5%, 2024-10 âˆ’5.0%, 2023-02 âˆ’4.4%, 2023-01 âˆ’4.2%

The single âˆ’15.9% month (Mar-2026) is the whole max drawdown.

## Drawdown episodes (deepest 5)

| Peak | Trough (depth) | Recovered |
|---|---|---|
| 2026-02-27 | 2026-03-30 (âˆ’16.9%) | 2026-06-04 (= window end) |
| 2024-09-30 | 2025-03-11 (âˆ’13.9%) | 2025-06-09 |
| 2022-12-15 | 2023-03-15 (âˆ’12.0%) | 2023-06-27 |
| 2023-09-18 | 2023-10-26 (âˆ’7.4%) | 2023-12-04 |
| 2024-06-04 | 2024-06-04 (âˆ’5.0%) | 2024-06-18 |

The âˆ’16.9% drawdown "recovers" exactly on the last bar â€” treat it as **still
near-trough / barely recovered**, not a clean recovery.

## Verdict

**Confirms the prior report.** Sharpe 1.17, CAGR 14.8%, +34.8 pp alpha reproduce
exactly from the same harness and data. The strategy is performing as advertised on
this window.

**Caveats / regime sensitivity to flag before the 2026-06-15 sandbox seed:**

1. **Edge is concentrated in 2024 (+17.6 pp).** Strip 2024 and the alpha is small/negative
   (underperformed in 2023). This is a high-dispersion-regime strategy, not an all-weather one.
2. **Recent-12m Sharpe is only 0.32** vs the 1.17 headline â€” the live deployment is
   entering a soft patch, not the strong regime that made the backtest look good.
3. **Short, ETF-data-bound window (3.8 yrs, one real cycle).** No 2020 COVID crash, no
   2018 in sample â€” the âˆ’16.9% max DD likely **understates** tail risk.
4. **No look-ahead bias found** â€” weights use the rebalance-day close, returns are earned
   strictly from the next day (`seg = days > rb`). Friction and expense drag are applied.
   The one modelling assumption is the **METALIETF index-shadow splice** before 2024-08
   (real ETF NAV unavailable), which injects index â€” not tradable-ETF â€” returns for ~2 yrs.
5. **No harness bug found.** (Only a cosmetic `pct_change` FutureWarning, already silenced
   in the extended script.)

**Bottom line:** the numbers are real and reproducible, but the operator should size the
2026-06-15 seed knowing the historical edge leans heavily on 2024 and the live entry is
into a low-Sharpe regime.
