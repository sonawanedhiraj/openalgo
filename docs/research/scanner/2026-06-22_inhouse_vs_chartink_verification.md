# In-house Screener vs Chartink — 2026-06-22 Verification

> **Dheeraj** — this report was triggered by the question "why does /scanner show
> '0 today' but still shows signals at 15:00 and 11:20-11:30?". Short answer: the
> WS proxy is down, so today was genuinely dead on both sides. The "signals" you
> saw are from **June 9** and **June 19** — not today. See P0 below.

---

## TL;DR

The "0 today" counter is **correct**. Today (2026-06-22 Monday) had **zero signals
on both sides**. Root cause: the WebSocket proxy subprocess on port 8765 was not
running — `ConnectionRefusedError [Errno 10061]` every ~10 seconds all day. This
had two consequences: (1) no ticks → in-house scanner produced 0 `scan_results`;
(2) 180+ errors/30-min window → Cowork `fno-scan-cycle` preflight aborted all 23
scan cycles → no Chartink data fetched today. The "Latest signals" on the /scanner
page (JSWENERGY/INDHOTEL/GODREJPROP/KAYNES/PATANJALI at "15:00", ZYDUSLIFE at
"11:20-11:30") are the **5 most-recent historical rows** — from June 9 and June 19
respectively — displayed without a date label, making them look current. There is
no signal-categorisation bug; there is a UX display bug and an operational WS
process failure.

---

## A — Raw data (2026-06-22)

### A.1 — In-house `scan_results` (today)

```
Row count: 0
```

Zero in-house scanner rows for today. The scanner's tick-driven evaluation loop
requires 5-minute bar closes; with no WS ticks, no bars closed, so no evaluation
ran.

### A.2 — Chartink `scan_cycle` (today)

```
ts_ist                 cycle_kind  screener_buy  screener_sell  post_status
─────────────────────  ──────────  ────────────  ─────────────  ──────────────────
2026-06-22 09:33:13    chartink    None          None           aborted_preflight
2026-06-22 09:48:28    chartink    None          None           aborted_preflight
2026-06-22 10:03:08    chartink    None          None           aborted_preflight
... (23 rows total, every ~15 min through 15:03:23, ALL aborted_preflight)
```

All 23 Cowork `fno-scan-cycle` runs today were `aborted_preflight`. Sample
`error_payload`:

```json
{
  "abort_reason": "preflight: 15 errors in last 30 min (raw 314 across 3 signatures, capped 5/sig)",
  "abort_stage": "preflight",
  "scan_name": "fno-scan-cycle"
}
```

By 15:03 IST the raw error count had grown to 315 across 5 signatures.

### A.3 — EOD scanner_comparison (today, run at 15:45)

```
screener_side  inhouse_count  chartink_count  intersection_count  jaccard  ratio
─────────────  ─────────────  ──────────────  ──────────────────  ───────  ─────
BUY            0              0               0                   NULL     NULL
SELL           0              0               0                   NULL     NULL
tuning_suggestion: "parity: no hits on either side today"
```

Both sides independently confirm zero signal activity today.

### A.4 — Errors.jsonl (today, representative)

```
ts                   logger                           message
────────────────────  ───────────────────────────────  ─────────────────────────────────────────
2026-06-22 14:01:41  services.websocket_client        Error in WebSocket connection:
                                                       [Errno 10061] Connect call failed
                                                       ('127.0.0.1', 8765)
2026-06-22 14:01:49  services.websocket_client        Failed to connect to WebSocket server
2026-06-22 14:01:49  services.websocket_service       Connection error for dheeraj.sonawane:
                                                       Failed to connect to WebSocket server
2026-06-22 18:01:26  connection_pool_zerodha          Adapter initialization failed:
                                                       Adapter initialized successfully
```

The file was auto-truncated (retains last 1000 entries). Oldest entry is 14:01
IST, but the first preflight abort at 09:33 already cited "20 errors in last 30
min" — meaning errors began no later than 09:00 IST. Two distinct error signatures:

1. **`ConnectionRefusedError [Errno 10061]`** — WS proxy port 8765 not listening;
   this fires on every WS client reconnect attempt (~every 10 s)
