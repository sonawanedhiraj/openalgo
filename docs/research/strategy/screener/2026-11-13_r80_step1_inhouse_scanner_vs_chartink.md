# R80 step 1 (partial): the OpenAlgo in-house scanner vs ChartInk, and what already exists

**Status:** PARTIAL. Only step 1 was completed before the usage limit. Steps 2–4 (backfill, exit scoring,
tuning) have not been run.

## The scanner

- **Code:** `services/scanner_service.py` runs the rules in `services/scan_rules/fno_intraday_buy_chartink.py`
  and `fno_intraday_sell_chartink.py`. These are line-by-line mirrors of ChartInk's `fno-intraday-buy-20`
  and `alert-for-intraday-sell-fno` screeners.
- **BUY (11 gates):**
  - Price: close between 100 and 5000.
  - Gap: close above yesterday's close by `buy_pct`; open above yesterday's close and above yesterday's pivot.
  - Volume: daily volume above its SMA50 and its SMA200.
  - Volatility: weekly ATR(21) above 5% of close.
  - Momentum: 15-minute RSI(14) above 50.
  - Trend: 5-minute Supertrend(7,3) below the running close, and the previous Supertrend at or above
    yesterday's close.
- **SELL (9 gates):** the mirror of BUY, with two differences. Volume is a single gate (today's volume above
  yesterday's), and the second Supertrend gate is absent, as in ChartInk's own screener.
- **Earlier validation:** R56 checked the offline replay against ChartInk on 2026-07-03 and matched 10 of 10.

## Measured live agreement (2026-06-30 → 11-13, 88 trading days)

The Cowork task `scanner-vs-chartink-daily-comparison` was retired. It was replaced by the in-process 15:45
job `scanner_comparison_eod`, which writes the `scanner_comparison` table (176 rows from 2026-06-11). The
figures below are recomputed directly from `scan_results` (in-house) and `scan_cycle` (ChartInk), per
symbol-day and side. "Alive days" excludes days the in-house feed produced nothing (outages and tick
starvation).

| Side | ChartInk symbol-days | In-house symbol-days | Both | Recall of ChartInk, all days / alive days | Share of in-house hits ChartInk also had | In-house first hit vs ChartInk (median) |
|---|---:|---:|---:|---:|---:|---:|
| BUY | 288 | 555 | 226 | 78.5% / **84.0%** | 40.7% | **13.7 min earlier** (88% earlier) |
| SELL | 299 | 708 | 236 | 78.9% / **89.7%** | 33.3% | **9.2 min earlier** (81% earlier) |
| All | 587 | 1,263 | 462 | 78.7% / **86.8%** | 36.6% | 12.1 min earlier |

**Verdict:**
- **Recall:** the in-house scanner reproduces about **87% of ChartInk's hits** on days its feed is up, and
  fires about 12 minutes earlier.
- **Over-firing:** it also fires about **2.2× as many symbol-days**. Only 37% of its hits were in ChartInk's
  list.
- **As a stand-in:** it is a credible superset of ChartInk, but not an equivalent. Run historically, it will
  generate more, and earlier, signals than ChartInk did.
- **Caveat:** part of the extra hits may be ChartInk's alert-delivery cadence rather than rule differences.
  That has not been separated yet.

## Already done — do not redo

- **R56** (registry row 56) already ran this scanner through the full engine over
  **2025-06-20 → 2026-07-06**: 1,177 trades, net −₹251,526, 0 of 14 green months.
- **R57 and R60** explored exits and entries on the same signals. Hold-to-EOD and the mild-gap afternoon band
  look promising but are fragile.
- **The untouched window for R80 is therefore 2024-01-01 → 2025-06-19 only.** The requested 2024–2026-05
  range overlaps R56 from 2025-06-20 onward.

Data: `backtest/r80_scanner/data/live_agreement.json` (untracked).
