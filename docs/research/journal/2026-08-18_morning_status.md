# Morning Status — Tuesday, 2026-08-18 (08:25 IST)

**Dheeraj — one sentence:** Nothing is stuck or running, all recent branches are merged, but `futures_follow_cap50` has been flagged **P0 for four straight days** with a NIFTY lot open since 2026-07-30 (18 days) on a T+1 strategy, and OpenAlgo restarted **13 times** yesterday.

> ⚠️ **Telegram blocked** — `api.telegram.org` returns `403 Forbidden` (Cowork sandbox allowlist). No alert was sent. Please read this journal directly in Cowork, or allowlist `api.telegram.org`.

---

## 1. 🔴 Stuck / Action Required

### A. P0 — `futures_follow_cap50` carry has not exited for 18 days (recurring 4 days)

`postmarket_review` has fired the same contract on **08-12, 08-13, 08-14 and 08-17**:

```
P0  futures_follow_cap50  t1_exit_for_carry
    1 lot open, oldest entry 2026-07-30 (18d), but 0 exits today
```

This is the **#497 shape**: a T+1 strategy holding a lot for 18 sessions means `run_exit` is squaring off nothing. The `futures_follow_exit` job *did* fire `ok` yesterday — so the job runs and finds no position to close, which points at the rehydrate/position-book read rather than the scheduler.

Mitigating: `strategy_mode` for `futures_follow_cap50` is **`sandbox`**, so this is virtual-book exposure, not real money. It is still a live risk-control failure that would bite in live mode (NRML has no MIS auto-square-off underneath it).

**Action:** worth one debugging session on `futures_follow_service.rehydrate` / the `get_positionbook(mode_key=...)` routing.

### B. `open15_vol_breakout` missed its arm on 2026-08-17 — and it is the one **LIVE** strategy

`job_run` for 08-17 shows `open15_exit`, `open15_exit_retry`, `open15_summary` — but **no `open15_arm`**. Consistent with the tracked commit `4de412877` ("round 62 — 1m reconstruction of the 2026-08-12/08-17 missed sessions").

`strategy_mode` says:

| strategy | mode | set by | when |
| --- | --- | --- | --- |
| `open15_vol_breakout` | **live** | ui:dheeraj.sonawane | 2026-07-24 |
| `simplified_engine` | sandbox | migration | 2026-06-12 |
| `sector_follow_cap5_vol` | sandbox | ui:dheeraj.sonawane | 2026-08-07 |
| `futures_follow_cap50` | sandbox | harness | 2026-06-24 |

open15 is the only strategy routing real money, and it is the one that missed two sessions this month. The likely cause is boot timing — see §4.

### C. OpenAlgo restarted 13× yesterday; the day's log hit 108 MB

- `log/openalgo_2026-08-17.log` = **108 MB** (vs 2.4 MB on 08-13, 6.7 MB on 08-14).
- `"Thread watchdog started"` appears **13 times** on 08-17, **1 time** today.
- Restart churn is the most plausible explanation for both the missed open15 arm and the error burst below.

### D. 745 `strategies_dashboard_api` errors in 24h

All the same shape — `Failed to query strategy_llm_config for <strategy>`:

| count | strategy |
| --- | --- |
| 174 | simplified_engine |
| 163 | intraday_pullback_top2 |
| 143 | open15_vol_breakout |
| 134 | futures_follow_cap50 |
| 119 | sector_follow_cap5_vol |

Clustered **08-17 19:00 (540) and 20:00 (135)** — i.e. during the restart churn, not during market hours. Underlying cause in the tracebacks is `sqlite3.OperationalError: database is locked` / `no such table`. Reads as boot-time DB contention rather than a schema problem, but 745 is too many to leave unexplained.

---

## 2. 🟢 Dispatch tasks complete in last 24h

All 30 sessions returned by `list_sessions` are `fno-scan-cycle` runs, and **all are `idle`** — none stuck, none errored, none awaiting input.

