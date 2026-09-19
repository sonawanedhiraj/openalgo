# Morning Status — Wednesday, 16 September 2026

**Dheeraj — nothing is stuck and nothing is broken; OpenAlgo booted at 08:19 IST, auto-login healed the dead Zerodha token by 08:20:39, and the only thing to actually watch today is a 2-lot NIFTY futures position due for its T+1 exit at 15:25.**

Generated 08:0x IST · read-only inventory · scheduled task `morning-status-report`

---

## 🔴 Stuck / Action Required

**None.** No stuck dispatch task, no errored session, no un-pushed work on `dev`.

Two caveats about *this report*, not about your system:

1. **The Cowork Linux sandbox is down** — the September-8 Windows update stops it mounting your files (`Plan9 share "c" which is not mounted`). Three identical failures, so I stopped retrying. Everything below came from direct file reads instead. **Consequence:** I could not run `git`, so per-branch ahead/behind counts are unavailable (see Git state). This has been hitting the `fno-scan-cycle` runs too — every EOD summary yesterday noted it.
2. **Telegram could not be sent.** The bot token in `bot_config` is Fernet-encrypted and decrypting it needs `API_KEY_PEPPER` plus a Python runtime — both only reachable through the dead sandbox. ⚠️ **Please read this journal directly in Cowork.**

---

## 🟢 Dispatch tasks — last 24h

Six `Fno scan cycle` sessions ran yesterday afternoon/evening. **All six: DONE.** Zero stuck, zero errored, zero awaiting input.

| Fired (IST, 15 Sep) | Mode | Verdict |
|---|---|---|
| 15:17 | EOD Summary — the substantive run | DONE — wrote the LEARNINGS entry |
| 15:32 | EOD Summary | DONE — verified, skipped duplicate |
| 15:47 | EOD Summary | DONE — verified, skipped duplicate |
| 16:02 | EOD Summary | DONE — verified, skipped duplicate |
| 16:32 | Post-cutoff | DONE — `aborted_market_closed` (row 17830) |
| 16:47 | Post-cutoff | DONE — `aborted_market_closed` (row 17831) |

The no-duplicate rule worked correctly — four fires landed in the EOD window and only the first wrote to `LEARNINGS.md`.

**Yesterday's result (simplified_engine, sandbox): 2 trades, 2W/0L, net +₹568.05.**

| Symbol | Dir | Qty | Entry | Exit | Reason | Net |
|---|---|---|---|---|---|---|
| TORNTPHARM | SHORT | 20 | 4879.00 | 4855.00 | eod_watchdog | **+₹397.44** |
| LICI | SHORT | 254 | 392.45 | 391.45 | stop_loss (trail) | **+₹170.61** |

Worth noting: charge drag was only ~23% of gross, against the 63–68% that has been flipping recent scratch days red — and both winners were shorts, snapping the short-book loss streak.

Two recurring annoyances the sessions flagged, neither new: the **bridge on :5001 is down** (so step 6 auto-fix is skipped every cycle), and `completed_trades_today` read 1 against 2 real trades (`trade_journal` is authoritative, as documented).

## ⏳ Dispatch tasks running

**None.** No session has run since 16:47 IST yesterday (~15h quiet, as expected overnight).

**Scheduled tasks — all healthy:**

| Task | Schedule | Next run (IST) |
|---|---|---|
| `morning-status-report` | 08:07 weekdays | ran 08:20 today |
| `weekday-trading-standup` | 08:48 weekdays | **08:47 today — pending** |
| `fno-scan-cycle` | every 15 min, 09:00–16:59 weekdays | **09:01 today — first fire** |

Three tasks are deliberately disabled (`daily-trading-pipeline`, `scanner-vs-chartink-daily-comparison`, `stuck-task-watchdog`) and one-shots from June are spent. Nothing has silently stopped firing.

---

## Git state

**`dev` is exactly in sync with `origin/dev`** at `e64c27b40495ebdf061af579ba26a24f73fc2fb1`. Nothing local waiting to push on the branch that matters.

⚠️ **Per-branch un-FF'd counts are unavailable this run** — that needs `git log origin/X..X` and the shell is down. What I can say from the refs: **391 local branches** exist, including a long tail of `worktree-agent-*` leftovers from parallel agent runs. Worth a cleanup pass when you have a quiet moment, but it is housekeeping, not a blocker.

**Working tree is dirty** (captured by the boot dirty-check at 08:20:00):

- **2 modified tracked files:** `.gitignore`, `strategies/simplified_engine/LEARNINGS.md` — the second is yesterday's EOD entry, uncommitted.
- **~100 untracked**, all of it accumulated research output and backups rather than code in flight:
  - **28 journal files** — `docs/research/journal/2026-08-21` … `2026-09-15`, both the daily and `_morning_status` variants. Every report this task has written since 21 Aug is sitting uncommitted.
  - **~18 backtest scripts** under `backtest/options_open15/`, `backtest/open15_rolling/`, `backtest/open15_missed_days/`, `backtest/news_event_study/`
  - **13 DB backups** (`db/openalgo.db.bak.*`, `db/sandbox.db.bak.*`) going back to 14 July
  - ~20 `log/restart_*.err|.out` files, plus `.claude/launch.json`

The journals and the backtest scripts are the ones worth a decision — commit them or gitignore them; a month of research output outside version control is the kind of thing that quietly disappears.

---

## OpenAlgo health

