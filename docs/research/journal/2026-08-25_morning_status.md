# Morning Status — Tuesday 2026-08-25

**For Dheeraj · generated 09:05 IST (task fired late; market opens in ~10 min)**

> **Headline: yesterday every open15 live entry was rejected by Zerodha's expiry-week physical-delivery block, and today is expiry Tuesday — the #669 fix that prevents it merged last night and gets its first live test at 09:16 this morning.**

---

## 🔴 Stuck / Action Required

### 1. open15 (LIVE money) — 4/4 entries rejected yesterday; today is the repeat risk. **WATCH 09:16.**

Every `open15_vol_breakout` entry on 2026-08-24 was refused by the broker with the identical message:

> *"Fresh buy orders are not allowed for stock options using MIS due to compulsory physical delivery. Try next month's expiry."*

| Symbol | Side | Contract | Qty | Outcome |
|---|---|---|---|---|
| BIOCON | S | BIOCON**25AUG26**410PE | 20,000 | rejected → paper (−₹11,000) |
| DIVISLAB | S | DIVISLAB**25AUG26**8500PE | 900 | rejected → paper (−₹675) |
| HEROMOTOCO | S | HEROMOTOCO**25AUG26**5700PE | 1,050 | rejected → paper (₹0) |
| CROMPTON | S | CROMPTON**25AUG26**250PE | 25,800 | rejected, `entry_rejected_paper_cap` (fill=`none`, unpriced) |

**Real fills yesterday: zero.** The #548 paper path caught it correctly — no money was lost, and no position was stranded.

**Why it happened and where it stands:**
- 2026-08-24 was expiry-Tuesday-minus-one. Zerodha blocks fresh stock-option longs on expiry day *and* the day before.
- Issue **#669** was written in response — commit `3ebe0b2ff` at **17:48 IST yesterday, i.e. AFTER the rejections**. It adds `is_expiry_blocked()` in `services/open15_option_shadow.py` and rolls `pick_contract` to next month, stamping `expiry_rolled` / `rolled_from`.
- The fix is on `origin/dev`, present in the working tree, and **OpenAlgo restarted at 08:57 IST today** — so the running process has it.

**Today (2026-08-25) is expiry Tuesday itself, so the block applies again.** This is the fix's first real exercise.

**What to check at ~09:16:** open `/open15_vol_breakout/logs` and confirm the `entry` events carry `expiry_rolled: true` and a **`29SEP26`-style `opt_symbol`, not `25AUG26`**. If you still see AUG contracts, the roll didn't fire and every entry will reject again.

### 2. Broker auto-login had a false start; child position reads were 403ing at 08:59

The #654 headless auto-login ran three times this morning:

| Time | Event |
|---|---|
| 08:57:33 | no live session at boot — auto-login attempted |
| 08:57:46 | **primary Zerodha auto-login succeeded** |
| 08:57:55 | child `Mai-Zerodha` (acct 2) auto-login succeeded |
| 08:58:17 | **`Primary auto-login failed at web-login step: Kite browser login timed out after 30s`** |
| 08:58:51 | primary auto-login succeeded (retry) |
| 08:59:02–03 | `account_open15_service`: *child positions read failed* ×2 — `Incorrect api_key or access_token` (403 on `/portfolio/positions`) |

The end state looks healthy (primary succeeded at 08:58:51 and the historify backfill below is pulling broker data fine as of 09:01), but the **last thing in the error log is a 403 on a child position read.** Only account 2 (`Mai-Zerodha`, ₹1.8L) is enabled; accounts 1 and 3 are disabled.

**Worth 30 seconds:** load `/accounts` and confirm the child shows connected before 09:10. A dead child token means open15 mirrors silently won't place.

Also: `services.broker_auto_login_service :: Failed to send auto-login summary notification` — the Telegram send from the host failed too, so the auto-login summary you'd normally get on your phone did not arrive.

### 3. OpenAlgo was down from ~15:35 IST Monday until 08:57 today — two post-close jobs never fired

`job_run` for 2026-08-24 stops at 15:35 IST (`trading_day_funnel`). Everything after that is missing:

- `sector_follow_data_health` (16:30) — never ran
- `postmarket_review` (17:15) — never ran