The three most recent, sampled:

| session | verdict | last message |
| --- | --- | --- |
| `local_a41e5e40` | **DONE** | "Outside market hours (16:47 IST) — skipping." |
| `local_a6c4e78e` | **DONE** | "Outside market hours — skipping. (16:32 Monday)" |
| `local_65ad30e5` | **DONE** | Full EOD summary for 08-17 |

The 08-17 EOD summary from `local_65ad30e5` is worth re-reading — it reports the **simplified engine's worst logged day**: 11 completed trades, 2W/9L (18.2% win rate), **net ≈ −₹3,894**, with the SHORT book 0/7. It also flags an anomaly already filed to `audit/proposed_fixes.jsonl`: **11 entries fired against `max_trades_per_day=6`** while `completed_trades_today` stayed at 0 — the daily cap was effectively unenforced. Sandbox-only, but a genuine risk-control gap.

## 3. ⏳ Dispatch tasks running

**None.** No session is in a running state.

Scheduled-task ledger:

| task | enabled | last run | next run |
| --- | --- | --- | --- |
| `fno-scan-cycle` | ✅ | 08-17 16:47 IST | **today 09:01 IST** |
| `weekday-trading-standup` | ✅ | 08-17 08:58 IST | today 08:47 IST |
| `morning-status-report` (this) | ✅ | today 08:24 IST | 08-19 |
| `stuck-task-watchdog` | ❌ disabled | 2026-06-20 | — |
| 4 others | ❌ deprecated/retired | — | — |

---

## 4. Git state

**Clean and fully synced — nothing pending on your side.**

- `origin/dev..dev` = **0 commits**. `dev..origin/dev` = **0**. Local `dev` is exactly `origin/dev`.
- Last fetch: today 08:23.
- Every August feature branch has **landed**: #593, #595, #597, #600, #602, #604, #606, #608, #612, #613, #617, #620, #622, #624 all confirmed merged into `origin/dev`.
- **One genuinely un-merged branch:** `docs/610-pre-ship-checklist` (1 commit, 08-17, 6 behind). Docs only.
- ~220 other local branches are pre-squash-merge leftovers — they show as "not merged" only because `dev` squash-merges. Safe to prune whenever you want the noise gone.

**Working tree — 2 tracked modifications, 150 untracked:**

| file | change |
| --- | --- |
| `strategies/simplified_engine/LEARNINGS.md` | +1054 lines (uncommitted) |
| `.gitignore` | +2 lines |

The LEARNINGS.md addition is substantial and uncommitted — per CLAUDE.md, strategy-learning updates go **direct to dev**. Worth committing before it grows further.

Untracked (150) is mostly research scratch that has never been committed: `backtest/options_open15/*` (~18 scripts), `backtest/open15_rolling/`, `backtest/open15_missed_days/`, `backtest/inhouse_scanner/`, plus `db/openalgo.db.bak.20260714_175722` and `audit/open15_replay_removal_backup_20260817_165557.json`.

**Recent `origin/dev` history:**

```
9c32809bd [#624] fix(intraday_pullback): resume replay must rebuild state, not re-decide the day
78a769e3a [#622] fix(open15): restore renderRejected — the replay removal deleted it
7cc6bf792 [#620] chore(open15): remove the replay feature
ce384b7cd [#617] fix(open15): replay config fallback no longer KeyErrors on a never-armed day
04a22c32f [#612][#615] fix(open15): clobbered-log days render their trades
```

---

## 5. OpenAlgo health

| signal | value | read |
| --- | --- | --- |
| App boot today | **08:23:49 IST** | 🟢 running, single clean boot |
| `log/openalgo_2026-08-18.log` | written 08:26:31 | 🟢 live |
| `log/errors.jsonl` | written 08:26:53 | 🟢 live |
| `db/openalgo.db` | 08:24:58 today | 🟢 |
| `db/sandbox.db` | 08:25:52 today | 🟢 |
| **`db/historify.duckdb`** | **08-17 15:28:40** | ⚠️ **no write today** |
| Boots on 08-17 | **13** | 🔴 churn |
| 08-17 log size | **108 MB** | 🔴 abnormal |

