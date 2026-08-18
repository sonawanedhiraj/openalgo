# Morning Status — Monday 2026-08-17 (08:00 IST)

**Dheeraj — one sentence:** OpenAlgo restarted cleanly at 08:56 and is mid pre-market backfill with zero errors today, no dispatch tasks are stuck, and the one thing that needs you is a **NIFTY futures lot that has been open in sandbox for 18 days on a T+1 strategy** and whose contract expires in 8 days.

- **Stuck dispatch tasks:** 0
- **Un-FF'd branches:** 0 (local `dev` == `origin/dev`)
- **Errors in last 4h:** 0 (last error logged Friday 16:00)
- **Action needed today:** YES — one item (below)

---

## 1. 🔴 Stuck / Action Required

### A. `futures_follow_cap50` — 1 NIFTY lot open since 2026-07-30 (18 calendar days) on a **T+1** strategy

This is the same P0 that `postmarket_review` has fired on **every trading day since at least 2026-08-11**, and I confirmed it against the journal rather than trusting the contract alone:

```
futures_follow_trades lot balance (all time):
  BUY  : 16 lots across 16 rows
  SELL : 15 lots across 11 rows
  → 1 lot unmatched
Last journal row of any kind: 2026-07-31 09:55
```

Contract detail:

- Open lot entered **2026-07-30**, symbol **`NIFTY25AUG26FUT`**, product **NRML**, `mode='sandbox'`, ~₹2.5L margin.
- `NIFTY25AUG26FUT` expires **Tuesday 2026-08-25** — 8 days out. NRML has no MIS auto-square-off underneath it, so nothing will clear this on its own before expiry.
- `futures_follow_exit` and `futures_follow_eod_watchdog` both ran `ok` on Friday per `job_run`, yet produced no exit. The jobs are firing; the position is not being seen or acted on.
- The strategy has journaled **no entries and no exits since 2026-07-31** (17 days). `futures_follow_entry` ran `ok` Friday, so it is evaluating and finding zero signals — plausible on its own, but combined with the stranded lot it means nothing has exercised the exit path in over two weeks.

**Why it matters despite being sandbox:** this is the exact failure *shape* of issue #497 (strategy wrote to one book and read the other, so `run_exit` squared off nothing for four days). It is currently costing nothing real, but it is the live rehearsal for the same bug on a real book — and it goes stale-and-untestable once the contract expires on the 25th.

**Suggested first look:** `rehydrate_from_positionbook()` / the `mode_key='futures_follow_cap50'` routing on the position read, then whether the FIFO carry is a genuine open lot or an unmatched-journal artifact.

### B. Two known-broken background jobs, both quiet failures

Not urgent, but they have been failing unnoticed and neither alerts:

1. **`journal_reflection` crashed again Friday 16:00** (`reflection: nightly run crashed`). Same root cause as documented in CLAUDE.md — it POSTs to the Cowork bridge on :5001, which is normally down.
2. **Telegram broadcast failed 3× on Friday** (14:15 `httpx.ConnectError`, 15:30 and 15:35 `RuntimeError('Event loop is closed')`). The 15:30 and 15:35 failures are the **EOD summary and mirror summary** — i.e. Friday's end-of-day Telegram never reached you.

---

## 2. 🟢 Dispatch tasks complete in last 24h

None. The last 24h spans Sunday, so no dispatch work ran. The most recent sessions are all Friday's `fno-scan-cycle` runs, which terminated correctly:

| Session | Verdict | Latest message |
|---|---|---|
| `local_76b4b46e` "Fno scan cycle" | **DONE** | "Outside market hours — skipping. Current IST time is 16:47 (Friday)" |
| `local_0a50c760` "Fno scan cycle" | **DONE** | "Outside market hours — skipping. Current IST time is 16:32" |
| ~26 further "Fno scan cycle" sessions | **DONE / idle** | Same clean-skip shape |

No `[result] error`, no "API Error", no "stream timeout", no AskUserQuestion blockers anywhere in the inventory.

## 3. ⏳ Dispatch tasks running

| Session | Status | Turns | Verdict |
|---|---|---|---|
| `local_c1bf4b44` "Weekday trading standup" | running | 10 (was 8 at first check) | **PROGRESSING** |