2. **`Adapter initialization failed: Adapter initialized successfully`** — a
   separate Zerodha adapter issue; the message is ironically mis-labelled (the
   text says "successfully" but logged at ERROR level; this appears to be a
   log-message bug in `connection_manager.py:448` — the error path is firing even
   though adapter init returned normally)

---

## B — "0 today" counter root cause

The counter is computed in
[`blueprints/scanner_api.py:120-127`](../../blueprints/scanner_api.py#L120):

```python
today_str = _today_ist()           # → "2026-06-22" (IST, correct)
today_count = (
    sess.query(ScanResult)
    .filter(
        ScanResult.scan_definition_id == d.id,
        ScanResult.run_at.like(f"{today_str}%"),   # → LIKE '2026-06-22%'
    )
    .count()
)
```

**`run_at` storage format** (confirmed from DB): IST with explicit timezone suffix,
e.g. `2026-06-22T15:03:23.340258+05:30`. The `.like("2026-06-22%")` pattern
matches all rows where `run_at` starts with today's IST date string — which it
does. **No UTC/IST bug here.**

**The counter is accurate: 0 today because there are genuinely 0 scan_results
rows for 2026-06-22.**

The "bug" is not in the counter. The confusion arises from the `latest_signals`
section (see Section C).

---

## C — Symbol-level diff

### C.1 — Today (2026-06-22)

| Class | BUY count | SELL count | Examples |
|---|---|---|---|
| Only in-house caught | 0 | 0 | — |
| Only Chartink caught | 0 | 0 | — |
| Both caught (same side) | 0 | 0 | — |
| Timing diff > 5 min | N/A | N/A | — |

**No divergence to analyse today** — both sides were dark. The EOD comparison
service independently confirmed this.

### C.2 — Prior trading day (2026-06-19 Friday) for baseline context

The last day where both sides had data:

| Class | BUY side | SELL side |
|---|---|---|
| In-house only | 0 | ZYDUSLIFE (3 hits, 11:20-11:30 IST) |
| Chartink only | AUROPHARMA, LICI, RADICO, ZYDUSLIFE (4 names) | HCLTECH, INFY, LTM, MPHASIS, PERSISTENT, TCS, TECHM, WIPRO (8 names) |
| Both (same side) | 0 | 0 |
| Jaccard | 0.0 | 0.0 |
| Recall (in-house/chartink) | 0% | 0% (ZYDUSLIFE was a BUY on Chartink, but SELL in-house — cross-side hit, not a match) |

June 19 is a known tick-starvation day (the scanner_comparison tuning note reads:
"structural mismatch: in-house and Chartink hits are fully disjoint — most likely
in-house tick starvation"). The pattern is the same as the June 11-12 collapse
documented in prior research.

### C.3 — The "Latest signals" display confusion

The `/scanner/api/definitions` endpoint returns `latest_signals` from
[`scanner_api.py:101-116`](../../blueprints/scanner_api.py#L101):

```python
latest_rows = (
    sess.query(ScanResult)
    .filter(ScanResult.scan_definition_id == d.id)
    .order_by(ScanResult.run_at.desc())
    .limit(_LATEST_SIGNAL_COUNT)          # 5 rows
    .all()
)
```

**No date filter.** When today has 0 signals, this returns the 5 most-recent
rows from any prior day. The `run_at` is included in full ISO format, so the
backend is not at fault. The React scanner page apparently renders only the
*time portion* of `run_at`, not the full date, making historical rows look like
they're from the current session.

**Confirmed historical origin of the signals Dheeraj saw:**

| Screen label | Actual `run_at` | True date |
|---|---|---|
| JSWENERGY at "15:00" (BUY) | `2026-06-09T15:00:11+05:30` | **June 9** |
| INDHOTEL at "15:00" (BUY) | `2026-06-09T15:00:10+05:30` | **June 9** |
| GODREJPROP at "15:00" (BUY) | `2026-06-09T15:00:10+05:30` | **June 9** |
| KAYNES at "15:00" (BUY) | `2026-06-09T15:00:10+05:30` | **June 9** |
| PATANJALI at "15:00" (BUY) | `2026-06-09T15:00:10+05:30` | **June 9** |
| ZYDUSLIFE at "11:20-11:30" (SELL) | `2026-06-19T11:20-11:30+05:30` | **June 19** |

The 15:00 timestamp is additionally misleading because it coincides with typical
daily close time — making June 9 signals look like "today's 3pm close."

---

## D — Pattern analysis

### D.1 — WS proxy outage (today's root cause)

The WebSocket proxy (`websocket_proxy/server.py`, port 8765) was not running.
Every ~10 seconds `services/websocket_client.py` attempted to reconnect and got
`ConnectionRefusedError [Errno 10061]`. This generated ~6 ERROR entries per
minute in `errors.jsonl` = ~180 entries per 30-min window.

The Cowork `fno-scan-cycle` preflight check counts recent errors and aborts when
the count exceeds a threshold (observed: abort triggered at ≥15 capped errors in
30 min). With 180+ errors/30-min, the abort fired on every single run from 09:33
to 15:03 IST. No Chartink data was fetched at all today.

The likely cause of the WS proxy being down: the daily Zerodha token expired at
~03:00 IST and the proxy was not restarted, OR the proxy process crashed. The
`Adapter initialization failed: Adapter initialized successfully` error in
`connection_manager.py:448` is a secondary symptom that needs its own
investigation (the log message appears to be inverted — logging an error when the
adapter actually initialised).

### D.2 — Scanner tick starvation (structural, ongoing)

When the WS proxy IS running but the Zerodha feed is thin (as seen on June 11-12
and June 19), the in-house scanner still produces far fewer hits than Chartink
because it only evaluates symbols where ticks arrived AND a 5-minute bar closed.
The scanner_comparison EOD job (15:45 IST) consistently shows Jaccard ≈ 0 on days
with partial tick coverage. This is not today's issue (today was a total outage)
but is the ongoing structural problem.

### D.3 — Cross-side ZYDUSLIFE anomaly (June 19)

ZYDUSLIFE appeared on Chartink **BUY** at 11:20-11:30 IST on June 19 AND on
in-house **SELL** at the same time. These are different screeners evaluating
different formulas, so it is *mathematically possible* for the same stock to
trigger both. However, a stock simultaneously signalling BUY in Chartink and SELL
in-house at the same time deserves a sanity check: either the conditions are
non-overlapping by design (and both are correct for their own formula), or one of
the rule formulas has a directional error. This is left as an open investigation
item (see recommendation P2b below).

---

## E — Recommendations (prioritised)

### P0 — Restart the WS proxy immediately

**Action (Dheeraj, operator):** Restart the OpenAlgo process or the WS proxy
subprocess. The entire signal pipeline — both in-house scanner and Cowork scan
cycle — depends on the WS proxy being alive. Tomorrow will also be a dead day
if this is not resolved tonight.

Command (after `uv run app.py` ensures the proxy subprocess starts with the main
process, or restart the whole app):
```bash
# Kill and restart OpenAlgo to respawn the WS proxy subprocess
# The proxy starts automatically as a subprocess when app.py boots
uv run app.py
```

Check that port 8765 is listening after restart: `netstat -an | findstr 8765`

Also investigate `connection_manager.py:448` — the `"Adapter initialization
failed: Adapter initialized successfully"` message is an inverted log that
generates spurious errors. If the adapter initialised successfully, this path
should log INFO, not ERROR.

### P1a — Fix React: show date on "Latest signals" cards

**File**: `frontend/src/` (scanner page component).

**Problem**: The scanner definition card shows `latest_signals` with only the
time portion of `run_at`, making previous-day signals look current.

**Fix**: Render the full `run_at` with date, e.g.
`Jun 9, 15:00` instead of `15:00`. When the signal's date is not today IST,
prepend the date string explicitly. The API already returns the full ISO `run_at`
string — this is a frontend-only change.

Example display logic:
```tsx
const signalDate = new Date(signal.run_at);
const todayIST = getTodayIST();  // or compare date portions
const label = isSameDay(signalDate, todayIST)
  ? formatTime(signalDate)          // "15:00"
  : formatDatetime(signalDate);     // "Jun 9, 15:00"
```

Until this fix lands, the "Latest signals" section should display the FULL ISO
timestamp or at minimum `YYYY-MM-DD HH:MM` to make the date unambiguous.

### P1b — Add WS proxy health check to /scanner page

**Problem**: The operator had no visual indicator that the WS proxy was down. The
scanner page showed "0 today" with no explanation of WHY — the operator had to
check errors.jsonl manually.

**Suggestion**: Surface the WS proxy status in the /scanner page header. The
existing `/scanner/api/data_health` endpoint (or a new
`/scanner/api/pipeline_health` endpoint) could expose:
- WS proxy running: Y/N
- Last tick received: timestamp
- Recent error count (from scan_cycle aborted_preflight count)

A red/yellow banner at the top of /scanner when the WS proxy is down would have
made today's diagnosis instant.

### P2a — Investigate Cowork preflight error threshold vs WS retry noise

**Problem**: The Cowork preflight aborts when `errors.jsonl` exceeds a threshold
of capped errors in 30 minutes. WS reconnect errors (~6/min) are intrinsically
noisy and will always blow past this threshold when the proxy is down. This is
actually **correct behaviour** (aborting when the system is broken), but the
preflight message could be more specific — currently it reports "15 errors in last
30 min" without naming which logger/module is generating them.

**Suggestion**: In the Cowork SKILL preflight step, log the top contributing
error signatures (logger names) so the operator can immediately see "ah, it's
websocket_client again — is the proxy running?" without having to read
errors.jsonl manually.

### P2b — Investigate ZYDUSLIFE BUY (Chartink) vs SELL (in-house) conflict (June 19)

ZYDUSLIFE fired as BUY on Chartink and as SELL on in-house at the same time on
June 19. The two formulas are different (Chartink's BUY formula vs the in-house
SELL mirror), but the directional contradiction should be reviewed:
- Pull the June 19 11:20 ZYDUSLIFE bar data (daily + 5m + 15m) from historify
- Run both BUY and SELL rule evaluators against those bars
- Identify which gate in the SELL formula fires on what was clearly a BUY setup

This could reveal a rule-logic error in one of the formulas. Low urgency (single
observation), but worth confirming before it becomes a live trade conflict.

### P3 — Symbol coverage gap (structural, tracked separately)

The in-house scanner evaluates only symbols in `SCANNER_SYMBOLS` that received WS
ticks. Chartink evaluates its full universe. On days the WS proxy is healthy, the
Chartink vs in-house recall ratio is still ~0% (June 19: Jaccard=0 on SELL,
Jaccard=0 on BUY) suggesting major tick starvation or universe mismatch.

This is the class of failure documented in the June 2026 tick-starvation memory
(`inhouse-scanner-hits-not-health-metric.md`) — tracked separately. Not
actionable today but should re-run the scanner-vs-Chartink comparison on a day
the WS proxy is confirmed healthy with full market volume to get a clean baseline.

---

## Appendix — `scan_definitions` active at time of investigation

| id | name | screener_type | rule_module | enabled |
|---|---|---|---|---|
| 1 | fno_intraday_buy_20 | buy | fno_intraday_buy_chartink | true |
| 2 | fno_intraday_sell_20 | sell | fno_intraday_sell_chartink | true |

Rule logic: BUY = 12-gate Chartink mirror (gap-up + vol surge + Supertrend + RSI).
SELL = 10-gate mirror (gap-down + simpler volume + Supertrend + RSI flipped).
Both rules have the Tier-1 hardening (D-bar-date verify + loud rejection logging)
and the market-hours gate from the 2026-06-15 Phase-B Tier-1 hardening.

## Appendix — Recent scanner_comparison history

| date | side | inhouse | chartink | jaccard | tuning |
|---|---|---|---|---|---|
| 2026-06-22 | BUY | 0 | 0 | NULL | parity: no hits on either side today |
| 2026-06-22 | SELL | 0 | 0 | NULL | parity: no hits on either side today |
| 2026-06-19 | BUY | 0 | 4 | 0.0 | too tight: only 0/4 Chartink names matched |
| 2026-06-19 | SELL | 1 | 8 | 0.0 | structural mismatch: in-house tick starvation |
| 2026-06-17 | BUY | 0 | 0 | NULL | parity |
| 2026-06-17 | SELL | 0 | 0 | NULL | parity |