**historify not written today is expected-but-worth-confirming.** The boot convergence check waits for a broker session, and the Zerodha token expires ~03:00 IST daily. Until you complete the Zerodha login, no backfill runs. The `database is locked` errors on `auth_db` at 08:24–08:26 are boot contention, not proof of a bad session — but **verify the Zerodha login before 09:15**.

**Errors, last 4 hours (all post-boot, 08:23–08:27):**

| count | logger |
| --- | --- |
| 13 | `database.auth_db` (10 × "database is locked" on auth token) |
| 4 | `blueprints.strategies_dashboard_api` |
| 2 | `app` (500 Internal Server Error) |
| 2 | `blueprints.python_strategy` |
| 1 each | `client.browser`, `blueprints.auth`, `services.broker_accounts_service` |

Note `errors.jsonl` was truncated at boot (its first entry is 08-17 17:51), so pre-restart error history for 08-17 is gone — the 24h counts above are a floor, not a total.

**Scheduled jobs on 08-17: zero non-`ok` runs.** `sector_follow_*` (all 8), `futures_follow_*` (all 6), `postmarket_review`, `scanner_comparison_eod`, `scanner_history_refresh` all fired clean. `intraday_pullback_eval` ran 76×, `scanner_dry_tripwire` 78×. Data health rows for `scanner_universe_1m` and `_D` both `overall_ok=1` with zero stale symbols as of 08-17 15:05.

**No active runtime overrides** — the three `pause` rows (futures_follow, intraday_pullback, sector_follow) all expired 2026-08-12 10:00.

**open15 P&L by fill bucket** (buckets never summed, per #555):

| date | real | paper | shadow | sim |
| --- | --- | --- | --- | --- |
| 08-14 | 1 trade, **−₹4,000** | — | 1, +₹8,636.25 | — |
| 08-13 | 1 trade, **+₹2,000** | 3, +₹96.25 | 3, −₹4,425 | — |
| 08-11 | — | — | 2, +₹2,805 | 1, +₹6,930 |

Real money is **−₹2,000 over the two trading days that filled**. The shadow (short) cohort out-earned the real cohort on 08-14 by a wide margin — that's the #581 measurement doing its job, and it's starting to accumulate a signal.

---

## 6. Today's schedule (Tuesday — trading day)

| IST | event |
| --- | --- |
| **before 09:15** | ⚠️ **Zerodha login** — token expired ~03:00, nothing backfills without it |
| 09:01 | `fno-scan-cycle` first run |
| 09:10 | `open15_arm` — **watch this one**, it missed 08-12 and 08-17 |
| 09:15 | Market open |
| 09:29 / 09:30 | open15 entry cutoff / exit |
| 15:18 | `sector_follow_cap5_vol` smoke check |
| 15:20 | sector_follow + futures_follow_cap50 entry evaluation |
| 15:25 | Exits |
| 15:30 | EOD summary |
| 15:45 | `scanner_comparison_eod` |
| 16:00 | `scanner_history_refresh` |
| 17:15 | `postmarket_review` — expect the P0 to fire a 5th time unless §1A is fixed |

---

## Sources

- `mcp__session_info__list_sessions` / `read_transcript` (30 sessions)
- `mcp__scheduled-tasks__list_scheduled_tasks`
- `git log` / `git status` / `git branch --no-merged` on `C:\workspace\ai-trade-agent\openalgo`
- `log/errors.jsonl`, `log/openalgo_2026-08-1{7,8}.log` (read-only)
- `db/openalgo.db` (read-only, `mode=ro`): `strategy_mode`, `strategy_runtime_override`, `data_health_check`, `job_run`, `postmarket_review`, `open15_trades`, `auth`
