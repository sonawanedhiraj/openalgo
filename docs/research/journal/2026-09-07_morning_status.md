# Morning status — Monday 2026-09-07 (08:25 IST)

**Headline, Dheeraj: the platform healed itself cleanly this morning, but all three
child accounts are DISABLED while open15 is still LIVE on the primary — so today's
09:16 entries will trade your money on one account only, into a five-session
−₹32.8k drawdown.**

Report generated 08:25 IST from a read-only snapshot. No DBs written, no git ops.

---

## 🔴 Stuck / Action required

### 1. All 3 child accounts are `is_enabled = 0` AND `auto_login_enabled = 0`
`broker_accounts` right now:

| id | Account | enabled | auto-login | capital | last login |
|----|---------|---------|-----------|---------|-----------|
| 1 | Swapna-zerodha | **0** | **0** | ₹96,000 | 2026-09-05 10:15 IST |
| 2 | Mai-Zerodha | **0** | **0** | ₹1,80,000 | 2026-09-05 10:15 IST |
| 3 | Didi-zerodha | **0** | **0** | ₹90,000 | 2026-09-05 10:15 IST |

The multi-account master switch (`multi_account_settings.enabled`) is still **ON**,
so the fan-out path is armed — it just has no enabled child to mirror to. **Zero
mirror orders will place today.**

I can't tell from the data whether you disabled these deliberately on Saturday
(plausible risk-off after the losing week) or whether it was incidental. **If it
was deliberate, ignore this.** If not, `/accounts` → enable + Connect each child
before 09:16.

Side effect worth a look: `services/account_open15_service._read_child_book` is
still calling `get_positions` for **disabled** children and throwing on every
refresh — 3 × `Incorrect api_key or access_token` at 08:19:17, and **315 of the
same on Saturday**. A disabled account shouldn't be polled. Small issue, but it's
the bulk of your error volume.

### 2. open15 is LIVE, on a −₹32.8k five-session drawdown
`strategy_mode.open15_vol_breakout = live` and `settings.analyze_mode = 0` →
**real orders at 09:16 today.**

Real closed fills, net (gross − modelled charges):

| Session | Trades | Net |
|---------|--------|-----|
| 2026-08-28 | 3 | **+₹11,705** |
| 2026-08-31 | 2 | −₹10,563 |
| 2026-09-01 | 3 | −₹9,016 |
| 2026-09-02 | 3 | −₹8,390 |
| 2026-09-03 | 3 | +₹146 |
| 2026-09-04 | 2 | −₹4,973 |

**Last 5 sessions: −₹32,796.** Cumulative since going live 2026-07-24:
**+₹44,293 over 35 real trades** — i.e. you've given back ~43% of peak.

Good news: the #696 risk controls you merged are **now ON** (config updated
2026-09-04 10:10 IST) and they fired — ANGELONE exited `stop_loss` on 09-04:

```
profit_lock_enabled = 1   profit_target_inr = ₹7,000   trail_giveback_inr = ₹1,500
stop_loss_enabled   = 1   stop_loss_inr     = ₹2,500
max_trades = 3   margin_per_slot = ₹60,000   instrument = atm_option   side = both
```

Nothing to fix — just know the number before 09:16.

### 3. `futures_follow_cap50` — 1 lot open since 2026-08-28, still no exit (P0, ~9th recurrence)
Net across all `placed` rows: **BUY 20 lots vs SELL 19 lots = 1 lot carried.** Oldest
unmatched entry 2026-08-28 — now **10 calendar days on a T+1 strategy.** The
2026-09-03 postmarket review filed this as its only violation
(`futures_follow_cap50/t1_exit_for_carry`, P0), same as every prior review since
~08-14. This is sandbox money, but it is the **#497 shape** and it has not been
fixed. No futures_follow entries at all since 08-28.

### 4. OpenAlgo was DOWN from Friday ~10:11 IST until 08:17 today
`job_run` last fire: **2026-09-04 10:11 IST.** Friday's entire afternoon schedule
never ran — 15:18 smoke check, 15:20 entries, 15:25 exits, 15:30 EOD, 15:45 scanner
comparison, 17:15 postmarket review. Corroborating gaps:

- `postmarket_review` has no row for **2026-09-04** (latest is 09-03)
- `sector_follow_cap5_vol` data-health last check **09-03 16:30 IST**
- `scanner_comparison` last rows **09-03**
- a dashboard SHUTDOWN was logged on 09-05

open15 itself was unaffected (it finishes by 09:30 and did trade Friday), but you
have **no Friday EOD record** for anything else.

### 5. `claude` CLI still logged out (3rd report running)
`postmarket_review.llm_status` on 09-03 = `not_logged_in`. Phase-3 triage has been
dead since 09-01. Fix: run `claude` → `/login`.

---

## 🟢 Healthy this morning

- **Primary broker auto-login worked.** 403 at 08:18:09 → watcher detected dead
  session → `Primary Zerodha account auto-login succeeded` 08:18:39 → master
  contract downloaded → **228/228 scanner symbols + 10/10 regime symbols
  re-subscribed** by 08:19:12. No intervention needed.
- **Daily-D backfill running now** (08:21+, ~40-symbol batches with cooldown). It's
  filling 09-03→09-07, so Friday's daily bars are being fetched. `historify.duckdb`
  main-file mtime reads 09-03 but the **`.wal` was written 08:18 today** — the file
  date is checkpoint lag, not staleness.
- **`dev` is fully merged and pushed** — 0 commits on local `dev` not on
  `origin/dev`.
- **No `job_run` errors or missed fires** in the last 6 days of recorded history.

---

## 🟢 Dispatch tasks — last 24h

