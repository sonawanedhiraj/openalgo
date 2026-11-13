# R79: simplified engine — signal-to-outcome study (traded vs vetoed vs raw ChartInk hits)

Issue [#782](https://github.com/sonawanedhiraj/openalgo/issues/782). The charge-model bug found here
is [#783](https://github.com/sonawanedhiraj/openalgo/issues/783). Follows R65 (false positives),
R67 (stop/exit sweep), R68 (entry side) and R78 (measured equity economics). The same results are in
section 16 of the updated audit document,
`Trading_Strategy_Audit_and_Handover_updated_2026-11-13_r79.docx` in this folder.

**The question:** does the ChartInk entry signal produce forward moves large enough to pay its own
costs, before any exit rule touches them? This is descriptive measurement only. Nothing was tuned.

## Verdict

- **83.2% of valid signals reach +8.9 bps before the engine's stop.** Valid means traded plus vetoed,
  n = 655. 90.7% reach it at some point by 15:20. +8.9 bps is the measured round-trip cost at about
  ₹86k notional (R78).
  - That is well above the 50% bar, so by the agreed rule **the entry does travel far enough, and the
    trailing exit is giving away real edge.**
  - A plain "engine stop, otherwise hold to 15:20" exit earns **+12.1 bps gross per traded signal,
    against the 2.2 bps the engine actually realised.** That is about ₹85 a trade at ₹86k notional.
- **Reaching +8.9 bps is necessary, not sufficient.** The stop sits a median **47 bps** away, five
  times the cost.
  - **Fixed exits come out near breakeven:**

    | Exit | Hit rate | Hit rate needed to break even | Net (bps) | t |
    |---|---:|---:|---:|---:|
    | 1R target | 54.4% | 59.5% | negative | — |
    | 2R target | 30.9% | 39.6% | +4.9 | 1.35 |
    | Stop, else hold to 15:20 | — | — | +3.3 | 0.71 |

  - **None of these is significant**, and all are before entry slippage: journal fills sit a median
    2.4 bps worse than the reference price used here.
  - **Prior result:** R67 ran this family of exits on tick replay of the actual fills and found none
    positive out of sample.
- **Long vs short splits the problem.**
  - **Long is an entry problem.** Under any plain exit the long side earns only 5.8–11 bps gross,
    around the 8.9 bps cost, so its net is −3 to +2 bps.
  - **Short is an exit problem.** It earns 10–18 bps gross under plain exits, but the trail realised
    only 4.2 bps.
- **The veto works, modestly.** Vetoed signals travel less on every measure:
  - MFE: median 40 vs 69 bps, mean 73 vs 100 bps (t = 3.1).
  - Reach cost before the stop: 75.0% vs 85.5% (z = 3.0).
  - Net about 0 to −6 bps under every exit.
  - It is not destroying value, so this does not confirm R65's hint that the veto's regime rule is
    inverted.
- **The mechanical filters work.** Raw ChartInk hits that never reached the engine drift −5.7 bps to
  15:20 (net t ≈ −3.5 under every exit). The filters select clearly better than the raw scan.
- **Recommendation:** run the two plain exits as **pre-registered, out-of-sample experiments on fresh
  signals**, with acceptance criteria set before the run:
  1. Engine ATR stop, no trail, hold to 15:20.
  2. Fixed 2R (the document's experiment B).

  Start with the short side. **The long side should be replaced at the entry, not re-tuned at the
  exit.**

## 1. Cohorts and coverage (unpriced rows kept, not dropped)

| Cohort | Raw rows | Events measured | Priced | Unpriced |
|---|---:|---:|---:|---:|
| (a) Traded: closed trades | 511 | 511 | 511 | 0 |
| (b) LLM-vetoed (`skip`, never taken) | **8,861 reviews** | **144** | 144 | 0 |
| (c) `review_failed` | 195 | all 195 were taken; 191 closed trades are inside (a) | — | — |
| (e) LLM `take` but not taken (2026-06-02 only) | 273 | 0 | — | 273 (no direction recorded) |
| (d) Raw ChartInk hits with no engine decision | 723 symbol-days, 404 never reached the engine | 404 | 397 | 7 (no bars that day) |

- **The 8,861 vetoed reviews are about 95 setups re-reviewed once a second while a trigger stayed
  live.**
  - The median gap between repeats is 1 second.
  - They collapse to 144 events when a new event starts after a 15-minute gap (the engine's signal
    expiry). The often-quoted "8,526 vetoed signals" counts repeats, not signals.
- **Traded cohort, by LLM verdict:** 311 `take`, 191 `review_failed`, 9 skips taken while the veto was
  still in shadow mode.
- **Excluded source:** `trend-up` decisions (May–June) come from a different source and are dated
  off-hours, so they are left out.
- **Bars:** 156 symbols from historify, 4 from Kite, 7 from both. 7 symbol-days remain missing.

## 2. Method (no look-ahead)

- **Reference price:** the close of the last *completed* 1-minute bar before the decision timestamp.
  - For (a) and (b) the decision timestamp is the LLM review time, i.e. when the engine's trigger had
    already fired.
  - For (d) it is the first ChartInk hit time.
- **Forward path:** the 1-minute bars starting at the next minute, through the 15:19 bar (the 15:20
  square-off).
- **Stop:** the engine's own, 1.5 × Wilder ATR(14) on *completed* 5-minute candles (prior trading day
  plus today, seeded the way the engine seeds it).
- **Same-bar hits:** if the target and the stop are both hit in the same bar, it counts as **stop
  first**. This touched 0.2% of events.
- **Direction:** everything is measured in the direction the signal implied. bps are of the reference
  price.

## 3. Results by cohort and side

| Cohort · side | n | +8.9 bps before stop | +8.9 bps by 15:20 | +1R before stop | +2R before stop | Median R (bps) | Median MFE / MAE (bps) | Minutes to MFE | Forward 15 / 30 / 60 min / 15:20 (mean bps) | t (15:20) |
|---|---:|---:|---:|---:|---:|---:|---|---:|---|---:|
| Traded · all | 511 | **85.5%** | 92.4% | 54.4% | 30.9% | 47.0 | 69 / 52 | 58 | +6.7 / +8.3 / +14.3 / **+11.7** | 2.12 |
| Traded · long | 240 | 87.9% | 94.2% | 52.5% | 27.1% | 52.9 | 70 / 55 | 56 | +5.5 / +6.5 / +13.4 / +9.2 | 1.22 |
| Traded · short | 271 | 83.4% | 90.8% | 56.1% | 34.3% | 42.6 | 68 / 48 | 62 | +7.8 / +9.9 / +15.2 / +13.9 | 1.74 |
| Vetoed · all | 144 | **75.0%** | 84.7% | 48.6% | 29.9% | 38.1 | 40 / 42 | 32 | −2.0 / −3.2 / +3.4 / +5.5 | 0.67 |
| Vetoed · long | 69 | 73.9% | 81.2% | 47.8% | 27.5% | 44.9 | 48 / 43 | 25 | +0.1 / +1.1 / +1.3 / +5.8 | 0.42 |
| Vetoed · short | 75 | 76.0% | 88.0% | 49.3% | 32.0% | 32.0 | 39 / 38 | 37 | −3.8 / −7.2 / +5.4 / +5.2 | 0.56 |
| Raw hit · all | 397 | 80.6% | 85.4% | 40.8% | 16.9% | 39.1 | 34 / 38 | 18 | +1.4 / −0.6 / −1.0 / **−5.7** | −1.36 |
| Raw hit · long | 207 | 78.3% | 82.1% | 35.7% | 13.5% | 42.7 | 31 / 41 | 14 | −0.6 / −5.2 / −3.5 / −8.4 | −1.42 |
| Raw hit · short | 190 | 83.2% | 88.9% | 46.3% | 20.5% | 36.1 | 36 / 30 | 25 | +3.5 / +4.5 / +1.7 / −2.8 | −0.47 |
| **Valid (traded + vetoed)** | 655 | **83.2%** | **90.7%** | 53.1% | 30.7% | 44.9 | 65 / 48 | 52 | +4.8 / +5.8 / +11.9 / +10.3 | 2.21 |

**Traded vs vetoed tests:**

| Measure | Traded | Vetoed | Test statistic |
|---|---:|---:|---|
| Reach +8.9 bps before the stop | 85.5% | 75.0% | z = 2.98 |
| +1R before the stop | 54.4% | 48.6% | z = 1.23 |
| Mean MFE | 99.8 bps | 72.8 bps | t = 3.1 |
| Forward return to 15:20 | +11.7 bps | +5.5 bps | t = 0.63 |

- **Sides:** both sides point the same way. The gap is clearest on longs for reaching cost
  (z = 2.85) and on shorts for MFE (t = 3.2).
- **Comparison with R67:** R67 found 47% of traded trades reach 1R; here it is 54.4%. The methods
  differ: R67 replayed ticks from the actual entry fill, while this study uses the pre-decision minute
  close, which is about 2.4 bps more favourable.
- **Realised winners and losers:** 99.3% of realised winners reached +8.9 bps before the stop, and so
  did **65.4% of realised losers**. Two thirds of the losing trades were at a profit that covered
  their costs before the exit gave it back.

## 4. Exit arithmetic (gross / net bps per signal; net = gross − 8.89; t on net)

These are fixed exits from the audit document's own experiment list, evaluated on the measured paths.
They are not a search.

| Cohort · side | Hold to 15:20, no stop | Engine stop, else hold to 15:20 | 1R target / stop / 15:20 | 2R target / stop / 15:20 |
|---|---|---|---|---|
| Traded · all | 11.7 / +2.8 (0.51) | 12.1 / +3.3 (0.71) | 8.1 / −0.8 (−0.32) | 13.8 / +4.9 (1.35) |
| Traded · long | 9.2 / +0.3 (0.04) | 5.9 / −3.0 (−0.49) | 5.8 / −3.1 (−0.79) | 11.0 / +2.1 (0.39) |
| Traded · short | 13.9 / +5.0 (0.62) | 17.7 / +8.8 (1.31) | 10.1 / +1.3 (0.39) | 16.3 / +7.4 (1.50) |
| Vetoed · all | 5.5 / −3.4 | 8.6 / −0.3 | 2.4 / −6.5 | 8.9 / 0.0 |
| Raw hit · all | −5.7 / −14.6 (−3.47) | −2.2 / −11.1 (−3.38) | −0.6 / −9.5 (−4.07) | −1.8 / −10.7 (−3.64) |

- **What the engine actually realised:** +2.21 bps gross per trade (R78). Every plain exit above
  beats that on the traded signals, which is direct evidence that the trail leaks.
- **Month to month,** the traded signals' drift to 15:20 is unstable:

  | Month | Mean drift to 15:20 (bps) |
  |---|---:|
  | Jun | +4 |
  | Jul | +32 |
  | Aug | −11 |
  | Sep | +20 |
  | Oct | +16 |
  | Nov (to the 13th) | −28 |

## 5. Charge reconciliation: ₹372.65, fully traced (issue #783)

The journal model totals ₹39,231.25 and Kite's `/charges/orders` totals ₹38,858.60 over the same
511 trades. The stored `charges_inr` values equal the engine model's recomputation exactly.

| Component | Model − Kite | Cause |
|---|---:|---|
| Exchange transaction | **+₹335.13** | The model uses 0.00345% of turnover; Kite applies 0.00307% |
| GST | +₹60.34 | 18% on the exchange over-charge above |
| Stamp duty | −₹20.68 | Kite rounds stamp duty per buy leg (₹2.98 → ₹3) |
| STT | −₹2.39 | −₹2.08 from short trades having their legs swapped (the model always treats entry as the buy); the rest is rounding |
| Brokerage | −₹0.10 | Rounding |
| **Total** | **+₹372.65** | 504 trades over (median +₹0.82), 7 under; range −₹0.89 to +₹1.49 |

Per-trade detail: `backtest/r79_signal_outcome/data/charges_recon.csv`. No conclusion in R78 changes:
the over-statement is 1% of charges.

## 6. Caveats

- **Sandbox fills:** every trade is sandbox. The reference price is the last completed minute close,
  and live fills would be worse by at least the 2.4 bps observed against journal fills.
- **1-minute bars hide intra-minute order.** Counting same-bar hits as stop first is conservative.
- **Sample:** this is one in-sample window (Jun–Nov 2026, 100 trading days). The vetoed cohort is
  144 events from about 40 days. The month-to-month spread is wide, and none of the exit results is
  significant at t ≥ 2.
- **ATR:** it is rebuilt from bars, not read from the engine's live state, so the stop distance is an
  approximation of what the engine used.
- **Raw-hit timing:** raw hits are timed at the first scan hit, not at a candle trigger, so they
  measure the scan's selection rather than an executable entry.

## Reproduce

`backtest/r79_signal_outcome/` (untracked), all read-only:
- `build_cohorts.py` — takes the DB byte-copy path.
- `fetch_bars.py` — historify through the app's local-DB path, Kite for gaps.
- `measure.py`
- `summarize.py`
- `charges_recon.py` — takes the DB byte-copy path, plus the Kite calculator.

Outputs are in `data/`.