That is the 08:45 `weekday-trading-standup` scheduled task running alongside this one — turn count climbed between my two checks, so it is working, not wedged. Nothing to do.

**Scheduled-task registry health:** `fno-scan-cycle` (enabled, next run 09:16 IST), `weekday-trading-standup` (enabled), `morning-status-report` (enabled, this run). `stuck-task-watchdog` remains **disabled** since 2026-06-20 — worth knowing, since it is the thing that would otherwise catch a wedged session between these reports.

---

## 4. Git state

**Un-FF'd commits: none.** `git log origin/dev..dev` is empty — local `dev` is exactly `origin/dev`.

**Branches:** 29 total. 10 `chore/*`, 14 `claude/*`, 5 `docs/*`. None carry un-pushed commits on `dev`; they are stale feature/agent branches, not pending work.

**Working tree: dirty (2 modified, ~24 untracked).** OpenAlgo logged the boot warning about this at 08:56.

*Modified (2) — these are the ones that represent real uncommitted work:*

- `.gitignore`
- `strategies/simplified_engine/LEARNINGS.md`

*Untracked — mostly research scratch, safe to leave:*

- `backtest/options_open15/` — 14 scripts (`july_*`, `bs.py`, `pipeline.py`, `iv_history.py`, …) plus `data/iv_history.parquet` and `data/july_fetch_cache.json`
- `backtest/open15_rolling/`, `backtest/inhouse_scanner/`
- `backtest/news_event_study/` — 3 scripts
- `.claude/launch.json`
- **3 DB backups** — `db/openalgo.db.bak.20260714_175722`, `.20260805_222414`, `.20260808_210110`. These are ~100 MB each and untracked; worth pruning the two oldest if disk matters.

**Recent `origin/dev` history (last 5):**

```
5dfd1c4ba [#597] fix(open15): late-boot arm must not clobber a traded day's persisted day_log (#598)
7589601d7 [#595] feat(open15): seed/rolling broker-OI (>=500 lots) watch-list filter (#596)
42db78217 [#593] fix(open15): look up broker_pnl by contract symbol for option rows (#594)
293804dca docs(parameter-log): OPEN15_ATM_LOT_COST_ENABLED + OPEN15_COVERAGE_TARGET_PCT (#591)
400b51dfd feat(open15): ATM lot-cost coverage ladder on /open15_vol_breakout/logs (#592)
```

All recent work is `open15_vol_breakout`. Nothing in flight on `futures_follow_cap50` — consistent with item 1A being unattended.

---

## 5. OpenAlgo health

