# Morning Status — Friday 2026-08-14 (09:05 IST)

**Dheeraj — nothing is stuck, but three things want your eyes before 09:15: the Zerodha WS just dropped at 09:02, the futures_follow P0 carry violation has now fired 3 days running, and one of yesterday's four OI-rejected open15 entries (BDL) was never papered.**

---

## 1. 🔴 Stuck / Action Required

No dispatch task is stuck, errored, or waiting on you. The action items are all system-side:

| # | Item | Why it matters | Suggested action |
|---|---|---|---|
| 1 | **Zerodha WebSocket disconnected at 09:02:37** (`ping/pong timed out` → `❌ WebSocket disconnected`) | 12 min before open. The WS-proxy supervisor + tick-liveness watchdog should heal it, but the boot log also says *"Scanner WS watchdog: no API key at boot; not started"* — so the watchdog that would normally catch this **did not start this boot**. | Check `/` dashboard tick flow at ~09:16. If bars aren't closing, restart OpenAlgo. |
| 2 | **`futures_follow_cap50/t1_exit_for_carry` P0 — 3rd consecutive day** (13 Aug, 12 Aug, 11 Aug) | 1 NIFTY lot, oldest entry **2026-07-30 (now 15 days)**, 0 exits. The 13 Aug LLM triage read it as *"a permanently uncloseable journal residue, not a live stranded lot"* — the exit path can only act on positions the store still shows. | If triage is right this is a **journal cleanup**, not a trading risk — but it will fire P0 every single day until someone clears the row. Worth a 10-min issue. |
| 3 | **open15 BDL rejection was not papered** (13 Aug) | Three of four OI-rejected entries became `fill='paper'` per #548 (ADANIENSOL, KALYANKJIL, UNOMINDA). **BDL** landed `status='rejected', fill='none', pnl=NULL` — no paper row, no measurement. | Possible gap in the #548 paper path (or #595's OI filter intercepting before the paper branch). Worth an issue — `open15_vol_breakout` is **live money**. |

**Not blocking, but note:** `sector_follow` logged a P0-shaped ERROR at 09:01:01 (*"position book UNREADABLE — 1 journalled position (M&M) NOT rehydrated"*) — this **self-resolved** at 09:01:28 once the broker session refreshed: *"M&M in journal but flat in the sandbox book — skipping."* Same shape as yesterday's alert. The journal-vs-book mismatch on M&M is still there though; it's the same class as item 2.

---

## 2. 🟢 Dispatch tasks complete in last 24h

| Task | Status | Verdict |
|---|---|---|
| **Morning status report** (13 Aug) | idle | **DONE** — journal written, Telegram blocked |
| **Weekday trading standup** (13 Aug) | idle | **DONE** — journal `docs/research/journal/2026-08-13.md`, Telegram blocked |
| **Fno scan cycle** ×27 | all idle | **DONE** — every one exited cleanly. Post-16:30 and pre-09:30 runs correctly report *"Outside market hours — skipping"*; none held a turn or errored. |

No `[result] error`, no API/stream timeouts, no AskUserQuestion blockers anywhere in the 30-session window.

## 3. ⏳ Dispatch tasks running

| Task | Turns | Latest |
|---|---|---|
| **Weekday trading standup** (today, 08:48) | 5 | *"I'll run the daily standup. Starting with repo state checks."* — **PROGRESSING**, started ~30s ago. Normal. |

Scheduled-task ledger is healthy: `fno-scan-cycle` (next 09:16), `weekday-trading-standup` and `morning-status-report` all enabled and fired today. `stuck-task-watchdog` is **disabled** (last run 2026-06-20) — worth re-enabling if you want stuck-session alerts during the day.

---

## 4. Git state

**`dev` is clean and in sync — 0 commits ahead of `origin/dev`.**

Recent `origin/dev`:

```
5dfd1c4ba [#597] fix(open15): late-boot arm must not clobber a traded day's persisted day_log (#598)
7589601d7 [#595] feat(open15): seed/rolling broker-OI (>=500 lots) watch-list filter (#596)
42db78217 [#593] fix(open15): look up broker_pnl by contract symbol for option rows (#594)
293804dca docs(parameter-log): OPEN15_ATM_LOT_COST_ENABLED + OPEN15_COVERAGE_TARGET_PCT (#591)
400b51dfd feat(open15): ATM lot-cost coverage ladder on /open15_vol_breakout/logs (#592)
```

**Working tree — 2 tracked files modified, rest is scratch:**

- `M .gitignore`
- `M strategies/simplified_engine/LEARNINGS.md`

**Untracked (~140 entries), none critical:** `backtest/options_open15/*` (≈18 scratch scripts), `backtest/open15_rolling/`, `backtest/inhouse_scanner/`, `backtest/news_event_study/*`, `.claude/launch.json`, and **3 database backups** (`db/openalgo.db.bak.20260714_175722`, `.20260805_222414`, `.20260808_210110` — the openalgo.db is 96 MB each, worth pruning).

**Stale branches:** 40+ local branches sit 1–4 commits "ahead" of `origin/dev` (`feat/305-reference-data-contract` at 4, `chore/github-issue-templates` and `feat/112-schema-migration` at 3, the rest 1–2). These are almost certainly squash-merged feature branches whose original commits no longer match — housekeeping, not risk. A `git branch --merged`-style sweep would clear most of them.

---

## 5. OpenAlgo health

