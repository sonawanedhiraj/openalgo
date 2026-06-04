---
name: fno-scan-cycle
description: FnO scan-post-monitor cycle (every 15 min during market hours) + EOD summary after 3:15 PM. Scans Chartink, POSTs to engine, checks status & errors.
---

> **Source of truth:** `C:\Users\Dheeraj\OneDrive\Documents\Claude\Scheduled\fno-scan-cycle\SKILL.md`. This is a tracked snapshot — update both when changing.

You are running a single FnO scan-post-monitor cycle for OpenAlgo's Simplified Stock Engine.

## Time gate
Check current IST time and branch into one of three modes:
- **Before 9:30 AM or after 4:30 PM IST**: Reply "Outside market hours — skipping." and stop. If an OpenAlgo tab is already open, first leave a best-effort trace so an unexpectedly-scheduled off-hours fire isn't a silent gap (skip silently if no tab / unreachable):
  ```javascript
  fetch('http://127.0.0.1:5000/chartink/cycle/aborted', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      abort_reason: 'outside market hours — scheduler fired before 09:30 or after 16:30 IST',
      abort_stage: 'market_closed',
      scan_name: 'fno-scan-cycle'
    })
  }).then(r=>r.json()).then(d=>{window.__abortTrace=d}).catch(()=>{})
  ```
- **Between 3:15 PM and 4:30 PM IST**: Jump directly to **Step 5 (EOD Summary Mode)** — skip Steps 0-4.
- **Between 9:30 AM and 3:15 PM IST**: Run the **full cycle** (Steps 0-7).

---

## Step 0: Preflight gate
Before scanning, check the operational preflight. This catches a stalled scheduler, undeclared `daily_intent`, a SKIP'd day, dead broker session, and recent error storms — surfacing reasons to abort *before* any cycle work runs.

