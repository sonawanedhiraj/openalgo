# R80 steps 2–4 — pre-registration (written 2026-11-15, BEFORE any 2024–2025 result was computed)

Issue #784. Nothing below may be changed after results exist; a changed threshold is a new, separately counted variant.

## Data and windows
- **Universe:** NSE F&O stock list of 2024-03-01 (182 names, `_bhavcopy_tmp/fo01Mar2024bhav.csv` FUTSTK). This is the only point-in-time list available, so the universe is APPROXIMATE:
  - Stocks added to F&O after 2024-03 are excluded.
  - Stocks removed before 2024-03 are absent.
  - IDFC and PEL are excluded because they merged and have no token.
  - Renames map to their current Kite token.
- **Bars:** Kite 1m (2023-12-18 → 2025-06-20) and daily (2022-06 → 2025-06), fetched fresh 2026-11-15. NIFTY 50 1m comes from the same fetch.
- **Windows:**
  - **TRAIN:** 2024-01-01 → 2024-12-31.
  - **VALIDATE:** 2025-01-01 → 2025-06-19.
  - 2025-06-20 onward is R56 territory: already used and not touched here.

## Signals and engine (unchanged production code)
- **Signals:** the real `fno_intraday_{buy,sell}_chartink.rule()` (R56 harness, frozen clock), gap 1.5% (`.env`), first fire per symbol-day.
- **Engine:** the real `SimplifiedStockEngine`, `mode=disabled`, `.env` values:
  - Risk and sizing: capital 20k ×5, max_risk 500, ATR14 Wilder 5m, sl 1.5×ATR.
  - Entry gates: volume 2.5×, ATR entry min 0.5, elapsed 0.70, reference expiry 20 min.
  - Limits: max 6 trades/day, cooldown 3, same-day stop-out block, global profit lock, no new entries after 15:10, EOD 15:20.
