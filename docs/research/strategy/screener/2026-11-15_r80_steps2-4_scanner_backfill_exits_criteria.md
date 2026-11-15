# R80 steps 2–4: in-house scanner backfill 2024-01 → 2025-06, exit comparison, and pre-registered signal criteria

- **Round:** R80. Issue #784. Step 1 (live scanner-vs-ChartInk agreement) is in `2026-11-13_r80_step1_inhouse_scanner_vs_chartink.md`.
- **Pre-registration:** `backtest/r80_scanner/PREREGISTRATION.md`, committed before any 2024–25 result existed:
  - `ee9194316`: criteria, windows, gates.
  - `11e65e53c`: Addendum A (hybrid exit, R ladder, ledger fields, long-side test).
- **Code:** `backtest/r80_scanner/` (`fetch_bars.py`, `signals_r80.py`, `engine_r80.py`, `score_r80.py`). Code hash `835aea7539` (sha1 of signals_r80.py, engine_r80.py, score_r80.py, PREREGISTRATION.md).
- **Outputs:** `data/variant_ledger.csv` (288 rows), `data/score.json`, `data/trades_r80.parquet`, `data/signal_ladder_r80.parquet`, `data/daily_cap.txt`.

## Verdict

**REJECT. No exit and no pre-registered filter produces a positive net expectancy that holds in both periods.**

- **Across the two backfill periods:** the scanner's signals, run through the unchanged engine, lose money under every exit, on both sides.
  - 2024-01-01 → 2025-06-19, 362 trading days.
  - 1,655 trades.
  - −₹41 to −₹163 per trade net.
- **Gross edge:** about zero. Gross is −₹2.2k on 1,655 trades under the current trail, or 0.3 bps a trade. That is against ₹74 of charges a trade.
- **Only one filter passes the pre-registered gates mechanically.** It is the long side taken only when NIFTY is up more than 0.10% at entry (C2 with-trend), and it is not an edge:
  - It loses money in TRAIN.
  - Its VALIDATE confidence interval spans about ±₹100 around +₹9 to +₹19 a trade.
  - It turns negative under the 5 bps slippage stress.
  - It is positive in 9 of 18 months.
  - About 3–4 of the 72 primary cells would pass p < 0.05 by chance.
- **The same rule on the short side flips sign between periods**, from +₹25/+₹62 to −₹239/−₹171. That is the pattern of a regime effect, not a rule.

This is consistent with R56 (2025-06-20 → 2026-07-06: 1,177 trades, net −₹2.52 lakh, no profitable month), which is the prior result on the overlapping window and the baseline any filter had to beat.

## 1. Data, universe and fidelity

