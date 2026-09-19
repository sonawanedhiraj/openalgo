# Morning status — Friday 2026-09-18 (08:52 IST)

**Headline:** Dheeraj — the system is healthy and armed for today, but yesterday's grade-A-only
filter left **₹6,091 of profitable grade-B signals untraded** while the two real trades netted
**₹1,798**, and that's the one thing worth a decision before 09:10.

---

## 🔴 Stuck / Action Required

Nothing is *stuck*. Three things want a decision or a click:

### 1. The `trade_grades = 'A'` filter cost money yesterday (decision, before 09:10 arm)
`open15_config.trade_grades` is set to **`A`** — so grade B and C triggers are shadow-papered,
not traded. Yesterday's scoreboard (2026-09-17, all net of modelled charges):

| Cohort | Rows | Net |
| --- | --- | --- |
| **Real (grade A)** | 2 | **+₹1,798** |
| Shadow (grade B, `rating_excluded`) | 3 | **+₹6,091** |
| Sim (`vol_ratio_cap`) | 1 | +₹1,454 |

Per-row: BANKINDIA L **−₹175**, PATANJALI S **+₹1,973** (both closed `profit_trail`);
BAJFINANCE L +₹3,151, CGPOWER L +₹4,128, ONGC S −₹1,187 (all grade B, untraded);
SOLARINDS S grade C hit `shadow_cap` and was never priced.

One day is not a verdict — the R63 pre-registered rule says judge at ~40 new real fills, and
you're nowhere near that. But `A`-only is a *tighter* setting than R63 asked for (it never said
gate out B), so it's worth confirming that's deliberate rather than left over from a test.
**No change made — read-only run.**

### 2. `futures_follow_cap50` carries 1 open NIFTY lot; the 09-17 postmarket review fired a **P0**
Contract `t1_exit_for_carry` failed on 2026-09-17: *"2 lots open, oldest entry 2026-09-15 (2d),
but 0 exits today."* Reading the journal directly, that looks like a **contract FIFO artefact,
not a real outage** — row 38 (SELL 2 lots, `t+1_exit`) did fire on 09-16 and closed the two
09-15 buys. What's genuinely open is **1 lot from 2026-09-17** (`NIFTY29SEP26FUT`, entry
23,328.9, sandbox), whose T+1 exit is due **today**. Watch that it fires.

⚠️ Related doc drift: this strategy is entering at **09:50** and exiting **09:55**, but
`CLAUDE.md` still documents 15:20 / 15:25. Worth reconciling.

### 3. Three carry-overs from yesterday's report, still open
- **`postmarket_review` LLM triage has timed out 3 days running** (09-15, 09-16, 09-17 all
  `llm_status='timeout'`). The deterministic contracts still work — only the triage prose is
  missing.
- **Stale `.git/index.lock`** from Sep 17 22:53 is still sitting there. Clear it before your
  next commit. (Read-only run — not removed.)
- **`journal_reflection` crashed again at 16:00 on 09-17** — the bridge on :5001 is still down.

---

## 🟢 Dispatch tasks — last 24h

Only two Cowork scheduled tasks fired; **no code-dispatch sessions ran at all**.

| Session | Status | Verdict |
| --- | --- | --- |
| Morning status report (09-17) | idle | **DONE** — delivered its report |
| Weekday trading standup | **running** | **PROGRESSING** (9 → 36 turns while this ran) |

The 26 `Fno scan cycle` sessions in the inventory are all idle and older than 24h.

**Good news on yesterday's #1 complaint:** the scheduled-task clock is fixed. Yesterday's
"08:00" report fired at **22:52 IST**; today both morning tasks fired at **08:47 IST**, on time.
`fno-scan-cycle` last ran 17-Sep 22:52 and is next due 09:01 today.

## ⏳ Dispatch tasks running
- `weekday-trading-standup` (local_63b9b3cc) — 36 turns, actively writing its journal. Healthy.

---

## Git state

- **`dev` is clean and in sync with `origin/dev`** — 0 local commits ahead. ✅
- **251 branches un-FF'd against `origin/dev`.** This is chronic accumulation, not new breakage.
  Most recent unmerged work: `feat/730-open15-watched-quote-poll` (09-16),
  `feat/728-open15-watched-break-counterfactual` (09-15), `feat/726-open15-trade-rating` (09-15)
  — all three of which **are** on origin/dev already via their merge commits; the local branch
  refs just linger. A branch-pruning pass would cut this number a lot.
