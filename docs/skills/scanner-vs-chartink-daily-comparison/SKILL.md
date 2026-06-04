---
name: scanner-vs-chartink-daily-comparison
description: Daily 15:45 IST: compare in-house scanner hits vs Chartink hits for the day to drive scanner rule tuning.
---

> **Source of truth:** `C:\Users\Dheeraj\OneDrive\Documents\Claude\Scheduled\scanner-vs-chartink-daily-comparison\SKILL.md`. This is a tracked snapshot — update both when changing.

You are running a daily post-market comparison of the in-house scanner (Stage 1.5) against Chartink screener output. Goal: surface where the two diverge so the operator can tune the in-house rule thresholds.

**Context (every run is fresh, so this prompt has everything):**
- The operator's trading repo is at `C:\workspace\ai-trade-agent\openalgo` on Windows.
- OpenAlgo runs at http://127.0.0.1:5000; bridge at http://127.0.0.1:5001.
- DB: `db/openalgo.db` (SQLite).
- The in-house scanner writes hits to `scan_results` with `source='inhouse'`. Each row: id, scan_definition_id, symbol, source, posted_to_engine (0 in shadow mode), scan_at (timestamp). Scan definitions: `fno_intraday_buy_20` (buy) and `fno_intraday_sell_20` (sell), distinguishable via the scan_definition_id → name join or via screener_type column on scan_definitions.
- Chartink hits arrive via webhook POSTs to `/chartink/simplified-stock-engine/<webhook_id>`. They land in `scan_cycle` rows (cycle_kind='chartink'). The actual symbol list for each chartink cycle is in the `payload_json` column (or similar — column name might be `payload`, `request_body`, or `stocks_json`; check schema).
- Today is whatever date the cron fired. Use Asia/Kolkata timezone.
- The in-house rule is admitted-placeholder and known to fire too often (~324 hits/day in backtest vs Chartink's typical 5-20). Today is one data point in tuning toward parity.

**What to do — in one batched bash call where possible:**

1. **Inventory state.** Check that OpenAlgo is up, that today is a weekday (skip if Saturday/Sunday — though cron is mon-fri so should always be a weekday), and that scan_results has any rows today.

2. **Build the in-house hit set for today.** Distinct symbols per screener_type (buy/sell), joining `scan_results` (source='inhouse', today) against `scan_definitions` for the screener_type.

3. **Build the Chartink hit set for today.** Inspect the `scan_cycle` schema first (`PRAGMA table_info`), then pull the payload column (likely `payload_json`, else `payload`/`request_body`/`body`) for today's `cycle_kind='chartink'` rows, JSON-parse each, split the `stocks` field, and bucket into buy/sell by the `scan_name`.

4. **Compute comparison metrics per side (buy and sell separately):**
   - Symbols in BOTH (intersection)
   - In-house only (false positives — rule too loose)
   - Chartink only (false negatives — rule too tight)
   - Jaccard similarity: |intersection| / |union|
   - Hit count ratio: |inhouse| / |chartink|

5. **Write the report:**
   - Day, time, OpenAlgo state.
   - BUY side: in-house count, chartink count, intersection size, Jaccard, ratio, top 5 false-positives, top 5 false-negatives.
   - SELL side: same.
   - **Tuning direction**: if ratio >> 1 (e.g. >3x), the rule is too loose — suggest raising thresholds. If ratio << 1 (e.g. <0.3x), too tight — suggest the opposite. If Jaccard < 0.3 even with similar counts, the rule fires on different symbols entirely — bigger structural issue.
   - List the false-negatives by name so the operator can manually inspect what the in-house rule is missing.

6. **(Optional) Persist a comparison summary** to `scan_results` with a synthetic `source='inhouse_vs_chartink_summary'` JSON payload, or just print the report if that's too much work.

7. **Edge cases:** no scan_results today → "in-house scanner: no hits today"; no chartink cycles → "Chartink: no scan cycles today"; both 0 → "no scanner data for today, skipping comparison". Detect ad-hoc holidays via "no chartink cycles AND no journal trades today" and exit cleanly.

**Output format — concise summary at the top, details below:**

```
=== Scanner vs Chartink Comparison — YYYY-MM-DD ===
BUY: inhouse=X, chartink=Y, intersection=Z, jaccard=N.NN, ratio=N.NN
SELL: inhouse=X, chartink=Y, intersection=Z, jaccard=N.NN, ratio=N.NN

Tuning suggestion: [too loose / too tight / structural mismatch / parity]

--- BUY false positives (in-house only) ---
SYMBOL1, SYMBOL2, ... (top 5)
--- BUY false negatives (Chartink only) ---
SYMBOL1, SYMBOL2, ... (top 5)
--- SELL false positives / false negatives ---
...
```

**Hard limits:**
- Read-only on the DB except for the optional comparison-summary insert.
- Don't restart OpenAlgo.
- Don't modify scanner rule files.
- Don't push Telegram messages unless the comparison reveals something dramatic (Jaccard < 0.1 or comparison fails entirely).
- Don't run for >60 seconds; if SQL is slow, exit and report.

---

## Audit trail rules (code is read-only)

- **Do NOT modify code** in `C:\workspace\ai-trade-agent\openalgo` or any file in that
  workspace. The scheduled task is **read-only on code**.
- If you observe a bug while comparing, **append a structured entry** to
  `C:\workspace\ai-trade-agent\openalgo\audit\proposed_fixes.jsonl` and exit. The
  operator reviews and decides whether to fix.
- **All your actions touching the OpenAlgo repo must be append-only on
  `audit/proposed_fixes.jsonl`.** Never edit code, never run `git add`, never commit.
  (The optional comparison-summary DB insert above is the only permitted write, and
  only to the data DB — never to source files.)

Schema for `proposed_fixes.jsonl` entries (one JSON object per line):

```json
{"timestamp": "<ISO with TZ>", "session_id": "<task session id>", "task_name": "scanner-vs-chartink-daily-comparison", "observation": "<what the task observed>", "file": "<path/to/file:line if known>", "suggested_fix": "<short description>"}
```