| Item | Value |
|---|---|
| Window | 2024-01-01 → 2025-06-19. TRAIN = 2024 (240 trade days); VALIDATE = 2025-01-01 → 06-19 (115). 2025-06-20 onward is R56 territory and is not touched. |
| Universe | NSE F&O stock list of **2024-03-01** (182 names): the only point-in-time list on disk (`_bhavcopy_tmp/fo01Mar2024bhav.csv`). **Approximate.** Names added to F&O later in the window are excluded, and names that left before March 2024 are absent. 180 resolved: 5 renames mapped (GMRINFRA, L&TFH, MCDOWELL-N, TATAMOTORS, LTIM), 2 by known token (ICICIPRULI, GUJGASLTD), and IDFC and PEL excluded (merged). |
| Coverage vs today's scanner list | Only 133 of today's 216 `SCANNER_SYMBOLS` were F&O in March 2024. 49 March-2024 F&O names are no longer in the list. The R70 extract (`bars.duckdb`) had 1m for only 103 of the 182, and stock daily from 2024-01 only (too short for SMA200), so everything was re-fetched from Kite: 1m 2023-12-18 → 2025-06-20 and daily 2022-06 → 2025-06 for all 180, plus NIFTY 50. Every series has 375 days. Cross-check: RELIANCE 2024-05-15, 375 bars, closes and volume identical to the R70 extract. |
| Signals | The real `fno_intraday_{buy,sell}_chartink.rule()` through the R56 harness: frozen clock, point-in-time daily/weekly, gap 1.5% as in `.env`. Result: **4,871 signals (2,749 BUY / 2,122 SELL), 13.5 a day, median fire 12:15.** |
| Engine | The real `SimplifiedStockEngine`, `mode=disabled`, `.env` config: capital 20k ×5, ₹500 risk, ATR14 Wilder 5m, stop 1.5×ATR, volume 2.5×, elapsed 0.70, reference expiry 20 min, max 6/day, cooldown 3, same-day stop-out block, global profit lock, 15:10 / 15:20. **Entries on completed 5m candles; stop and trail driven by 1m bars** (stop checked against the bar extreme first, gap → bar open, trail on the close). |
| Charges | Zerodha MIS, legs by real side, **exchange 0.00307%** (the #783 correction), ₹20 per-order brokerage cap. ₹73.8 a trade on a median ₹91.8k a side. |
| Subscription cap 5 | The handover spec names it, but **the code has no such cap**: the engine arms every hit. The base run follows the code. A cap-5 run (first 5 armed symbols a day, 1,237 trades) changes nothing material: every exit stays negative, and the same single C2-long cell is the only pass. |

**Scanner caveat (carried on every number).** Live, the scanner finds 86.8% of ChartInk's hits on feed-alive days, but only 36.6% of its own hits are ChartInk's, and it fires a median 12 minutes earlier. This backfill is therefore mostly NOT the ChartInk population.

| Population | Rate |
|---|---|
| Backfill signals | 13.5 a day; the scanner finds about 2.2× the symbol-days |
| Engine caps and gates (trades) | 4.57 a day here, against 5.1 a day in the live ChartInk record (511 trades in 100 days) and 4.6 a day in R56 |

**Economics vs the live ChartInk record:**

| Population | Gross | Net |
|---|---|---|
| Live ChartInk record | +₹19 a trade (2.2 bps) | −₹58 a trade |
| This backfill | −₹1.4 a trade (0.3 bps) | −₹75 a trade |

The backfill is therefore a slightly worse but same-shaped picture. The extra, non-ChartInk hits are not better signals. The difference against R56 (−₹214 a trade) is partly R56's 5m-bar stop replay and old charge model, so the two are not strictly comparable.

## 2. Exits on identical entries (net ₹ per trade, day-block 95% CI)

| Exit | Side | TRAIN n | TRAIN net | VALIDATE n | VALIDATE net (CI) | VAL stress 5 bps | VAL PF |
|---|---|---|---|---|---|---|---|
| (a) current trail 0.6R | long | 741 | −84 | 285 | −52 (−108, +6) | −93 | 0.75 |
| (a) | short | 397 | −59 | 232 | −102 (−154, −47) | −145 | 0.56 |
| (b) stop, hold to 15:20 | long | 741 | −62 | 285 | −63 (−149, +23) | −105 | 0.78 |
| (b) | short | 397 | −24 | 232 | −163 (−235, −93) | −205 | 0.49 |
| (c) fixed 2R | long | 741 | −61 | 285 | −47 (−121, +28) | −89 | 0.83 |
| (c) | short | 397 | −5 | 232 | −133 (−204, −67) | −175 | 0.56 |
| (h) hybrid ½ at 1R, trail 1R | long | 741 | −86 | 285 | −48 (−111, +21) | −89 | 0.79 |
| (h) | short | 397 | −40 | 232 | −115 (−179, −55) | −158 | 0.54 |

**Hybrid (pre-registered, Addendum A).** At +1R, half the quantity is sold at exactly +1R. The remainder's stop goes to at least breakeven and trails 1.0R behind the best 1m close; stop-first applies within a bar. It is no better than the current trail. The neighbours (partial at 0.75R and 1.25R) are within ±₹8 of it. The hybrid does capture the giveback on the trades that reach 1R, but those are too few to pay for the 50% that hit the stop first.

**Fixed 2R:** least bad on both sides in TRAIN, but it is not positive anywhere. Break-even for 2R/−1R with C ≈ 0.15R of charges is (1 + 0.15)/3 ≈ 38% of trades reaching 2R before the stop. Observed: 15–25%.

## 3. R-threshold ladder (doc 17.2)

Reaching k before −1R, median minutes from entry, and the share of those that later reverse to the initial stop. Stop-first within a 1m bar.

**Engine entries** (R = the engine's risk per share; median R = 54 bps):

| Side / period | n | 0.5R | 1R | 1.5R | 2R | Stop first |
|---|---|---|---|---|---|---|
| long TRAIN | 741 | 62.8% · 8 min · 30.5% rev | 42.5% · 21 · 17.8% | 30.0% · 32 · 15.3% | 20.8% · 52 · 11.7% | 49.8% |
| long VAL | 285 | 64.9% · 10 · 34.1% | 43.9% · 30 · 24.0% | 33.3% · 40 · 15.8% | 24.9% · 58 · 11.3% | 50.9% |
| short TRAIN | 397 | 64.2% · 11 · 28.6% | 46.6% · 27 · 17.8% | 34.3% · 44 · 12.5% | 25.2% · 59 · 11.0% | 49.4% |
| short VAL | 232 | 63.4% · 11 · 42.2% | 43.1% · 24 · 29.0% | 28.4% · 36 · 19.7% | 14.7% · 67 · 23.5% | 56.9% |

**All armed signals** (4,572 with an ATR at arm; reference = the 5m close at fire, R = 1.5 × the engine's 5m ATR):

| Side / period | 1R reached | 2R reached | Median min to 2R |
|---|---|---|---|
| long, TRAIN / VAL | 41.5 / 39.7% | 19.8 / 18.4% | 50 / 46 |
| short, TRAIN / VAL | 44.7 / 38.9% | 21.7 / 18.0% | 51 / 58 |

The engine's own entry trigger adds 1–4 points over the raw armed signal.

**Reading:**
- A 2R target is reachable in the session; the median time is about an hour, and entries are at a median of 13:35.
- It is reached first by only about one trade in five.
- 1R comes first in only about 43%, below the 50% a symmetric random walk would give with the stop as close as the target.

**Giveback under the current trail (measured, exit a):**
- Peak unrealised R median is 0.74, against realised 0.32.
- Giveback median is 0.81R (p75 1.23, p90 1.53).
- Among trades whose peak reached ≥1R (349 in TRAIN, 165 in VALIDATE), median giveback is 0.65 / 0.71R.

The giveback is real, but removing it (exits b, c, h) does not make the strategy positive.

## 4. Pre-registered criteria (thresholds fixed before results; ledger rows 1–288)

Five primary filters × four exits × three sides = 60 primary cells, plus plateau neighbours and two extra hybrid variants: 288 ledger rows in total. Gate for CANDIDATE:
- delta > 0 in both periods;
- VALIDATE net > 0 with n ≥ 30;
- both plateau neighbours improve VALIDATE;
- R67 placebo p < 0.05.

| Criterion (pass rate) | Result |
|---|---|
| **C3 target space (priority).** 5-session swing high/low (D-1..D-5). Pass if beyond it, or room ≥ 2R. | 69.7% of entries are already beyond the 5-day level ("open air"), 20.2% have ≥2R of room, and only **10.1% are blocked**. Filtering them out moves VALIDATE by +₹2 to +₹27 a trade, but TRAIN by −₹5 to +₹8, and VALIDATE stays negative everywhere (best: (c) long −₹29). **Fails.** Target space is not the binding problem: most of these signals have no daily resistance within 5 days, and they still do not reach 2R. |
| **C1 structure.** Higher high and higher low (lower high and lower low) over the last 6 × 5m bars: 58% pass. | Improves both periods under (a) and (h) (+₹6 to +₹15), but VALIDATE stays negative (−₹36 to −₹89) and the placebo p is 0.26–0.41. Under (b) it reverses in VALIDATE (−₹31 to −₹51). **Fails.** |
| **C2 with-trend.** NIFTY move in the trade direction > +0.10% at entry. | **Longs:** VALIDATE +₹9 (b), +₹19 (c), +₹17 (h), n = 136, placebo p 0.045 / 0.028 / 0.012, neighbours agree → flagged CANDIDATE under b, c and h. But TRAIN net −₹46 / −₹50 / −₹83, VALIDATE CI about (−₹100, +₹130), every cell negative under 5 bps stress, 9/18 months positive. **Shorts:** TRAIN +₹25 / +₹62 → VALIDATE −₹239 / −₹171. **Verdict: a regime effect (2024 rewarded with-trend shorts, H1 2025 with-trend longs), not a rule. Not deployable.** |
| **C2 counter-trend.** NIFTY move < −0.10% against the trade. | Worse than baseline in nearly every cell. **R65 tension, measured directly:** P(1R before stop) = **0.41 counter, 0.44 neutral, 0.46 with-trend** (n = 593 / 278 / 784). R65 had 0.61 counter vs 0.47 with on the live sample. On 1,655 out-of-sample trades the counter-trend advantage does not exist; if anything it is reversed. R79's refusal of the inverted-veto reading is supported. |
| **C4 entry extension.** (entry − 09:15 open) / daily ATR14 ≤ 0.75: 52% pass. | Mixed: train-only on some cells, validate-only on others, VALIDATE negative everywhere (best (c) long −₹17). **Fails.** **What differs from R68:** R68's "not extended" variant was selected in-sample on a few hundred live trades and flipped out of sample (0.59–0.67 → 0.33–0.40). Here the threshold was fixed in advance on an independent 18-month sample. It does not flip, because it never had an effect to lose. |

**Multiple testing.** At p < 0.05 across 72 primary cells, about 3.6 false passes are expected by chance. Three CANDIDATE cells were found, and they are one filter on one side under three correlated exits. Bonferroni (p < 0.0007) passes nothing.

## 5. Long-side removal (doc 17.5): tested, not confirmed

Short-only net per day minus both-sides net per day:

| Exit | TRAIN, ₹/day (CI) | VALIDATE, ₹/day (CI) |
|---|---|---|
| a | +260 (+142, +377) | +128 (−17, +264) |
| b | +192 | +157 (−65, +363) |
| c | +189 | +118 (−65, +307) |
| h | +265 | +118 (−39, +276) |

- **The pre-registered test is not met.** It required the VALIDATE CI to exclude 0, and it does not under any exit.
- **Dropping longs only means losing less.** Short-only is still negative, at −₹40 to −₹330 a day.
- **The side ranking is regime-dependent.** In VALIDATE, longs beat shorts on net per trade under every exit, the opposite of TRAIN.

**On this evidence the long side should NOT be disabled as an improvement.** The honest reading is that neither side earns its costs.

**Doc 17.5 first-trade and daily caps** (approximate: first k entries kept, later engine state not re-simulated). The first trade of the day averages −₹50 in both periods. Caps 1/2/3 do not produce a cell positive in both periods; short cap-1 to cap-3 is positive in TRAIN under (b) and (c) and negative in VALIDATE (`data/daily_cap.txt`).

## 6. Section 17 of the handover, point by point

| Section | What was done |
|---|---|
| 17.1 Define 1:2 | R = the engine's risk per share × quantity (≤ ₹500 price risk); 2R is a gross price move; net R and gross R reported separately; the labels kept distinct (net winner, 0.5/1/1.5/2R reached, stop first). **All-in ceiling:** under the current trail, **457 of 1,655 trades (27.6%) lose more than ₹500 after charges**, because sizing uses price risk only and 12% of trades sit on the ₹1 minimum risk per share. A ₹500 all-in rule would need sizing to reserve about ₹75 of charges plus stop slippage. |
| 17.2 Ladder | Done (section 3): both populations, every threshold, n, %, median time, % reversing to stop, plus giveback. |
| 17.3 Identical entries | Yes. Same entries, stop, quantity, cost model; a target is not credited on a gap through it; the hybrid was pre-registered; the alternative-target study (1.5R) was not run, because the ladder shows 2R reached first by about 20% and the 1.5R neighbour adds nothing; **the time exit was not tested** (the one 17.3 item not covered). |
| 17.4 Net expectancy | Every row reports net and gross expectancy, PF, win %, max drawdown, charges by component, stress, CI and attainment. Hit rate is never the criterion. |
| 17.5 Selection | First-trade and caps 1/2/3 checked (approximate); long and short evaluated independently; long removal tested as a hypothesis (not confirmed); size never increased. |
| 17.6 Ledger | `data/variant_ledger.csv`: 288 rows, every failure included, with code hash, config, split, signal count, trade count, gross, net, six charge components, slippage assumption + 5 bps stress, net expectancy, PF, max drawdown, ₹500 breaches, attainment, day-block CI and placebo p. |
| 17.7 Acceptance | No variant meets it; nothing is adopted. |
| 17.8 Next actions | MFE/MAE and threshold flags added; threshold table by side × in-sample/out-of-sample; trail / 2R / hybrid on the same entries; ledger published before any decision. (The 511 live trades were path-reconstructed in R79.) |
| 16.5 Method | No look-ahead (every feature uses data ≤ the entry candle close; daily ≤ D-1); 1m sequencing with stop-first ambiguity; chronological train/validate; R56's window untouched; universe disclosed as approximate. |

## 7. What this means

- **A ChartInk replacement for research is usable but not neutral.** The scanner's backfilled signals behave like the live ChartInk record, slightly worse, so a historical study on them is informative about the strategy's shape.
- **The entry has no edge to recover.** 18 months of signals that were never seen before give gross ≈ 0. No exit fixes zero gross. The four signal criteria Dheeraj proposed do not create one either.
- **What fails, and what remains open.**
  - Fails: the doc's own decision rule (16.9: "if the entry signal does not show a repeatable positive gross edge out of sample, redesign or retire the entry logic rather than trying more trailing-stop settings") is now met with an out-of-sample test.
  - Remains open: a different entry trigger, not a filter on this one.
- **For the C2 long observation**, the only defensible use is a forward observation log (no money). Count with-trend vs counter-trend long outcomes over the next 40+ live trades before anyone treats it as more than one lucky half-year.
