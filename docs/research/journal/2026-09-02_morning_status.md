# Morning Status — Wednesday, 2026-09-02 (08:50 IST)

**Dheeraj — the system is up and fully logged in. Nothing is stuck. Your only real
decision this morning is whether to keep open15 running at ₹60k/slot after two
consecutive losing sessions (−₹9.0k yesterday, −₹10.6k Monday).**

Report generated read-only from the Cowork sandbox. Telegram delivery failed
(see §7) — this file is the delivery.

---

## 1. 🔴 Stuck / Action required

**Nothing is stuck.** No dispatch task is hung, no scheduled job errored, no
position is stranded. Three items want your eyes, in order:

| # | Item | Why it matters |
|---|------|----------------|
| 1 | **open15 P&L: two red days in a row** — 2026-09-01 net ≈ **−₹9,016**, 2026-08-31 net ≈ **−₹10,563**. All six legs were real broker fills. | open15 is the only strategy in `live` mode. Config still says `margin_per_slot = ₹60,000`, `max_trades = 3`, `trade_side = both`. If you want a smaller footprint today, change it on `/open15_vol_breakout/logs` **before 09:10** — the arm reads config at 09:10 and never re-reads. |
| 2 | **Postmarket review 2026-09-01 raised 1 × P0** — `futures_follow_cap50 / t1_exit_for_carry`, fingerprint `38bd638a20a9`: *"1 lot open, oldest entry 2026-08-28 (4d), 0 exits today."* Same fingerprint as 2026-08-26. | Sandbox-only, so no money at risk, but this is the exact #497 shape. The journal looks FIFO-balanced to me (2 BUY on 08-28 → 2-lot SELL on 08-31), so the contract and the ledger disagree — one of them is wrong, and it has now fired twice. Worth an issue. |
| 3 | **sector_follow boot backfill reported 38/38 symbols still stale** at 08:48 (30/30 stocks, 8/8 indices, `errors=2`) | Almost certainly benign: the convergence check ran *during* the 08:47 auto-login, before the token was usable, and "stale vs today's 15:30 close" is trivially true pre-market. Yesterday's 16:30 health check was `overall_ok=1` with an empty stale list. **If the 15:18 smoke check fails today, this was the tell** — sector_follow is `sandbox` so the blast radius is measurement only. |

Not action items, but noted honestly:

- **Yesterday evening (18:59–19:00) there was a burst of 171 open15 errors**, including 27 × `[sandbox] open15 ENTRY REJECTED AAA qty=1447 — IP (122.169.47.35) is not allowed to place orders for this app`, plus `open15: rolling watch-list re-rank failed` (36) and `open15: tick capture failed` (24), and fill-reconcile rows for **HAL** and **AAA** demoted to paper. **None of these symbols appear in `open15_trades` for 2026-09-01** — the day's real trades were DIVISLAB, HEROMOTOCO, POLYCAB, PRESTIGE. Read: this was an evening replay/test run against a dead session, not live trading. Flagging it because "*HAL exit rejected — position may be open*" is a scary string to find in a log and I'd rather you see it here than trip over it later. `AAA` is not an NSE F&O name, which supports the test-harness reading.
- **`sandbox.catch_up_processor` IntegrityError** at 08:49 today (`UNIQUE constraint failed: sandbox_daily_pnl.user_id, date`) — recurring boot-time duplicate-insert, cosmetic, but it fires every restart.

---

## 2. 🟢 Dispatch tasks — last 24h

Nothing to celebrate and nothing to chase. `list_sessions` (30 most recent of
2,212) returned **one running session and 29 idle `Fno scan cycle` runs** — the
recurring scheduled task, all idle, none errored.

## 3. ⏳ Dispatch tasks running

| Session | Title | State |
|---|---|---|
| `local_aa8f8f91…` | **Weekday trading standup** | **running**, 3 assistant turns, latest: *"I'll run the standup. Starting with repo state checks."* — this is the 08:00 sibling task, progressing normally, not stuck. |

