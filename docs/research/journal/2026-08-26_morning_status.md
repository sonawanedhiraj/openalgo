# Morning Status — Wednesday, 2026-08-26

**Generated 08:50 IST** (scheduled task `morning-status-report`, read-only)

> **Dheeraj — OpenAlgo is not running. It shut down cleanly at 20:38 IST last night and has not been booted today; boot it before 09:10 IST or open15 loses the day.**

---

## 🔴 Stuck / Action Required

### 1. OpenAlgo is DOWN — boot before 09:10 IST (P0, ~20 min left)

- Last log line: `log/openalgo_2026-08-25.log` @ **2026-08-25 20:38:32 IST** — a clean shutdown (WS pool disconnected, ZMQ context cleaned, port released). Not a crash.
- **No `log/openalgo_2026-08-26.log` exists.** Nothing has written to `log/`, `db/openalgo.db`, or `db/historify.duckdb` today.
- Consequence if not booted: `open15_vol_breakout` arms at **09:10** and its selection window closes **09:29** — a late boot skips the day loudly (per CLAUDE.md ops note). The 15:18/15:20 sector_follow + futures_follow chain also needs the process up well before then.
- Action: `uv run app.py` (or your usual launcher), then confirm the boot-time backfill convergence runs.

### 2. Zerodha daily re-login required

- Yesterday's tail carries `broker.zerodha.api.funds: Incorrect api_key or access_token` (42×) and `order_api: Incorrect api_key or access_token` — the daily token was already dead by evening.
- Today's token has not been minted (nothing running). Log in at `/broker` after boot, or confirm the headless auto-login watcher is enabled and Chromium is installed.

### 3. Static-IP whitelist may be stale — verify before 09:15

Last night's run rejected every open15 entry with:

```
IP (122.169.47.35) is not allowed to place orders for this app.
Update allowed IPs on the Kite developer console.
```

These particular rejections came from a **test/dev run at ~20:20 IST** (symbol `AAA`, qty 1447 — synthetic), so it is not proof of a live-path break. But if `122.169.47.35` is your current WAN IP and it is not on the Kite developer console allow-list, **real orders will be rejected today the same way** — and the #548 paper path will quietly demote them rather than trade. Worth a 30-second check on the Kite console.

---

## 🟢 Dispatch tasks — last 24h

| Session | Verdict | Notes |
|---|---|---|
| `Fno scan cycle` (16:47 IST) | **DONE** | Outside market hours, skipped as designed |
| `Fno scan cycle` (16:32 IST) | **DONE** | Outside market hours, skipped as designed |
| `Fno scan cycle` (16:17 IST) | **DONE** | Full EOD summary produced — 2 trades, net **−₹710.82**, 0W/2L (ABCAPITAL −₹328.51 stop_loss, MOTHERSON −₹382.31 eod_watchdog). Tick log 3.58M ticks, 0 drops. Nothing new filed to `proposed_fixes.jsonl`. |
| ~26 earlier `Fno scan cycle` sessions | **DONE / idle** | All idle, no error terminations |

**0 STUCK. 0 ERRORED.**

## ⏳ Running now

| Session | State | Notes |
|---|---|---|
| `Weekday trading standup` | **PROGRESSING** | Started 08:49:54 IST, 3 turns, latest: "gathering repo state, data freshness, scheduler status". Normal — this is the 08:48 sibling task, not stuck. |

### Scheduled-task inventory (enabled only)

- `fno-scan-cycle` — every 15 min, 09:00–16:59 Mon–Fri. Next run **09:01 IST**. Last ran 2026-08-25 16:46 IST.
- `weekday-trading-standup` — 08:48 Mon–Fri. Ran 08:49 today (running now).
- `morning-status-report` — this one. 08:07 Mon–Fri.

Disabled/retired: `daily-trading-pipeline`, `scanner-vs-chartink-daily-comparison`, `stuck-task-watchdog`, and two one-shot June report tasks.

---

## Git state

**`dev` is clean relative to `origin/dev` — zero un-FF'd local commits.** Nothing waiting to push.

Recent `origin/dev` history:

```
67fbc1772 Merge PR #679 from fix/675-tick-liveness-tick-aware-ladder
57d4e1b8c [#675] fix(tick-liveness): ladder paces on ticks — bar close stays the health verdict
d97425dcf Merge PR #678 from feat/677-open15-feed-health
f9b8c86c4 [#677] feat(open15): feed-health indicator + clock-based selection finalize
648e05da6 Merge PR #674 from fix/673-ws-proxy-pooled-reconnect-snapshot
```

Note: #673/#675/#677 all landed **after** yesterday's process started. **Today's boot is the first time the live app runs the tick-liveness and pooled-reconnect fixes** — worth watching the 09:15–09:30 window to see whether the #673 subscription-restore path behaves.

**Local branches:** 60+ stale branches (oldest June 10). None ahead of `origin/dev` in a way that matters — everything merged. A branch-prune session would clear ~50 of them, but it is not urgent.

**Working tree: dirty (2 modified, 93 untracked).**

