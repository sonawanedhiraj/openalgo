# 2026-06-23 Silent Outage — Analysis and Resilience Plan

## Incident Summary

| Time (IST) | Event |
|---|---|
| 09:38:51 | First ECONNREFUSED on port 8765 — WS proxy was already dead (or never started) at this timestamp |
| 09:38 – 11:10 | Continuous ECONNREFUSED flood from websocket_client and websocket_service (~every 7s) |
| 10:22, 10:42 | Telegram bot init failed: "Timed out" — network block already in effect |
| 11:10:59 | First boot: thread watchdog started; OpenAlgo came up but DuckDB contention errors spike at 11:11 |
| 11:11:11 | DuckDB: "The process cannot access the file" — cross-process file lock from prior zombie job |
| 11:11:12 | Telegram inbound bot: "Timed out" — network still blocked |
| 11:11:17 | Telegram inbound bot: start=False |
| 11:11:18 | Flask first run: Scanner started (216 symbols), WS proxy connected at 8765 |
| 11:18:41 | Second boot: thread watchdog started — ~7 minute restart with DuckDB cleanup |
| 11:18:45 | scanner_dry_tripwire CRIT: last_inhouse_at was 2026-06-19 (4 days stale) |
| 11:18:46 | ImportError: `cannot import name 'notify' from 'services.notification_service'` — tripwire alert silently swallowed |
| 11:18:59 | Flask second (final) run: all services registered; WS proxy reconnects at 8765 |
| 13:14:16 | Zerodha WebSocket ping/pong timed out — self-healed in 3 seconds (re-connected by 13:14:20) |
| 14:50:04 | **LAST LOG LINE** — `fno_intraday_sell_chartink RBLBANK: rejecting — bars_daily is None` |
| 14:50:04 – end | Flask silent death: no traceback, no shutdown message, no ERROR. All scheduled jobs (15:14 watchdog, 15:20 sector_follow, 15:25 exit, 15:30 reconciliation, 15:45 scanner comparison) missed. |

**Duration of silence**: approximately 3h 38m from last log line to expected EOD jobs completing around 15:45.

**Reported last healthy observation**: `/health/ws_proxy` returning "healthy + 224 symbols" as late as 14:10 IST. This is consistent with the log: WS proxy (port 8765) was alive and connected at 11:11 and 11:18, and remained so through the Zerodha reconnect at 13:14. The ECONNREFUSED errors from 09:38 are from a PRIOR session — they are logged by persistent internal clients retrying against a port that wasn't open yet (from the previous OpenAlgo run that was down overnight or since early morning).

---

## Evidence from Logs

### What the logs confirm

1. **Last log line**: `[2026-06-23 14:50:04,136] WARNING in fno_intraday_buy_chartink: fno_intraday_buy_chartink RBLBANK: rejecting — bars_daily is None (no daily-D data)` — line 25742 out of 25742.

2. **No shutdown event**: There is no ERROR, WARNING, "Shutting down", "Flask", SIGTERM, or exception anywhere near line 25742. The log simply terminates mid-stream at a routine WARNING. This is consistent with an external process kill (Windows Task Manager, system reboot, memory termination) rather than a Python exception or clean shutdown.

3. **WS proxy contradiction resolved**: The health endpoint at `blueprints/health.py:129` does a real TCP socket probe (`sock.connect_ex`) — it is NOT reading in-process state. The "ECONNREFUSED at 09:38" entries in errors.jsonl are from internal WebSocket clients (websocket_service, websocket_client) that retained connections from a prior session and began retrying at 09:38 when they found port 8765 unreachable. Flask itself was running a prior session (log entries at 07:xx and 08:xx are test output and simplified engine ticks from that session). The 11:10/11:18 entries are two successive boots. Port 8765 was genuinely dead before 11:11 and genuinely alive from 11:11 onward. `/health/ws_proxy` at 14:10 reporting "healthy + 224 symbols" was accurate.

4. **Telegram fully blocked all day**: Every Telegram call from 10:22 onward returned "Timed out". The `notification_service.notify` fallback also dropped every event because no live bot and no chats. The tripwire CRIT at 11:18:45 (in-house scanner 4 days stale) would have been the correct operator signal — it was silenced by the `ImportError` and then by the Telegram block. Operator had zero visibility into this CRIT.

