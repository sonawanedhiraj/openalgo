# R77: NIFTY noise-boundary intraday momentum — reproduce the handover, then price it honestly

> Picks up Dheeraj's handover *"NIFTY intraday momentum (noise boundary)"* (a cloud research
> chat with no Kite access). The operator asked for this as **R74**, but **R74 is already in the
> registry** (open15 2-year backtest) and R76 is taken too, so it is filed as **R77**.
> Issue [#780](https://github.com/sonawanedhiraj/openalgo/issues/780).
> This round only reproduces the baseline and checks costs. **No rule was changed** and
> none of the handover's 5-step plan was started.

**Headline: at Zerodha's real current charges the strategy is underwater before any further work.**

**Data (see §0):**
- **Kite serves NIFTY 50 1-minute bars from 2015-01-09.** Kite is now the primary dataset.
- **The handover's public file is a Kite dump.** All 851,460 of its bars are identical to Kite's,
  and Kite returns the same backtest result. So the data was clean, and the mismatch against the
  handover is in its code.
- **Real cost:** a 1-lot NIFTY futures round trip costs **5.95 bps** of notional, measured with
  Kite's own charges calculator at 2026-11-13 prices. **STT alone is 5.00 bps** of that (0.05% on
  the sell leg since 2026-04-01). The handover assumed **2.5 bps**.
- **Handover's own figures re-costed:** its baseline (gross +4.60 bps/day, 2.61 position changes a
  day) goes from **+1.32 to −3.16 bps/day**, i.e. from +3.3%/yr to −7.9%/yr.
- **Our reproduction, re-costed:** it trades less (1.68 changes a day), and goes from +2.08 to
  **−0.82 bps/day** (Sharpe 0.72 → −0.28).
- **Breakeven is below today's cost either way:** about 5.0 bps for our reproduction and about
  3.5 bps for the handover's figures.

## 0. Data source — why public data, and what Kite and historify hold (addendum, 2026-11-13)

Dheeraj asked: "Why public data if we can download from Zerodha or leverage historify data?"
He was right to ask. The answer, measured:

| Source | NIFTY 50 index 1-minute coverage | Role in R77 |
|---|---|---|
| **Kite historical API** (token 256265) | **From 2015-01-09 09:15 to today.** Probed back to 2008: every January 2008–2014 returns 0 bars, and the first bar Kite has is 2015-01-09 09:15. | **Primary dataset** (2015-01-09 → 2024-03-28 fetched, 2,274 days, 851,610 bars) |
| Public GitHub file (sandeepkapri) | 2015-01-09 → 2024-03-27 12:59 | **Fidelity check only.** It tests that our code reads the handover's own data the same way |
| historify.duckdb | **From 2024-01-01** to 2026-11-12, 265,519 bars. Confirmed unchanged (read through the app's local-DB history path; the file is locked while OpenAlgo runs) | Third cross-check where it overlaps |

**Bar-by-bar comparison (decisive for the data question):**
- **Public file vs Kite, 2015-01-09 → 2024-03-27:**
  - **All 851,460 public bars match a Kite bar exactly**, 100.000% on open, high, low and close.
  - All 2,273 days are in both, with identical bar counts per day.
  - No timestamp offset: shifting by ±1 minute drops the match to 0.8%.
  - No OHLC violations and no bad prints.
  - **The public file is a Kite dump.** Its only defects are at the end: **2024-03-27 is truncated at 12:59** (150 bars short, and its last bar was captured mid-minute: close 22,169.95 vs Kite's 22,169.30), and **2024-03-28 is missing**.
  - The 30 short days (1–8 bars missing) are in Kite too. They are Kite's own gaps, not file corruption.
- **Kite vs historify:**
  - 2024-01-01 → 2024-03-28: 22,980 bars, **100% identical**.
  - Over the holdout window (2024-04-01 → 2026-11-12): 242,479 bars, **100% identical**, with the same days and the same bar counts.
- **Conclusion:** the handover's baseline was not built on dirty data. **Kite is now the primary dataset** (auditable, current, and the same source as everything else we run). The public file stays only as the code-fidelity check.

**Baseline on Kite vs the public file:** same span; Kite has one more full day, because its 2024-03-27 is complete.

| Source | Days | Gross bps/day (Sharpe) | Net @2.5 bps (Sharpe) | Net @ real 5.95 bps (Sharpe) | Changes/day |
|---|---:|---:|---:|---:|---:|
| Public file, full days only | 2,229 | +4.18 (1.46) | +2.08 (0.72) | −0.82 (−0.28) | 1.68 |
| **Kite, full days only** | 2,230 | **+4.18 (1.46)** | **+2.08 (0.72)** | **−0.82 (−0.28)** | 1.68 |
| Public file, ≥90%-complete days | 2,235 | +4.44 (1.54) | +2.36 (0.82) | −0.51 (−0.18) | 1.66 |
| Kite, ≥90%-complete days | 2,236 | +4.44 (1.54) | +2.36 (0.82) | −0.51 (−0.18) | 1.66 |
| *Handover (reported)* | *2,263* | *+4.60 (1.63)* | *+1.32 (0.46)* | *−3.16 (re-costed)* | *2.61* |

- **The results are identical across the two sources.** So the gap against the handover (mainly 1.7 vs 2.6 position changes a day) **comes from the implementation, not the data**.
- Resolving it needs the handover's `noise_boundary.py`.
- The cost verdict is the same on every source: **negative at today's 5.95 bps**.

## 1. Reproduction on the public file — a code-fidelity check (CONTRADICTED on turnover; shown side by side, not tuned)

**Data:** github.com/sandeepkapri/Nifty50-Minute-Data, `nifty50_candlestick_data.csv`
(51.3 MB, 852,087 bars).
- The file runs from **09-01-2015 to 27-03-2024**. The last day is partial (ends 12:59), and
  2024-03-28 is not in the file.
- 2,273 days in total. 2,243 have all 375 bars (09:15–15:29); 30 are 1–8 bars short.

**Implementation:** written from the spec, not from the handover's code
(`backtest/noise_boundary_r77/noise_boundary.py`).
- sigma per minute-of-day slot = mean of `|close/day_open − 1|` over the previous 14 days.
- Bands = max/min(open, prev close) × (1 ± sigma).
- Decisions at 09:44 then every 30 minutes up to 15:14; flat at the 15:29 close.
- Cost = 1.25 bps × |position change|, counting a reversal as 2 units and including the final
  flatten. This is exactly the handover's arithmetic: its 4.60 − 1.32 = 3.28 = 2.61 × 1.25.

| | Days | Gross bps/day | Gross Sharpe | Net bps/day @2.5 | Net Sharpe | Changes/day | Gross %/yr | Net %/yr |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **Handover (reported)** | 2,263 | **+4.60** | **1.63** | **+1.32** | **0.46** | **2.61** | +11.5 | +3.3 |
| R77 A — full days only (spec) | 2,229 | +4.18 | 1.46 | +2.08 | 0.72 | 1.68 | +10.5 | +5.2 |
| R77 B — keep ≥90%-complete days (ffill) | 2,235 | +4.44 | 1.54 | +2.36 | 0.82 | 1.66 | +11.2 | +5.9 |

- **Gross is close; turnover is not.** Our gross edge is 4.2–4.4 bps/day against 4.60, but we
  trade **1.66–1.68 units a day against 2.61**. Higher net and higher net Sharpe come purely from
  the lower turnover.
- **Spec readings tried, none reproduces the handover's 2.61 changes a day.** These were probes
  for an ambiguity in the spec, not tuning:

  | Reading | Gross bps/day | Changes/day |
  |---|---:|---:|
  | Day open taken as the 09:15 close | 3.96 | 1.83 |
  | Decisions on the :45/:15 grid | 3.75 | 1.71 |
  | Bands from today's open only (no prev-close max/min) | 5.83 | 2.38 |
  | Sigma measured from prev close | 3.18 | 1.23 |
  | Warm-up with fewer than 14 days | 4.23 | 1.68 |
  | Decide every minute (diagnostic, not the spec) | 4.86 | 7.16 |

- **Next step to resolve it:** the handover's `noise_boundary.py` would settle the turnover
  difference. Please paste it. The cost conclusion below does not depend on it: **both** versions
  are negative at the real cost.

**Per year (reproduction A; net bps/day):**

| Year | Days | Gross | Net @2.5 | Net @ real 5.95 | Changes/day |
|---|---:|---:|---:|---:|---:|
| 2015 | 215 | +2.89 | +0.52 | −2.75 | 1.90 |
| 2016 | 246 | +6.23 | +4.28 | +1.61 | 1.55 |
| 2017 | 246 | +2.69 | +0.79 | −1.83 | 1.52 |
| 2018 | 245 | +3.60 | +1.41 | −1.61 | 1.76 |
| 2019 | 240 | +5.01 | +2.78 | −0.29 | 1.78 |
| 2020 | 247 | +5.35 | +3.15 | +0.12 | 1.76 |
| 2021 | 245 | +4.90 | +2.95 | +0.24 | 1.57 |
| 2022 | 246 | +6.08 | +4.07 | +1.29 | 1.61 |
| 2023 | 240 | −0.07 | −2.25 | −5.25 | 1.74 |
| 2024 (Jan–Mar) | 59 | +7.16 | +5.17 | +2.42 | 1.59 |

- At 2.5 bps the gross edge pays in 9 of 10 years.
- At the real cost, 5 of the 9 full years are negative.
- 2023 had no gross edge at all.

## 2. The charges question (decisive)

**Kite's own virtual contract note** (`POST /charges/orders`) is a calculator: nothing is ordered.
It priced 1 lot (65) of `NIFTY26NOVFUT` at 25,118 (notional ₹16,32,670), a BUY plus a SELL.
NRML and MIS are priced identically.

| Component (round trip) | ₹ | bps of notional |
|---|---:|---:|
| **STT** (0.05%, sell leg only) | 816.34 | **5.000** |
| Exchange turnover (both legs) | 59.76 | 0.366 |
| Brokerage (₹20 × 2) | 40.00 | 0.245 |
| Stamp duty (buy leg) | 33.00 | 0.202 |
| GST | 18.54 | 0.114 |
| SEBI fee | 3.27 | 0.020 |
| **Total, round trip** | **970.90** | **5.947** |
| Same, at the pre-2026-04-01 STT of 0.02% | 481.10 | 2.947 |
| Slippage of 1 index point per leg (not in the above) | 130.00 | +0.796 |

- **About 84% of the cost is the tax.** Brokerage and exchange fees are about 0.95 bps of the
  total. No broker choice and no order type changes STT.
- **Pre-April-2026:** at the old 0.02% STT the round trip was 2.95 bps. That is close to the
  handover's 2.5 assumption, which was reasonable until April 2026 and is not now.
- **Cost per unit of position change** = half the round trip. Every day starts and ends flat, so
  buys and sells balance.

## 3. Net at each cost level (+ turnover)

Net bps/day, net %/yr and net Sharpe. Gross does not change with cost: A +4.18 bps/day
(Sharpe 1.46); B +4.44 (1.54).

| Round-trip cost | R77 A (1.68 chg/day) | R77 B (1.66 chg/day) | Handover numbers re-costed (2.61 chg/day) |
|---|---|---|---|
| 2.50 bps (handover assumption) | +2.08 · +5.2% · 0.72 | +2.36 · +5.9% · 0.82 | +1.34 · +3.3% |
| 2.95 bps (pre-Apr-2026 STT) | +1.71 · +4.3% · 0.59 | +1.99 · +5.0% · 0.69 | +0.75 · +1.9% |
| 4.46 bps (real −25%) | +0.43 · +1.1% · 0.15 | +0.73 · +1.8% · 0.25 | −1.22 · −3.1% |
| **5.95 bps (REAL, today)** | **−0.82 · −2.1% · −0.28** | **−0.51 · −1.3% · −0.18** | **−3.16 · −7.9%** |
| 7.43 bps (real +25%) | −2.07 · −5.2% · −0.71 | −1.75 · −4.4% · −0.60 | −5.10 · −12.8% |
| 6.74 bps (real + 1 pt slippage/leg) | −1.49 · −3.7% · −0.51 | −1.17 · −3.0% · −0.40 | −4.20 · −10.5% |

- The handover column is arithmetic on its reported means (net = 4.60 − 2.61 × cost/2), so it has
  no Sharpe.
- %/yr in that column uses the handover's own scaling (net × 2.5 ≈ its 1.32 → 3.3%).
- **Turnover is what makes cost decisive.** Each unit of position change costs about 2.97 bps
  today. At 1.7–2.6 units a day that is **5.0–7.8 bps/day of cost against a 4.2–4.6 bps/day gross
  edge**.
- **Breakeven round trip:** about 4.98 bps (A), 5.35 bps (B) and 3.52 bps (handover). Today's 5.95
  bps is above all three, and it rises to 6.7 bps once even 1 point of slippage per leg is counted.

## 4. Answers to the handover's open questions

1. **Does Kite serve intraday history for expired futures? No. Re-verified today.**
   - Kite issues intraday bars only for contracts that are still trading; expired contracts have
     no tokens in the instrument dump. This was found in R72, Phase 1.
   - `continuous=1` on a minute interval returns **`API Error: invalid interval for continuous
     data`** (re-tested 2026-11-13 on NIFTY26NOVFUT). The same flag on `day` returns bars
     (22 rows for Sep-2026), and real continuous daily futures go back to 2010-10 (R73).
   - **So a VWAP-stop variant, or any futures-priced intraday backtest, cannot be run on history.**
     Intraday work on this strategy is a spot-index backtest with costs applied, as here.
2. **NIFTY 1m from Apr-2024 to today as a clean holdout: fetched, validated, parked, NOT
   evaluated.**
   - **Token:** `kite.instruments("NSE")` lists **NIFTY 50 = instrument_token 256265**
     (segment INDICES), as the handover assumed.
   - **Data:** 242,722 bars, 2024-04-01 09:15 → 2026-11-13 12:17, 651 days. 645 days have exactly
     375 bars.
   - **Short days, all explainable:**
     - 2024-04-23: 369 bars.
     - 2024-05-18: special Saturday DR session, 105 bars.
     - Muhurat sessions 2024-11-01, 2025-10-21 and 2026-11-08: 60–70 bars, partly outside
       09:15–15:29 (the 60 out-of-window bars).
     - 2026-11-13: today's session, fetched mid-day.
   - **Sanity:** 0 OHLC violations; 0 duplicate timestamps; the largest 1-minute move is 0.88%
     (2024-06-04, election-result day).
   - **Gap between the two datasets:** 2024-03-27 (partial) and 2024-03-28 are covered by neither.
     The holdout comes from a different source (Kite) than the in-sample file (GitHub).
   - **Parked at** `backtest/noise_boundary_r77/data/holdout_nifty50_1m_kite.parquet`
     (untracked). **It should stay untouched until the rules are frozen.**

## 5. Execution facts that will matter if this goes further (not modelled)

- **Instrument vs backtest:** the backtest uses the spot index, but the tradeable instrument is the
  NIFTY future.
  - 1x notional ≈ 1 lot (65 × ~25,100 ≈ ₹16.3L), needing about ₹1.65L of NRML margin (R72).
  - The strategy cannot size below 1 lot, so "1x" is fixed at about ₹16L of exposure.
- **The 15:29 exit:** Zerodha's F&O MIS auto-square-off runs around 15:25. A 15:29 flat exit
  therefore needs NRML with an explicit exit order.
- **Basis:** intraday basis drift on the future is small but not zero, and a spot backtest
  ignores it.

## 6. Verdict and what this does not decide

- **Gross: real.** On 9 years of spot data the noise-boundary rule makes about 4.2–4.6 bps/day
  before costs (gross Sharpe 1.5–1.6). This round confirms that part of the handover.
- **Net at today's costs: negative.** Futures STT at 0.05% puts the round trip at 5.95 bps, and
  the strategy is negative both on our reproduction (−0.8 bps/day) and on the handover's own
  figures (−3.2 bps/day). That holds **before** slippage, and before any holdout test.
- **What it would take:** the plan steps (cutting turnover, volatility sizing) would have to
  roughly **halve turnover without losing gross edge** just to reach zero.
- **Not decided yet:**
  - **Turnover gap:** the gap between our 1.67 and the handover's 2.61 changes a day is
    unresolved until the reference code is seen.
  - **Holdout:** the rules are not frozen, so the holdout is untouched.
  - **5-step plan:** not started, pending Dheeraj's walk-through.

## Reproduce

```
uv run python backtest/noise_boundary_r77/kite_checks.py     # Kite: charges, continuous probe, token, holdout (read-only)
uv run python backtest/noise_boundary_r77/kite_1m_depth.py   # Kite 1m depth probe + 2015-2024 fetch (read-only)
uv run python backtest/noise_boundary_r77/crosscheck.py      # public vs Kite vs historify, baseline on Kite
cd backtest/noise_boundary_r77 && uv run python noise_boundary.py
```

The scripts, the downloaded CSV, `results.json` and the holdout parquet are in
`backtest/noise_boundary_r77/` (untracked). Related rounds:
- **R72:** RSI/WMA 30m, modelled proxy. Established that there is no intraday history for expired
  futures, and built the Zerodha futures cost model.
- **R73:** RSI/WMA daily on real continuous futures. Same STT finding: turnover × 0.05% STT
  dominates.
