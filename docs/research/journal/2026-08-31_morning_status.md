# Morning status — Monday 2026-08-31

**Generated 09:34 IST** (the 08:00 slot fired late — this is a *post-open* report, not a pre-market one. Everything below already happened.)

> **Dheeraj — nothing is stuck and no branch is stranded, but OpenAlgo only booted at 09:13 (two minutes before the open), open15 ran live and is **down ~₹10.6k** on two real fills, and the day's biggest move (KAYNES, paper **+₹20.0k**) was refused by Zerodha's OI-<500-lots rule that our own #595 filter is supposed to catch first.**

---

## 1. 🔴 Stuck / action required

Nothing is *stuck* in the dispatch sense — no ERRORED sessions, no un-FF'd work, no active runtime overrides. Three things want your eyes:

### 1a. The #595 OI filter let a blocked contract through — and it was today's best signal
`KAYNES29SEP263850PE` was added to the rolling watch list at 09:16:02, triggered at 09:17:51, and Zerodha refused it:

> *MIS LIMIT orders are blocked for this KAYNES contract due to its open interest (OI) being less than 500 lots.*

The arm event shows the filter was **on and active** (`option_min_oi_lots: 500, oi_filter_active: True`), so either the batched pre-trade OI read passed a contract the broker then rejected, or the strike that got resolved at trigger time was not the strike that was screened. The `#548` paper path caught it correctly (`fill: paper`, `slot_released: True`) — no money moved — but the paper row prices at **+₹20,014.70**, i.e. the single best outcome of the day was on a contract we were structurally unable to buy. Worth an issue.

### 1b. Today's live open15 result is negative
| Symbol | Contract | Qty | Entry fill | Exit fill | Gross | Charges | **Net** |
|---|---|---|---|---|---|---|---|
| HDFCBANK | 29SEP26 740 CE | 3,250 | 16.50 | 14.50 | −6,500.00 | 494.83 | **−6,994.83** |
| LTF | 29SEP26 310 PE | 4,500 | 9.60 | 8.90 | −3,150.00 | 417.74 | **−3,567.74** |