5. **ImportError confirmed**: `scanner_dry_tripwire_service.py:183: ImportError: cannot import name 'notify' from 'services.notification_service'` — this is the issue that PR #104 / fix/tripwire-import-hotfix addresses. The tripwire CRIT fired at boot, computed a 5748-minute gap (last_inhouse_at was 2026-06-19), but the notifier ImportError swallowed the alert entirely.

6. **DuckDB file lock at 11:11**: Prior zombie backfill jobs from the first session held `historify.duckdb` open. This caused "The process cannot access the file" errors for ~7 minutes before the second boot resolved them with zombie cleanup ("Marked zombie job ... as failed").

7. **Zerodha ping/pong at 13:14**: Self-healed in 3 seconds. Not related to the Flask death.

8. **In-house scanner stale 4 days**: Confirmed by tripwire at boot. No scan results since 2026-06-19. This is why all daily scanner comparison jobs for the week would have been empty.

### What cannot be determined from logs

- The exact kill cause (OOM? Windows process termination? forced restart from another process?). No Windows Event Log or Task Manager snapshot was captured.
- Whether any threads were starved or deadlocked before the kill (thread watchdog at 11:18 registered but no WARN/CRIT thread count alerts appear in the log after that).
- Whether port 5000 was still accepting HTTP requests between 14:50 and when the operator noticed.

---

## Root Cause Hypothesis

**Best-supported hypothesis: External process termination with no Python-visible signal.**

The evidence:
- Log terminates mid-stream at a routine WARNING with no exception, no flush failure, no shutdown hook output
- Bridge (port 5001) and packet monitor survived — only Flask on port 5000 was affected
- No OOM Killer entry, no SIGTERM handler log (Flask/Werkzeug would log "Restarting with stat" or similar on clean stop)
- Windows Task Manager forceful termination, system resource pressure, or an automated Windows Update restart are all consistent with this pattern

**Contributing factors** (not root cause but amplifiers):
- Telegram network block meant the operator had no alerting channel
- ImportError in tripwire meant the 4-day scanner staleness was never alerted even when it fired
- No external liveness probe meant the death went undetected until EOD jobs were missed
- No "missed fire" telemetry meant there was no second-channel alert when APScheduler jobs didn't execute

---

## Testing Gap Analysis

### Failure Class Mapping

| Failure class | Status | Notes |
|---|---|---|
| External process kill (OOM, taskkill, Windows forceful termination) | **COMPLETE GAP** | No test can catch this. No external watchdog exists to detect or restart. |
| OpenAlgo never started / not restarted after crash | **COMPLETE GAP** | No external probe; operator only notices at EOD or when checking manually |
| WS proxy thread died inside the process | Partially covered | PR #103 has WS proxy tests; ConnectionPool tests in #89. But these test internal paths, not "thread died silently at runtime" |
| /health/ws_proxy returns healthy while 8765 isn't listening | **RESOLVED** (not the actual bug today) | health.py:129 does a real TCP probe. This hypothesis was incorrect for today's incident. |
| Scheduled APScheduler job didn't fire (silent miss) | **COMPLETE GAP** | No heartbeat written after each job fires; no "missed fire" alert exists |
| Network block (Telegram blocked) | **COMPLETE GAP** | Only alert channel. No fallback (desktop notification, ntfy.sh, file-watch) |
| Historical 1m data goes stale silently | Covered by tripwire | Tripwire fires but silenced by ImportError today; ImportError fix in PR #104/branch fix/tripwire-import-hotfix |
| Boot-time ImportError disables safety service silently | **OPEN ISSUE #104** | Fix is in WIP branch. The exact failure mode occurred today. |

### Why Testing Didn't Catch This

Testing validated what happens inside a running Flask process under controlled conditions. No test exercises "what happens when the OS kills the process," "does the process restart automatically," or "does the operator receive an alert within N minutes if all jobs stop firing." The entire test suite assumes Flask is alive and reachable — the outage class is structurally outside the test boundary.

---

## Structural Diagnosis

