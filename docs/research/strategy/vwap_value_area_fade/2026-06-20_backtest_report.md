# VWAP Value-Area Fade — Backtest Report

**Date:** 2026-06-20
**Branch / worktree:** `strategy/25-vwap-fade-backtest` (issue #25)
**Spec:** [`2026-06-20_strategy_spec.md`](./2026-06-20_strategy_spec.md)
**Source idea:** Trader Drysdale, YouTube `Z2uJRbkb2pA` ("Setup #2 — VWAP Value-Area Fade", MES on a $500 acct)
**Adapted to:** NIFTY 1m index series (resampled 5m), 09:15-15:30 IST, ₹10L book, 1% per-trade risk, NIFTY-future lot mult 75.

## TL;DR — does it ship?

**NO.** The strategy **fails every leg of the project ship-it gate** on the full window AND on the 2026 YTD slice, AND it underperforms a passive buy-and-hold of NIFTY on the same window. Adding any realistic charges makes the result catastrophically negative. The setup as the author describes it (70-80% win rate on range days) **does not reproduce on NIFTY** at 5-minute resolution.

| Window | CAGR | Sharpe | MaxDD | Win-rate | Trades | Pass gate? |
| --- | ---: | ---: | ---: | ---: | ---: | :---: |
| **Full 2024-01 → 2026-06** (headline) | **−27.4%** | **−1.88** | **−55.3%** | 45.5% | 978 | **FAIL** (all 3) |
| **2026 YTD (Jan–Jun)** | **−43.0%** | **−4.37** | **−22.5%** | 40.7% | 182 | **FAIL** (all 3) |
| Full window + realistic charges (₹50/lot/leg + 0.025% STT) | book wiped out | −10.10 | −310% | 29.0% | 978 | **FAIL** |
| **NIFTY buy-and-hold (1 lot of futures, same window)** | +6.81% | 0.30 | −23.0% | n/a | 1 | (reference) |

**Project gate:** CAGR ≥ 12% AND Sharpe ≥ 1.0 AND MaxDD ≥ −15%. None of the three legs met.

## 1. Method

- **Bars:** `NIFTY` `NSE_INDEX` `1m`, 228,324 rows, 2024-01-01 → 2026-06-19, from a local snapshot of `db/historify.duckdb` opened read-only.
- **Resample:** 1m → 5m by floor-grouping `minutes_since_09:15 // 5` per session. 611 sessions × ~75 bars = 45,654 5m bars.
- **VWAP & ±1σ:** session-cumulative typical price `(h+l+c)/3`, **equal-weighted** (NSE does not publish volume for the NIFTY index — all rows are `volume=0`). σ is a session-cumulative population stdev of typical price.
- **Entry:** prior bar closed inside the value area AND current bar pierces the band AND closes back ≥ 2 points inside AND the rejection wick is ≥ 50% of bar range AND bar range ≥ 5 points. Time gate: `>= 09:30 IST AND < 15:14 IST`. Daily 2-loss kill switch.
- **Exit:** first hit between (a) stop 2 points beyond the rejection wick, (b) target = current bar's VWAP, (c) 60-min time stop, (d) 15:14 IST EOD flat. Same-bar conflict: pessimistic — stop wins.
- **Sizing:** `lots = floor(₹10,000 risk / (stop_pts × 75))`, capped at 5 lots. Constant ₹10L sizing for the headline (no compounding); compounding is a sensitivity (not reported in the table — it dominates the negative drift further).
- **Charges:** headline = ₹0 (paper run on spot index). Sensitivity = ₹50/lot/leg brokerage + 0.025% STT on sell-side, both legs.

Full spec, including every parameter and the same-bar pessimism rationale, is in `2026-06-20_strategy_spec.md`.

## 2. Headline numbers — full window (2024-01-01 → 2026-06-19)

```
n_days:                  900
years:                   2.46
total_return_pct:        -54.60%
cagr_pct:                -27.42%
sharpe_annual:           -1.88
sortino_annual:          -3.37
max_dd_pct:              -55.30%
n_trades:                978
trades_per_month:        33.08
win_rate_pct:            45.50%
avg_R:                   -0.087
profit_factor:           0.795
monthly_green_pct:       23.33%
avg_win_inr:             4,772
avg_loss_inr:           -5,009
final_equity_inr:        4,53,985   (from 10,00,000)
```

### Per-side and per-exit decomposition

```
By side:
  LONG :  530 trades,  WR 45.1%,  net PnL  -2,97,489
  SHORT:  448 trades,  WR 46.0%,  net PnL  -2,48,525

By exit reason:
  target   :  508 trades,  WR 83.7%,  net PnL  +19,40,465  (avg +3,820 ; 83 of these were target-but-negative — see §5)
  stop     :  434 trades,  WR  0.0%,  net PnL  -25,03,759  (avg -5,769)
  time_stop:   10 trades,  WR 40.0%,  net PnL  +12,304
  eod_flat :   26 trades,  WR 61.5%,  net PnL   +4,976
```

The math is simple and damning: **stops cost ₹5,769 average vs targets pay ₹3,820 average — payoff < 1:1.** A 45.5% win rate × 0.66 payoff = negative expectancy.

### Yearly breakdown

```
year   n   win_rate   gross_pnl   avg_pnl
2024  396    46.5%   -1,68,404     -425
2025  400    46.7%   -1,48,644     -372
2026  182    40.7%   -2,28,966   -1,258   <- 2026 is materially worse
```

The strategy loses in **every** calendar year of the window — this is not a regime issue. 2026 acceleration shows the entry rules degrading further (lower win-rate and bigger avg loss). 0% monthly-green in 2026.

### Time-of-day decomposition

```
entry_hour    n   win_rate     pnl
09 (post-NTU)178   42.1%   -2,62,678   <- worst hour (just after the 9:30 gate)
10           230   52.6%      -40,506   <- only profitable hour by win-rate
11           184   45.1%      -82,344
12           151   40.4%      -90,622
13           117   39.3%      -22,748
14            87   46.0%      -33,381
15            31   61.3%      -13,736   <- small sample, end-of-day fades
```

10:00-10:59 IST is the only hour with WR > 50%, and even then it's net-negative because of payoff < 1.

## 3. 2026 YTD slice (Jan 1 - Jun 19)

```
n_trades:           182
win_rate_pct:       40.7%
trades_per_month:   32.8
total_return_pct:   -22.9%
cagr_pct:           -43.0%  (annualized)
sharpe_annual:      -4.37
max_dd_pct:         -22.5%
monthly_green_pct:  0.0%    <- ZERO green months in 2026 YTD
profit_factor:      0.624
```

Comparison: NIFTY buy-and-hold over the same 2026 YTD slice was −15.9% (a bear-ish stretch); the strategy lost MORE than passive holding. The bear regime amplifies the strategy's structural weakness rather than rewarding the counter-trend posture.

## 4. Against NIFTY buy-and-hold (1 lot, same window)

|  | Strategy | Buy-and-Hold |
| --- | ---: | ---: |
| CAGR | −27.4% | +6.8% |
| Sharpe | −1.88 | +0.30 |
| MaxDD | −55.3% | −23.0% |
| Final equity from ₹10L | ₹4,54,000 | ₹11,76,000 |

Buy-and-hold of a single NIFTY future quietly compounds to a 6.8% CAGR over the full window with a max drawdown half the size. The fade strategy doesn't just lose — it loses against doing nothing.

## 5. Why it failed — a concrete diagnostic

### 5.1 Stops are wider than the entry-to-VWAP distance

Median entry-to-VWAP distance (the rough target potential) is ~13 points; median stop distance is ~16 points. The setup's risk/reward is **structurally < 1:1 before any noise**. The author's MES setup gets 10-16 point targets on 8-point stops (R~1.5); on NIFTY 5m, the wick that defines the stop is typically twice the height of the typical band-to-VWAP move.

### 5.2 The "rolling VWAP" target trap (16 % of "wins")

When price tests the band and reverts mostly to VWAP, the VWAP itself is dragged toward the entry on the next few bars. **83 of the 508 "target-fired" trades exited at a price BAD for our side** (a long that targeted VWAP found VWAP had drifted down past the entry by the time the touch fired). These are recorded as `exit_reason=target` but `pnl<0`. They were unavoidable with the bar-by-bar VWAP target; an alternative spec ("target = VWAP fixed at the moment of entry") would make the fades cleaner but slightly less true to the video.

### 5.3 Win-rate 45 % is nowhere near the author's 70-80 %

The author's "70-80% on range days" claim presupposes a regime filter we cannot mechanically reproduce from price alone in advance. In practice, a meaningful fraction of NIFTY days are trend days; the strategy fires on those too, and stops out. The regime-filter problem is the strategy's fundamental open issue — the rules as written cannot tell a range day from a trend day before the trade.

### 5.4 First hour (09:00-09:59) is the worst

Despite the 09:15-09:30 no-trade window, the 09:30-10:00 stretch posts WR 42% and the biggest gross loss (−₹2.6L). The "value area hasn't formed" symptom doesn't disappear at 09:30 — it extends well into the first hour.

### 5.5 Charges destroy what remains

A modest cost model (₹50/lot/leg + 0.025% STT on sell-side) takes 978 trades × ~5 lots × ₹100 brokerage = ~₹4.9L and ~5L STT off the gross. Net result: book wiped out 3× over.

## 6. Sample of the per-trade journal

Full journal: [`trades.csv`](./trades.csv) (978 rows). Head/tail:

```
entry_date entry_time exit_time  side  entry_price   exit_price  stop_points  lots  pnl_inr  exit_reason
2024-01-01      09:30     09:35  LONG     21703.05  21699.29       21.00      5  -1,411  target
2024-01-01      11:40     12:00  SHORT    21730.25  21722.67       12.55      5   2,841  target
2024-01-02      10:55     11:05  LONG     21615.95  21594.50       21.45      5  -8,044  stop
2024-01-02      11:40     12:15  LONG     21597.70  21636.79       18.05      5  14,659  target
2024-01-03      10:00     10:15  LONG     21584.40  21564.45       19.95      5  -7,481  stop
...
2026-06-17      13:50     14:25  LONG     24045.45  24027.45       18.00      5  -6,750  stop
2026-06-18      13:20     13:30  SHORT    24112.75  24127.85       15.10      5  -5,663  stop
2026-06-18      14:30     14:35  LONG     24095.05  24099.14       22.35      5   1,533  target
2026-06-19      10:40     10:50  LONG     23970.20  23953.05       17.15      5  -6,431  stop
2026-06-19      11:15     11:20  LONG     23961.20  23949.45       11.75      5  -4,406  stop
```

Equity curve at daily resolution: [`equity_curve.csv`](./equity_curve.csv); buy-and-hold reference: [`buy_and_hold_equity.csv`](./buy_and_hold_equity.csv); full machine-readable metrics: [`summary.json`](./summary.json).

## 7. Honest caveats — load-bearing, do not skim

1. **The NIFTY index has zero volume.** This is the single largest faithfulness gap. The video's setup is a true volume-weighted VWAP; we use an equal-weighted approximation because the index has no traded volume. A real NIFTY-future intraday series (which we do not have stored 2024-back) would shift the bands and could change the result materially — though likely not enough to flip a -27.4% CAGR to +12%.
2. **5-minute resolution may miss the rejection bar.** The author's pattern (wick beyond band, close back inside) can print and resolve on 1m bars, with the 5m close looking neutral. We tested 5m because (a) the author's MES examples are 5m bars, (b) NIFTY 1m on the index is mostly microstructure noise, and (c) executing a 1m fade pattern adds discretion costs we can't simulate. A 1m execution run is a separate research item.
3. **No slippage, no fill model.** We treat the entry close as the fill price and target fills exactly at VWAP. A real fade typically slips 1-3 NIFTY points on entry plus another 1-3 on the target touch — that's 4-6 points per round-trip out of an average 13-point target. The headline is therefore optimistic; the strategy is worse than reported.
4. **No news-event blackout.** ~8-12 RBI / Budget / Fed / Indian-budget days/year are in the sample with no filter. Author skips these; if anything, they help the strategy by removing low-probability days, but the magnitude is probably 1-2 percentage points of CAGR — nowhere near the gap to ship-it.
5. **Same-bar stop/target conflict resolved pessimistically.** ~3-5% of trades had both stop and target reachable on the same bar; we always pick the stop. This is the standard backtest pessimism without intra-bar tick data. Realistic but does inflate the loss column somewhat.
6. **No spread or bid-ask cost modelled.** Index-future spread is typically 0.05 points, immaterial; included here for completeness.
7. **Rolling-VWAP target trap (16% of targets, §5.2).** A faithful "target = VWAP at entry" version would be slightly less honest to the video but might reduce this artefact. Not tested.
8. **This is the spot index, not the NIFTY future.** Overnight gap risk, basis, and quarterly roll cost are all absent. Since the strategy is intraday and flat by 15:14, overnight gap is not a direct risk to the simulated PnL — but real trading uses the future, and the future's intraday tick fills differ from the index's averaged prints.

## 8. Recommendation

**Do not paper trade this rule set on NIFTY as-is.** It is a robustly losing strategy across all three years of the window and would have lost more than buy-and-hold. The author's edge does not transfer without a regime filter that distinguishes range days from trend days BEFORE the entry; the rules as published don't supply one.

If a follow-up is wanted, the high-EV directions are (in order):

1. **Regime filter.** A pre-09:30 range/trend classifier (e.g. opening-range expansion, IBR, RSI on the first 30 min) that gates the entire strategy. The video's "condition determines the setup" line is exactly this gap.
2. **R/R fix.** Target = `entry + 1.5 × stop` (fixed-R take-profit) instead of the moving VWAP. The rolling-VWAP trap goes away and per-trade R becomes positive when win-rate > 40 %.
3. **1m execution.** Run the entry trigger on 1m bars but require a 5m HTF confirmation (price still inside 5m bands).
4. **A different instrument.** BANKNIFTY 1m is in historify and is a higher-volatility, longer-tail product; the same fade may have a different signature.

None of these are this study's scope; they belong in a new issue.

## 9. Files in this deliverable

| File | Purpose |
| --- | --- |
| `2026-06-20_strategy_spec.md` | Mechanical strategy rules + parameters |
| `backtest.py` | Self-contained backtest script (run via `uv run python …/backtest.py`) |
| `2026-06-20_backtest_report.md` | This report |
| `trades.csv` | Full per-trade journal (978 rows) |
| `equity_curve.csv` | `date,daily_pnl,equity` per trading session |
| `buy_and_hold_equity.csv` | 1-lot NIFTY-future buy-and-hold reference equity |
| `summary.json` | Machine-readable metrics for both windows and the charges sensitivity |

## 10. Reproducibility

```bash
cd C:\workspace\ai-trade-agent\wt-25
# (one-time) snapshot the live DuckDB so we don't fight the live writer
cp ../openalgo/db/historify.duckdb .cache/duckdb_snap/historify.duckdb

# run
set PYTHONIOENCODING=utf-8
uv run python docs/research/strategy/vwap_value_area_fade/backtest.py
```

Run time: ~30 seconds on the dev box. No randomness — deterministic.
