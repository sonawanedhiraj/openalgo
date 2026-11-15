# R81 — new ENTRY-GENERATION rules for the simplified stock engine: pre-registration

- **Written:** 2026-11-15, BEFORE any test.
- **Issue:** #785.
- **Supersedes** `backtest/r81_survey/PREREGISTRATION.md` (commit 2a6cf06c4), which was redirected by the operator before any test ran: Dheeraj's focus is this engine's signal quality, not new books.
- **Status: NOT RUN.** Nothing below has been computed. It runs only after Dheeraj approves this list. Any change is a new commit made before a result.
- **Write-up:** `docs/research/strategy/simplified_engine/2026-11-15_r81_new_entry_rules_preregistration.md`.

## What stays and what is swapped

- **Stays (unchanged production code, `services/simplified_stock_engine_core.py`):**
  - Sizing: `calculate_qty` (₹500 risk, capital 20k × 5).
  - Stop: risk per share = max(1.5 × Wilder ATR14 on 5m, ₹1).
  - Exits: the RR trail (0.6R start, 0.5 × ATR floor) and the global profit lock.
  - Limits: max 6 trades a day, cooldown 3, same-day stop-out block, no new entries after 15:10, EOD 15:20.
  - The corrected Zerodha MIS charge model (exchange 0.00307%, legs by side, #783).
- **Swapped:** the ENTRY. Today it is "ChartInk/scanner arms the symbol, then the engine's reference-candle breakout fires". Each candidate below is a complete entry decision: symbol, side, time and reference price.
- **Harness:** the R80 harness (`backtest/r80_scanner/engine_r80.py`), with entries injected as `EntrySignal` → `confirm_entry`.
  - The engine computes ATR from the same 5m history it would see live, and `calculate_qty` sizes the trade.
  - From the next 1m bar onward the unchanged engine manages the position at 1m resolution: stop checked first against the bar extreme, a gap fills at the bar open, then `on_price_update(close)` runs.

## Common protocol (applies to every candidate)

1. **No look-ahead.** Every rule is evaluated on COMPLETED bars only, and the decision is made at a bar's close. The reference price is that bar's close: never a level inside the bar (the R58 lesson). Daily inputs (ATR14, previous close, 20-day averages) use settled days ≤ D−1 only.
2. **Data.**
   - Kite 1m bars for the stock universe and sector indices, plus NIFTY 50. Kite serves stock 1m from 2015-02 (probed 2026-11-15).
   - The R80 fetch (`backtest/r80_scanner/data/bars_r80.duckdb`, 2023-12-18 → 2025-06-20) is reused.
   - 2019-01 → 2023-12 is fetched the same way: about 5,600 requests, about 35 minutes.
3. **Universe.** The NSE F&O stock list of 2024-03-01 (180 resolvable names, the R80 universe). This is APPROXIMATE and survivorship-biased, more so before 2024: stocks that left F&O before 2024 are absent, and later additions are excluded.
   - If Dheeraj approves downloading NSE's public month-end F&O lists, point-in-time membership replaces it. That would be recorded by amendment before any run.
   - Every candidate also requires a price of ₹100–₹5,000 (the scanner's band) and a 20-day average daily turnover ≥ ₹50 crore (D−1).
4. **Windows.**

   | Segment | Dates | Trading days | Use |
   |---|---|---|---|
   | TRAIN | 2019-01-01 → 2022-12-30 | ~990 | Thresholds may be read here |
   | VALIDATE | 2023-01-02 → 2023-12-29 | ~245 | Confirms the frozen spec |
   | **SEALED HOLDOUT** | **2024-01-01 → 2025-06-19** | 362 | Not computed until every candidate is frozen by a commit after VALIDATE |

   2025-06-20 onward is R56/R80 territory and is not used.

   Disclosure: R80 ran ChartInk-armed trades over the holdout dates and saw their regime (2024 favoured shorts, H1 2025 longs). It never ran any of these rules there.
5. **Costs.**
   - Engine charges at the corrected rate.
   - Slippage of **2.4 bps per side** (R79's measured entry slippage), applied to entry and exit.
   - A **zero-slippage upper bound** is reported beside every result.
   - Stress: 5 bps per side.
6. **Primary metric.** Net expectancy per trade (₹) under the unchanged engine exit, with each candidate's long and short reported separately and combined.
   - Secondary, reported but never used to choose: "engine stop, else hold to 15:20" (R79/R80 found the trail leaks).
7. **Statistics.**
   - The t-stat on net per trade is clustered by day: the day-level sum, then the day-block bootstrap 95% CI with 2,000 resamples.
   - **|t| < 2 is never called a result.**
   - Five candidates, so family-level CONFIRMED needs |t| ≥ 2.58 (Bonferroni 0.05/5) on the HOLDOUT.
8. **Regime rule.**
   - Net expectancy must be > 0 in TRAIN, in VALIDATE and in HOLDOUT.
   - It must also be > 0 in at least 7 of the 10 half-years 2019H1 → 2023H2 (half-year table always reported).
   - A rule that works in one year or one regime is **noise**, whatever its t.
9. **Plateau.** Each candidate declares two thresholds, each with one neighbour on either side. All four single-step neighbours must keep a positive VALIDATE net expectancy. A lone peak fails.
10. **Placebo (R67 method), 1,000 draws each.**
    - (a) *Timing placebo:* same symbol, day and side, with the entry time drawn uniformly from the candidate's allowed window.
    - (b) *Selection placebo:* same day, time and side, with the symbol drawn from that day's eligible universe.
    - Both are priced with the fast stop-or-15:20 simulator (apples-to-apples with the candidate's secondary metric). The primary engine replay is too slow for 1,000 draws.
    - p = share of draws ≥ the candidate's net. Both p < 0.05 are required.
11. **Cost bar (from Phase 1).** A candidate must clear about **14 bps / ₹120 / 0.24R of gross expectancy per trade** at the engine's ~₹86–92k notional. It is reported as "gross needed vs gross achieved".
12. **Sizing breaches.** For every candidate, report the share of trades losing more than ₹500 after charges under the engine's current sizing. R80 found 27.6%; fixing it is a separate engine change.
13. **Ledger.** One row per variant, every failure included (`data/variant_ledger.csv`). Each row carries:
    - code hash, config and window;
    - n entries and n days;
    - gross, net, the six charge components and slippage;
    - net expectancy, PF, win %, max drawdown, ₹500 breaches;
    - 0.5/1/1.5/2R-before-stop attainment;
    - day-clustered t, CI and both placebo p's;
    - the half-year table and the verdict.

## The five candidates

All rules are symmetric (long and mirror short) unless stated. Every candidate carries its own daily cap, chosen to keep frequency low; the engine's 6-per-day cap is the outer limit.

### E1 — VWAP trend-day pullback
- **Rationale.**
  - VWAP is the benchmark that institutional execution algorithms track (Berkowitz, Logue & Noser 1988; Madhavan 2002).
  - On a trend day, buy-side algorithms working a large order accumulate below VWAP, so pullbacks to VWAP in a trending name are absorbed rather than reversed.
  - The edge, if any, is in entering a name that has *already shown* trend persistence at a price where the stop is close to a level that matters.
- **Setup** (evaluated 10:15 → 14:30 on 5m closes). Session VWAP is computed from 1m (typical price × volume).
  - The stock's return from the previous close is ≥ +1.0 × daily ATR14% (in the trade direction).
  - ≥ 80% of the 5m closes since 09:30 are on the trade side of VWAP.
- **Trigger.** The first completed 5m bar whose low (high, for shorts) touches VWAP within 0.1% and closes back on the trade side. One trigger per symbol per day.
- **Daily cap.** 3 trades, in order of trigger time.
- **Plateau.** Trend threshold 0.75 / 1.25 ATR; side-of-VWAP share 70% / 90%.
- **Not a repeat:**
  - R44's "VWAP reclaim" was tested only on news-spike stocks (−0.79% a trade), where the stock had crossed VWAP the wrong way.
  - E1 needs an established trend and takes the first touch, universe-wide.
  - Its nearest neighbour is the R57/R60 pullback idea (`intraday_pullback_top2`, sandbox −₹2.7k on 13 trades), which pulls back on *scanner* signals. E1 has no scanner input.

### E2 — Sector-relative strength at 09:45 (LOW-FREQUENCY: at most 1 long + 1 short a day)
- **Rationale.**
  - Within-industry information diffuses with a lag (Hou 2007, lead-lag), and stock-specific order flow at the open persists intraday.
  - Conditioning on the stock beating its own *moving* sector isolates stock-specific demand from sector beta and from NIFTY direction.
  - That sector-beta and market-direction component is what R62/R65 found the ChartInk book was secretly betting on.
- **Setup** at the 09:45 close (six completed 5m bars). Only stocks with a sector in `strategies/intraday_pullback_top2/sector_map.json` (163 mapped; unmapped names are excluded).
  - Sector index return since 09:15 ≥ +0.30% (long) / ≤ −0.30% (short).
  - RS = stock return since 09:15 − sector return; RS ≥ +0.5 × daily ATR14% (long) / ≤ −0.5 × ATR (short).
- **Entry.** The single highest-RS long and the single lowest-RS short, at the 09:45 close.
- **Plateau.** Sector move 0.20% / 0.40%; RS 0.35 / 0.65 ATR.
- **Not a repeat.** R3 "sector movers" armed sector-aligned names into a low-volatility-base *breakout* trigger, and R29 gated the ChartInk entry on sector momentum. E2 has no breakout trigger and no ChartInk input. The overlap in selection logic is acknowledged.

### E3 — Large-gap fade after a failed first 15 minutes
- **Rationale.**
  - Large opening gaps in large-cap stocks are partly an overnight liquidity imbalance, because pre-open price discovery is thin. They tend to retrace once the continuous book refills.
  - R58 measured this directly: a naive breakout in the gap direction mean-reverts (33% favourable-first). That is a pre-existing finding pointing at the fade side.
- **Setup.**
  - |gap| = |open − previous close| / daily ATR14 ≥ 1.0.
  - The first 15-minute bar (09:15–09:30) closes AGAINST the gap: close < open for a gap-up, close > open for a gap-down.
- **Entry.** At the 09:30 close, against the gap.
- **Daily cap.** 2 trades, the largest |gap| first.
- **Plateau.** Gap 0.75 / 1.25 ATR; confirmation bar 10 / 20 minutes.
- **Not a repeat.** R15 faded *any* 3%+ intraday move (net −₹8.4 lakh). E3 fades only the open-auction gap, and only after the first 15 minutes have already failed to extend it.

### E4 — First-hour compression → range expansion
- **Rationale.**
  - Volatility clusters, and a quiet first hour relative to the stock's own norm is followed by range expansion later in the session (Crabel's narrow-range work; GARCH-type clustering).
  - The breakout of a *compressed* range has a smaller stop-to-move ratio than a breakout of an ordinary range. That improves the per-trade cost arithmetic, which is the binding constraint.
- **Setup.** The first-hour range (09:15–10:15 high − low) is ≤ 0.6 × its own 20-day average first-hour range.
- **Trigger** (10:15 → 14:00). A completed 5m bar CLOSES beyond the first-hour high (long) or low (short), and that bar's volume is ≥ 1.5 × the stock's 20-day average volume for the same 5-minute slot.
- **Entry.** At that bar's close.
- **Daily cap.** 3 trades, in time order.
- **Plateau.** Compression 0.5 / 0.7; volume 1.25 / 1.75.
- **Not a repeat.**
  - R58 / open15 trade the 09:15 *15-minute* candle and its breakout at the open.
  - R44's ORB (−0.83%) used news names.
  - E4 trades a 60-minute range, only after compression, from 10:15, on bar closes.

### E5 — Opening drive efficiency (LOW-FREQUENCY: at most 1 trade a day)
- **Rationale.**
  - Informed order flow moves price efficiently, in a straight line, while noise trading moves it back and forth.
  - A high path-efficiency opening that is also large in ATR terms marks a trend day, and trend days continue (Kaufman's efficiency ratio; the intraday momentum literature on the first half hour, Gao et al. 2018).
  - Taking only the single best setup a day directly tests the "fewer, bigger, better" route the cost floor demands.
- **Setup** at the 09:45 close.
  - ER = |close(09:45) − open(09:15)| / Σ|1m close changes| ≥ 0.40.
  - |close(09:45) − open| ≥ 0.5 × daily ATR14.
- **Entry.** The single stock with the highest ER × |move|/ATR, in the move direction, at the 09:45 close.
- **Plateau.** ER 0.30 / 0.50; move 0.35 / 0.65 ATR.
- **Not a repeat.**
  - R77 / R71 tested intraday momentum on the NIFTY index (futures); it was rejected on futures costs.
  - E5 is single-stock, needs path efficiency, and takes one trade a day.

## Explicitly excluded

| Excluded | Why |
|---|---|
| Any ChartInk / in-house-scanner arming, and the engine's reference-candle breakout trigger | R1–R2, R56, R61–R68, R76, R78–R80 |
| Opening 15-minute range breakout, with or without a volume gate | R58 found every honest bar-level variant loses. The tick-level version is open15, which has run live since July: −₹21.5k parent, −₹56.5k children. Historical ticks do not exist (Kite has none; the in-app capture starts 2026-08), so tick data does not reopen it for a backtest. |
| Gap-and-go continuation at the open | R58's naive breakout mean-reverts |
| VWAP *reversion* fades and any generic move fade | R15, R32 |
| Wyckoff colour / doji, Markov, bull flag | R4, R5, R12, R13 |
| News-triggered entries | R43, R44 |
| Volume-gate latency | R48 |
| Closing-window intraday | R70, R71 |
| Index noise-boundary momentum | R77 |
| Calendar timing | R22 |

## Order of work after approval

1. Fetch 2019–2023 1m bars (universe, 15 sector indices, NIFTY) and checkpoint.
2. E2 and E5 first: once-a-day setups, cheapest and lowest frequency.
3. Then E3, E1, E4.
4. TRAIN → VALIDATE, then a freeze commit, then the HOLDOUT once for all five.
5. Ledger, write-up, registry, Telegram.
