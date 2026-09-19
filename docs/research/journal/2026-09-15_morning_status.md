# Morning Status — Tuesday, 2026-09-15

**For Dheeraj · generated ~08:55 IST · read-only inventory**

> **Headline: OpenAlgo is UP and the Zerodha token is live (booted 08:50:54, auto-login 08:51:36) — the blackout is over, but Monday was a total loss and the FnO scan-cycle task is still structurally dead.**

Run quality: **degraded, 4th trading day.** The sandboxed Linux shell failed to mount again
(`Plan9 share "c" is not mounted`, the 2026-09-08 Windows update), so there was no `git`,
`python`, `duckdb` or Telegram send. Everything below was reconstructed from `.git` ref files
and the raw logs via direct file reads. Where I could not verify something, I say so.

---

## 1. 🔴 Stuck / Action Required

### A. FnO scan cycle is dead — 1 confirmed total skip, and it cannot self-heal
The most recent `Fno scan cycle` session (`local_587edbbc`) ran and **did nothing at all** —
no preflight, no scan, no engine POST, not even an abort trace. All three routes to
`127.0.0.1:5000` were closed simultaneously:

| Route | State |
|---|---|
| Claude in Chrome extension | **not connected** (retried twice) |
| Workspace bash | **down** — Plan9 mount failure, 09-08 Windows update |
| Built-in browser pane | **access to `127.0.0.1:5000` declined** |

The session before it (`local_127ffdd5`) ended mid-tool-call on `tabs_context_mcp` — same
root cause, abnormal termination. An older cycle (`local_c74fc106`) *did* reach OpenAlgo and
wrote an abort trace (row 17697), so this is a recent regression, not a permanent config break.

**Fix:** open the Claude side panel in Chrome and sign in with the same account. That alone
restores the scan cycle; the sandbox mount is a separate problem.

### B. Sandbox mount failure — day 4, still unfixed
This is the root cause of **every** missing Telegram delivery and every unverifiable DB check
across the last four reports. It needs a real fix, not another workaround. Today it also cost
us: per-branch un-FF'd commit counts, historify freshness numbers, and `strategy_runtime_override`
row inspection.

### C. Monday 2026-09-14 was a total blackout — confirm nothing is owed
There is **no `openalgo_2026-09-14.log` file and zero errors logged on 09-14**. The app was
down from Friday 09-11 09:32:13 (graceful shutdown) until today 08:50:54 — 3 calendar days,
1 full trading day lost. Proof from this morning's boot, APScheduler's own miss report:

```
Sector Follow CAP5_VOL daily reset (09:00 IST)  — missed by 23:50:59
Post-market review (17:15 IST)                  — missed by 15:35:59
Scanner vs Chartink EOD comparison (15:45 IST)  — missed by 17:05:59
open15 option-liquidity sweep (15:45 IST)       — missed by 17:05:59
_arm_job (09:10)                                — missed by 23:40:59
... 35 jobs total reported missed at boot
```

Nothing armed, nothing traded, no evening jobs. **This is the fifth consecutive session with
no afternoon/evening jobs** — the recurring pattern is that the app stops shortly after the
open15 window (Friday it died at 09:32, two minutes after the 09:30 exit). **Watch it again
around 09:30 today.**

### D. One number needs explaining
Today's standup found the boot catch-up wrote a **prior-day realized P&L snapshot of
−₹64,675.58 on the primary account** for a day on which no strategy ran. That is either a
stale/ misattributed snapshot or something real that happened outside the strategies. Worth
five minutes before you trust today's P&L surfaces.

---

## 2. 🟢 Dispatch tasks complete in last 24h

| Session | Verdict | Notes |
|---|---|---|
| `local_ea93c993` — Weekday trading standup (today) | **DONE** | Completed during this run. Journal: `docs/research/journal/2026-09-15.md`. Telegram not sent. |
| `local_c1bcf22c` — Morning status report (09-14) | **DONE** | Full report delivered. Telegram not sent. |
| `local_d58e3760` — Weekday trading standup (09-14) | **DONE** | Degraded run; correctly called the Friday-09:32 shutdown. |

## 3. ⏳ Dispatch tasks running

**None.** Every session in the ledger is `idle`. The standup was running when this report
started and finished mid-run.

## 4. ⚠️ Dispatch tasks failed

| Session | Verdict | Notes |
|---|---|---|
| `local_587edbbc` — Fno scan cycle | **ERRORED (skipped)** | Infrastructure unavailable — see §1A. Zero side effects; no code touched. |
| `local_127ffdd5` — Fno scan cycle | **ERRORED** | Terminated mid-tool-call (`tabs_context_mcp`), no final message. |

---

## 5. Git state

| Check | Result |
|---|---|
| `HEAD` | `ref: refs/heads/dev` |
| `dev` | `088dd672d4a47e7e0fc4b461e38d12bee1e7f889` |
| `origin/dev` | `088dd672d4a47e7e0fc4b461e38d12bee1e7f889` |
| **Un-FF'd commits on `dev`** | **0** — local and origin are identical |
| Movement since yesterday | `4781088d` → `088dd672` — **dev advanced.** Today's standup attributes this to #726 (open15 A/B/C trade rating) merging at 08:49, one minute before boot. |

**Working tree (from the boot dirty-check at 08:50:54, `OPENALGO_BOOT_DIRTY_CHECK_ENABLED`):**

Tracked modifications — only two, and both are 4+ days old:
```
M  .gitignore                                  (STAGED — still looks like an interrupted intent)
 M strategies/simplified_engine/LEARNINGS.md   (unstaged)
```

