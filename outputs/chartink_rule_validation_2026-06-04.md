# Chartink BUY rule — Day-1 validation (2026-06-04)

## Methodology & limitations

This is a **daily-gates-only** validation. We do not have today's intraday tape (scanner WS issues + the rule wasn't deployed in the running OpenAlgo), so gates **3, 4, 5, 13** (5m Supertrend, 15m RSI, 5m volume surge) are **not evaluated**. The 8 daily/weekly gates (1, 2, 6, 7, 8, 9, 10, 12) are evaluated against cached DuckDB bars. The production rule (`services/scan_rules/fno_intraday_buy_chartink.py`) is unchanged; gate logic is re-implemented inline here.

End-of-day settled bars are used (today = iloc[-1], yesterday = iloc[-2]) — no live-forming-bar offset.


### Data notes

- Set A from scan_cycle (ground truth): 33 chartink rows today, 14 non-empty, 4 unique BUY symbols deduplicated across the day.
- Firing window (IST): 2026-06-04T10:49:45.386472+05:30 → 2026-06-04T14:05:22.856259+05:30.
- Union of 43 symbols across 7 NL scans. NOTE: NL builder ignored the 'FnO' qualifier — these are cash-segment scans, and row display was truncated to ~10-13 per scan by the capture.
- Universe: 211 distinct symbols with daily bars (exchange=NSE).
- Universe symbols evaluated: 211; skipped (insufficient/NaN): 0

## Chartink firing detail (scan_cycle ground truth)

- Source: `scan_cycle` table — 33 chartink rows today, 14 with non-empty BUY payloads.
- Firing window (IST): **2026-06-04T10:49:45.386472+05:30 → 2026-06-04T14:05:22.856259+05:30**.
- Chartink re-alerts the same stock every scan cycle while it still matches, so the per-symbol counts below are how many cycles each symbol appeared in (1 unique stock can fire many times).
  - **TRENT**: fired **9×** (10:49:45 → 14:05:22 IST)
  - **CGPOWER**: fired **5×** (11:05:06 → 13:18:53 IST)
  - **SAIL**: fired **5×** (10:49:45 → 14:05:22 IST)
  - **NATIONALUM**: fired **1×** (14:05:22 → 14:05:22 IST)

## Set sizes

- **Set A** (Chartink `fno-intraday-buy-20` BUY): **4**
- **Set B** (union of 7 NL scans): **43**
- **Set C** (in-house daily-gates-only): **2**

## Intersections

- **A ∩ C** = 1 → CGPOWER
- **B ∩ C** = 0 → (none)
- **C − (A ∪ B)** = 1 (rule fires, no Chartink scan caught) → SAMMAANCAP
- **(A ∪ B) − C** = 46 (Chartink fires, daily gates miss) → AGARIND, AJMERA, AKSHARCHEM, AMANTA, APOLLO, BAFNAPH, BHAGCHEM, BIRLACABLE, BLUESTONE, CEMPRO, CHEMFAB, GTECJAINX, HITECHCORP, IDEAFORGE, INDOFARM, ITDC, JNKINDIA, JTLIND, KAYA, KRIDHANINF, … (+26 more)

## Per-set symbol lists (first 20)

- **Set A**: CGPOWER, NATIONALUM, SAIL, TRENT
- **Set B**: AGARIND, AJMERA, AKSHARCHEM, AMANTA, APOLLO, BAFNAPH, BHAGCHEM, BIRLACABLE, BLUESTONE, CEMPRO, CHEMFAB, GTECJAINX, HITECHCORP, IDEAFORGE, INDOFARM, ITDC, JNKINDIA, JTLIND, KAYA, KRIDHANINF, … (+23 more)
- **Set C**: CGPOWER, SAMMAANCAP

## Interpretation

The daily gates fire on **2** symbols — comparable to or tighter than the 43-symbol NL union, suggesting the daily gates are about right to slightly strict. B∩C overlap is 0% of Set B.

**Note on the 4 skipped gates.** Gates 3 & 4 (5m Supertrend vs daily close) demand price be riding above a freshly-flipped 5-min trend line; gate 5 (15m RSI>50) demands intraday momentum; gate 13 (5m vol > 2× SMA10) demands a live volume burst on the firing bar. Together they turn a daily 'eligible' list into a moment-of-entry trigger. Set C is therefore an **upper bound** on what the full rule would fire — every intraday gate can only shrink it. Validate these once we have an intraday tape (5m + 15m bars) for a session.