- Modified: `.gitignore` (+2), `strategies/simplified_engine/LEARNINGS.md` (**+1350 lines** — a large uncommitted learnings body. Worth committing; that's real content sitting outside git.)
- Untracked, categorized:
  - **26** backtest scratch files (`backtest/options_open15/*`, `backtest/open15_rolling/`, `backtest/open15_missed_days/`, `backtest/inhouse_scanner/r60/`)
  - **9** `db/openalgo.db.bak.*` + 2 `db/sandbox.db.bak.*` snapshots (dating back to 2026-07-14 — safe to prune the older ones)
  - **5** journal files in `docs/research/journal/` (incl. yesterday's) never committed
  - ~40 rotated `log/openalgo_*.log.*` files
  - `.claude/launch.json`, `audit/open15_replay_removal_backup_*.json`

Expect the boot-time dirty-tree WARNING again on restart (`OPENALGO_BOOT_DIRTY_CHECK_ENABLED`).

---

## OpenAlgo health

| Signal | Last write | Read |
|---|---|---|
| `log/openalgo_2026-08-25.log` | 2026-08-25 **20:38:32** | Clean shutdown. **No 08-26 log = process down.** |
| `log/errors.jsonl` | 2026-08-25 20:34:04 | 1194 rows, all within 20:20–20:34 (see below) |
| `log/error_digest_2026-08-25.json` | 2026-08-25 17:15:00 | Postmarket review fired on schedule ✅ |
| `db/historify.duckdb` | 2026-08-25 19:36 | **No pre-market backfill today** — expected, app is down |
| `db/openalgo.db` | 2026-08-25 19:35 | — |
| `db/sandbox.db` | 2026-08-25 19:07 | — |

### Error rate — two distinct pictures

`errors.jsonl` is truncated to 1000 lines on startup, so its current contents cover only the **evening test/dev run (20:20–20:34)**, not the trading day. Both are reported separately below; conflating them would misread the day.

**A. Evening test run, 20:20–20:34 (1194 errors in 14 min — mostly synthetic):**

| Count | Logger | Dominant message |
|---|---|---|
| 297 | `open15_breakout_service` | ENTRY REJECTED `AAA` (static-IP), rolling re-rank failed, tick capture failed |
| 210 | `strategies_dashboard_api` | `strategy_llm_config` / stats aggregation query failures |
| 140 | `option_symbol_service` | `Failed to fetch quotes: Invalid openalgo apikey` |
| 92 | `scanner_dry_tripwire_service` | — |
| 63 | `place_options_order_service` | LTP fetch failed, invalid apikey |
| 42 | `broker.zerodha.api.funds` | `Incorrect api_key or access_token` |
| 28 | `live_position_reconciliation_service` | SUPPRESS exit NIFTY25AUG26FUT — broker flat (journaled=455, broker=0) |

Symbol `AAA` and the invalid-apikey wall mark this as a harness run against a dead token, **not live trading**. The 210 `strategies_dashboard_api` failures are the one bucket here that looks like a genuine code-path problem rather than test-fixture fallout — worth a look when you have time, not before market.

**B. Actual trading day, from the 17:15 postmarket digest (34 templates, 17 loggers — low volume, one real incident):**

| Count | Template |
|---|---|
| 16 | `tick_liveness_watchdog` — **TICK LIVENESS CRIT: no live bar closes**, and 3× **auto-heal EXHAUSTED (tried [none])** |
| 7 | `connection_pool_zerodha` |
| 4 | `websocket_client: Failed to authenticate with WebSocket server` |
| 3 | `auth_utils: Failed to emit broker_session_refreshed event` |
| 2 each | zerodha_websocket, websocket_service, broker_auto_login, order_api (bad token), `account_open15_service: child positions read failed`, scanner_smoke_check, telegram broadcast failure |

That is the 2026-08-25 feed incident already diagnosed and fixed by #673/#675/#677 — all three merged to `origin/dev` yesterday evening. **Today's boot is their first live exercise.**

---

## Today's schedule (Wednesday — trading day)

| IST | Event |
|---|---|
| **09:10** | open15 arm — **hard deadline for boot** |
| 09:15 | Market open |
| 09:16 | open15 seed selection (or 09:17 scheduler fallback, #677) |
| 09:29 | open15 entry cutoff |
| 09:30 | open15 exit |
| 09:40 | multi_account_fill_reconcile |
| 15:18 | sector_follow smoke check |
| 15:20 | sector_follow + futures_follow_cap50 entry evaluation |
| 15:25 | Exits |
| 15:28 | futures_follow EOD watchdog |
| 15:30 | EOD summary |
| 15:45 | scanner_comparison_eod |
| 16:00 | scanner_history_refresh |
| 17:00 | Backfill convergence loop ends |
| 17:15 | postmarket_review |

---

## ⚠️ Telegram

**Blocked.** `api.telegram.org` returns `403 Forbidden (tunnel connection failed)` from the Cowork sandbox — the same allow-list restriction the standup task hit. No alert was sent; please read this journal directly in the Cowork app, or allow-list `api.telegram.org` in the sandbox network policy to enable push delivery.

---

## Sources

- `log/openalgo_2026-08-25.log`, `log/errors.jsonl`, `log/error_digest_2026-08-25.json`
- `git log/status/for-each-ref` on `C:\workspace\ai-trade-agent\openalgo`
- `mcp__session_info__list_sessions` + `read_transcript` (30 sessions)
- `mcp__scheduled-tasks__list_scheduled_tasks`

All reads were read-only. No git operations, no DB writes.
