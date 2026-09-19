# Morning status — 2026-09-11 (Friday)

**Headline, Dheeraj:** Everything booted and self-healed cleanly before market open — the one
decision waiting on you is whether open15's **₹2,500 per-trade stop** arms live again at 09:10,
because it has now cost money two sessions running.

> ✅ **This report is on time.** Ran at ~08:50 IST, before the 09:10 open15 arm and the 09:15
> open — unlike yesterday, which executed after 09:40 and was a post-open review.
>
> ⚠️ **The Linux sandbox is DOWN for a second day** (`failed to mount … Plan9 share "c" is not
> mounted`, 3 attempts). No `git`, no Python, no `sqlite3`, no Telegram send. Everything below
> was reconstructed from `log/openalgo_2026-09-11.log`, `log/errors.jsonl`,
> `audit/proposed_fixes.jsonl` and raw `.git` refs via file reads. This is a Cowork-side
> outage, not an OpenAlgo one — OpenAlgo itself is fine.

---

## 🔴 Stuck / Action Required

### 1. open15's per-trade stop arms live in ~20 minutes — and the evidence is trending against it

This is the third morning in a row this has been flagged, and it is the only item here that
touches **real money**.

| Day | Symbol | Stop saved (net) |
|---|---|---|
| 09-10 | VBL | **+₹8,070** |
| 09-10 | CANBK | **−₹12,092** |
| 09-10 | MCX | **+₹2,239** |
| 09-10 | **net** | **−₹1,782** |
| 09-09 | MCX | held would have been +₹1,283 gross |
| 09-09 | BIOCON | held would have been +₹600 gross |

Yesterday open15 went **0-for-3 live (≈ −₹7,524 net)** with all three exits fired by the stop,
and on VBL the stop tripped **3 seconds after entry on the bid-ask spread alone** (bought into
a ~4.8% spread; per #716 the rule marks long premium at the bid, so the position opened
−₹2,295 underwater against a ₹2,500 threshold).

**What to do before 09:10:** open `/open15_vol_breakout/logs` and read the **STOP-LOSS
SCORECARD** card. The pre-registered decision rule is in code, not tunable — *keep the stop
only if after 20 events cumulative stop-saved > 0 AND ≥ 50% of stops were right*. If you want
to act rather than observe, the candidate fixes are already written up in
`docs/research/strategy/open15_vol_breakout/2026-09-07_stop_loss_whipsaw_dwell_plan.md`
(dwell/grace window before the stop arms · entry-time max-spread gate · mark the first N polls
at mid). **That plan doc is still untracked** — it is real work product sitting outside git.

I could not read the scorecard myself: it lives in `db/openalgo.db` and the sandbox is down.
I also could not confirm open15's current `strategy_mode` row — yesterday it was **live** with
`config_source: ui`, `stop_loss_inr 2500`, `profit_target_inr 5500`, `trail_giveback_inr 2000`,
`margin_per_slot ₹60,000`, `max_trades 3`. Assume unchanged unless you flipped it.

### 2. Cowork sandbox outage — second consecutive day

Every scheduled task is running degraded. Today's "Fno scan cycle" worked around it via the
browser and completed correctly; this report is running on file reads only. Nothing in the
trading path is affected, but you lose `git`, `pytest`, `sqlite3` and Telegram delivery from
every dispatch session until it clears. Worth a Cowork restart.

### 3. Three unresolved items in `audit/proposed_fixes.jsonl`

| Logged | Age | Item |
|---|---|---|
| 2026-09-09 | **recurring since 08-07** | `strategies_dashboard_api.py:402` — `no such table: strategy_llm_config`, ~282 ERRORs/day (5 per dashboard poll). Log noise, not order-path. |
| 2026-09-09 | **recurring since 08-07** | `database/historify_db.py:2068` — duckdb `Table with name download_jobs does not exist`, 4×/day. |
| 2026-09-10 | new | `services/simplified_stock_engine_service.py:1488` — **phantom BDL position at EOD**: engine held BDL SHORT −83 internally while the sandbox book read flat. The #626/#497 suppression behaved correctly and the trade was still journaled, but engine state and the sandbox book disagreed. Marked **manual-review — do not auto-change the order/position path.** |

Note the first two were reported as *resolved* in yesterday's report ("zero `no such table`
errors today"). Today's boot does log `strategy_llm_config table ready`, so the schema exists —
but the audit entries were never closed. Worth reconciling the two.

### 4. Housekeeping — working tree at 101 dirty entries

From the 08:37:26 boot dirty-check: **2 tracked modifications** (`.gitignore` staged,
`strategies/simplified_engine/LEARNINGS.md` unstaged) and **99 untracked**. Up two from
yesterday's 99 — the new ones are the journal files this report and its predecessor wrote.
The untracked pile is ~20 `backtest/options_open15/*` scratch scripts, 11 `db/*.bak.*`
snapshots, ~29 `docs/research/journal/*` files dating to 2026-08-21 (every morning status
report since), ~26 `log/restart_*.{err,out}`, and assorted one-offs. The journal files and the
stop-loss plan doc are real work product; the `.bak` DBs and restart logs are pure clutter
inflating this warning every boot.

### 5. `SCANNER_SYMBOLS` still carries 4 names with no NFO contracts