- **Working tree: 116 entries.**
  - Tracked modifications: **2** — `.gitignore`, `strategies/simplified_engine/LEARNINGS.md`
  - Untracked: **114** — dominated by research/backtest scratch
    (`backtest/options_open15/*`, `backtest/open15_rolling/`, `backtest/open15_missed_days/`,
    `backtest/inhouse_scanner/r60/`, `backtest/news_event_study/*`), plus
    `.claude/launch.json` and an `audit/open15_replay_removal_backup_*.json`.
- Recent `origin/dev`: `cf767c202` (merge #731) ← `76430b246` [#730] ← `e64c27b40` (merge #729)
  ← `4d744ede3` [#728] ← `426fb8854` [#728].

---

## OpenAlgo health

**Verdict: healthy and armed.** OpenAlgo restarted this morning at **08:42:35** — comfortably
before the 09:10 open15 arm, which is what the ops rule asks for.

| Signal | State |
| --- | --- |
| `log/openalgo_2026-09-18.log` | writing **now** (08:50+) — daily-`D` backfill in progress |
| `log/errors.jsonl` | last write 08:43:14 — **nothing since the broker login succeeded** |
| `db/historify.duckdb` | 08:43 ✅ |
| `db/openalgo.db` / `db/sandbox.db` | 08:44 / 08:47 ✅ |
| Broker session | **LIVE** — `auth.token_updated_at` = 2026-09-18 08:43 IST, `is_revoked=0` |
| Scanner warm-up | 212 symbols, **0 errors** |
| open15 jobs | 6 registered (arm 09:10 / candles 09:16 / exit 09:30 / retry 09:32 / summary 09:35) |
| Child accounts | all 3 **disabled** — no mirroring today |

### Errors: 20 in the last 4h, 28 in the last 24h — but they're all boot noise

Every one of the 20 is from the **08:42:39–08:43:14 window, before the auto-login completed**:
`Incorrect api_key or access_token`, WS handshake `403 Forbidden`, adapter connection timeouts.
The watcher then did its job — *"no live session at boot — attempting auto-login"* at 08:42:53,
`Primary Zerodha account auto-login succeeded` at **08:43:15**, session refreshed again 08:47:41.
**Zero errors since.** Same shape as yesterday morning. Not a fault; the expected cold-start
sequence before the daily Kite token lands.

The other 8 errors (24h window) are yesterday's, already listed above: 4 Telegram broadcast
failures at 15:05/15:20/15:30/15:35 (`Event loop is closed`), the 09:18 scanner smoke-check fail,
the 09:30 dry tripwire, the 09:10 "4 symbols have no NFO contracts" drop, and `journal_reflection`.

**Job runs 2026-09-17: 216 fired, 0 non-`ok`.** APScheduler side is clean.

⚠️ **Telegram broadcast is still broken on the OpenAlgo side** (`Event loop is closed` ×4
yesterday, hitting exactly the entry / exit / EOD / mirror-summary slots). If that repeats today
you will again get no EOD number pushed to your phone.

---

## Today's schedule (Friday — trading day)

| Time | What |
| --- | --- |
| 09:10 | open15 arm (universe, funds clamp, grade rules frozen) |
| 09:15 | **Market open** |
| 09:16 | open15 first-candle selection |
| 09:18 | scanner smoke check ← *failed yesterday; watch it* |
| 09:24 | open15 entry cutoff |
| 09:30 | open15 exit |
| 09:32 / 09:35 | open15 retry / summary |
| ~09:50 / 09:55 | futures_follow entry / **T+1 exit of the open 09-17 lot** |
| 15:18 / 15:20 / 15:25 | sector_follow smoke / entries / exits |
| 15:30 | EOD summary |
| 15:45 | `scanner_comparison_eod` |
| 16:00 | `scanner_history_refresh` + `journal_reflection` (bridge still down) |
| 17:15 | `postmarket_review` (LLM triage timing out 3 days running) |

**Strategy modes:** `open15_vol_breakout` = **LIVE**; `simplified_engine`,
`sector_follow_cap5_vol`, `futures_follow_cap50` = sandbox. No active runtime overrides
(the three `pause` rows all expired 2026-08-12).

---

## ⚠️ Telegram delivery

**Blocked.** `api.telegram.org` refused from the Cowork sandbox
(`Tunnel connection failed: 403 Forbidden`) — same as the previous run's finding. Please read
this journal in the Cowork app, or allowlist `api.telegram.org` for the sandbox.

---

*Read-only run. No DB writes, no git operations, no config changes.*
