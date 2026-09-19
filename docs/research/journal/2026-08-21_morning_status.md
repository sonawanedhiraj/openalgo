# Morning Status — Friday, 2026-08-21 (08:25 IST)

**Headline for Dheeraj:** Broker session re-logged in cleanly at 08:23 and all 229 scanner symbols are seeded, but the two backfill-convergence daemon threads went stale for ~4 hours yesterday evening and need a look before the 15:02 pre-entry refresh depends on them.

**Action needed: YES (1 item).** Stuck dispatch tasks: 0. Un-FF'd branches: 0. Errors last 4h: 19 (all pre-relogin WS 403 noise, benign).

---

## 🔴 Stuck / Action Required

### 1. Backfill convergence threads went STALE yesterday evening (P1)

`thread_registry` logged a rising stale-heartbeat warning every ~30 min from 17:09 to 19:40 on 2026-08-20:

| Thread | Owner | Worst silence |
| --- | --- | --- |
| `Scanner backfill convergence` (ScannerBackfillPeriodic) | `services/scanner_backfill_scheduler.py` | 14,482 s (4h 01m) |
| `sector_follow feed convergence` (SectorFollowBackfillPeriodic) | `services/sector_follow_backfill_scheduler.py` | 12,662 s (3h 31m) |

These are exactly the "beat once, then went silent" shape the #539 thread registry was built to catch — so the alert worked; what's missing is the diagnosis. Both threads re-initialised fine at 08:22 today and the scanner boot convergence started at 08:23, so this is not currently blocking. But the periodic loop is what keeps the 1m/`D` feeds converged in the 15:30–17:00 window, and a wedged loop is the upstream shape of the 2026-06-11/12 tick-starvation class.

Worth checking: whether the loop was genuinely wedged (socket/DuckDB lock) or simply backed off after reporting fresh — the current heartbeat only beats per tick, so a legitimate "everything fresh, sleep until tomorrow" back-off may be indistinguishable from a hang. If it's the latter, the registry is crying wolf nightly and should be taught the difference.

### 2. Two issues sat in DRY RUN and were never filed (P2)

Yesterday's 17:15 postmarket review ran clean (`violations=1, triage=ok, telegram=True`) and the investigating agent proposed two issues, but `POSTMARKET_FILING_MODE=dry_run` means neither exists on GitHub:

- *"Post-market data_health reads the latest row regardless of date, hiding intraday"*
- *"Stop the 09:18 smoke check from consuming a pre-open convergence verdict that ca…"* (truncated in log)

Both look real and both touch the freshness machinery implicated in item 1. Your call whether to file them by hand or flip filing mode.

### 3. No morning-status journal for 2026-08-19 or 2026-08-20

The journal directory jumps from `2026-08-18_morning_status.md` to today. Either this scheduled task didn't fire on Wed/Thu, or it fired and failed to write. Worth a glance at the scheduled-task session list if you care about the gap.

---

## 🟢 Dispatch tasks complete in last 24h

Nothing substantive. The 30 most recent sessions are almost entirely `Fno scan cycle` runs, and every one I sampled exited immediately on its own market-hours gate:

- `local_81fe1cd0` — *"Outside market hours — skipping. It's 08:23 IST, before the 09:30 gate."* (today, DONE)
- `local_94da4369` — *"Outside market hours — skipping. Current time is 16:47 IST (after the 16:30 cutoff)."* (DONE)

No `[result] error`, no API errors, no AskUserQuestion stalls anywhere in the sample. **0 STUCK, 0 ERRORED.**

## ⏳ Dispatch tasks running

- `local_ba2e1e09` **"Weekday trading standup"** — running, 3 assistant turns, latest message *"I'll run the standup checks now."* Started minutes ago alongside this task. PROGRESSING, not stuck.

---

## Git state

**Un-FF'd commits: none.** `git log origin/dev..dev` is empty — local `dev` is exactly at origin.

**Branches:** 25 stale local branches (16 `claude/*`, 6 `chore/*`, 3 `docs/*`). None carry unpushed work relative to dev, but the `claude/*` set is pure accumulated debris from worktree runs — a `git branch -d` sweep is overdue.

**Working tree — dirty:**

*Tracked modifications (2):*
- `.gitignore`
- `strategies/simplified_engine/LEARNINGS.md`

*Untracked — research/backtest WIP (~24 paths), notably:*
- `backtest/options_open15/` — 15 new scripts (`july_*.py`, `bs.py`, `iv_history.py`, `multiplier_study.py`, `pipeline.py`, `validate_formula.py`) plus two data artifacts (`iv_history.parquet`, `july_fetch_cache.json`)
- `backtest/open15_missed_days/`, `backtest/open15_rolling/`, `backtest/inhouse_scanner/`
- `backtest/news_event_study/` — 3 scripts
- `.claude/launch.json`, `audit/open15_replay_removal_backup_20260817_165557.json`
- `db/openalgo.db.bak.20260714_175722` — a DB backup showing as untracked in `db/`

The options_open15 work is the biggest uncommitted block. Per CLAUDE.md, any backtest round it belongs to also needs a `strategies/STRATEGY_REGISTRY.md` entry committed direct-to-dev.

**Recent origin/dev (last 5):**

```
71e836bdd [#651] refactor(open15): delete the four correctness flags — a guarantee is not a preference (#653)
8fd5e8cda [#648] feat(scanner): split the universe into watched vs tradeable (#650)
d4dc837de [#647] fix(open15): not in F&O = not watched — a fact, not a gate (#649)
d444a8726 [#645] fix(open15): "effective today" describes the ARM, not the process boot (#646)
9a8438140 [#643] fix(open15): a raise in _enter erased the trigger; spend the residual cash (#644)
```

---

## OpenAlgo health