`postmarket_review` now has **no row for 2026-08-21 or 2026-08-24** (last is 2026-08-20). This is the **second consecutive occurrence** — Friday's standup flagged exactly the same shape for 08-21. It is a pattern, not a one-off, and it deserves an issue.

Read-only replays when you have a moment:
```bash
uv run python -m services.postmarket_review_service --date 2026-08-21 --dry-run
uv run python -m services.postmarket_review_service --date 2026-08-24 --dry-run
```

**Knock-on effect (self-healing right now):** the 15:30–17:00 backfill convergence also never ran Monday, so `historify.duckdb` was last written **2026-08-24 14:33 IST** and is missing Monday's settled daily bars. The boot-time convergence check kicked in this morning and is downloading `NSE:D` for the full scanner universe as of 09:01 (~2 s/symbol + batch cooldowns across ~200 names, so ~10 min). It should land before 09:15, but if the scanner behaves oddly early on, stale `yest_d` is the first thing to suspect.

---

## 🟢 Dispatch tasks complete in last 24h

| Session | Verdict | Note |
|---|---|---|
| **Fno scan cycle** (`local_9b9f73db`) | **DONE** | EOD summary for 08-24 delivered. Simplified engine sandbox: 6 trades (cap hit), 0 open at close, **net +₹150.88**, 66.7% win rate. Longs +₹533.84 (3/3), shorts −₹382.96 — a reversal of the usual SHORT edge, flagged for you. Tick log 3.72 M ticks / 298 MB, 0 drops. No code touched, no restart. |
| **Weekday trading standup** (`local_29b5b8b3`) | **DONE** | 08:45 standup written. Flagged the missing 08-21 postmarket review, confirmed no live-code drift, confirmed Telegram is blocked from the sandbox. |

The remaining ~25 `Fno scan cycle` sessions in the list are the normal intraday cadence, all idle and none carrying an error result.

## ⏳ Dispatch tasks running

| Session | Status | Note |
|---|---|---|
| **Weekday trading standup** (`local_2777d334`) | **PROGRESSING** | 2 turns, started moments ago, doing read-only repo / freshness / scheduler checks. Not stuck. |

**No STUCK sessions. No ERRORED sessions.**

---

## Git state

**`dev` is clean and in sync — `origin/dev..dev` is empty.** No local commits waiting to push.

Recent `origin/dev`:

```
fc9812d8e  Merge PR #672 from feat/671-open15-ladder-preview
ba03866e1  [#671] feat(open15): next-arm planning ladder on /logs
e763dc6be  Merge PR #670 from fix/669-open15-expiry-week-roll
b2de18efd  [#669] docs: CLAUDE.md expiry-week-roll block + open15 LEARNINGS
3ebe0b2ff  [#669] fix(open15): roll option expiry past Zerodha's block window
```

**Working tree: 94 entries, none of them live code.**

- **2 tracked modified:** `.gitignore`, `strategies/simplified_engine/LEARNINGS.md`
- **92 untracked:** all scratch — `backtest/options_open15/*`, `backtest/inhouse_scanner/r60/`, `backtest/open15_rolling/`, `backtest/news_event_study/*`, `audit/open15_replay_removal_backup_*.json`, `.claude/launch.json`
- **Nothing untracked under `services/`, `blueprints/`, `database/`, `restx_api/`, or `app.py`** — verified. No drift in live code.