`08:37:33 · scanner universe: 216 watched, 212 tradeable (dropped: DALBHARAT, EXIDEIND,
NUVAMA, SAMMAANCAP)`. The #647 fail-open filter handled it correctly. Better than yesterday —
today this logged at **INFO**, not ERROR — so it is now pure housekeeping: clean the env list
when convenient.

---

## 🟢 Dispatch tasks complete in last 24h

- **`local_c41058e9`** "Fno scan cycle" (today, 08:48) — **DONE**. Correctly detected
  pre-market (`08:48 < 09:30`), skipped the full cycle, and recorded a `market_closed` abort
  trace (cycle row 17695, `aborted_market_closed`) so the off-hours fire isn't a silent gap.
  Flagged "16 errors in the last 30 min (8 signatures)" as a preflight risk — **that is the
  pre-login WS churn in §Health below and it will fall outside the 30-min window well before
  09:30.** No action needed.
- **`local_d10b9f6d`** + **`local_addd7e69`** "Fno scan cycle" EOD runs for 2026-09-10 —
  **DONE**, and consistent with each other: simplified engine (sandbox) **6 trades, 3W/3L**,
  gross −₹266.05, charges ₹493.54, **net −₹759.59**. Essentially scratch gross; the loss is
  charge drag (₹494 ≈ 2× the gross loss) plus one bad long — **VBL alone was −₹582 net, 77% of
  the day's loss**. Stops/trail worked on all six. Tick log healthy: 3.2M ticks / ~272 MB,
  0 dropped. LEARNINGS.md entry written; one item proposed to the audit log (§3 above).
- **No STUCK or ERRORED sessions.** Nothing abandoned mid-work today (yesterday had one,
  `local_df127dc9`, killed by the same bash outage).

## ⏳ Dispatch tasks running

- **`local_c92f66c9`** "Weekday trading standup" — **PROGRESSING** (6 → 46 turns while this
  report ran). Working around the bash outage via log file reads, same as this task.

---

## Git state

- **`dev` is exactly in sync with `origin/dev`** — both at `674c6f8f`. Nothing to push, nothing
  to fast-forward. (Yesterday was `fcd42ef3`, so dev advanced and the work was pushed.)
- **Per-branch un-FF'd commit counts could not be computed** — that needs `git`, and bash is
  down. **389 local branch refs** exist under `.git/refs/heads/` (feature, variant, worktree-agent
  and research branches going back to June). I'm not going to guess at their state.
- **Working tree: 101 dirty entries** — see §4 above.

---

## OpenAlgo health

**Verdict: healthy, and it self-healed everything it hit.** Booted 08:37:25, logging
continuously through 08:45+.

| Check | Result |
|---|---|
| Broker session | ✅ No live session at boot → watcher auto-logged in. **Primary Zerodha succeeded 08:38:03**; child `Didi-zerodha` **08:38:12**. Watcher running (every 300 s, window 06:15–23:30 IST). |
| Master contract | ✅ Refreshed today (`should_download=True`, last was 09-10). |
| Tick feed | ✅ **Up.** WS 403 "Authentication failed" at 08:37:33–08:38:03 were **pre-auto-login** — the normal Kite 06:45–07:30 token flush. Subscriptions confirmed flowing from 08:38:07. |
| Session cookie | ✅ Expired at 03:00 as designed; **issue #719 preserved the fresh broker token** ("stored broker token is fresh — preserved, only the browser session is cleared"). Working exactly as intended. |
| Daily-D backfill | ✅ Running, mid-alphabet at 08:45 (2 records/symbol, 09-09→09-11). Historify is being written to today. |
| News ingest | ✅ 08:40 — 85 fetched / 82 new across livemint + ET. 08:45 incremental — 3 new. |
| Errors today | **16 lines** in `errors.jsonl`, all timestamped 08:37:33–08:38:03, **all pre-login WS/token churn**. Zero errors after the token landed. No unexplained errors. |
| Postmarket review (09-10) | ❓ **Could not read** — lives in `db/openalgo.db` and the sandbox is down. Not asserting it ran. |

---

## Today's schedule (Friday, trading day)

| IST | What |
|---|---|
| **09:10** | open15_vol_breakout arm — **live money, see §1** |
| 09:15 | Market open |
| 09:16 | open15 seed selection |
| 09:29 / 09:30 | open15 entry cutoff / hard flatten |
| 09:40 | multi-account fill reconcile |
| 15:18 | sector_follow_cap5_vol smoke check |
| 15:20 | sector_follow + futures_follow_cap50 entry evaluation |
| 15:25 | exits |
| 15:28 | futures_follow EOD watchdog |
| 15:30 | EOD summaries |
| 15:45 | scanner_comparison_eod |
| 17:15 | postmarket_review |

---

## Telegram

⚠️ **Not sent.** The Telegram path needs the Linux sandbox (Python + `requests` + Fernet
decryption of the bot token against `APP_KEY`), and the sandbox failed to mount on all
attempts. Second consecutive day. **Please open this journal directly in the Cowork app.**

Separately: OpenAlgo's own Telegram sender is unaffected and was working yesterday — this only
blocks *this report* from reaching your phone, not strategy alerts.
