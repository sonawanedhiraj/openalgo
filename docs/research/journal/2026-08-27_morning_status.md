# Morning Status — Thursday, 27 August 2026 (08:37 IST)

**Dheeraj — nothing is broken. OpenAlgo booted at 08:30, both Zerodha accounts auto-logged in, the feed is up (228/228 symbols subscribed), and the only pending item is 1,427 uncommitted lines in `simplified_engine/LEARNINGS.md`.**

---

## 🔴 Stuck / Action Required

**No stuck or errored dispatch tasks.** Two housekeeping items, neither urgent:

1. **`strategies/simplified_engine/LEARNINGS.md` — 1,427 uncommitted insertions.** Per CLAUDE.md, strategy-learnings updates go **direct to `dev`**. This is the single largest piece of unsaved work in the tree. Worth committing before the day's sessions start appending more.
2. **778 MB of `db/*.bak.*` backups** sitting untracked in the working tree (9 × `openalgo.db` snapshots from 14 Jul → 18 Aug, plus 2 sandbox). The two newest (`20260817_624repair`, `20260818_194611_626repair`) are worth keeping; the older seven are ~596 MB of dead weight.

---

## 🟢 Dispatch tasks complete in last 24h

All 30 most-recent sessions are titled **"Fno scan cycle"** and all are **idle** — none running, none stuck, none errored. Sampled 5 transcripts for verdicts:

| Session (short id) | Verdict | Latest message |
|---|---|---|
| `ce0034e5` | **DONE** | 16:47 IST — off-hours, skipped silently (correct) |
| `35e0d348` | **DONE** | 16:32 IST — past 16:30 cutoff, skipped |
| `530cd0a6` | **DONE** | 16:17 IST — full EOD summary for 26 Aug |
| `8f67b7ee` | **DONE** | 16:02 IST — EOD summary (flagged itself a duplicate fire) |
| `2d16424a` | **DONE** | 15:32 IST — primary EOD summary for 26 Aug |

**Observation worth acting on eventually:** the `fno-scan-cycle` cron (`*/15 9-16 * * 1-5`) fired **four times** in the 15:30–16:47 EOD window on 26 Aug, and three of those runs explicitly recognised themselves as duplicates and no-opped. It's self-correcting (the no-duplicate rule holds), but it's burning four sessions to produce one report. A `>=15:15 and not already-summarised` early exit would save the turns.

**Yesterday's result, for context** (from the 15:32 session): simplified engine, sandbox, 6/6 trades (cap hit), 4W/2L = 66.7% win rate, **gross +₹133.19 → net −₹356.79** after ₹489.98 modelled charges. Third confirmed charge-drag day this month — a green gross day flipped red by ~₹82/trade of fixed cost. The lone short (MPHASIS) lost again.

## ⏳ Dispatch tasks running

**None.** Zero running sessions at 08:37 IST.

**Scheduled and pending today:**

| Task | Next fire (IST) | Status |
|---|---|---|
| `weekday-trading-standup` | 08:47 | enabled, ~10 min out |
| `fno-scan-cycle` | 09:01 | enabled, first cycle of the day |
| `morning-status-report` | this run | ✅ |
| `stuck-task-watchdog` | — | **disabled** since 20 Jun |
| `daily-trading-pipeline` | — | disabled (deprecated) |
| `scanner-vs-chartink-daily-comparison` | — | disabled (retired → in-app `scanner_comparison_eod`) |

---

## Git state

**`dev` is clean vs `origin/dev`** — zero unpushed commits. Latest origin/dev:

```
3f70434be Merge PR #683 from feat/682-open15-waiting-and-holiday-gate
0aa9a7a15 [#682] feat(open15): pre-open 'waiting' feed state + holiday gate at the arm
71fa81c03 Merge PR #681 from fix/680-scanner-reference-registry-test-leak
67fbc1772 Merge PR #679 from fix/675-tick-liveness-tick-aware-ladder
477ca05d9 [#680] fix(test): reset scanner_reference_data registry at both writer and consumer
```

### Branches with un-FF'd commits