- **Replay fidelity:** entries are evaluated on completed 5m candles (elapsed 1.0, i.e. the candle close). Price updates, trailing and the stop are driven at 1m: stop checked first against the bar extreme, then the trail updated on the close.
- **Charges:** Zerodha MIS, legs by real side, exchange **0.00307%** (#783 corrected). Same formula otherwise as `compute_zerodha_intraday_charges`.
- **Subscription cap 5:** the handover spec names this cap, but no such cap exists in the code (the engine arms every hit). Base runs = the code. A cap-5 run (first 5 armed symbols/day) is ONE sensitivity variant.

## Exits (identical entries = the engine's entries under exit a)
- **(a) Current:** engine trail, rr_trail_start_r 0.6.
- **(b) No trail:** initial engine stop only; otherwise exit at the 15:19 1m close (15:20).
- **(c) Fixed 2R:** target = entry ± 2 × risk_per_share; initial stop; 15:20 otherwise.
- **Fill rules:**
  - Within a 1m bar, the stop is assumed to come before the target.
  - A gap through the stop fills at the bar open.
  - The target fills at the target price; a gap through it is not credited.

## Pre-registered signal-quality criteria (evaluated at the entry candle close, information ≤ entry only)
1. **C3 target space (priority).**
   - Level = the 5-session swing: highest high (longs) / lowest low (shorts) of the 5 settled daily bars D-1..D-5. That is the ONE definition used.
   - PASS (long) if entry ≥ level (no overhead daily resistance in the lookback) OR level − entry ≥ 2R; mirror for shorts.
   - R = risk_per_share.
2. **C1 market structure.**
   - Take the last 6 completed 5m bars including the entry candle, as blocks A (bars −6..−4) and B (bars −3..−1).
   - Long PASS if max(high B) > max(high A) AND min(low B) > min(low A) (higher high and higher low); short = lower high and lower low.
3. **C2 index alignment, two filters, both reported.**
   - NIFTY 50 return from today's 09:15 open to the last 1m close at/before entry.
   - **WITH** PASS if the return in the trade direction is > +0.10%.
   - **COUNTER** PASS if the return in the trade direction is < −0.10%.
   - Tension to report: R65 found counter-NIFTY entries had higher P(1R before stop), 0.61 vs 0.47, while R79 did not support an inverted-veto reading.
4. **C4 entry extension.** ext = (entry − today's 09:15 open) / daily ATR14(D-1), signed in the trade direction. PASS if ext ≤ 0.75.

**Plateau neighbours** (not tuned; reported to show stability):
- C3: 1.5R / 2.5R.
- C1: 4- and 8-bar windows (2×2, 2×4).
- C2: ±0.05% / ±0.20%.
- C4: 0.5 / 1.0.

## Metric and acceptance
- **Target:** net expectancy per trade (₹) after corrected charges, long and short separately and combined, under each of exits a/b/c.
- **A filter is a CANDIDATE only if all of these hold:**
  - It improves net expectancy vs the unfiltered baseline on TRAIN.
  - It also improves it on VALIDATE (same exit, same side).
  - VALIDATE net expectancy > 0.
  - VALIDATE n ≥ 30.
  - Both plateau neighbours improve VALIDATE in the same direction.
  - Placebo p < 0.05.
- **Placebo:** keep a random subset of the same size from the same exit/side/period VALIDATE pool; 2,000 draws; p = share of draws with net expectancy ≥ the filter's (R67 method).
- **Day-block bootstrap CI** (95%, 2,000 resamples by date) for every reported VALIDATE expectancy.
- Every variant evaluated is written to `data/variant_ledger.csv` with its count. Any rule that works in one period only is reported as noise.

## Addendum A (2026-11-15, still BEFORE any 2024–2025 result; fetch not yet complete)

### Exit (h) — hybrid, the one exit R67 did not sweep
- **Entry, initial stop and quantity Q:** identical to (a), (b) and (c).
- **Partial:** when the 1m bar reaches entry ± 1R, sell floor(Q/2) at exactly entry ± 1R.
  - Stop-first applies within a bar: if the stop and +1R are both inside the same bar, the stop wins and nothing is partialled.
  - If Q = 1 there is no partial, and the single share follows the remainder rule from +1R.
- **Remainder stop protection:**
  - From the bar AFTER the partial, the remainder's stop is at least breakeven (entry).
  - Trail formula: stop = max(previous stop, entry, best 1m CLOSE since entry − 1.0 × R) for longs; mirrored for shorts.
  - The trail is evaluated on each 1m close and applies from the next bar.
  - A gap through the stop fills at the bar open.
- **Session end:** whatever remains exits at the 15:19 close.
- **Charges:** three orders (entry, partial, remainder), Zerodha's ₹20 cap per order, same rates.
- No other hybrid parameters are tried. Neighbours for the plateau (partial at 0.75R and 1.25R, trail 1.0R) are reported as stability only.

### R-threshold ladder (doc 17.2)
- **Thresholds:** for k ∈ {0.5, 1, 1.5, 2}R, long and short separately and by period, report:
  - n;
  - % reaching k before −1R;
  - median minutes from entry to reach k;
  - % of those that subsequently hit the initial stop before 15:20.
- **Two populations:**
  - Engine entries (entry and stop are the engine's).
  - All armed signals: reference = 5m close at fire, R = 1.5 × the engine's 5m Wilder ATR at arm.
- **Giveback, measured on exit (a):** peak unrealised R up to the actual exit, realised gross R, and the gap between them. Report the distribution (median, p75, p90).

### Long-side removal (doc 17.5)
This is a HYPOTHESIS, not a decision. Pre-registered test: "short-only beats both-sides" requires short-only net expectancy per DAY > both-sides net expectancy per day on TRAIN and on VALIDATE, under the same exit, with the day-block bootstrap CI of the difference excluding 0 on VALIDATE. Otherwise the long side is not removed on this evidence.

### Ledger fields (doc 17.6)
One row per variant, failures included:
- **Provenance and sample:** code hash (sha of the r80 scripts) + config, date split, signal count, executed trades.
- **P&L and costs:** gross, net, each charge component (brokerage, STT, exchange, SEBI, GST, stamp), net expectancy, net profit factor.
- **Risk:** max drawdown (cumulative net in trade order), risk-cap breaches (net loss > ₹500 on a trade).
- **Attainment and uncertainty:** R-threshold attainment, day-block 95% CI.
- **Slippage:** the base assumption is 0. The pre-registered adverse stress is 5 bps per round trip (2.5 per side, about R79's measured 2.4 bps entry slippage), reported for every row.
