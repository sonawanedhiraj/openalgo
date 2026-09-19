# Morning Status — Friday 2026-09-04 (generated 08:52–09:02 IST)

**Dheeraj — OpenAlgo is up and the broker session auto-healed at 08:53; nothing blocks today's 09:10 open15 arm, but a P0 futures_follow carry has now been open 6 trading days and the `claude` CLI is still logged out.**

---

## 🔴 Stuck / Action Required

### 1. P0 — `futures_follow_cap50` stranded lot, 6 days and counting (recurring since 2026-07-30)

The 17:15 postmarket contract `t1_exit_for_carry` has failed on **every trading day it evaluated since 2026-08-14** (with 08-27 the only clean day):

| Review date | Open lots | Oldest entry | Age | Exits that day |
|---|---|---|---|---|
| 2026-09-03 | 1 | 2026-08-28 | 6d | 0 |
| 2026-09-01 | 1 | 2026-08-28 | 4d | 0 |
| 2026-08-26 | 3 | 2026-07-30 | 27d | 0 |
| 2026-08-25 | 1 | 2026-07-30 | 26d | 0 |
| 2026-08-17..20 | 1 | 2026-07-30 | 18–21d | 0 |

This is the #497 shape on a **T+1** strategy. Corroborating evidence this morning: at 08:53:22 the
`broker_session_refreshed` handler logged `futures_follow: rehydrated 0 open position(s)` — the service
believes it is flat while the journal says a lot is carried. Strategy mode is `sandbox`, so this is
**virtual money, not real** — but it is an un-exited position on a strategy whose whole design is
one-night carry, and it has survived at least three prior fixes.

**Action:** open/attach a GitHub issue and decide whether the row is a journal artifact or a genuine
un-exited sandbox lot. Nothing here is safe to auto-repair.

### 2. `claude` CLI on the host is logged out — postmarket triage dead again

`postmarket_review` for 2026-09-03 recorded `llm_status = not_logged_in`. Yesterday's morning report
flagged the same thing (`OAuth session expired and could not be refreshed`, 5× that day), so this has
now cost two consecutive nights of LLM triage **and** means the Stage-1 signal veto fails open on every
signal.

**Action:** run `claude` in a terminal → `/login`. One minute of work; it has been outstanding 2 days.

### 3. Telegram is not delivering from OpenAlgo

At 08:50:57 the bot failed to initialise (`httpx.ConnectError`), and at 08:52:24 the notification service
logged:

> `no live telegram bot (legacy inactive, inbound unavailable/no chats; event=broker_auto_login) — dropping notification`

`bot_config.is_active = 1` and a chat id is configured (`1345069591`), so this is a startup-time network
failure, not a config problem — but **operator alerts are being dropped right now**. The auto-login
notification itself was one of them.

**Action:** worth a restart of the bot from the UI once you're at the laptop, so today's 15:30 EOD summary
and any kill-switch/big-loss alert actually reach your phone.

### 4. open15 is LIVE and down ≈ ₹16.1k net over the last 5 sessions — and both new risk controls are OFF

Real fills only (net of modelled charges):

| Date | Real trades | Net |
|---|---|---|
| 2026-09-03 | 3 | **+₹146.02** |
| 2026-09-02 | 3 | −₹8,390.31 |
| 2026-09-01 | 3 | −₹9,016.38 |
| 2026-08-31 | 2 | −₹10,562.57 |
| 2026-08-28 | 3 | +₹11,705.07 |
| **5-day net** | | **−₹16,118.17** |

The #696 risk controls merged yesterday (`profit_lock_enabled = 0`, `stop_loss_enabled = 0`) are both
**disabled**, and thresholds are arm-frozen at 09:10 — so if you want them live today you must flip them
on `/open15_vol_breakout/logs` **before 09:10 IST**. Not a recommendation either way; just noting that the
lever exists and is currently off.

Today's effective config: `atm_option`, `max_trades 3`, `margin_per_slot ₹60,000`, `trade_side both`,
rolling watch-list ON (30 s), shadow side ON, residual sizing ON, `option_min_oi_lots 500`.