| Hypothesis | Rating | Evidence |
|---|---|---|
| 1. All tests are PROCESS-INTERNAL | **Confirmed** | Every test in `test/` imports Flask app and runs inside the process. No test spawns a subprocess and kills it. |
| 2. No external liveness probe | **Confirmed** | Nothing on the Windows machine restarts Flask if it dies. Bridge on 5001 survived, indicating the failure was Flask-specific, not machine-wide. |
| 3. /health/ws_proxy reads in-process state, not actual port | **Noise for today's incident** | The health.py implementation DOES do a real TCP socket probe (confirmed at line 129). This hypothesis was wrong for today, but the distinction matters for documentation accuracy. |
| 4. Critical scheduled jobs have no "did it fire?" telemetry | **Confirmed** | There is no heartbeat written after each APScheduler job fires. The 15:14 watchdog, 15:20 entry, 15:25 exit, 15:30 reconciliation, and 15:45 comparison all missed silently. No secondary alert fired because no missed-fire monitoring exists. |
| 5. Telegram-only safety alerts; blocked today | **Confirmed** | 10:22 first timeout. Every subsequent `notify()` call dropped. ImportError in tripwire compounded this. No fallback channel. |
| 6. Tests validate code; they don't validate operational invariants | **Confirmed** | The test suite answers "does this code path work?" not "is the system operating correctly across a full trading day?" |

---

## Remediation Plan

### P0 — This week (to never lose another trading day)

---

**P0-1: External liveness watchdog + auto-restart**

What it does: A Windows Task Scheduler task or NSSM service that polls `http://127.0.0.1:5000/health` every 60 seconds and restarts `uv run app.py` if two consecutive probes fail. This is the only item that would have caught today's outage and auto-recovered before 15:14.

How to implement:
- Option A (NSSM): Install NSSM, wrap `uv run app.py` as a Windows service with automatic restart on failure. NSSM handles log rotation and restart-on-crash natively.
- Option B (Task Scheduler): A PowerShell script in Task Scheduler running every 2 minutes: if `(Invoke-WebRequest http://127.0.0.1:5000/health -TimeoutSec 5 -ErrorAction SilentlyContinue).StatusCode -ne 200`, kill the existing `python` process and restart `uv run app.py` in background, log timestamp to `log/watchdog.log`.
- Files: `scripts/watchdog/health_watchdog.ps1`, `scripts/watchdog/install_watchdog.ps1`
- Effort: ~2 hours
- Would have caught today: **YES** — Flask was dead by 14:50; a 60-second probe at 14:51 would have restarted it before the 15:14 watchdog needed to fire.

---

**P0-2: Per-scheduled-job heartbeat + missed-fire alert**

What it does: An APScheduler `job_listener` (listening for `EVENT_JOB_EXECUTED` and `EVENT_JOB_ERROR`) writes a heartbeat row to a lightweight SQLite table `scheduler_heartbeats(job_id, fired_at, status, next_expected_at)` after every job fires. A second lightweight daemon thread checks every 5 minutes: if any critical job (watchdog, sector_follow_entry, sector_follow_exit, reconciliation, scanner_comparison) has a `next_expected_at` more than 10 minutes in the past without a new heartbeat row, it fires a non-Telegram alert (see P0-3).

How to implement:
- `database/scheduler_heartbeat_db.py` — table + upsert
- `services/apscheduler_heartbeat_listener.py` — `EVENT_JOB_EXECUTED` listener + missed-fire checker thread
- Register listener in `app.py` alongside other service inits
- Files: two new files + registration in app.py (~3 hours)
- Would have caught today: **YES** — the 15:14 watchdog's non-fire would have been detected by 15:19 at the latest.

---

**P0-3: Non-Telegram fallback alert channel**

What it does: When `notification_service.notify()` fails or when the operator-facing alert needs to be guaranteed, fall through to a local Windows desktop notification and/or write a sentinel file to `log/ALERT_<timestamp>.txt` that the operator can detect via file-watch.

Recommended implementation:
- **Primary fallback**: `plyer` desktop toast notification (cross-platform, no network needed). `from plyer import notification; notification.notify(title="OpenAlgo ALERT", message=msg, timeout=0)`. This pops a persistent Windows notification even if Telegram is blocked.
- **Secondary fallback**: Write `log/ALERTS/ALERT_<YYYYMMDD_HHMMSS>_<event>.txt` — the operator can watch this directory or check it on login.
- Modify `notification_service.py`: after the Telegram-dropping WARNING, call `_local_notify(msg)` which tries plyer first, then file-write.
- Files: modify `services/notification_service.py`, add `services/local_notification_service.py`
- Effort: ~2 hours
- Would have caught today: **YES** — the 11:18:45 tripwire CRIT would have produced a desktop popup even though Telegram was blocked.

