# Chartink BUY rule — Day-1 validation (2026-06-04)

## Methodology & limitations

This is a **daily-gates-only** validation. We do not have today's intraday tape (scanner WS issues + the rule wasn't deployed in the running OpenAlgo), so gates **3, 4, 5, 13** (5m Supertrend, 15m RSI, 5m volume surge) are **not evaluated**. The 8 daily/weekly gates (1, 2, 6, 7, 8, 9, 10, 12) are evaluated against cached DuckDB bars. The production rule (`services/scan_rules/fno_intraday_buy_chartink.py`) is unchanged; gate logic is re-implemented inline here.

End-of-day settled bars are used (today = iloc[-1], yesterday = iloc[-2]) — no live-forming-bar offset.


### Data notes

- Baseline file chartink_fno_baseline_2026-06-04.json NOT present; documented fno-intraday-buy-20 count today is 0 (BUY side). Set A = empty.
- Union of 43 symbols across 7 NL scans. NOTE: NL builder ignored the 'FnO' qualifier — these are cash-segment scans, and row display was truncated to ~10-13 per scan by the capture.
- Universe: 211 distinct symbols with daily bars (exchange=NSE).
- Universe symbols evaluated: 0; skipped (insufficient/NaN): 211
  - skip `insufficient daily bars`: 211

## Set sizes

- **Set A** (Chartink `fno-intraday-buy-20` BUY): **0**
- **Set B** (union of 7 NL scans): **43**
- **Set C** (in-house daily-gates-only): **0**

## Intersections

- **A ∩ C** = 0 → (none)
- **B ∩ C** = 0 → (none)
- **C − (A ∪ B)** = 0 (rule fires, no Chartink scan caught) → (none)
- **(A ∪ B) − C** = 43 (Chartink fires, daily gates miss) → AGARIND, AJMERA, AKSHARCHEM, AMANTA, APOLLO, BAFNAPH, BHAGCHEM, BIRLACABLE, BLUESTONE, CEMPRO, CHEMFAB, GTECJAINX, HITECHCORP, IDEAFORGE, INDOFARM, ITDC, JNKINDIA, JTLIND, KAYA, KRIDHANINF, … (+23 more)

## Per-set symbol lists (first 20)

- **Set A**: (none)
- **Set B**: AGARIND, AJMERA, AKSHARCHEM, AMANTA, APOLLO, BAFNAPH, BHAGCHEM, BIRLACABLE, BLUESTONE, CEMPRO, CHEMFAB, GTECJAINX, HITECHCORP, IDEAFORGE, INDOFARM, ITDC, JNKINDIA, JTLIND, KAYA, KRIDHANINF, … (+23 more)
- **Set C**: (none)

## Interpretation

> **DATA-READINESS BLOCKER — Set C is NOT a rule signal.**

All 211 universe symbols were skipped: every symbol has only ~121 daily bars in DuckDB, below the **200** the rule requires for the SMA(volume, 200) warm-up (gate 8). **Zero** symbols were actually evaluated, so Set C = 0 reflects missing history, not gate strictness. The production rule's own warm-up guard (`len(bars_daily) < 200`) would likewise reject all 211 symbols today — it would fire 0 (coincidentally matching Chartink's 0), but via warm-up rejection, not gate logic. **No conclusion can be drawn about whether the daily gates are loose or tight until the historify backfill extends to >=200 trading days (currently ~121, ~6 months).** The script is correct and re-runnable once that backfill lands.

**Note on the 4 skipped gates.** Gates 3 & 4 (5m Supertrend vs daily close) demand price be riding above a freshly-flipped 5-min trend line; gate 5 (15m RSI>50) demands intraday momentum; gate 13 (5m vol > 2× SMA10) demands a live volume burst on the firing bar. Together they turn a daily 'eligible' list into a moment-of-entry trigger. Set C is therefore an **upper bound** on what the full rule would fire — every intraday gate can only shrink it. Validate these once we have an intraday tape (5m + 15m bars) for a session.