### 5. `fno-scan-cycle` did not run during market hours yesterday

`lastRunAt` = 2026-09-03T11:17Z (**16:47 IST**), and the two most recent sessions both exited with
"outside market hours — skipping" (16:32 and 16:47 IST). Same pattern yesterday's report called out — the
Cowork scheduler appears dormant while the laptop sleeps, then fires catch-up runs on wake. `nextRunAt` is
**09:16 IST today**, so it should resume normally if the machine stays awake.

*(Note: this only affects the Cowork-side Chartink mirror. OpenAlgo's own in-process scanner and open15
ran normally — 3 real trades yesterday.)*

---

## 🟢 Dispatch tasks complete in last 24h

| Session | Title | Verdict | Notes |
|---|---|---|---|
| `local_c55e9dfc` | Weekday trading standup | **DONE** | Finished ~08:52 today. Telegram sent (msg 19682), journal `docs/research/journal/2026-09-04.md`. Flagged the same pre-login convergence artifact I see below. |
| `local_d699b60f` | Morning status report | **DONE** | Yesterday 15:41 IST. Flagged claude-CLI logout + Telegram — both still open today. |
| `local_6b195edf`, `local_00ccf3a1` | Fno scan cycle ×2 | **DONE (no-op)** | Both "outside market hours — skipping" at 16:47 / 16:32 IST. |

## ⏳ Dispatch tasks running

**None.** The standup session showed as `running` when I started and had gone idle by the time I read it.
No AskUserQuestion blockers, no `[result] error`, no stream timeouts in any session I inspected.

---

## Git state

- **Un-FF'd branches: 0.** `origin/dev..dev` is empty, and the five most recently touched feature branches
  (`feat/696-open15-risk-controls`, `feat/694-system-shutdown`, `feat/692-open15-pnl-curve`,
  `feat/690-child-residual-sizing`, `fix/688-auto-login-notify-importerror`) are all `ahead=0`. Everything
  is merged and pushed.