No STUCK, no ERRORED sessions.

---

## 4. Git state

**Working tree: `dev`, in sync with `origin/dev` — 0 local commits ahead.**

Recent `origin/dev`:

```
bfa910595 Merge pull request #695 from sonawanedhiraj/feat/694-system-shutdown
cc037354e feat(system): guarded shutdown button on /dashboard (#694)
543bec5ad Merge pull request #693 from sonawanedhiraj/feat/692-open15-pnl-curve
d835d426b feat(open15): intra-hold P&L curve + live P&L on /logs (#692)
27bb7444a Merge pull request #691 from sonawanedhiraj/feat/690-child-residual-sizing
```

**Dirty working tree** — 2 modified tracked files, ~26+ untracked:

- Modified: `.gitignore`, `strategies/simplified_engine/LEARNINGS.md`
- Untracked, research WIP (expected, gitignored-adjacent): `backtest/options_open15/*` (~15 scripts), `backtest/open15_rolling/`, `backtest/open15_missed_days/`, `backtest/inhouse_scanner/r60/`, `backtest/news_event_study/*`, `.claude/launch.json`, `audit/open15_replay_removal_backup_20260817_165557.json`

**Un-FF'd local branches** — 25+ branches sit 2–5 commits ahead of `origin/dev`.
These are old merged feature branches whose local tips carry stale commits; none
is a work-in-progress you'd lose. Top offenders:

```
5  feat/425-open15-midbar-sandbox
4  feat/305-reference-data-contract
3  feat/444-open15-decision-log-history-ui-browsable
3  feat/440-per-strategy-live-routing
3  feat/418-in-house-scanner-signals-table-show-hit-
3  feat/330-buy-sell-price   3  feat/323-…   3  feat/231-…   3  feat/112-…
```

Housekeeping only — a `git branch -d` sweep would clear ~25 of these.

---

## 5. OpenAlgo health

**App is up and was cleanly restarted this morning.**

| Signal | Timestamp | Read |
|---|---|---|
| `log/openalgo_2026-09-02.log` | 08:50:01 | Live, writing now |
| `log/errors.jsonl` | 08:49:17 | Live |
| `db/openalgo.db` | 08:50:00 | Live |
| `db/historify.duckdb.wal` | 08:49:57 | Boot backfill ran |
| Shutdown/restart | 19:36 IST yesterday (via dashboard button, `dheeraj.sonawane`) → boot 08:46 today | Deliberate |

**Logins — all four accounts green (auto-login, 08:47–08:49):**

| Account | Auto-login | Last login |
|---|---|---|
| Primary (`dheeraj.sonawane`) | ✅ succeeded 08:47:40 | today |
| 1 · Swapna-zerodha (₹96,000) | ✅ enabled | 2026-09-02 08:48 IST |
| 2 · Mai-Zerodha (₹1,80,000) | ✅ enabled | 2026-09-02 08:48 IST |
| 3 · Didi-zerodha (₹90,000) | ✅ enabled | 2026-09-02 08:48 IST |

Multi-account mirroring is **ON** (`multi_account_settings.enabled = 1`, primary
book ₹10L).

Boot also completed: master contract downloaded, aggregator seeded **228/228
symbols / 107,784 bars in 51.3s**, scanner D-interval backfill started.

**Error rate:**

- **Last 4 hours: 6 errors** — all boot-transient. 2 × `Incorrect api_key or access_token` + 2 × `child positions read failed` at 08:48:17–18 (read fired *before* the child token landed at 08:48:32 — self-healed), 1 × `Symtoken swap failed — previous contract retained`, 1 × the sandbox P&L UNIQUE constraint.
- **Last 24 hours: 321 errors**, dominated by the 18:59–19:00 evening test burst: `open15_breakout_service` 171, `strategies_dashboard_api` 45 (`Failed to query strategy_llm_config`), `open15_breakout_db` 31 (`config read failed`, `realized-pnl sum failed`), `zerodha.api.funds` 14, `open15_fill_reconcile` 9.