Use JavaScript on an OpenAlgo tab (http://127.0.0.1:5000, same-origin):
```javascript
fetch('http://127.0.0.1:5000/preflight')
  .then(r=>r.json()).then(d=>{window.__preflight=d})
```

Wait 1 second, then read `window.__preflight`.

**If `go_decision === "abort"`:**
- Report the failed checks concisely. Example: `"Preflight abort — reasons: no daily_intent declared for today; 12 errors in last hour"`.
- **Record the aborted cycle so the gap leaves a trace.** Without this, the scheduler shows `lastRunAt` but no scan_cycle row appears — turning a 5-second SQL lookup into an hour-long forensic. POST to `http://127.0.0.1:5000/chartink/cycle/aborted` from an OpenAlgo tab (same-origin):
  ```javascript
  fetch('http://127.0.0.1:5000/chartink/cycle/aborted', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      abort_reason: 'preflight: ' + ((window.__preflight?.failed_checks || []).join('; ') || 'unknown'),
      abort_stage: 'preflight',
      scan_name: 'fno-scan-cycle'
    })
  }).then(r=>r.json()).then(d=>{window.__abortTrace=d}).catch(()=>{})
  ```
  This is best-effort — if it fails, still exit (don't block on the trace).
- DO NOT proceed to Step 1 — exit the cycle here.
- Treat this as a successful preflight run (not a failure) — it's the gate doing its job.

**If `go_decision === "go"`:**
- Note `effective_mode` (`live`, `sandbox`, or `skip`) — include it in your final summary.
- Continue to Step 1.

If `/preflight` itself errors (network, 5xx), report the error and exit — do NOT proceed without a clear preflight result. Leave a trace first (best-effort, same endpoint):
```javascript
fetch('http://127.0.0.1:5000/chartink/cycle/aborted', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    abort_reason: 'preflight endpoint error (network/5xx) — could not obtain a go/abort decision',
    abort_stage: 'preflight',
    scan_name: 'fno-scan-cycle'
  })
}).then(r=>r.json()).then(d=>{window.__abortTrace=d}).catch(()=>{})
```

---

## Step 1: Scan Chartink BUY screener
1. Open a new tab and navigate to: https://chartink.com/screener/fno-intraday-buy-20
2. Wait 3 seconds for the page to load
3. Click "Run Scan" if visible
4. Wait 3 seconds for results
5. Extract stock symbols from the results table using JavaScript:
   ```javascript
   Array.from(document.querySelectorAll('table')[1]?.querySelectorAll('tbody tr') || []).map(r => r.cells[2]?.textContent?.trim()).filter(Boolean)
   ```
6. Record the BUY symbols list

## Step 2: Scan Chartink SELL screener
1. Navigate to: https://chartink.com/screener/alert-for-intraday-sell-fno
2. Wait 3 seconds for the page to load
3. Click "Run Scan" if visible
4. Wait 3 seconds for results
5. Extract stock symbols using the same JavaScript as above
6. Record the SELL symbols list

## Step 3: Log scan results for backtesting (best-effort)
Save today's scan results via the bridge server. **CORS rule**: Never use `fetch()` to call port 5001 from a port 5000 tab — it will fail. Instead, use `navigate` to go directly to the bridge endpoint, then `get_page_text` to read the response.

For BUY symbols (replace SYMBOL1,SYMBOL2 with actual comma-separated list):
1. Navigate to: `http://127.0.0.1:5001/run` — but since this is a POST, use a dedicated OpenAlgo tab and `javascript_tool` on a tab already navigated to port 5001:
   - First navigate a tab to `http://127.0.0.1:5001/status` (to set origin to port 5001)
   - Then run fetch from THAT tab (same-origin, no CORS):
   ```javascript
   fetch('/run', {
     method: 'POST',
     headers: {'Content-Type': 'application/json'},
     body: JSON.stringify({
       prompt: 'Append this line to backtest/scans/' + new Date().toISOString().slice(0,10) + '_BUY.txt (create if missing): "' + new Date().toISOString().slice(11,16) + ' SYMBOL1,SYMBOL2"'
     })
   }).then(r=>r.json()).then(d=>{window.__logBuy=d}).catch(()=>{})
   ```
2. Repeat for SELL symbols with `_SELL.txt`.

If the bridge is unreachable, skip this step silently — it's best-effort.

## Step 4: POST to Simplified Engine
Use JavaScript on any OpenAlgo tab (http://127.0.0.1:5000) to POST — these are same-origin calls and work fine:

**For BUY stocks — ALWAYS POST, even when the list is empty:**
```javascript
fetch('http://127.0.0.1:5000/chartink/simplified-stock-engine/c7d08357-6fe1-4603-bd2a-be4c9f9e06ac', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    stocks: 'SYMBOL1,SYMBOL2,...',  // pass '' (empty string) when nothing matched
    scan_name: 'BUY FnO Intraday Buy 20'
  })
}).then(r=>r.json()).then(d=>{window.__buyResult=d})
```

Note: empty `stocks` is intentional — it leaves a "scanner ran, no matches today" audit row in the scan_cycle table so the preflight freshness gate doesn't deadlock when both screeners come up empty.

**For SELL stocks — ALWAYS POST, even when the list is empty:**
```javascript
fetch('http://127.0.0.1:5000/chartink/simplified-stock-engine/c7d08357-6fe1-4603-bd2a-be4c9f9e06ac', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    stocks: 'SYMBOL1,SYMBOL2,...',  // pass '' (empty string) when nothing matched
    scan_name: 'SELL FnO Intraday Sell'
  })
}).then(r=>r.json()).then(d=>{window.__sellResult=d})
```

## Step 5: Check engine status
Use JavaScript on an OpenAlgo tab (http://127.0.0.1:5000, same-origin):
```javascript
fetch('http://127.0.0.1:5000/chartink/simplified-engine/api/status')
  .then(r=>r.json()).then(d=>{window.__status=d})
```
Report: engine_mode, active positions count, armed watches, direction states, trades today.

### Step 5a: Trade recap (EOD Summary Mode only)
If running in EOD Summary Mode (3:15–4:30 PM), also fetch the tradebook to build a trade-by-trade recap:
```javascript
fetch('/tradebook/export?format=json')
  .then(r=>r.text()).then(d=>{document.title='TB:'+d.substring(0,3000)})
```
Parse the CSV output. For each pair of entry+exit fills on the same symbol:
- Calculate P&L = (exit_price - entry_price) × quantity (negate for SHORT trades where SELL is first)
- Report a table: Symbol, Direction (LONG/SHORT), Entry Price, Exit Price, Qty, P&L
- Sum up Net P&L and Win Rate (winners / total trades)

If the tradebook is empty or unreachable, note "No trades today" and continue.

### Step 5b: Update strategy learnings (EOD Summary Mode only)
After computing the trade recap, update the active strategy's LEARNINGS.md file at
`strategies/simplified_engine/LEARNINGS.md` (use the file Read/Edit tools, NOT the browser).

**How to update:**
1. **Read** the current `strategies/simplified_engine/LEARNINGS.md` to understand the existing format and see the last daily entry.
2. **Append a new daily entry** under the `## Daily Results Log` section, following the exact format of previous entries. Include:
   - Date and day number (increment from the last entry)
   - Market regime description (based on which screeners produced signals, BUY vs SELL mix)
   - Live result summary: trade count, W/L, net P&L, win rate
   - Trade breakdown: each trade with symbol, direction, entry/exit prices, P&L, hold duration
   - Notable observations (e.g., which direction worked, re-entries, cooldown effects)
   - Tick log stats (ticks written, bytes, drops)
   - Armed watches at close
   - Any errors encountered and their impact
3. **Add or update Key Learnings** if today's results reveal new patterns:
   - New learning? Add a numbered section under `## Key Learnings`.
   - Existing learning confirmed/contradicted? Add a bullet to the relevant section.
   - Open question resolved? Mark it `[x]` in `## Open Questions / Future Research` with evidence.
4. **Do NOT duplicate**: If today's entry already exists (matching date), skip this step.

**Important**: Keep entries factual and data-driven. Every claim should reference specific trades, numbers, or comparisons. Flag observations with small sample sizes explicitly.

## Step 6: Check for errors and auto-fix via bridge
**CORS rule**: Do NOT fetch port 5001 from a port 5000 tab. Instead:
1. Navigate a tab to `http://127.0.0.1:5001/read-errors?n=5`
2. Use `get_page_text` to read the JSON response
3. Parse the errors list

If the bridge is unreachable (navigate fails or page is empty), skip the rest of this step silently.

### 6a: Evaluate errors
- Filter for **new errors only** — errors from today (match current date in the `ts` field).
- Ignore errors that are clearly from tests (tracebacks referencing `test/` paths or `unittest.mock`).
- Ignore `engineio.server` session-disconnected warnings — these are benign.
- If there are **no actionable new errors today**, skip to Step 7.

### 6b: Auto-fix via bridge
For each actionable error (up to 2 per cycle to avoid overload):
1. Navigate the bridge tab to `http://127.0.0.1:5001/status` to set origin
2. Use `javascript_tool` on THAT tab (same-origin) to POST to `/fix-bug`:
   ```javascript
   fetch('/fix-bug', {
     method: 'POST',
     headers: {'Content-Type': 'application/json'},
     body: JSON.stringify({
       error_message: '<THE ERROR MESSAGE>',
       traceback: '<THE TRACEBACK STRING>',
       file_path: '<THE FILE PATH FROM THE ERROR>'
     })
   }).then(r=>r.json()).then(d=>{window.__fixResult=d})
   ```
3. Wait 5 seconds, then read `window.__fixResult` to check the outcome.
4. If the fix was applied successfully (`status: "completed"` or similar), record it for the summary.

### 6c: Restart app if fixes were applied
If at least one fix was applied in step 6b, restart the app so changes take effect:
1. Use `javascript_tool` on the bridge tab (same-origin) to POST to `/restart-app`:
   ```javascript
   fetch('/restart-app', {
     method: 'POST',
     headers: {'Content-Type': 'application/json'},
     body: JSON.stringify({})
   }).then(r=>r.json()).then(d=>{window.__restartResult=d})
   ```
2. Wait 10 seconds for the app to come back up.
3. Verify OpenAlgo is running by navigating to `http://127.0.0.1:5000` and checking it loads.

**Safety guardrails**:
- Only fix up to **2 errors per cycle** — don't go on a fixing spree.
- If the bridge returns `status: "busy"`, skip fixing — another job is running.
- Never fix errors that involve core trading logic (`order_router_service`, `place_order_service`) — only fix infrastructure bugs (DB errors, serialization, logging, etc.). Report trading-logic errors in the summary for manual review.
- If `restart-app` fails or OpenAlgo doesn't come back within 30 seconds, report it prominently in the summary.

## Step 7: Cleanup
Close any tabs opened during this cycle.

## Output
**During market hours (full cycle):** Summarize in 3-5 lines: time, BUY symbols found, SELL symbols found, POST results, engine status, errors found, fixes applied (if any), restart status (if applicable).

**EOD Summary Mode (3:15–4:30 PM):** Provide an end-of-day report including:
- Engine mode and total trades completed today
- Trade-by-trade recap table (Symbol, Direction, Entry, Exit, Qty, P&L)
- Net P&L and win rate
- Armed watches and cooldown symbols at close
- Tick log stats (ticks written, bytes)
- Error summary (count, any actionable issues, fixes applied)
- Learnings update confirmation (what was added/updated in LEARNINGS.md)

---

## Audit trail rules (code is read-only)

- **Do NOT modify code** in `C:\workspace\ai-trade-agent\openalgo` or any file in that
  workspace. The scheduled task is **read-only on code**.
- If you observe a bug while scanning, **append a structured entry** to
  `C:\workspace\ai-trade-agent\openalgo\audit\proposed_fixes.jsonl` and exit. The
  operator reviews and decides whether to fix.
- **All your actions touching the OpenAlgo repo must be append-only on
  `audit/proposed_fixes.jsonl`.** Never edit code, never run `git add`, never commit.

Schema for `proposed_fixes.jsonl` entries (one JSON object per line):

```json
{"timestamp": "<ISO with TZ>", "session_id": "<task session id>", "task_name": "fno-scan-cycle", "observation": "<what the task observed>", "file": "<path/to/file:line if known>", "suggested_fix": "<short description>"}
```