**OpenAlgo restarted this morning at 09:00:49 and is live.** Today's log is being written as of 09:03:55.

| Signal | Value | Read |
|---|---|---|
| `log/openalgo_2026-08-14.log` | 09:03:55 today | ✅ live |
| `log/errors.jsonl` | 09:02:37 today | ✅ live |
| `db/openalgo.db` | 09:03:54 today | ✅ live |
| `db/sandbox.db` | 09:02:04 today | ✅ live |
| `db/historify.duckdb` | **13 Aug 15:44** | ⚠️ mtime stale, but the **boot convergence resettle is running right now** (09:02:00: *"scanner daily-D resettle: overwrite re-fetch 2026-08-12..2026-08-14, 216 symbols"*, job `40dcdf58`). DuckDB flushes lazily — expect the mtime to move shortly. |

**Errors — last 4 h: 4** (all in the boot window, all explained):

| Count | Logger | Message |
|---|---|---|
| 1 | `services.sector_follow_service` | M&M rehydrate — **self-resolved 27s later** |
| 3 | `zerodha_websocket` / `websocket` | `ping/pong timed out` → disconnect at 09:02:37 (**item 1 above**) |

**Errors — last 24 h: 93.** Breakdown, and what each one is:

| Count | Source | Read |
|---|---|---|
| 43 | `open15_breakout_service` | Yesterday's live session. Of these: **12 × "rolling watch-list re-rank failed"** (the #529 rolling additions were degraded most of the day), **8 × "tick capture failed"**, **4 × live OI rejections** (UNOMINDA, KALYANKJIL, ADANIENSOL, BDL — the exact failure #595 shipped to prevent, so those predate the fix landing), 2 × "oi filter raised — OI check skipped", 2 × liquidity-snapshot raise, 1 × *"3 watched symbols have NO NFO option contracts — SCANNER_SYMBOLS is stale: EXIDEIND, NUVAMA, SAMMAANCAP"*. The `AAA`/`CCC` "IP not allowed" rejections in the same window are **pytest fixtures, not real orders** — ignore. |
| 18 | `telegram.ext.Updater` | Polling exceptions; plus 2 × `telegram_bot_service` "Event loop is closed" broadcast failures. Telegram delivery is degraded. |
| 22 | websocket stack | 6 × `getaddrinfo failed` (network blip) + ping/pong timeouts |
| 1 | `scanner_smoke_check_service` | **09:18 smoke check FAILED yesterday** — `scanner_universe_1m` and `_D` both stale. |
| 1 | `scanner_dry_tripwire_service` | **CRIT at 14:33 — last in-house scan hit was 10:25.** The scanner went dry for ~4 hours yesterday afternoon. |
| 1 | `journal_reflection_service` | "nightly run crashed" — the known bridge-on-:5001-is-down failure. |
| 2 | `database.auth_db` | transient "database is locked" |

**Runtime overrides: none active.** The three `pause` rows (`futures_follow_cap50`, `intraday_pullback_top2`, `sector_follow_cap5_vol`) all expired **2026-08-12 10:00** — leftovers from the 12 Aug dead-token morning.

**Strategy modes (unchanged):**

| Strategy | Mode |
|---|---|
| `open15_vol_breakout` | **live** (real money, since 2026-07-24) |
| `sector_follow_cap5_vol` | sandbox |
| `futures_follow_cap50` | sandbox |
| `simplified_engine` | sandbox |

**Broker session:** `auth` rows present and un-revoked for `dheeraj.sonawane`, `admin`, `acct:1`. The boot log shows a `broker_session_refreshed` at 09:01:28 and `aggregator_seeder: seeded 229/229 symbols, 108179 bars, 0 errors` at 09:02:58 — **the feed came up fully**, which is what makes the 09:02:37 WS drop worth a second look rather than a shrug.

**Yesterday's open15 result (live):** 8 rows — 1 real fill (**ASHOKLEY +₹2,000**), 4 rejected (3 papered, 1 not), 3 shadow shorts. Net real: **+₹2,000**.

---

## 6. Today's schedule (Friday, trading day)

| IST | Event |
|---|---|
| 09:15 | Market open |
| 09:16 | `scanner_preentry_refresh` |
| 09:18 | `scanner_smoke_check` (min coverage 0.50) — **watch this one, it failed yesterday** |
| 09:10→09:29 | open15_vol_breakout arm → entry window → 09:30 exit |
| 15:05 / 15:10 | sector_follow_cap5_vol entry / exit (post-CAS timing) |
| 15:18 / 15:20 / 15:25 / 15:28 | futures_follow_cap50 smoke / entry / exit / watchdog |
| 15:30 | EOD summaries |
| 15:45 | `scanner_comparison_eod` |
| 16:00 | scanner history refresh |
| 17:00 | Backfill convergence loop ends |
| 17:15 | `postmarket_review` |

---

## ⚠️ Telegram

**Not delivered.** `api.telegram.org` does not resolve from the Cowork sandbox (`[Errno -3] Temporary failure in name resolution`) — same block the 13 Aug standup and morning-status runs hit. Please read this journal in Cowork directly, or allowlist `api.telegram.org` if you want phone alerts from scheduled runs. Separately, the 18 `telegram.ext.Updater` polling errors suggest the on-host bot is also unhealthy — worth checking independently of the sandbox block.

*Read-only run — no DBs written, no git operations, no commits.*