~29 stale feature branches remain (`chore/*`, `claude/*`, `docs/364-*`). Not urgent, but a cleanup pass would help. *(I could not enumerate per-branch un-FF'd commit counts — the loop over 30 branches on the mounted network path timed out. `dev` itself being in sync is the check that matters this morning.)*

---

## OpenAlgo health

| Signal | Value | Read |
|---|---|---|
| `log/openalgo_2026-08-25.log` | writing live (09:01+) | 🟢 app up, boot ~08:57 |
| `log/errors.jsonl` | last write 08:59:03 | 🟢 no errors in the last 5 min |
| `db/openalgo.db` | 08:58 | 🟢 |
| `db/sandbox.db` | 08:58 | 🟢 |
| `db/historify.duckdb` | **2026-08-24 14:33** | 🟡 stale — backfill catching up now |

**Errors today: 17 total, all in the 08:57–08:59 boot window, zero since.** Frequency table:

| Count | Source | Message |
|---|---|---|
| 2 | `services.websocket_client` | Failed to authenticate with WebSocket server |
| 2 | `services.websocket_service` | Connection error for user dheeraj.sonawane |
| 2 | `broker.zerodha.api.order_api` | Incorrect `api_key` or `access_token` |
| 2 | `services.account_open15_service` | child positions read failed (broker=zerodha) |
| 3 | zerodha websocket / `websocket` | Handshake status 403 Forbidden (03:27 GMT) |
| 1 | `connection_pool_zerodha` | Adapter connection failed: Connection timeout |
| 1 | `services.broker_auto_login_service` | Primary auto-login failed at web-login step (timeout 30s) |
| 1 | `services.broker_auto_login_service` | Failed to send auto-login summary notification |
| 3 | `services.websocket_client` | misc (Connection timeout / No broker configuration found) |

All of these are the boot-sequence race before the 08:58:51 successful login — **except** the two `account_open15_service` child-position failures, which are the *last* entries written and are item #2 above.

**Strategy modes** (unchanged): `open15_vol_breakout` = **live** · `simplified_engine`, `sector_follow_cap5_vol`, `futures_follow_cap50` = sandbox.

**Active runtime overrides: none.** The three `pause` rows all expired 2026-08-12.

**Job errors in the last 7 days: zero** — every `job_run` row is `ok`. The problem isn't jobs failing, it's jobs never firing because the app is down.

**open15 config** (unchanged since 08-19): `atm_option`, `max_trades` 3, `margin_per_slot` ₹60,000, entry cutoff 09:29, exit 09:30, both sides, rolling watch-list ON (30 s / top 3), shadow ON (max 3), OI floor 500 lots, residual sizing ON, impact gate ON at 2.0%.

---

## Today's schedule (Tuesday — trading day, **August monthly expiry**)

| IST | Event |
|---|---|
| **09:10** | open15 arm — **⚠ watch for the expiry roll** |
| **09:15** | Market open |
| 09:16 | open15 seed ranking + first triggers |
| 09:29 | open15 entry cutoff |
| 09:30 | open15 flatten |
| 15:02 / 15:03 | sector_follow pre-entry refresh / smoke check |
| 15:05 | sector_follow entry |
| 15:10 | sector_follow exit |
| 15:18 / 15:20 | futures_follow smoke check / entry |
| 15:25 / 15:28 | futures_follow exit / EOD watchdog |
| 15:30 | EOD summaries |
| 15:45 | `scanner_comparison_eod` |
| 16:30 | `sector_follow_data_health` |
| 17:15 | `postmarket_review` |

**Please leave OpenAlgo running past 17:15 today.** That single habit fixes item #3 — the 16:30 and 17:15 jobs, and the 15:30–17:00 backfill convergence that keeps `historify.duckdb` current for tomorrow morning.

---

## ⚠️ Telegram not sent

`api.telegram.org` is blocked by the Cowork sandbox network allowlist, and the host's ports 5000/5001 are unreachable from here, so there is no send path. The `bot_config` token is Fernet-encrypted and can't be decrypted in this environment either.

This is now the **third consecutive day** the morning/standup Telegram has failed, and separately the host's own `broker_auto_login_service` failed to send its summary at 08:57 — so the phone channel appears to be down on *both* paths. **Please open this journal directly in Cowork.** Worth an issue: either allowlist `api.telegram.org` for the sandbox, or expose a host-side send endpoint.

---

## Notes on completeness

Things I could not verify, stated plainly rather than guessed:

- **Live `/preflight` and engine status** — the sandbox cannot reach `localhost:5000`. Everything above is from log mtimes, log contents, and read-only DB copies.
- **Per-branch un-FF'd counts** — the enumeration timed out on the network mount (see Git state).
- **Direct reads of `db/openalgo.db` returned `disk I/O error`** while the live app held it; I worked from a snapshot copy instead. Every DB access in this run was read-only. No git operations, no commits, nothing written outside this journal.
