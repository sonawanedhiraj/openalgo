# Morning Status — Monday 2026-07-20 (08:30 IST)

**Headline for Dheeraj: broker re-login already succeeded at 08:24 and the 2 carried NIFTY lots rehydrated correctly, so the day is on track — but `sector_follow_cap5_vol` is still in `live` mode with 4 real-money positions due to exit at 15:25, and the brand-new `open15_vol_breakout` service failed to start at boot.**

---

## 🔴 Stuck / Action Required

### 1. `open15_vol_breakout` failed to initialize at boot — it will NOT run at 09:15 today

```
[2026-07-20 08:23:55,619] ERROR in app:
Failed to initialize Open15 breakout service: cannot pickle '_thread.lock' object
```

This is the strategy that shipped Friday (`[#425] feat(open15_vol_breakout)`, merged as #426).
Today is its first intended trading day. Its 4 APScheduler jobs (arm 09:10 / exit 09:30 /
retry 09:32 / summary 09:35) never registered, so **there will be no 09:15 measurement run**.

The error signature (`cannot pickle '_thread.lock'`) is APScheduler trying to serialize a job
whose bound argument holds a live lock or ZMQ socket — the service has its own additive ZMQ
tick SUB per the design notes. Likely a job registered with the service instance as an argument
against a non-memory jobstore.

**Action:** this needs a code fix, not a restart — two boots today (08:23 and 08:29) both hit it.
No harm to live trading if left broken today; the strategy is a sandbox measurement, not an edge.

### 2. `sector_follow_cap5_vol` is in `live` mode, set by `harness` — not by you

```
sector_follow_cap5_vol | live    | harness   | 2026-06-24 18:55
futures_follow_cap50   | sandbox | harness   | 2026-06-24 18:55
simplified_engine      | sandbox | migration | 2026-06-12
```

Sunday's deploy checklist raised this too. It is real money, and `updated_by='harness'`
means no operator ever explicitly confirmed it. **Four open live CNC positions** from Friday
are riding on it:

| Symbol | Qty | Entry | Entry date | Exit |
|---|---|---|---|---|
| RELIANCE | 37 | 1326.20 | 2026-07-17 | due today 15:25 |
| BAJFINANCE | 47 | 1054.50 | 2026-07-17 | due today 15:25 |
| AXISBANK | 37 | 1330.30 | 2026-07-17 | due today 15:25 |
| JIOFIN | 205 | 242.90 | 2026-07-17 | due today 15:25 |

Plus **2 sandbox NIFTY lots** (`NIFTY28JUL26FUT`, qty 130, entry 24304.00, 2026-07-17).

**Good news — these rehydrated correctly this morning:**
```
[08:23:55] WARNING futures_follow_service: REHYDRATED open sandbox position
NIFTY28JUL26FUT qty=130 lots=2 (entry_date 2026-07-17, T+1 exit due today 2026-07-20)
```

**Action:** decide whether `live` is intended. If yes, no action — just be at the laptop at
15:25. If no, flip the mode row before 15:20.

---

## 🟢 Dispatch tasks complete in last 24h

- **`sunday-deploy-checklist`** — ran 2026-07-19 19:05 IST, completed clean. Verdict: **DONE**.
  Flagged the same two items above, plus 25 scheduler jobs correctly timed, no active runtime
  overrides, Friday data-health 8/8 indices + 30/30 stocks fresh.
  ⚠️ Its transcript notes the **Telegram bot token was decrypted into the session transcript**
  during a delivery attempt. Rotating it via BotFather is cheap and safe. (Do **not** touch
  `API_KEY_PEPPER`.)

- **`fno-scan-cycle`** — last ran Friday 2026-07-17 16:47 IST, exited cleanly ("outside market
  hours — skipping"). Verdict: **DONE**. Next fire today ~09:01 IST. Sampled 3 recent sessions,
  all idle with clean skip messages — no stuck or errored runs.

## ⏳ Dispatch tasks running

**None.** All 30 inspected sessions are `idle`. Zero STUCK, zero ERRORED.

Scheduled-task inventory (9 tasks, 4 enabled): `fno-scan-cycle`, `sunday-deploy-checklist`,
`weekday-trading-standup` (fires 08:48 today), `morning-status-report` (this run).
`stuck-task-watchdog` remains **disabled** since 2026-06-20 — worth re-enabling if you want
mid-day staleness alerts.

---

## Git state

- **Un-FF'd commits: none.** `git rev-list --count origin/dev..dev` = **0**. `dev` is level with `origin/dev`.
- **Working tree: dirty** — 1 modified + 8 untracked, per the app's own boot check at 08:23:44:

```
M  strategies/simplified_engine/LEARNINGS.md      <- WIP, expected
?? .claude/launch.json
?? backtest/inhouse_scanner/                       <- research WIP
?? backtest/news_event_study/analyze_sue_pead.py   <- research WIP
?? backtest/news_event_study/fetch_prices_2020.py  <- research WIP
?? backtest/news_event_study/harvest_results_eps.py<- research WIP
?? backtest/options_open15/                        <- research WIP
?? db/openalgo.db.bak.20260714_175722              <- stray backup, safe to delete
?? db/sandbox.db.bak.20260714_175722               <- stray backup, safe to delete
```

Split: **6 research/WIP files**, **2 stray DB backups from 2026-07-14**, **1 editor config**.
Nothing here blocks the day.

⚠️ **Method caveat, Dheeraj:** `git status --porcelain` **times out at 40s** over the sandbox
mount (the repo is large — `historify.duckdb` alone is 4.3 GB). The file list above is taken
from OpenAlgo's own boot-time dirty-tree warning, not from a live sandbox `git status`. It's
accurate as of 08:23:44 today.

**Recent `origin/dev`:**
```
d4fa3b024 docs(open15): embed look-ahead entry-mistake diagram in R58 research doc
4425ba068 [#425] feat(open15_vol_breakout): sandbox strategy — mid-bar volume-surge breakout (#426)
f42fa79dd docs(strategy): R58 corrected to REJECT — entry-price look-ahead; BS pricing recalibrated (#424)
46d1cf712 [#418] feat(scanner): show hit price (bar close at match time) in signals table (#420)
b29117fc3 docs(strategy-registry): add round 57 (pullback levers on scanner signals — edge found)
```

Branch count is high (~30+ local branches, several already merged: `chore/213-cd-build-split`,
`feat/112-schema-migration`, etc.). Housekeeping opportunity, not urgent.

---

## OpenAlgo health

### Broker session — **recovered, was dead at boot**

The overnight Zerodha token expiry played out exactly as expected and **self-healed**:

| Time | Event |
|---|---|
| 08:23:44 | App boot (dirty-tree warning) |
| 08:23:58 | WS handshake **403 Forbidden** — `Authentication failed` |
| 08:23:58 | `Broker token expired or invalid — empty funds response`; stored auth marked revoked |
| 08:24:03–08:24:13 | 28× `Error fetching margin data` (expected cascade) |
| **08:24:29** | **`Emitted broker_session_refreshed` — re-login succeeded** |
| 08:24:29 | Master contract download started |
| **08:25:17** | Master contract status: **success** |
| 08:25:25 | Master Contract Database Processing Completed |
| 08:28+ | historify daily-D backfill actively downloading (GAIL, GLENMARK, GMRAIRPORT…) |

**So: the session is live and the pre-market backfill is running right now.** No action needed.

### Log & DB freshness

| File | Last write | Read |
|---|---|---|
| `log/errors.jsonl` | 2026-07-20 08:25:12 | fresh; last error is the pre-login cascade |
| `log/openalgo_2026-07-20.log` | 2026-07-20 08:28:28 | **actively writing** |
| `db/historify.duckdb.wal` | 2026-07-20 08:27:36 | **actively writing** — backfill in flight |
| `db/historify.duckdb` | 2026-07-17 13:25 | main file stale-looking, but that's just an un-checkpointed WAL — **not a problem** |
| `db/openalgo.db` | 2026-07-20 08:25:28 | fresh |
| `db/sandbox.db` | 2026-07-20 08:25:25 | fresh |

### Error frequency — last 4 hours (114 errors, 186 in 24h)

| Count | Logger | Nature |
|---|---|---|
| 37 | `broker.zerodha.api.data` | multiquotes / instrument-token — pre-login |
| 36 | `services.history_service` | `Could not find instrument token for NSE_INDEX:NIFTYMETAL` etc. — master contract not yet loaded |
| 28 | `broker.zerodha.api.funds` | `Incorrect api_key or access_token` — pre-login |
| 4 | `services.websocket_client` | WS auth fail — pre-login |
| 8 | zerodha WS / adapter / pool | 403 handshake — pre-login |
| **1** | **`__main__`** | **`Failed to initialize Open15 breakout service`** ← the one real bug |

**Read: ~113 of 114 errors are one causal chain — the expired overnight token before the
08:24 re-login. All of them stopped at 08:25:12.** The genuinely new signal is the single
Open15 line.

### Data health (latest rows, all Friday — today's runs haven't fired yet)

```
sector_follow_cap5_vol   2026-07-17 11:00  ok=1  stale=[]
scanner_universe_D       2026-07-17 10:11  ok=1  stale=[]
scanner_universe_1m      2026-07-17 10:11  ok=1  stale=[]
```

No active `strategy_runtime_override` — the only row is an expired pause from 2026-07-01.

**Two boots today** (08:23:44 and ~08:29:38). If you didn't restart it manually, worth a glance
at why — but both completed rehydration cleanly, so no state was lost.

---

## Today's schedule (Monday — trading day)

| Time IST | Event |
|---|---|
| 09:01 | `fno-scan-cycle` first fire |
| 09:10 | `open15_vol_breakout` arm — ⚠️ **will not fire, service failed to init** |
| 09:15 | **Market open** |
| 09:18 | scanner smoke check |
| 09:30 / 09:32 / 09:35 | open15 exit / retry / summary — ⚠️ **also dead** |
| 15:18 | `sector_follow_cap5_vol` smoke check |
| 15:20 | sector_follow + futures_follow_cap50 entry evaluation |
| **15:25** | **exits — the 4 live CNC positions + 2 sandbox NIFTY lots square off here** |
| 15:28 | futures_follow EOD watchdog |
| 15:30 | EOD summary |
| 15:45 | `scanner_comparison_eod` |
| 16:00 | scanner history refresh |

The 15:25 exit is the load-bearing moment today. Token is healthy as of 08:25, so it should
fire — but that's the window to be watching.

---

## ⚠️ Telegram

**Not attempted this run.** Sunday's checklist established that the Cowork sandbox proxy blocks
`api.telegram.org` (403 tunnel) and that decrypting the bot token exposes it in the transcript.
Rather than repeat a delivery that fails *and* leaks the token, I skipped it deliberately.

**Please read this journal directly in the Cowork app.** To fix delivery long-term, allowlist
`api.telegram.org` for the Cowork sandbox — and rotate the bot token via BotFather first, since
Sunday's run already exposed it.

---

*Read-only run. No DB writes, no git operations, no commits. Generated 2026-07-20 08:30 IST.*