33 branches (of ~300 local) carry commits not on `origin/dev`. Filtered to those touched since 10 Aug:

| Branch | Ahead |
|---|---|
| `fix/626-open15-rejected-order-published-as-fill` | **7** |
| `fix/633-sqlite-wal-busy-timeout` | 3 |
| `feat/595`, `feat/600`, `feat/604`, `feat/648-scanner-tradeable-universe` | 2 each |
| `fix/612`, `fix/624`, `fix/627`, `fix/637`, `fix/641`, `fix/643` | 2 each |
| `main` | 1 |
| 19 others (`chore/620`, `docs/610`, `fix/587`…`fix/666`, 2× `claude/*`) | 1 each |

These are almost certainly **already-merged branches whose local ref is behind the squashed merge commit** — the work is on `dev`, the branch just wasn't deleted. `fix/626` at 7 ahead is the only one large enough to be worth a `git log origin/dev..fix/626` glance before pruning. A branch-cleanup pass would cut the local list substantially.

### Working tree

**Modified (2):**
- `.gitignore` (+2)
- `strategies/simplified_engine/LEARNINGS.md` (+1,427) ← the action item above

**Untracked (95),** grouped:
- **Research/backtest scratch (~30):** `backtest/options_open15/*.py` (BS pricing, IV history, July variants), `backtest/news_event_study/*`, `backtest/inhouse_scanner/r60/`, `backtest/open15_rolling/`, `backtest/open15_missed_days/`
- **DB backups (11, 778 MB):** see action item 2
- **Misc:** `.claude/launch.json`, `audit/open15_replay_removal_backup_20260817_165557.json`

---

## OpenAlgo health

**Verdict: healthy.** The app restarted this morning and self-healed the daily Zerodha token expiry without intervention.

| Signal | Timestamp (IST) | Read |
|---|---|---|
| `log/openalgo_2026-08-27.log` | 08:37:41 | 🟢 live, writing now |
| `log/errors.jsonl` | 08:33:11 | 🟢 last error 4 min ago, all transient |
| `db/logs.db` | 08:33:11 | 🟢 |
| `db/openalgo.db` | 08:31:52 | 🟢 |
| `db/sandbox.db` | 08:33:10 | 🟢 |
| `db/historify.duckdb` | **26 Aug 14:25** | 🟡 stale-looking — but see below |

The duckdb mtime is misleading: the **boot convergence backfill is running right now** (`Job 78fad74b`, daily-`D` re-settle, 120-symbol batches with 9.5 s cooldowns — `LODHA`, `LT`… as of 08:37). The file mtime will advance as it commits. Not a concern.

### Boot timeline this morning

```
08:30:47  broker_auto_login_settings table ready
08:30:51  boot_db_probe: duckdb transient lock, no holder PID — did not abort (correct)
08:31:06  watcher: no live session at boot — attempting auto-login
08:31:20  ✅ Primary Zerodha auto-login succeeded
08:31:30  ✅ scanner pre-subscribed 228/228 · regime 10/10 · WS up
08:31:52  ✅ child:Mai-Zerodha auto-login succeeded
08:31:53  ✅ Master contract download completed (31.5 s)
08:32:20  child re-auth confirmed, caches cleared
08:33:10  catch-up tasks running
08:37+    historify D re-settle backfill in progress
```

### Errors — last 4 hours: **25**, all in the 08:30:51–08:33:10 pre-login window

| Count | Logger | Message |
|---|---|---|
| 4 | `services.websocket_client` | Failed to authenticate with WebSocket server |
| 3 | `broker.zerodha.api.order_api` | Incorrect `api_key` or `access_token` |
| 3 | `services.account_open15_service` | child positions read failed (broker=zerodha) |
| 2 | `broker.zerodha.api.data` | Error fetching multiquotes |
| 2 | `services.quotes_service` | get_multiquotes → incorrect token |
| 2+1+1 | `zerodha_websocket` / `websocket` | Handshake 403 Forbidden (03:00 GMT = 08:30 IST) |
| 2 | `services.websocket_service` | Connection error for user dheeraj.sonawane |
| 1 | `connection_pool_zerodha` | Adapter connection failed: Connection timeout |