**Verdict: healthy and freshly restarted.** OpenAlgo came up at **08:56:37 IST today** and is actively running the daily-`D` convergence backfill as of 09:00 (upserting 2 records per symbol, `2026-08-13 → 2026-08-17`, currently in the E's — `EXIDEIND`).

| Signal | Timestamp | Read |
|---|---|---|
| `log/openalgo_2026-08-17.log` | 08:59:53 today | Live, 74 KB since boot |
| `log/errors.jsonl` | 08:56:02 today | mtime = the boot-time truncation, **not** a new error |
| `db/health.db` | 08:59:44 today | Watchdog ticking |
| `db/openalgo.db` | 08:58:24 today | Active |
| `db/sandbox.db` | 08:57:45 today | Active |
| `db/historify.duckdb` | 2026-08-14 14:01 (at check time) | Being written now by the 09:00 backfill |

**Errors:**

- **Today: 0.** **Last 4h: 0.** The oldest entry in `errors.jsonl` is 2026-08-12 09:17 and the newest is **2026-08-14 16:00** — nothing since Friday.
- **Friday 2026-08-14 total: 10 errors**, by logger: `telegram_bot_service` 3, and one each from `sector_follow_service`, `zerodha_websocket` (×2 loggers), `websocket`, `open15_breakout_service`, `scanner_smoke_check_service`, `journal_reflection_service`.
- Friday's `scanner_smoke_check_service` FAILED at 09:18 (`scanner_universe_1m stale; scanner_universe_D stale`) — **but it self-healed**: `data_health_check` shows `scanner_universe_1m` and `scanner_universe_D` both `overall_ok=1` with empty stale lists by 09:36, 09:52 and 10:15. The straggler recheck did its job.

**Runtime overrides:** three `pause` rows exist (`futures_follow_cap50`, `intraday_pullback_top2`, `sector_follow_cap5_vol`) but **all expired 2026-08-12 10:00** — none are active. Nothing is held this morning.

**Strategy modes:**

| Strategy | Mode |
|---|---|
| `open15_vol_breakout` | **live** (real money) |
| `simplified_engine` | sandbox |
| `sector_follow_cap5_vol` | sandbox |
| `futures_follow_cap50` | sandbox |

**`open15_vol_breakout` (the live one) recent P&L**, net, real fills only — buckets kept separate per the #555 convention:

| Date | Real net | n | Paper net | Shadow net |
|---|---|---|---|---|
| 2026-08-14 | **−₹4,445.82** | 1 (CUMMINSIND L) | — | +₹8,087 |
| 2026-08-13 | +₹1,438.16 | 1 (ASHOKLEY L) | −₹1,055.42 (3) | −₹6,010 |
| 2026-08-11 | — | 0 | — | +₹1,804 |
| 2026-08-06 | +₹5,334.34 | 2 | — | — |
| 2026-08-05 | — | 0 | +₹1,383.81 (3) | — |

**All-time real: 4 fills, +₹2,326.68 net.** Friday's single real trade was the worst day so far; the shadow (excluded-side) cohort was strongly positive the same day, which is the #581 measurement doing exactly what it was deployed to do. Small sample — not a signal to act on yet.

**Scanner vs Chartink, Friday:** BUY parity 1.00 (3/3), SELL Jaccard 0.56 (9 Chartink vs 5 in-house; missed `HINDZINC`, `HYUNDAI`, `IOC`, `SAMMAANCAP`). Recall 1.00 — in-house is a strict subset, tighter, not diverging.

**`job_run` Friday:** every job reported `ok`. `intraday_pullback_eval` and `scanner_dry_tripwire` fired 83× each; all singleton jobs (open15 arm/exit/retry/summary, futures_follow ×5, postmarket_review, multi_account ×3, option_liquidity_eod) fired once and succeeded.

---

## 6. Today's schedule (Monday, trading day)

| IST | What |
|---|---|
| 09:15 | Market open |
| 09:16 | `fno-scan-cycle` first run (then every 15 min to 16:59) |
| 09:18 | Scanner smoke check |
| 15:18 | `sector_follow_cap5_vol` smoke check |
| 15:20 | `sector_follow` + `futures_follow_cap50` entry evaluation |
| 15:25 | Exits |
| 15:30 | EOD summary |
| 15:45 | `scanner_comparison_eod` |
| 16:00 | `scanner_history_refresh` (+ `journal_reflection`, which will likely crash again) |
| 17:15 | `postmarket_review` |

Note the schedule above is the reminder set carried in this task. Per CLAUDE.md, `sector_follow_cap5_vol`'s own chain moved to **15:02/15:03/15:05/15:10** on 2026-08-03 for the NSE Closing Auction Session — if the 15:20 line matters to you today, that is the one to double-check against `resolve_schedule()`.

---

## 7. Delivery

⚠️ **Telegram: NOT sent — please open this journal directly in the Cowork app.**

Exact failure, so it is not mistaken for a bot problem:

```
socket.gethostbyname('api.telegram.org')
→ gaierror [Errno -3] Temporary failure in name resolution
```

The Cowork sandbox has **no DNS resolution for `api.telegram.org`**, so the send fails before any HTTP request is made. This confirms the same finding the standup task hit. It is a sandbox network policy, not a config fault:

- `bot_config.is_active = 1` — the bot is enabled
- `telegram_chat_ids = 1345069591` — your chat ID is configured
- `bot_token` is present and Fernet-encrypted (would also need `APP_KEY` to decrypt, which I did not attempt — read-only run)

**To fix delivery for future runs:** allowlist `api.telegram.org` for the Cowork sandbox. Until then these reports are journal-only.

Separately — and worth not conflating with the above — **OpenAlgo's own Telegram sends are also failing intermittently** from the host (3 errors Friday, including the 15:30 EOD summary). That one is a real bug on your machine, not a sandbox limitation. See §1B.

---

*Read-only run. No DB writes, no git operations, no code edits.*