Every session in the recent inventory is **idle**. Nothing running, nothing stuck,
nothing errored.

- The recent `Fno scan cycle` runs correctly self-skipped: *"Outside market hours —
  skipping. Current IST time is 09:04 on Saturday, September 5"*.
- Last `Morning status report` (09-04) completed with a clean `Done.` summary.

**Verdict: 0 STUCK, 0 ERRORED, 0 PROGRESSING (none active).**

---

## Git state

**Un-FF'd branches:** essentially none. Of the 12 most recent branches, only one is
ahead of `origin/dev`:

- `fix/684-open15-today-pnl-by-mode` — **1 commit** ahead
  (`460dd7422 [#684] fix(strategies-dashboard): today chips attach to the row's own
  mode; only real fills count`)

⚠️ Honest caveat: enumerating **all** ~120 local branches timed out, so I checked
only the 12 most recently committed. Older `claude/*` and `chore/*` branches are
unverified.

**Working tree — dirty (2 tracked, 88 untracked):**

Tracked modifications:
- `.gitignore`
- `strategies/simplified_engine/LEARNINGS.md`

Untracked, notable buckets:
- `backtest/options_open15/*` — ~18 research scripts (bs.py, july_*.py, iv_history.py…)
- `backtest/inhouse_scanner/r60/`, `backtest/open15_rolling/`, `backtest/open15_missed_days/`
- **9 × `db/openalgo.db.bak.*`** dating back to 2026-07-14 — worth pruning
- `db/.fuse_hidden0000003400000001`

The boot dirty-tree warning fired again at 08:17:58.

**`origin/dev` last 5:**
```
43ee5494f Merge pull request #703 from sonawanedhiraj/feat/702-console-tradebook-fetch
a2a1989d1 Merge pull request #701 from sonawanedhiraj/feat/700-account-pnl-card
0b9f14a93 docs+ui: headless Console fetch in CLAUDE.md/SYSTEM_MAP (#702)
aa9922bbf fix(multi-account): Console tradebook join — order_id is the exchange id (#702)
3a88df21f feat(multi-account): headless Console tradebook fetch per child (#702)
```

---

## OpenAlgo health

| Signal | Value |
|--------|-------|
| `log/openalgo_2026-09-07.log` | 08:21 IST — **live, actively writing** |
| `log/errors.jsonl` | 08:19 IST |
| `db/openalgo.db` / `-wal` | 08:19–08:20 IST |
| `db/sandbox.db` | 08:18 IST |
| `db/historify.duckdb.wal` | 08:18 IST (main file 09-03 = checkpoint lag) |
| `log/*.err.log` | none exist — boot stderr is `openalgo_boot_stderr.log` (08:21) |

**Errors, last 4 hours: 23** — all inside the 08:18–08:19 boot/auth window.

| Count | Logger |
|-------|--------|
| 4 | `broker.zerodha.streaming.zerodha_websocket` (403 handshake, pre-login) |
| 4 | `services.websocket_client` |
| 3 | `broker.zerodha.api.order_api` (`Incorrect api_key or access_token`) |
| 3 | `services.account_open15_service` (child position reads — item #1) |
| 2 | `zerodha_websocket` / 2 `websocket` / 2 `websocket_service` |
| 2 | `connection_pool_zerodha` (adapter connection timeout) |
| 1 | `services.sector_follow_service` |

All the WS/auth errors are the **expected pre-auto-login sequence** and healed by
08:19:12. Only the 3 child-position errors are unresolved.

For context, Saturday (09-05) logged **791** errors — 630 of them the child-account
loop (315 `order_api` + 315 `account_open15_service`), plus ~141
`strategies_dashboard_api: Failed to query strategy_llm_config`. Same root cause as
item #1.

**Boot warnings:** two scheduler jobs reported missed run times (scanner history
refresh 16:00, sector_follow index refresh 16:05) — expected after a weekend
shutdown, both rescheduled for today. `boot_db_probe` flagged a transient
historify lock, did not abort.

---

## Today's schedule (Monday, trading day)

| IST | What |
|-----|------|
| 09:10 | open15 **arm** — freezes the day's config, funds clamp, watch list |
| 09:15 | Market open |
| 09:16 | open15 seed selection → **LIVE entries on primary** (children disabled) |
| 09:16–09:29 | Rolling watch-list additions (on, 30s cadence, top-3/side) |
| 09:30 | open15 hard exit |
| 09:40 | multi-account fill reconcile |
| 15:02–15:10 | sector_follow refresh → smoke → entry 15:05 → exit 15:10 *(sandbox)* |
| 15:18 | futures_follow smoke check |
| 15:20 | futures_follow entry evaluation *(sandbox)* |
| 15:25 / 15:28 | futures_follow T+1 exit / EOD watchdog — **watch for the carried lot** |
| 15:30 | EOD summaries |
| 15:35 | multi-account mirror EOD summary |
| 15:45 | `scanner_comparison_eod` |
| 16:00 | scanner history refresh |
| 17:15 | `postmarket_review` (LLM triage will be `not_logged_in` unless you fix #5) |

---

## ⚠️ Telegram

**Not delivered.** `api.telegram.org` is blocked from the Cowork sandbox —
`URLError: Tunnel connection failed: 403 Forbidden`. Third consecutive report.
Please read this journal in the Cowork app, or allowlist `api.telegram.org` for the
sandbox.

Separately: the app's own Telegram bot is `is_active = 1` with chat id
`1345069591` configured, so OpenAlgo's own alerts should reach you — but Friday's
report noted a `httpx.ConnectError` on bot init. Worth confirming you actually got
Friday's 09:30 open15 summary.

---

*Read-only inventory. No databases written, no git operations, no orders placed.*