**Every one of these predates the 08:31:20 auto-login** — they are the expected dead-token transients between process start and token refresh, and the WS came up clean at 08:31:30 immediately after. This is the same benign morning pattern the 26 Aug EOD session described ("mostly benign morning re-auth transients").

**Two errors that fired *after* the successful login and are worth a look:**

1. `broker_auto_login_service: Failed to send auto-login summary notification` (08:31:52) — **also fired on 26 Aug.** Two consecutive days. The auto-login is working; its Telegram confirmation is not. Given that the auto-login is now the thing standing between you and a dead session at 09:15, having its success/failure notification silently broken is the wrong failure to tolerate. Likely the same eventlet event-loop send failure the 26 Aug session logged to `audit/proposed_fixes.jsonl` ("6 dropped Telegram broadcasts").
2. `sandbox.catch_up_processor` — `IntegrityError: UNIQUE constraint failed: sandbox_daily_pnl.user_id, …` plus `sandbox.holdings_manager` — `Instance '<SandboxPositions>' has been deleted` during T+1 settlement (both 08:33:10). New today. Cosmetic on a sandbox book, but it means the catch-up daily P&L snapshot did not write.

### Error volume trend

| Date | Errors |
|---|---|
| 25 Aug | 867 ← the #673/#675 WS-proxy pooled-reconnect incident |
| 26 Aug | 133 |
| 27 Aug (to 08:37) | 25 |

Sharp and sustained decline after the #673/#675/#680 fixes merged. `errors.jsonl` is truncated to the last 1,000 lines on startup, so the 25 Aug figure is a floor, not a total.

---

## Today's schedule (Thursday — trading day)

| IST | Event |
|---|---|
| 09:10 | `open15_vol_breakout` arm (universe select, F&O filter, funds clamp) |
| **09:15** | **Market open** |
| 09:16–09:29 | open15 entry window (rolling watch-list re-ranks if enabled) |
| 09:30 | open15 hard flatten |
| 09:40 | `multi_account_fill_reconcile` |
| 15:02 | sector_follow pre-entry refresh |
| 15:03 | sector_follow smoke check |
| 15:05 | **sector_follow_cap5_vol entry** (post-#512 CAS shift — *not* 15:20) |
| 15:10 | sector_follow T+1 exit (hard-clamped — never into the auction) |
| 15:18 | futures_follow smoke check |
| 15:20 | **futures_follow_cap50 entry evaluation** |
| 15:25 | futures_follow T+1 exit |
| 15:28 | futures_follow EOD watchdog |
| 15:30 | EOD summaries (sector_follow, futures_follow) |
| 15:35 | multi-account mirror EOD summary |
| 15:45 | `scanner_comparison_eod` |
| 15:30–17:00 | scanner backfill convergence loop |
| 17:15 | `postmarket_review` (+ expectation contracts, LLM triage) |

⚠️ **Note on the schedule reminder in this task's spec:** it still lists sector_follow at 15:18/15:20/15:25. That was superseded on 2026-08-03 by issue #512 (NSE Closing Auction Session) — sector_follow now runs **15:02 / 15:03 / 15:05 / 15:10**, hard-clamped to 15:10. Only `futures_follow_cap50` still runs the 15:18/15:20/15:25 chain. Worth correcting in the SKILL.md so this section stops being wrong daily.

---

## ⚠️ Telegram delivery

**Blocked.** `api.telegram.org` fails DNS resolution from the Cowork sandbox (`[Errno -3] Temporary failure in name resolution`) — same finding as the standup task and as yesterday's morning report. This is a sandbox network restriction, not a bot-config problem.

**Please read this journal directly in Cowork.** If you want these on your phone, the fix is to allowlist `api.telegram.org` for the Cowork sandbox — or, better, have OpenAlgo itself send it (it already has a working outbound Telegram path and the credentials, and it isn't sandboxed). That would also give the broken `auto-login summary notification` a second look, since it's the same delivery layer.

---

*Read-only run. No DB writes, no git operations, no commits.*