**Scheduled jobs — every job fired `ok` yesterday**, 0 errors, 0 missed:
`sector_follow_{preentry_refresh,smoke_check,entry,exit,eod_summary,data_health}`,
`futures_follow_{smoke_check,entry,exit,eod_watchdog,eod_summary}`,
`intraday_pullback_{eval×84,eod_flatten,eod_summary}`, `scanner_dry_tripwire×84`,
`scanner_comparison_eod`, `scanner_history_refresh`, `option_liquidity_eod`,
`multi_account_{fill_reconcile,eod_summary,login_reminder_1500}`, `open15_summary`,
`postmarket_review`, `trading_day_funnel`. Nothing has fired yet today (expected —
first job is 09:10).

**Strategy modes:**

| Strategy | Mode | Note |
|---|---|---|
| `open15_vol_breakout` | **LIVE** ⚠ real money | atm_option · max_trades 3 · ₹60k/slot · both sides · rolling watch-list ON (30s) · shadow ON · residual sizing ON · min OI 500 lots · window 09:15→09:29, exit 09:30 |
| `futures_follow_cap50` | sandbox | |
| `sector_follow_cap5_vol` | sandbox | |
| `simplified_engine` | sandbox | |

No active runtime overrides (latest three all expired 2026-08-12).

**open15 trade tape:**

| Date | Symbol | Side | Fill | Gross P&L | Charges |
|---|---|---|---|---|---|
| 09-01 | DIVISLAB | S | real | +2,940.00 | 552.54 |
| 09-01 | HEROMOTOCO | L | real | **−8,793.00** | 498.06 |
| 09-01 | POLYCAB | S | real | −1,537.50 | 575.28 |
| 09-01 | PRESTIGE | S | sim | +225.00 | 208.51 |
| 08-31 | LTF | S | real | −3,150.00 | 417.74 |
| 08-31 | HDFCBANK | L | real | −6,500.00 | 494.83 |
| 08-31 | KAYNES | S | paper (OI-blocked) | +20,535.00 | 520.30 |

**Real net: 09-01 ≈ −₹9,016 · 08-31 ≈ −₹10,563.** Note KAYNES: the broker
blocked it on open interest and it would have been the week's best trade at
+₹20.5k — that's the #595 OI filter doing its job, and it's costing you.
One data point, not a case.

---

## 6. Today's schedule (Wed 2026-09-02, trading day)

| IST | Event |
|---|---|
| **09:10** | open15 **arm** — config frozen here. Last chance to change slot size. |
| 09:15 | Market open · open15 seed selection 09:16 |
| 09:29 / 09:30 | open15 entry cutoff / hard exit |
| 15:02 → 15:10 | sector_follow refresh → smoke → entry 15:05 → exit 15:10 |
| 15:18 / 15:20 / 15:25 / 15:28 | futures_follow smoke → entry → T+1 exit → EOD watchdog |
| 15:30 | EOD summaries |
| 15:45 | `scanner_comparison_eod` |
| 16:00 | `scanner_history_refresh` |
| 17:15 | `postmarket_review` (watch for the P0 recurrence) |

---

## 7. ⚠️ Telegram blocked

Direct send failed: `api.telegram.org` → **403 Forbidden (tunnel)** — the Cowork
sandbox proxy does not allowlist it. Same finding as the standup task. I did not
attempt to decrypt the bot token (`.env` / `API_KEY_PEPPER` are off-limits to
agent sessions by policy).

**Please read this journal directly in Cowork.** To get Telegram working from
scheduled tasks, `api.telegram.org` needs allowlisting in the Cowork sandbox
network config.

---

*Read-only run. No DB writes, no git operations, no orders. Data sources: session
inventory, `git`, `log/errors.jsonl`, `log/openalgo_2026-09-02.log`, and a local
snapshot copy of `db/openalgo.db`.*
