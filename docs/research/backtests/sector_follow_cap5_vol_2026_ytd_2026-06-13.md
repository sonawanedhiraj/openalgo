<!-- migrated from outputs/2026-06-13_sector_follow_2026_backtest.md on 2026-06-13 | summary: sector_follow_cap5_vol â€” 2026 Calendar-Year Backtest -->

# sector_follow_cap5_vol â€” 2026 Calendar-Year Backtest

**Date:** 2026-06-13
**Strategy:** `sector_follow_cap5_vol` (intraday sector-follow; entry 15:20 IST, T+1 exit 15:25 IST MARKET)
**Window requested:** 2026-01-01 â†’ 2026-06-12
**Window with activity:** 2026-02-02 â†’ 2026-06-10 (first qualifying entry 02-02; stock 1m data ends 06-10)
**Harness:** `outputs/_sector_follow_2026_backtest.py` (adapted from the 4-month parity harness)
**Mode:** read-only on `db/historify.duckdb`. No orders, no DB writes, no commits.

---

## How this was produced (and the one adaptation)

This reuses the production signal code â€” `load_config`, `_series_metrics`,
`passes_gates`, `select_entries`, `compute_qty` imported live from
`services.sector_follow_service` â€” so gate logic, selection, sizing and the cost
model are byte-for-byte identical to the live engine. **Parity check:** the
Mar/Apr/May monthly net P&L reproduce the prior 4-month run exactly
(Mar +6,483.94, Apr âˆ’2,674.08, May +1,391.46).

**The one adaptation:** the live `duckdb_metrics_provider` re-opens the DuckDB file
and re-queries the whole table on every trading day with no upper time bound. Over
109 days, with the live app also holding the DB, that OOM'd. I replaced *only the DB
I/O* â€” the 1m series is loaded once into memory and fed to the **same**
`_series_metrics` function. No look-ahead: `_series_metrics` drops any bar with
`ts > as_of`. The computation is unchanged; only where the bytes come from changed.

---

## Parameters (live config, unchanged)

| Param | Value |
|---|---|
| Universe | `LOCK_STATIC_30` (30 names) |
| Capital | â‚¹2,50,000 |
| Max position | â‚¹50,000 |
| Max concurrent | 5 |
| Gates | sector >+1.0% **AND** stock >+0.5% **AND** vol >1.0Ã— 20d avg |
| Tiebreaker | volume-ratio desc |
| Daily kill switch | âˆ’3% (âˆ’â‚¹7,500) MTM on held book at 15:20 |
| Round-trip cost | 0.0857% (charged once on entry notional, per live `_compute_mtm`) |
| Entry / Exit | 15:20 IST / next-day 15:25 IST MARKET |

---

## âš ï¸ Read this before the numbers â€” two structural caveats

**1. The 6 sector indices only have 1m data from 2026-04-27.** NIFTY and FINNIFTY
go back to Dec 2025; the 6 NIFTY-sector indices (PVTBANK, PSUBANK, IT, AUTO, METAL,
FMCG) do not exist in `historify.duckdb` before 2026-04-27. Because the sector gate
**fails closed** when `sector_ret is None`, the 15 universe names mapped to those 6
indices were **structurally ineligible to trade before late April.** Only the 15
names mapped to NIFTY/FINNIFTY could fire. The `n_via_sector_index` column makes this
explicit: **Feb 0, Mar 0, Apr 1, May 11, Jun 7.** So **Febâ€“Apr exercised only half
the universe and never the real sector-index gate** â€” and that half-universe period
produced the bulk of the cumulative P&L. The genuine full-logic sector-follow
strategy has only ~33 trading days of history (late Apr â†’ Jun).

**2. The strategy is flat 77% of the time.** Of 109 trading days, 84 (77%) produced
**zero** entries; the 5-slot cap was filled on only **7 days (6.4%)**; average 0.64
entries/day. January produced **no trades at all** â€” in a falling market no day
cleared the "sector up >1%" gate. The daily-return Sharpe below is computed on a
series that is mostly zeros, which **suppresses volatility and inflates the
annualized Sharpe** versus a fully-invested book. Treat the 2.30 as not directly
comparable to the R40/R41 blended Sharpes, and as driven by a handful of trades.

---

## Month-by-month

| Month | Trades | Win % | Gross P&L | Net P&L | Avg trade | Best day | Worst day | Cum. net | via sector-idx |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-01 | 0 | â€” | â‚¹0 | â‚¹0 | â€” | â€” | â€” | â‚¹0 | 0 |
| 2026-02 | 6 | 66.7% | â‚¹6,153 | â‚¹5,902 | â‚¹984 | +â‚¹3,033 | +â‚¹2,869 | â‚¹5,902 | 0 |
| 2026-03 | 17 | 58.8% | â‚¹7,196 | â‚¹6,484 | â‚¹381 | +â‚¹7,260 | âˆ’â‚¹2,610 | â‚¹12,386 | 0 |
| 2026-04 | 22 | 40.9% | âˆ’â‚¹1,763 | âˆ’â‚¹2,674 | âˆ’â‚¹122 | +â‚¹2,518 | âˆ’â‚¹1,979 | â‚¹9,711 | 1 |
| 2026-05 | 18 | 44.4% | â‚¹2,156 | â‚¹1,391 | â‚¹77 | +â‚¹2,444 | âˆ’â‚¹3,045 | â‚¹11,103 | 11 |
| 2026-06 | 7 | 71.4% | â‚¹7,867 | â‚¹7,580 | â‚¹1,083 | +â‚¹6,028 | +â‚¹391 | â‚¹18,683 | 7 |