- **origin/dev, last 5:**
  - `409c092a2` Merge PR #697 from `feat/696-open15-risk-controls`
  - `deafef8fb` feat(open15): day profit lock + trail, per-trade stop loss, background risk monitor (#696)
  - `bfa910595` Merge PR #695 from `feat/694-system-shutdown`
  - `cc037354e` feat(system): guarded shutdown button on /dashboard (#694)
  - `543bec5ad` Merge PR #693 from `feat/692-open15-pnl-curve`
- **Working tree: 89 entries — 2 tracked, 87 untracked.**
  - Tracked (needs a decision): `.gitignore`, `strategies/simplified_engine/LEARNINGS.md`
  - Untracked: backtest/research scratch (`backtest/options_open15/*`, `backtest/open15_rolling/`,
    `backtest/inhouse_scanner/r60/`, `backtest/open15_missed_days/`, `backtest/news_event_study/*`),
    plus **8+ `db/openalgo.db.bak.*` snapshots** dating back to 2026-07-14 and a stale
    `db/.fuse_hidden0000003400000001`. Those backups are large; worth a cleanup pass sometime.

---

## OpenAlgo health

**Verdict: healthy. The 08:50–08:52 error burst was the normal pre-login window and self-healed.**

Timeline this morning:

| Time (IST) | Event |
|---|---|
| 08:50:57 | Telegram bot init failed (`httpx.ConnectError`) |
| 08:51:00–08:51:20 | WS handshake **403 Forbidden**, adapter connect timeouts — daily Zerodha token flush |
| 08:51:41–08:51:52 | `scanner universe backfill: no API key available — skipping` (×3) |
| 08:52:03 | `data_health_check` insert failed — `database is locked` (SQLite contention, transient) |
| 08:52:24 | Auto-login watcher started (300 s, window 06:15–23:30); primary read dead 1/2 |
| **08:53:22** | **Primary Zerodha auto-login SUCCEEDED**; `broker_session_refreshed` emitted |
| 08:53:22 | Adapter reconnected — **228/228 symbols re-subscribed**; scanner pre-subscribe 228/228 |
| 08:53:30 | Master contract loaded (already downloaded 08:52) |

- **Auto-login:** master switch **ON**; all 3 child accounts enabled with auto-login ON. It worked.
- **Log mtimes:** `log/openalgo_2026-09-04.log` → 08:55:28 (live). `log/errors.jsonl` → 08:52:04 (i.e. no
  errors since the session healed). `db/openalgo.db` 08:52:13, `db/sandbox.db` 08:52:18.
- **`db/historify.duckdb` last written 2026-09-03 15:02** — no write yet today, which is expected: the
  boot convergence at 08:51 ran *two minutes before* the broker session came up and so had no API key.
  `data_health_check` at 03:22 UTC recorded `scanner_universe_D` **not OK** with
  `errors: ["no api key available"]`. Yesterday's 12:57 rows were clean, so this reads as a pre-login
  timing artifact rather than an overnight failure.
  **Watch at ~09:30:** the scanner straggler tick (09:20–15:30, every 15 min) should clear it. sector_follow's
  own convergence doesn't retry until 15:30–17:00, leaving the 15:18 smoke check as the backstop — the shape
  that caused pauses on 2026-08-12.

**Error frequency**

- **Last 4 h: 24 errors**, all in the 08:50–08:52 pre-login window, none since 08:52:04.
  Top loggers: `broker.zerodha.streaming.zerodha_websocket` (4), `services.websocket_client` (4),
  `connection_pool_zerodha` (4), `services.scanner_universe_backfill` (3).
- **Last 24 h: 323 errors** — but **282 of them landed in a single 17:00 bucket on 09-03** and reference
  synthetic symbols (`AAA`, `CCC`, `AAA28JUL26105CE`, "arm 2026-08-26"). That is a **pytest run**, not
  production. Discounting it, real production errors over 24 h are ~41, and the pre-login burst is 24 of them.

**Modes and overrides**

| Strategy | Mode |
|---|---|
| `open15_vol_breakout` | **LIVE** |
| `simplified_engine` | sandbox |
| `sector_follow_cap5_vol` | sandbox |
| `futures_follow_cap50` | sandbox |

No active `strategy_runtime_override` rows. Multi-account mirroring master switch **ON**, 3 children enabled.

---

## Today's schedule (Friday — trading day)

| IST | Event |
|---|---|
| **09:10** | open15 arm — **last moment to flip profit-lock / stop-loss** (thresholds freeze at arm) |
| 09:15 | Market open |
| 09:16 | open15 seed selection; `fno-scan-cycle` next run |
| 09:20–15:30 | Scanner straggler re-check every 15 min (should clear the stale D/1m rows) |
| 09:29 / 09:30 | open15 entry cutoff / exit |
| 09:40 | `multi_account_fill_reconcile` |
| 15:18 | `sector_follow_cap5_vol` smoke check |
| 15:20 | sector_follow + futures_follow_cap50 entry evaluation |
| 15:25 | Exits |
| 15:28 | futures_follow EOD watchdog |
| 15:30 | EOD summaries |
| 15:35 | Multi-account mirror EOD summary |
| 15:45 | `scanner_comparison_eod` |
| 16:00 | Scanner history refresh |
| 17:15 | `postmarket_review` — **will fail LLM triage again unless you `/login` the claude CLI** |

---

## Delivery notes

⚠️ **Telegram not sent from this task.** `api.telegram.org` is blocked from the Cowork sandbox
(`Tunnel connection failed: 403 Forbidden`) — same as yesterday. The bot token is Fernet-encrypted and I
would not put it in a URL regardless. **Please open this journal directly in the Cowork app.**

Note the 08:48 standup *did* deliver a Telegram (msg 19682) covering broker status and git state, so you
have partial coverage on the phone; items 1–4 above are only in this file.

**Sources not reachable / not used:** `localhost:5000` preflight (sandbox cannot reach it — log mtimes used
as the proxy, per the task spec); `db/historify.duckdb` direct read (locked by the running app — freshness
taken from `data_health_check` rows); live `auth` table read (SQLite `disk I/O error` under contention — a
`/tmp` snapshot copy was read instead, so auth state is as of 08:52:13).
