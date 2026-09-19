# 2026-09-14 (Monday) — Morning Status Report

**Dheeraj — OpenAlgo is not running right now, and open15 arms live-money at 09:10. Boot it first.**

> ⚠️ **Degraded run — 4th day in a row.** The Cowork Linux sandbox failed to mount again
> (`Plan9 share "c" not mounted`, 2 identical failures, then stopped retrying). So: **no
> `git`, no `duckdb`, no `sqlite`, no `python`, no Telegram.** Everything below was
> reconstructed read-only via file tools from `.git` refs/reflogs and `log/`. What I could
> NOT verify is listed under "Gaps" — I have not guessed anything.

---

## 🔴 Stuck / Action Required

### 1. OpenAlgo is NOT RUNNING — boot before 09:10 IST

There is **no `log/openalgo_2026-09-14.log`**. The most recent daily log is Friday's, and it
ends at **`09:32:14` with a clean shutdown** (full unsubscribe of 228 symbols → pool
disconnect → `WebSocket server closed and port released`). That is a deliberate stop, not a
crash — but nothing has started since.

Consequences if you don't boot:

- **09:10 open15 arm will not happen.** open15 was `mode: live` on Friday — real money.
- Zerodha auto-login (#654) fires on boot after 07:30 IST, but only if the app is up. No app,
  no token.
- The `option_liquidity` convergence loop (every 1200 s) only runs in-process. It needs
  ~20 min of runtime to heal the stale coverage ladder before the arm reads it.

**Practical deadline: boot by ~08:45** to give auto-login + feed + convergence time before 09:10.

### 2. Friday's entire afternoon never ran — 0 post-close jobs, again

The app died at 09:32 Friday, so **every job after that missed**:

| Time (IST) | Job | Ran? |
| --- | --- | --- |
| 09:35 | open15 summary + final fill reconcile | ❌ |
| 15:05 / 15:10 | `sector_follow_cap5_vol` entry / exit | ❌ |
| 15:18–15:28 | `futures_follow_cap50` smoke / entry / exit / watchdog | ❌ |
| 15:30 | EOD summaries | ❌ |
| 15:45 | `scanner_comparison_eod` + open15 option-liquidity sweep | ❌ |
| 16:30 | `sector_follow` data-health check | ❌ |
| 17:15 | `postmarket_review` | ❌ |

This is now the **third+ consecutive session** with no post-close jobs (09-10 and 09-11 boot
logs both reported the 15:45 and 16:30 jobs as *missed*). The compounding effect: the
**option-liquidity coverage ladder feeding today's 09:10 arm is stale again**, so the #591
coverage read will likely be overstated. Glance at the coverage card on
`/open15_vol_breakout/logs` before the window opens.

Mitigating detail: the 09:35 summary missing is *less* bad than it looks — the #641 inline
reconcile ran at each exit, so Friday's P&L below is already **fill-true**, not quote-derived.

### 3. open15 lost real money again Friday — two ~₹7.5k days back to back

**Friday 2026-09-11, `mode: live`, 2 trades, both long, both stopped out inside 60 seconds.**

| Symbol | Entry | Exit | Contract | Qty | Gross | Charges | **Net** |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ONGC | 09:20:51 @ ₹4.14 | 09:21:14 @ ₹3.91 | `ONGC29SEP26242.5CE` | 13,500 | −₹3,105.00 | ₹531.21 | **−₹3,636.21** |
| TECHM | 09:22:32 @ ₹43.17 | 09:23:39 @ ₹40.40 | `TECHM29SEP261540CE` | 1,200 | −₹3,324.00 | ₹493.70 | **−₹3,817.70** |

**Day net ≈ −₹7,453.91** (fill-reconciled, `pnl_source: fill`). That follows 09-10's
−₹7,524. Two sessions, ≈ **−₹15k** of real money.

> ⚠️ **Number conflict with today's 08:45 standup — use this one.** The standup reports
> Friday as **−₹5,978.93**. That is the sum of the two `[exit]` events' quote-derived `pnl`
> (−2,963.22 + −3,015.71), i.e. the figure *before* #555 fill reconciliation. The
> `[fill_reconcile_row]` events then re-derived both rows in place with `pnl_source: 'fill'`
> (ONGC gross −3,105.00 / TECHM gross −3,324.00), which is the authoritative record per the
> #552 single-convention rule. Real loss is **≈₹1,475 worse** than the standup says.

**The stops were right, though** — both counterfactuals say holding to 09:30 would have been
worse:

- ONGC `stop_saved` **+₹134.36** (MAE −₹5,265 / MFE +₹7,965)
- TECHM `stop_saved` **+₹1,313.72** (MAE −₹9,084 / MFE −₹2,844)

So the stop rule netted **+₹1,448** vs. no stop. The problem isn't the exit — it's that both
entries reversed within a minute.

**#721 did its job:** HINDALCO triggered at `vol_ratio 2.14` against the `max_vol_ratio: 1.8`
ceiling and was declined (`entry_skipped · vol_ratio_cap`, sim-priced, no slot consumed). But
ONGC and TECHM both triggered *below* the ceiling and still lost — the ceiling is not
catching this failure mode.

**Decision for you:** open15 is live, has lost ~₹15k in two sessions, and the pre-registered
#721 review isn't due until ~80 real fills. Options are (a) let it run and keep collecting,
(b) cut `margin_per_slot` from ₹60,000, or (c) flip to sandbox for a few sessions. This is
your call — I'm flagging it, not recommending.

### 4. This morning's other scheduled task will likely fail the same way

`local_d58e3760` "Weekday trading standup" is running and its last action was a
`mcp__workspace__bash` call — the same tool that is failing for me. Expect it to produce a
degraded report or error out. **The sandbox mount failure is now a 4-day-old unfixed blocker**
and it is the root cause of both the missing Telegram delivery and the unverifiable DB checks.

---

## 🟢 Dispatch tasks — complete in last 24h

No code/dispatch sessions ran overnight. Everything in the window is scheduled-task routine:

| Session | Title | Verdict |
| --- | --- | --- |
| `local_127ffdd5` | Fno scan cycle | **DONE** — aborted correctly, IST 09:18 < 09:30 |
| `local_c74fc106` | Fno scan cycle | **DONE** — `aborted_market_closed`, cycle 17697 |
| `local_e02fdaff` | Fno scan cycle | **DONE** — `aborted_market_closed`, cycle 17696 |
| `local_49c30d16` | Morning status report (09-11) | **DONE** — clean report, Telegram N |
| `local_c92f66c9` | Weekday trading standup (09-11) | **DONE** — degraded (no sandbox), journal only |

**0 STUCK. 0 ERRORED.** The ~24 older "Fno scan cycle" sessions are all the same
market-closed abort pattern — normal, no action.

## ⏳ Dispatch tasks — running

| Session | Title | Turns | State |
| --- | --- | --- | --- |
| `local_587edbbc` | Fno scan cycle | 7 | PROGRESSING — checking tabs / IST time |
| `local_d58e3760` | Weekday trading standup | 5 | PROGRESSING — but blocked on `bash` (see item 4) |

Both started this morning on schedule. Neither is stuck yet.

---

## Git state

**`dev` == `origin/dev` @ `4781088d` — 0 un-FF'd commits.** Clean.

Last movement was Friday:

| Time (IST) | Action | Result |
| --- | --- | --- |
| 09-11 08:38 | `pull: Fast-forward` | `fcd42ef` → `674c6f8` (**#721** volume-ratio ceiling) |
| 09-11 08:58 | `pull: Fast-forward` | `674c6f8` → **`4781088d`** (**#722** per-side stop loss) |

**Correction to Friday's standup:** it reported #722 as *"NOT merged, branch-only"*. That was
true when it was written (~08:45) but you merged it **13 minutes later at 08:58**, and the
boot log confirms the migration fired — `open15_config: added column stop_loss_inr_long` /
`stop_loss_inr_short` at `08:58:20`. **Both #721 and #722 are live at HEAD.** Friday's arm
event proves it: `stop_loss_inr_long: 3000.0, stop_loss_inr_short: 2500.0`.

**Nothing has been pulled or committed since Friday 08:58.**

### Working tree

**Not verifiable this run.** `app.py`'s boot dirty-check is what logs `git status --porcelain`
verbatim, and the app hasn't booted today. Friday's boot recorded **101 entries — only 2
tracked**:

- `M ` `.gitignore` — **staged**, not just modified. Still looks like an interrupted `git add`.
- ` M` `strategies/simplified_engine/LEARNINGS.md` — unstaged.

The other 99 were untracked scratch (backtest scripts, 13 × `db/*.db.bak.*`, 29 uncommitted
journals, 24 restart logs). Assume unchanged — nothing has run since.

### Branch sprawl

**389 local branch refs** under `.git/refs/heads/`, including ~50 `worktree-agent-*` refs from
the parallel-task rule. Not urgent, but this is well past the point where `git branch` is
useful. Worth a prune session.

---

## OpenAlgo health

| Signal | Reading |
| --- | --- |
| Daily log for today | ❌ **absent** — app not started |
| Last daily log | `openalgo_2026-09-11.log`, ends `09:32:14` (clean shutdown) |
| Log line count Friday | 21,329 lines, all between 08:37 and 09:32 |
| `errors.jsonl` | Last entry **2026-09-11 09:18** — nothing since |
| `historify.duckdb` freshness | ❓ **unverifiable** (no duckdb client) |

### Friday's 18 errors — 16 benign, 2 real

**16 benign (08:37:33–08:38:03):** WS handshake 403 churn across
`zerodha_websocket` / `websocket_client` / `connection_pool_zerodha`. This is the adapter
trying the stale overnight token before auto-login completed at 08:38:03, then reconnecting.
Known-good pattern, same as 09-10. Ignore.

**2 worth reading:**

1. `09:10:03` · `open15_breakout_service` —
   *"4 watched symbols have NO NFO option contracts and were DROPPED — SCANNER_SYMBOLS is
   stale: **DALBHARAT, EXIDEIND, NUVAMA, SAMMAANCAP**"*
   This is #647 **working as designed** (fail-open exclusion), but it fires every morning
   until `SCANNER_SYMBOLS` is pruned. Small, real, easy cleanup.

2. `09:18:00` · `scanner_smoke_check_service` —
   *"scanner smoke check 09:18 FAILED: scanner_universe_1m stale; scanner_universe_D stale"*
   Armed the #390 per-symbol post-hold. Affects the in-house scanner only — **open15 does not
   depend on it**, so Friday's trades were unaffected. But it's the same stale-feed story that
   the missing post-close backfill jobs keep causing.

**Errors in last 4 hours: 0** — because nothing is running, not because it's healthy.

### Friday's arm config (for reference — what today will likely arm with)

```
mode live · trade_side both · instrument atm_option · universe 207
vol_mult 1.50 · max_vol_ratio 1.80 (#721)
stop_loss_inr_long 3000 · stop_loss_inr_short 2500 (#722)
profit_target 5500 · trail_giveback 2000 · trail_confirm_polls 2
max_trades 3/3 · margin_per_slot 60000 · notional 300000
available_cash 220900.40 · funds_clamp None · residual_sizing True
option_liquidity_gate_enabled False · option_min_oi_lots 500
no_entry_after 09:24 · exit_time 09:30 · config_source ui
```

---

## Today's schedule (Monday — trading day)

| IST | Event |
| --- | --- |
| **~08:45** | ⚠️ **Boot OpenAlgo** (auto-login + feed + convergence need lead time) |
| **09:10** | open15 arm — **live money** |
| 09:15 | Market open |
| 09:16 | open15 first candles · `scanner_preentry_refresh` |
| 09:18 | scanner smoke check |
| 09:24 | open15 no-entry-after cutoff |
| 09:30 | open15 exit |
| 09:32 / 09:35 | open15 retry / summary |
| 15:05 / 15:10 | `sector_follow_cap5_vol` entry / exit (sandbox) |
| 15:18 / 15:20 / 15:25 / 15:28 | `futures_follow_cap50` smoke / entry / exit / watchdog (sandbox) |
| 15:30 | Market close · EOD summaries |
| 15:45 | `scanner_comparison_eod` + option-liquidity sweep |
| 16:30 | `sector_follow` data-health check |
| 17:15 | `postmarket_review` |

> Note: the task SKILL still lists "15:20 sector_follow entry". That is **stale** — it moved to
> **15:05** on 2026-08-03 for the NSE closing-auction change (#512). Worth editing the SKILL.

---

## Gaps in this run

1. **Sandbox mount failure, 4th consecutive day.** No `git`, `duckdb`, `sqlite`, `python`, or
   network. This is no longer an intermittent annoyance — it has silently degraded every
   morning report since 09-10 and it blocks Telegram entirely. **Fix this before the reports
   become worthless.**
2. **⚠️ Telegram NOT sent.** The bot token is Fernet-encrypted and needs
   `database.telegram_db.get_bot_config()` to decrypt — which needs Python. **This journal is
   today's only delivery. Please open it directly in Cowork.**
3. Not verified: `historify.duckdb` freshness, `strategy_runtime_override` rows, open15's
   current mode row (Friday's `live` is assumed to persist), today's working-tree state, and
   the `postmarket_review` row for 09-11 (the job never ran, so there is none).

---

## Suggested first 20 minutes

1. **Boot OpenAlgo now.** Confirm Zerodha auto-login lands (primary + child) and the feed
   reaches 228/228.
2. **Decide on open15 before 09:10** — it is live, down ~₹15k over two sessions, and will arm
   at ₹60k/slot × 3 unless you change it.
3. Check the **option-liquidity coverage card** on `/open15_vol_breakout/logs` — the 15:45
   sweep hasn't run in days, so the ladder is stale.
4. **Find out why the app keeps stopping mid-morning.** Friday's 09:32 shutdown was clean, so
   something or someone is stopping it right after the open15 window. Three days of missing
   afternoon jobs all trace back to this.
5. Optional cleanup: prune the 4 stale `SCANNER_SYMBOLS` names, and the staged `.gitignore`.