Untracked — **~105 files**, unchanged in character from Monday:
- 13 × `db/*.db.bak.*` (oldest `20260714`) + `db/.fuse_hidden0000003400000001`
- 24 × `backtest/options_open15/*` research scripts, plus `backtest/open15_rolling/`,
  `backtest/open15_missed_days/`, `backtest/inhouse_scanner/r60/`
- 28 × `docs/research/journal/*.md` (every journal since 08-21, including yesterday's)
- 22 × `log/restart_*.{out,err}`
- `.claude/launch.json`, `url_1345069591.txt`

**Not verified:** per-branch un-FF'd counts across the other ~90 local heads. `dev` is the one
that matters and it is clean; the rest needs a shell.

---

## 6. OpenAlgo health

### Boot and recovery — clean
```
08:50:54  boot (dirty-tree warning, 35 missed jobs reported)
08:51:05  WS handshake 403 "Authentication failed"   ← dead overnight token, expected
08:51:14  auto-login watcher: no live session at boot — attempting auto-login
08:51:35  broker_session_refreshed emitted (dheeraj.sonawane / zerodha)
08:51:36  Primary Zerodha account auto-login SUCCEEDED
08:51:36  WS adapter reconnected, full universe re-subscribed (quote mode)
08:52:05  master contract downloaded — 110,315 records, 28s, 11 exchanges
08:52:00+ historify daily-D catch-up running (2026-09-10 → 2026-09-15)
```
The 403s were **boot-order noise, not an outage** — the WS tried the flushed overnight token
before auto-login minted the new one. No errors logged after 08:51:35.

### Error rate
| Window | Count | Assessment |
|---|---|---|
| 2026-09-15 total | 162 | — |
| 04:22–04:28 burst | **146 — PYTEST NOISE, ignore** | Traceback cites `test/test_open15_trade_rating.py:306`; symbols are `AAA`/`CCC`/`CAP`/`UNG`. A #726 test run, not live. |
| 08:51:05–08:51:35 | **16 — genuine but resolved** | WS 403 × 4, `websocket_client` auth-fail × 4, `connection_pool_zerodha` timeout × 2, etc. All cleared by the 08:51:36 token. |
| **Real unexplained errors** | **0** | |
| 2026-09-14 | **0** | Because nothing was running. |

### Data freshness — genuinely behind
- Scanner 1m: **112 bars vs `min_required=166`** across the universe. The seeder's broker
  fallback is returning 500 bars and winning, so the aggregator is being seeded correctly —
  but stored historify 1m is short.
- Daily-D resettle for Fri + Mon was **actively filling** as of 08:52.
- **Option-liquidity sweep last ran 2026-09-03 — 8 trading days stale.** The #591 coverage
  ladder that the 09:10 arm reads is therefore overstated. This is the one freshness item that
  can change today's behaviour.
- Sector indices: 8/8 reported stale by the standup.
- `boot_db_probe` flagged a transient `historify.duckdb` lock with no holder PID — did not
  abort boot, benign.

**Not verified:** exact historify `MAX(timestamp)` per symbol, `data_health_check` rows,
`strategy_runtime_override` rows — all need a shell.

---

## 7. Today's schedule (Tuesday — trading day)

| IST | Event | Note |
|---|---|---|
| 09:10 | **open15 `_arm_job`** | ⚠️ First live session on #726 (A/B/C rating), merged 08:49 — 21 min before arm. **Confirm `trade_grades` on the `/logs` effective-config line after it arms.** |
| 09:15 | Market open | |
| 09:16 | open15 seed selection · scanner pre-entry refresh + WS nudge | |
| 09:17–09:30 | open15 entry window · `_entry_verify_job` every minute | |
| 09:18 | Scanner + Intraday Pullback smoke checks | |
| 09:30 | open15 exit · **watch for the app dying again here** | |
| 09:35 | open15 summary | |
| 09:40 | Multi-account child fill reconciliation | |
| 15:02→15:10 | sector_follow refresh → smoke → entry 15:05 → T+1 exit 15:10 | |
| 15:18→15:28 | futures_follow smoke 15:18 → entry 15:20 → exit 15:25 → watchdog 15:28 | |
| 15:30 | EOD summaries (sector_follow, futures_follow, intraday pullback) | |
| 15:35 | Trading-day funnel + multi-account EOD mirror summary | |
| 15:45 | scanner_comparison_eod · **open15 option-liquidity sweep** | ← the stale one; it should finally run today |
| 16:00 | scanner_history_refresh | |
| 16:30 | sector_follow data-freshness check | |
| 17:15 | postmarket_review | ← hasn't fired in 5 sessions |

---

## 8. Telegram

⚠️ **Not sent — 4th trading day running.** The Cowork sandbox cannot mount your filesystem
(Plan9 share failure from the 2026-09-08 Windows update), so there is no Python available to
read and Fernet-decrypt the bot token from `db/openalgo.db`. **Please open this journal
directly in the Cowork app.** Fixing the mount fixes delivery.

---

## Sources

- `C:\workspace\ai-trade-agent\openalgo\log\openalgo_2026-09-15.log`
- `C:\workspace\ai-trade-agent\openalgo\log\errors.jsonl`
- `C:\workspace\ai-trade-agent\openalgo\.git\{HEAD, refs/heads/dev, refs/remotes/origin/dev}`
- Session transcripts: `local_ea93c993`, `local_587edbbc`, `local_c1bcf22c`, `local_d58e3760`,
  `local_127ffdd5`, `local_c74fc106`
- Companion standup: `docs/research/journal/2026-09-15.md`