**Real net today ≈ −₹10,562.57** (both `pnl_source='fill'`, broker-reconciled at 09:30:04 — these are the broker's own numbers, not quotes).
Paper: **+₹20,014.70** (KAYNES) — *do not blend*.

Context — real net, last 5 sessions: 08-31 **−10.6k** · 08-28 +11.7k · 08-27 +11.5k · 08-26 −4.0k · 08-21 +38.0k.

Both real entries came from the **rolling** watch list (`watch_source: 'rolling'`); all six **seed** picks ended `no_entry` (none cleared the 1.5× volume gate while beyond the level). Worth noting for the #529 rolling-vs-seed cohort question — today the rolling additions were the only trades, and both lost.

### 1c. Late boot — 09:13:45, arm at 09:13:51
CLAUDE.md's own ops rule is *"boot OpenAlgo before 09:15 IST on trading days"*. It cleared by 84 seconds. The boot-time noise was the usual set (Zerodha token warm-up: multiquote / funds / websocket 403 handshake at 09:13–09:14) and it recovered — the 09:17 order placement proves the primary token was live. But there is **no margin left**: a boot 90 seconds later and the 09:10 arm is missed entirely.

### 1d. Two housekeeping gaps
- **`postmarket_review` has no row for Friday 2026-08-28.** Latest persisted review is 2026-08-27 (`0 violations, llm_status=ok`). The 17:15 job either did not fire or did not persist. Friday's `job_run` shows 191 ok / 3 missed but no review row.
- **Friday's sector_follow chain missed all three fires**: `sector_follow_preentry_refresh` (15:02 IST), `sector_follow_smoke_check` (15:03), `sector_follow_entry` (15:05) — all `missed`. sector_follow is in `sandbox`, so no money at stake, but a whole entry cycle was skipped.
- **Child Zerodha account is not logged in.** `services.account_open15_service` → `child positions read failed (broker=zerodha)` with `Incorrect api_key or access_token` at 09:14:35 and again 09:31:09. The parent is fine; the child needs its own Connect on `/accounts`.
- **`SCANNER_SYMBOLS` is stale** — 4 watched names have no NFO contracts and were dropped from today's universe: `DALBHARAT, EXIDEIND, NUVAMA, SAMMAANCAP`. The #647 fail-open handled it, but the list wants pruning.

---

## 2. 🟢 Dispatch tasks complete in the last 24h

| Session | Verdict | Outcome |
|---|---|---|
| `Fno scan cycle` (e9412bb3) | **DONE** | 09:34 full cycle — preflight go, sandbox, broker session live. Both screeners 0 matches (Chartink delayed data, early session). Engine posted empty → audit rows written. 31 SELL watches carried from 09:25. Tick log healthy (155,560 ticks / 12.9 MB / 0 drops). Bridge :5001 unreachable → error step skipped (best-effort, expected). |
| `Fno scan cycle` (e16d3aaf) | **DONE** | Friday EOD summary — simplified engine 6/6 trades, 0 open, gross **+₹734.80** (5W/1L, 83%), ≈+₹235.66 net after ~₹499 charges. **Charge drag took 68% of gross — the 5th such data point.** 3.25M ticks, 0 drops. 0 fixes proposed. |
| `Weekday trading standup` (59b58a9b) | **DONE** | Friday/prior standup. Flagged the same Telegram egress block; noted a `sim` row at +₹17,377 on 08-27 where the day's biggest move was a slot you didn't have. That question is now **two days old and repeating** — today's KAYNES is the same shape (best signal, no slot / no fill). |

The remaining ~25 `Fno scan cycle` sessions in the inventory are the recurring scan task's own history — idle, each self-terminating with a cycle report. None errored.

## 3. ⏳ Dispatch tasks running

| Session | State | Note |
|---|---|---|
| `Weekday trading standup` (6bb52cee) | **PROGRESSING** | 33 turns, actively working. This is today's 08:00 standup running concurrently with this report — expect overlap. Not stuck. |

**0 STUCK · 0 ERRORED.**

---

## 4. Git state

**`dev` is fully pushed** — `git log origin/dev..dev` is empty. Working from `dev`.

**Un-FF'd branches: 2, both benign.**

| Branch | Ahead of origin/dev | What |
|---|---|---|
| `fix/684-open15-today-pnl-by-mode` | 1 | `460dd7422 [#684] fix(strategies-dashboard): today chips attach to the row's own mode` — **already merged via PR #683/#685 lineage; the branch is just un-deleted.** Safe to prune. |
| `main` | 1 | `e2205b562 Merge pull request #657 from sonawanedhiraj/dev` — normal main/dev divergence. |

Every other recent feature branch (`#688`, `#686`, `#682`, `#680`, `#677`, `#675`, `#673`, `#671`, `#669`) is `ahead=0` — all merged, all safe to delete.

**Working tree: 102 entries — but only 2 are tracked.**
- `M .gitignore`
- `M strategies/simplified_engine/LEARNINGS.md` ← **this one matters.** Per CLAUDE.md, strategy learnings go **direct to dev**. It has now been uncommitted across at least two standups. Worth flushing today.
- 100 untracked: backtest research scratch (`backtest/options_open15/*`, `backtest/open15_rolling/`, `backtest/open15_missed_days/`, `backtest/inhouse_scanner/r60/`), plus **9 `db/openalgo.db.bak.*` snapshots** dating back to 2026-07-14 and a `db/.fuse_hidden*` artifact. The .bak files are ~110 MB each — that's roughly a gigabyte of stale backups worth sweeping.

**origin/dev, last 5:**
```
34052675c Merge PR #689 from fix/688-auto-login-notify-importerror
e008a7dfb [#688] fix(notify): silently-dropped alerts — three sites imported a `notify` that never existed
c6bdd8b31 Merge PR #687 from feat/686-per-mode-realized-metrics
4d2fef1cd [#686] feat(strategies-ui): realized CAGR/Sharpe/MaxDD for open15, futures_follow and intraday_pullback
3f70434be Merge PR #683 from feat/682-open15-waiting-and-holiday-gate
```

---

## 5. OpenAlgo health

**Alive and current.**

| Artifact | Last write | Read |
|---|---|---|
| `log/openalgo_2026-08-31.log` | **09:39:41 today** | ✅ actively writing |
| `log/errors.jsonl` | 09:31:09 today | ✅ |
| `db/openalgo.db` | 09:27:04 today | ✅ |
| `db/historify.duckdb` | **2026-08-28 14:41** | ⚠️ see below |

**⚠️ `historify.duckdb` last wrote Friday at 14:41 — *before* Friday's 15:30–17:00 convergence window.** That looks like Friday's post-close backfill did not land. Countering evidence: `data_health_check` rows for `scanner_universe_1m` and `scanner_universe_D` are both `overall_ok=1` with **zero stale symbols**, stamped 03:56 UTC = **09:26 IST today** — so this morning's boot convergence ran and found nothing behind. Most likely reading: the file mtime is stale because writes are landing in the WAL rather than the main file, and coverage is genuinely fine. Not treating it as broken, but it is the one health signal I could not fully reconcile from the sandbox.

**Errors — 26 in the last 24h, 26 in the last 4h** (i.e. all of today's, none over the weekend).

| Count | Logger | Nature |
|---|---|---|
| 4 | `services.websocket_client` | Failed to authenticate with WS server — boot warm-up, 09:14 |
| 3 | `broker.zerodha.api.data` | multiquotes — `Incorrect api_key or access_token`, 09:13 |
| 3 | `services.quotes_service` | same root cause |
| 3 | `broker.zerodha.api.funds` | margin data / positions-for-PnL 403 |
| 2 | `services.open15_breakout_service` | stale `SCANNER_SYMBOLS` (§1d) + the KAYNES rejection (§1a) |
| 2 | `broker.zerodha.streaming.zerodha_websocket` | 403 handshake, then correctly `will not retry` |
| 2 | `services.account_open15_service` | **child account not logged in** (§1d) — 09:14 and 09:31 |
| 2 | `broker.zerodha.api.order_api` | child token |
| 3 | `websocket` / `zerodha_websocket` / `connection_pool_zerodha` | same 403 / adapter timeout |

Every one of these is either **boot warm-up before the token settled (09:13–09:14, self-healed)** or **the child-account token**. Nothing after 09:31. No feed-death signature — the 09:16 selection event reports `'source': 'tick'`, meaning live ticks finalized the watch list (not the #677 scheduler fallback).

**Jobs:** 21 fires today, **0 errors, 0 missed**. Friday: 191 ok / **3 missed** (the sector_follow chain, §1d).
**Modes:** `open15_vol_breakout=live` · `simplified_engine`, `sector_follow_cap5_vol`, `futures_follow_cap50` = `sandbox`.
**Runtime overrides:** none active.

---

## 6. Today's schedule (Monday — trading day)

| IST | What | Status |
|---|---|---|
| 09:10 | open15 arm | ✅ done 09:13:51 (universe 207, live, `atm_option`, 3 slots, ₹60k/slot) |
| 09:15 | Market open | ✅ |
| 09:16 | open15 first candles / selection | ✅ 6 seeds + 4 rolling adds |
| 09:29 | open15 entry cutoff | ✅ 3 triggers (2 real, 1 paper) |
| 09:30 | open15 exit | ✅ both flat, fill-reconciled |
| 09:35 | open15 summary | ✅ `selected 10, entered 3, filled 2, paper 1` |
| 15:02 | sector_follow pre-entry refresh | pending — **missed on Friday, watch it** |
| 15:03 | sector_follow smoke check | pending |
| 15:05 | sector_follow entry | pending |
| 15:10 | sector_follow exit | pending |
| 15:18 | futures_follow smoke check | pending |
| 15:20 | futures_follow entry | pending |
| 15:25 | futures_follow exit | pending |
| 15:28 | futures_follow EOD watchdog | pending |
| 15:30 | EOD summaries | pending |
| 15:45 | `scanner_comparison_eod` | pending |
| 17:15 | `postmarket_review` | pending — **no row for Friday, watch it** |

> Note: the schedule times in the task template (15:18/15:20/15:25) are futures_follow's. sector_follow moved to 15:02/15:03/15:05/15:10 on 2026-08-03 for the NSE Closing Auction Session (issue #512).

---

## ⚠️ Telegram

**Not delivered.** The Cowork sandbox blocks all outbound egress — `api.telegram.org` returns `Tunnel connection failed: 403 Forbidden` at the proxy, the same block the prior standup hit (it also blocks `pypi.org`, so it is general, not Telegram-specific). Token decryption was not even attempted since the network path is dead regardless.

**Dheeraj — read this file directly in Cowork; the phone alert is not coming.** If you want these to reach your phone, `api.telegram.org` needs allowlisting for the Cowork sandbox.

---

*Read-only throughout: no commits, no git operations, no DB writes. `db/openalgo.db` was copied to a scratch path and opened `mode=ro`.*
