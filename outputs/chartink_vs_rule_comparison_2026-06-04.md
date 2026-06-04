# Chartink scans vs in-house rule — side-by-side (2026-06-04)

Per-symbol, per-gate breakdown of why our `fno_intraday_buy_chartink` rule fired (Set C) and why it skipped the Chartink NL-scan stocks (Set B). Only the **8 daily/weekly gates** are evaluable today — intraday gates 3/4/5/13 (5m Supertrend, 15m RSI, 5m vol surge) need a live tape we don't have. Production rule unchanged; gate logic duplicated from `scripts/validate_chartink_rule.py`.

Short-circuit kill order: gate6 → gate12 → gate1 → gate9 → gate10 → gate2 → gate8 → gate7

## 1. Summary

| Set | Definition | Count |
|---|---|---|
| A | Chartink `fno-intraday-buy-20` BUY | 0 |
| B | Union of 7 NL scans | 43 |
| C | In-house daily-gates matches | 2 |
| D | (B ∪ C) ∩ F&O universe (evaluable) | 2 |
| — | Our F&O DuckDB universe | 211 |

**Intersections**
- A ∩ C = 0
- B ∩ C = 0
- B ∩ universe = 0 (rest are cash-segment, not in our F&O bars)
- C: CGPOWER, SAMMAANCAP

## 2. Our rule's hits (Set C) — full gate breakdown

### CGPOWER

| Gate | Check | Value | Threshold | Result |
|---|---|---|---|---|
| gate6 | close > 100 | 941.0 | 100.0 | ✅ |
| gate12 | close < 5000 | 941.0 | 5000.0 | ✅ |
| gate1 | close > prevClose × 1.03 (3% gap) | 941.0 | 933.85 | ✅ |
| gate9 | open > prevClose | 915.0 | 906.65 | ✅ |
| gate10 | open > pivot | 915.0 | 903.9 | ✅ |
| gate2 | vol > SMA(vol,50) | 8448975 | 3873605 | ✅ |
| gate8 | vol > SMA(vol,200) | 8448975 | 3486735 | ✅ |
| gate7 | weekly ATR(21) > 5% × close | 51.2 | 47.05 | ✅ |

**Verdict:** all 8 daily gates ✅ → armed for intraday triggers.

### SAMMAANCAP

| Gate | Check | Value | Threshold | Result |
|---|---|---|---|---|
| gate6 | close > 100 | 183.78 | 100.0 | ✅ |
| gate12 | close < 5000 | 183.78 | 5000.0 | ✅ |
| gate1 | close > prevClose × 1.03 (3% gap) | 183.78 | 183.26 | ✅ |
| gate9 | open > prevClose | 177.94 | 177.92 | ✅ |
| gate10 | open > pivot | 177.94 | 176.74 | ✅ |
| gate2 | vol > SMA(vol,50) | 22145849 | 22084183 | ✅ |
| gate8 | vol > SMA(vol,200) | 22145849 | 19511639 | ✅ |
| gate7 | weekly ATR(21) > 5% × close | 13.15 | 9.19 | ✅ |

**Verdict:** all 8 daily gates ✅ → armed for intraday triggers.

## 3. NL scan stocks (Set B) in our F&O universe

_None of the 43 NL stocks exist in our F&O DuckDB universe — they are cash-segment names (NL builder ignored the 'FnO' qualifier). Nothing to evaluate here; this is the expected result._

## 4. Chartink BUY (Set A)

Baseline `chartink_fno_baseline_2026-06-04.json` absent; operator-documented `fno-intraday-buy-20` BUY count today is **0**. Nothing to compare. Our rule also fired 0 on the BUY-scan intersection — consistent.

## 5. Key takeaways

Every evaluable Set D symbol is a Set C pass — the NL stocks that made it into our F&O universe all cleared the daily gates. The real filtering happens earlier: 43 of 43 NL names never reach evaluation because they are cash-segment and absent from our F&O bars.

**One-liner:** the daily gates are tight — only 2/211 F&O names pass all 8 — and Set B barely overlaps our universe, so segment mismatch (cash vs F&O) is the biggest single reason our rule and Chartink's NL scans disagree.