**Running and healthy.** Booted **08:19:58 IST today** and the process is live — the daily-D convergence backfill was still streaming symbols at 08:23.

### The token scare that wasn't

Between **08:19:03 and 08:20:38** there were 24 ERROR lines, and they look alarming at a glance:

- `WebSocket error: Handshake status 403 Forbidden … "Authentication failed."` (×3 rounds)
- `Error fetching multiquotes: API Error: Incorrect api_key or access_token` (×4)
- `Adapter connection failed: Connection timeout`, `Failed to authenticate with WebSocket server`

**This is the designed boot sequence, not a fault.** Kite flushes every access token 06:45–07:30 IST; the app booted into a dead token, the auto-login watcher detected it and fixed it:

```
08:20:02  broker auto-login boot+watcher initialized
08:20:17  no live session at boot — attempting auto-login
08:20:39  Primary Zerodha account auto-login succeeded.
08:20:39  watcher started (every 300s, window 06:15..23:30 IST)
```

**Zero errors after 08:20:38.** A NIFTY futures quote fetched cleanly further down the log, which confirms the new token is working against the broker API. Master contract re-download also triggered correctly (last was 2026-09-15).

### Error rate

| Window | ERROR lines | Assessment |
|---|---|---|
| Today, 08:19–08:20 | 24 | Pre-auto-login token errors — resolved |
| Today, after 08:20:39 | **0** | Clean |
| Yesterday (15 Sep) | 341 | ~250 were pytest test noise (synthetic "re-rank exploded"/"disk full" from `test_open15_*`); the rest were the known `strategy_llm_config` boot-race and the 09:18 scanner cold-start, both already in `audit/proposed_fixes.jsonl` |

`log/errors.jsonl` currently holds 1020 lines spanning 09 Sep → 08:20 today (it truncates to the last 1000 on each app start).

### Services registered at boot — all present

Scanner up on **228 symbols** (5m, ZMQ 5555). Registered cleanly: `scanner_smoke_check` (09:18, min_cov 0.50), `scanner_preentry_refresh` (09:16), `scanner_dry_tripwire`, scanner WS watchdog, `tick_liveness` watchdog (autoheal on, 10m threshold), WS recovery service (20 min lookback), thread watchdog, EOD watchdog, `scanner_comparison_eod` + `option_liquidity` (15:45), `postmarket_review` (17:15), multi-account jobs, `trading_day_funnel` (15:35), `scanner_history_refresh` (16:00).

**All four strategies are in `sandbox` mode** — `sector_follow` (strategy_id 1), `futures_follow` (id 2, entry_mode `legacy`, smoke_check on), `intraday_pullback`, `open15_breakout` (6 jobs: arm 09:10 / first-candles 09:16 / exit 09:30 / retry 09:32 / summary 09:35 / entry-verify every minute in the window).

One benign warning at boot: `boot_db_probe` saw a transient `historify.duckdb` lock with no holder PID and correctly did not abort.

### ⚠️ The one thing to watch today

```
08:20:03 WARNING futures_follow_service: REHYDRATED open sandbox position
         NIFTY29SEP26FUT qty=130 lots=2
         (entry_date 2026-09-15, T+1 exit due today 2026-09-16)
```

Rehydration worked — this is exactly the `#497` failure mode (T+1 exits silently dead because the position book was never read back) behaving correctly. **But the exit still has to fire at 15:25.** If you check one thing today, check that those 2 lots close. It is sandbox money, so the risk is to the measurement, not the account.

---

## Today's schedule (Wednesday — trading day)

| IST | Event |
|---|---|
| 09:10 | `open15_vol_breakout` arm |
| **09:15** | **Market open** |
| 09:16 | open15 first candles · `scanner_preentry_refresh` |
| 09:18 | `scanner_smoke_check` (min coverage 0.50) |
| 09:30 / 09:32 / 09:35 | open15 exit / retry / summary |
| 09:40 | multi-account fill reconcile |
| 15:02 → 15:10 | `sector_follow_cap5_vol`: refresh → smoke → entry 15:05 → exit 15:10 |
| 15:18 → 15:30 | `futures_follow_cap50`: smoke 15:18 → entry 15:20 → **exit 15:25 (the NIFTY lots)** → watchdog 15:28 → EOD 15:30 |
| 15:35 | Multi-account EOD summary · `trading_day_funnel` |
| 15:45 | `scanner_comparison_eod` · `option_liquidity` EOD |
| 16:00 | `scanner_history_refresh` |
| 17:15 | `postmarket_review` |

📝 **Note:** the schedule block in this task's own SKILL.md is stale — it still lists sector_follow entry at 15:20/15:25. Those moved to **15:05/15:10** on 2026-08-03 (issue #512, NSE Closing Auction Session — CNC MARKET orders must clear before the 15:15 auction). The table above reflects what actually registered at boot today. Worth correcting in the SKILL.

---

## Summary

| Area | State |
|---|---|
| Stuck / errored tasks | **0** |
| Dispatch tasks done (24h) | 6 |
| Dispatch tasks running | 0 |
| `dev` vs `origin/dev` | **In sync** (`e64c27b`) |
| Un-FF'd branch count | Unavailable — shell down |
| Dirty tree | 2 modified, ~100 untracked |
| Errors since auto-login | **0** |
| OpenAlgo | **Up since 08:19:58, token healthy** |
| Action needed before open | **No** — optional: watch the 15:25 futures exit |