*(P&L net of 0.0857% friction. "via sector-idx" = trades whose sector gate used one
of the 6 real sector indices rather than NIFTY/FINNIFTY.)*

---

## Full-window summary

| Metric | Value |
|---|---|
| Trading days in window | 109 (first entry 02-02; 84 zero-entry days) |
| Total trades | 70 |
| Win rate | 51.4% (36W / 34L) |
| Avg win / Avg loss | +â‚¹1,078 / âˆ’â‚¹591 |
| Payoff ratio | 1.82 |
| Avg trade (net) | +â‚¹267 |
| **Total gross P&L** | **+â‚¹21,609** |
| **Total net P&L** | **+â‚¹18,683** |
| Friction paid | â‚¹2,925 |
| Return on â‚¹2.5L capital | **+7.47%** (gross +8.64%) |
| Max drawdown | âˆ’â‚¹5,185 (âˆ’2.07%) â€” the April episode |
| Sharpe (daily, annualized 252) | **2.30** âš ï¸ see caveat 2 |
| Kill-switch days | 0 |
| Best month / Worst month | **2026-06 (+â‚¹7,580)** / **2026-04 (âˆ’â‚¹2,674)** |
| 5-cap fill rate | 7/109 days (6.4%); avg 0.64 entries/day |
| Entry distribution | 0:84, 1:8, 2:5, 3:3, 4:2, 5:7 |

### NIFTY-50 benchmark (same window)
- NIFTY 26,141 â†’ 23,626 = **âˆ’9.62%** buy-and-hold.
- Strategy +7.47% on capital â†’ **~+17 pp relative**, in a clearly down market.
- **Honest framing:** this is *not* market-beating in a like-for-like sense. The
  strategy sits in cash 77% of days and deploys tiny notional when in; its near-zero
  net market exposure is *why* the âˆ’9.6% tape didn't hurt it. The alpha is real but
  comes from being absent, not from out-trading a long book.

---

## Equity curve (cumulative net P&L, â‚¹, by month-end)

```
 18683 |                                            â—  Jun
       |                                          â•±
 12386 |              â— Mar                      â•±
       |            â•±     â•²                    â•±
 11103 |          â•±        â•²           â— May â•±
  9711 |         â•±          â— Apr â”€â”€â”€â•±
  5902 |    â— Feb
       |   â•±
     0 â—â”€â”€â”´â”€â”€â”€â”€â”´â”€â”€â”€â”€â”´â”€â”€â”€â”€â”´â”€â”€â”€â”€â”´â”€â”€â”€â”€â”´â”€â”€
      Jan  Feb  Mar  Apr  May  Jun
```

Shape: a clean run-up (Febâ€“Mar), a give-back through April, a flat-to-down May, then
a sharp June recovery. Two of the three best months (Feb +5.9k, Jun +7.6k) carry the
whole year and each ran with **zero intra-month drawdown** â€” characteristic of very
few, very clean trades rather than a steady edge.

---

## Honest verdict

**Did the 6-pp / Sharpe-0.97 of the 4-month report decay or hold?** It *improved on
paper* â€” full-2026 Sharpe 2.30 vs the 4-month 0.97, net +â‚¹18.7k vs +â‚¹5.2k â€” but the
improvement is almost entirely the two bookend months (Feb +5.9k from late-Jan
breakouts, Jun +7.6k), each only 6â€“7 trades with no drawdown. Strip those and the
Marâ€“May core is +â‚¹5.2k with a âˆ’2.07% drawdown and a sub-45% win rate in two of three
months. The core is the same modest, choppy engine the 4-month run showed; the
year-level headline is a small-sample tailwind, not a regime shift.

**Where it failed:** April â€” 22 trades, 41% win rate, âˆ’â‚¹2,674, and the âˆ’5,185 max
drawdown. This is also the period right before sector-index data existed, so the gate
was running on the NIFTY/FINNIFTY half-universe only.

**Where it shone:** June (71% win, +â‚¹7,580 on 7 trades) and February (67% win,
+â‚¹5,902 on 6 trades) â€” high-payoff, low-trade-count bursts on strong-breadth days.

**The load-bearing caveats:**
1. **Half the universe couldn't trade before late April** â€” Feb/Mar profit (the bulk)
   never used the real sector-index gate. The strategy as *designed* has ~33 days of
   honest history.
2. **77% of days are flat.** This is a sparse opportunistic engine, not a daily
   5-position book; the "cap5" rarely binds (7 days all year). The Sharpe is inflated
   by the zero-days and rests on a handful of trades.
3. **The NIFTY alpha is an artifact of cash-heaviness**, not trading skill.

**Bottom line:** 2026 *held up* and looks better than the 4-month cut, but the
quality of evidence is thin â€” the encouraging full-logic window (late-Aprâ†’Jun, where
real sector data exists) is only ~6 weeks and ~36 trades. Verdict: **consistent with
a small positive edge, not yet proven.** Keep it on the existing scaffold/paper path;
do not read the 7.5% / Sharpe-2.30 headline as deployable conviction. The single most
valuable next step is more sector-index 1m history (or backfill pre-April) so the
strategy can be judged on the logic it actually runs.

---

## Artifacts
- Per-trade detail: `outputs/2026-06-13_sector_follow_2026_trades.json`
- CSVs: `outputs/2026-06-13_sector_follow_2026/{trades,daily_pnl,monthly_summary}.csv`
- Summary JSON: `outputs/2026-06-13_sector_follow_2026/_summary.json`
- Re-runnable harness: `outputs/_sector_follow_2026_backtest.py`