---

**P0-4: Fix the ImportError (already in WIP)**

The `scanner_dry_tripwire_service.py:183` ImportError (`cannot import name 'notify'`) is already addressed in the `fix/tripwire-import-hotfix` branch (PR #105). This is a P0 blocker: the tripwire correctly detected the 4-day scanner staleness at boot, but the broken import silenced the only alert. Merge PR #105 immediately.

- Files: `services/scanner_dry_tripwire_service.py`
- Effort: already done; needs PR merge
- Would have caught today: **PARTIALLY** — the alert would have fired at 11:18, but Telegram was blocked. Needs P0-3 to be actionable.

---

### P1 — Rest of June

---

**P1-1: PID file + heartbeat timestamp (unclean-shutdown detector)**

What it does: On every Flask boot, write `log/openalgo.pid` containing the PID and a start timestamp. A background daemon thread overwrites `log/openalgo_heartbeat.ts` with the current epoch every 30 seconds. On the next boot, if `openalgo.pid` exists and `openalgo_heartbeat.ts` is less than 60 seconds old (the process was alive very recently), log a WARNING: "Prior run ended without clean shutdown at <heartbeat_ts>". This makes "what killed Flask" answerable for the next incident.

- Files: add to `app.py` boot sequence (~1 hour)
- Would have caught today: **YES** — on next restart, the operator would know exactly when Flask died.

---

**P1-2: Promote /health/ws_proxy TCP probe to be checked by external watchdog**

The external liveness watchdog (P0-1) should check `http://127.0.0.1:5000/health/ws_proxy` in addition to `/health`. A response of `{"status": "down"}` should trigger a Telegram alert (if available) or a desktop notification (P0-3), but NOT necessarily restart Flask (the WS proxy subprocess dying doesn't kill Flask). This makes the WS proxy status independently observable from outside the process.

- Files: update `scripts/watchdog/health_watchdog.ps1`
- Effort: ~30 minutes on top of P0-1

---

**P1-3: Boot-time import validator**

What it does: A pre-boot script (or a boot hook in `app.py`) that imports every safety service module and fails loud before the main app starts if any import is broken. Catches ImportError regressions before they silently disable safety services at runtime.

Specifically: import `services.scanner_dry_tripwire_service`, `services.notification_service`, `services.eod_watchdog_service`, `services.sector_follow_service`, `services.futures_follow_service` and assert that their key symbols (e.g., `notify`, `register_jobs`) are callable. On failure, write to `log/ALERTS/` and exit non-zero.

- Files: `scripts/boot_validator.py`, called from `start.sh` or as a pre-step in the Task Scheduler task before starting Flask
- Effort: ~1 hour

---

**P1-4: Resolve bars_daily=None for index symbols**

At the time of the Flask death (14:50), the log shows repeated WARNINGs: `fno_intraday_buy_chartink BANKNIFTY: rejecting — bars_daily is None (no daily-D data)`. The scanner D-interval backfill had an error at boot (scanner backfill boot [D]: errors=1 due to DuckDB Binder Error). The D-arm for index symbols was not refreshed. This means the in-house screener was rejecting all index-adjacent signals for the entire afternoon. This is a separate operational degradation from the Flask death but explains the poor screener coverage.

- Resolution: fix the DuckDB Binder Error in `data_freshness_service.connect_historify_readonly()` for the D-interval scan. The existing fallback in `connect_historify_readonly` should handle this — confirm the D-arm uses the same fallback path as the 1m arm.

---

### P2 — Nice-to-have

---

**P2-1: ntfy.sh push notification channel**

Self-hosted or cloud `ntfy.sh` as a third notification channel (after Telegram, after local desktop). ntfy.sh sends push notifications to the operator's phone via a lightweight HTTP POST to a personal topic URL. Works even if the machine's network is partially blocked, as long as the ntfy.sh server is reachable. Falls back if Telegram and plyer both fail.

**P2-2: APScheduler missed-fire report in Telegram EOD summary**

After the 15:45 EOD scanner comparison job, append a "Scheduled jobs today" section listing each critical job with its last-fired timestamp. If any critical job's last-fired timestamp is `None` or older than 24 hours, mark it as MISSED. This surfaces silent misses in the daily operator review.

**P2-3: Windows Event Log integration**

On Windows, write startup and shutdown events to the Windows Application Event Log using `pywin32`. This allows diagnosis of external kills via Event Viewer without needing to correlate Python log files. Particularly useful for OOM kills (Event ID 1001) and forced restarts.

---

## P0 Items in Detail

### P0-1: External liveness watchdog in detail

The watchdog must satisfy three properties:
1. **Independent of Flask**: runs as a separate Windows process/service, not a thread inside Flask
2. **Network-independent**: polls localhost only, doesn't need Telegram or internet
3. **Auto-restart**: does not just alert, actually kills and restarts the Flask process

Recommended: NSSM (Non-Sucking Service Manager). Install NSSM, run:
```
nssm install OpenAlgo "C:\path\to\uv.exe" "run app.py"
nssm set OpenAlgo AppDirectory "C:\workspace\ai-trade-agent\openalgo"
nssm set OpenAlgo AppRestartDelay 5000
nssm set OpenAlgo AppStdout "C:\workspace\ai-trade-agent\openalgo\log\openalgo_stdout.log"
nssm set OpenAlgo AppStderr "C:\workspace\ai-trade-agent\openalgo\log\openalgo_stderr.log"
nssm start OpenAlgo
```
NSSM restarts the process on any exit (clean or unclean) with a configurable delay. The health-check polling is then a secondary layer on top (use a Task Scheduler task calling the PS script every 2 minutes to catch hung-but-alive processes that aren't responding to HTTP).

Expected state change: Flask is auto-restarted within 5-10 seconds of dying. The 15:14 watchdog would have fired (Flask restarts at ~14:51, watchdog fires at 15:14 on a fresh process). Sector_follow entry at 15:20 would have been attempted.

### P0-2: Per-scheduled-job heartbeat in detail

APScheduler fires `EVENT_JOB_EXECUTED` after every successful job run. Add:
```python
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR

def _heartbeat_listener(event):
    job_id = event.job_id
    status = 'executed' if not event.exception else 'error'
    database.scheduler_heartbeat_db.upsert(job_id, status, fired_at=datetime.utcnow())

scheduler.add_listener(_heartbeat_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
```
The checker thread runs every 5 minutes. Critical job IDs to monitor:
- `simplified_engine_eod_watchdog` (expected at 15:14)
- `sector_follow_entry` (15:20)
- `sector_follow_exit` (15:25)
- `scanner_comparison_eod` (15:45)
- `sector_follow_data_health` (16:30)

If `fired_at` for any critical job is `None` or `(next_expected_at - now) > 10 min AND fired_at < next_expected_at - 10 min`, emit a local alert (P0-3).

### P0-3: Non-Telegram fallback alert in detail

Modify `services/notification_service.py` to add after the "dropping notification" WARNING:
```python
try:
    from services.local_notification_service import local_notify
    local_notify(event_type, msg)
except Exception:
    pass  # never raise from fallback
```

`services/local_notification_service.py`:
```python
import os, time
from pathlib import Path

def local_notify(event_type: str, message: str):
    # 1. Windows desktop toast (requires plyer: uv add plyer)
    try:
        from plyer import notification
        notification.notify(title=f"OpenAlgo: {event_type}", message=message[:256], timeout=0)
    except Exception:
        pass
    # 2. File sentinel (always)
    alert_dir = Path("log/ALERTS")
    alert_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    (alert_dir / f"ALERT_{ts}_{event_type}.txt").write_text(f"{ts}\n{event_type}\n{message}\n")
```

Expected state change: even with Telegram blocked, the operator sees a Windows notification popup AND finds `log/ALERTS/` populated when they check the machine. The tripwire CRIT from 11:18 today would have produced both.

---

## One-line Answer

Testing didn't catch today's outage because **all tests run inside a live Flask process and validate code paths — none test the operational invariant that the process is running, jobs are firing on schedule, and the operator is notified when either condition fails.**