**Verdict: healthy right now.** The app is up and this morning's login cycle completed normally.

| Artifact | Last write | Read |
| --- | --- | --- |
| `log/openalgo_2026-08-21.log` | 08:24:53 | Live, actively writing |
| `log/errors.jsonl` | 08:24:12 | Live |
| `db/openalgo.db` | 08:23:21 | Live |
| `db/sandbox.db` | 08:24:47 | Live |
| `db/historify.duckdb` | 2026-08-20 14:11 | ⚠️ **looks** stale — but `historify.duckdb.wal` is at 08:25:23 today. DuckDB is writing to the WAL; the main file only rolls at checkpoint. **Not a problem.** |

**Errors last 4 hours: 19**, and every one is the same benign cluster:

| Count | Logger |
| --- | --- |
| 5 | `broker.zerodha.streaming.zerodha_websocket` |
| 4 | `services.websocket_client` |
| 3 | `zerodha_websocket` |
| 3 | `websocket` |
| 2 | `services.websocket_service` |
| 2 | `connection_pool_zerodha` |

All timestamped ~02:52 IST — `Handshake status 403 Forbidden` / `Auth/token failure detected — will not retry` — i.e. the expected daily Zerodha token expiry at ~03:00. This is the normal shape, not an incident.

**This morning's boot sequence looks right:**
- 08:22:13 — sector_follow + scanner backfill convergence initialised; `scanner_preentry_refresh` registered for 09:16
- 08:23:09→08:23:22 — master contract atomic swap committed, **116,430 records**
- 08:24:12 — one `ping/pong timed out` on the Zerodha WS, auto-reconnected in 3s, re-subscribed 229 tokens across 2 batches. Self-healed; no action.
- 08:24:37 — `aggregator_seeder: seeded 229/229 symbols, 108,219 bars (0 empty, 0 errors) in 72.8s` ✅
- 08:24:47 — python_strategy post-login restore: 0 pending

**Could not read:** every query against `db/openalgo.db` from the sandbox returned `disk I/O error` (the live app holds it with a 39 MB WAL). So `postmarket_review`, `open15_trades`, `strategy_mode`, `strategy_runtime_override` and `data_health_check` are **unverified from the DB** — everything below is reconstructed from logs instead. Flagging honestly rather than guessing.

---

## Yesterday (2026-08-20) — what actually happened

**open15_vol_breakout:** `selected 11, entered 3, filled 2, paper 1, rolling_added 7`.

| Symbol | Source | Contract | Outcome |
| --- | --- | --- | --- |
| PFC | rolling | `PFC25AUG26370PE` | Filled. Reconciled: entry 3.71 / exit 4.90 × 15,600 → gross **+18,564**, charges 652.07 → **net +17,912** |
| HINDALCO | rolling | `HINDALCO25AUG261020PE` | Filled. Reconciled: entry 10.89 / exit 10.04 × 5,600 → gross **−4,795**, charges 568.79 → **net −5,329** |
| BANDHANBNK | rolling | `BANDHANBNK25AUG26175CE` | **Rejected → paper.** *"MIS orders are not allowed for stocks in ban period. Please use NRML product type instead."* Slot released. Paper net +16,887 — **not real money.** |

**Real net ≈ +₹12,583.** Both fills were `rolling` watch-list adds, not the 09:16 seed — worth noting for the #528/#529 rolling-cohort question. Fill reconcile ran and corrected both rows from quote-derived to fill-true, so the #555 machinery is working.

The BANDHANBNK rejection is a *new* reason shape — F&O ban period, not the #548 static-IP 403 or the #626 funds rejection. The paper path handled it correctly (slot released, no money moved), but if ban-period names keep getting selected they'll keep burning selection slots. Possibly worth a pre-entry ban-list check in the same spirit as #647's not-in-F&O exclusion.

**sector_follow_cap5_vol:** 0 entries, 0 exits (mode=sandbox). Pre-entry refresh at 15:02 verified 6 stale indices back to fresh, 0 still stale.

**futures_follow_cap50:** 0 lots placed, 0 skipped on cap, 0 LLM vetoes (mode=sandbox). 0 exits. EOD summary emitted.

Both zero-signal days look genuine, not degraded — the pre-entry convergence reported clean.

---

## Today's schedule (Friday — trading day)

| IST | Event |
| --- | --- |
| 09:10 | open15 arm (universe seed, funds clamp, F&O filter) |
| 09:15 | **Market open** |
| 09:16 | `scanner_preentry_refresh` (registered ✅) |
| 09:18 | scanner smoke check |
| 09:29 / 09:30 | open15 entry cutoff / hard flatten |
| 15:02 | sector_follow pre-entry refresh |
| 15:03 | sector_follow smoke check |
| 15:05 / 15:10 | sector_follow entry / T+1 exit |
| 15:18 | futures_follow smoke check |
| 15:20 | futures_follow entry evaluation |
| 15:25 / 15:28 | futures_follow T+1 exit / EOD watchdog |
| 15:30 | EOD summaries |
| 15:45 | `scanner_comparison_eod` |
| 15:30–17:00 | backfill convergence periodic window — **watch this one given item 1** |
| 17:15 | `postmarket_review` |

Note: **25-AUG-26 is the NIFTY monthly expiry** (last Tuesday), so futures_follow's resolver should already be skipping the near contract for any T+1 hold placed on Monday. Nothing to do today.

---

## ⚠️ Telegram

**Blocked.** `api.telegram.org` returned HTTP 000 from the Cowork sandbox — same allowlist restriction the prior standup task hit. No alert was sent. Please read this journal directly in the Cowork app, or allowlist `api.telegram.org` for the sandbox if you want push delivery.

---

*Read-only run. No DB writes, no git operations, no commits.*
