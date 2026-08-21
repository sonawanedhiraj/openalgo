# Morning Status — Monday, 2026-08-03 (08:00 IST)

**Dheeraj — headline:** OpenAlgo is up and no dispatch task is stuck, but **two strategies are set to LIVE for today** and the thread-watchdog's alert path is throwing every fire (32× today) — one config confirmation + one code bug to note before open.

---

## 🔴 Stuck / Action Required

Nothing is *stuck*, but three things want your eyes before 09:15:

1. **Two strategies route to the REAL broker book today** (carried over from Sunday's deploy checklist, unverified):
   - `sector_follow_cap5_vol` = **live** (set 06-24 by `harness`)
   - `open15_vol_breakout` = **live** (set 07-24 by `ui:dheeraj`)
   - `simplified_engine` and `futures_follow_cap50` = sandbox ✅
   - → If either live mode is stale/unintended, flip it on `/strategies` **before 09:15**.

2. **Thread-watchdog alerting is broken (code bug, low urgency but real).** Every anomaly the watchdog detects fails to notify — `services/thread_watchdog_service.py:77` calls `get_notification_service().publish_anomaly_alert(...)`, but the method is named **`publish_anomaly`** (`AttributeError`). Fired **32 times today** (00:13 → 08:02, ~every 15 min). Net effect: the watchdog is silently swallowing whatever thread anomaly it keeps tripping on. Worth a one-line fix (rename the call) — and worth asking *what* thread it's flagging, since that message is currently masked.

3. **Zerodha re-login + boot data-convergence.** Broker token expired ~3 AM (daily). `historify.duckdb` was last written Fri 15:30 (weekend gap, expected). Today's boot/pre-market backfill has **not run yet** — it fires after you re-login. Confirm data-health convergence is green after login.

---

## 🟢 Dispatch tasks complete in last 24h

- **"Sunday deploy checklist"** (idle) — **DONE.** Ran Sunday evening. Produced the Monday-open checklist (source of items 1 & 3 above). Its own caveat: **Telegram delivery failed** — this sandbox has no outbound path to `api.telegram.org` (403), so the checklist was never pushed to your phone. Nothing written to the repo.

No other session had activity in the last 24 hours.

## ⏳ Dispatch tasks running

None. All 30 most-recent sessions are **idle**. The "Fno scan cycle" sessions below the deploy checklist all ran **Friday** (last content "Friday 16:47 — outside market hours, skipping") — none are in the 24h window, none stuck, none errored.

- **STUCK:** 0
- **ERRORED:** 0
- **RUNNING:** 0

---

## Git state

- **Local `dev` vs `origin/dev`:** in sync — `origin/dev..dev` is empty (no un-pushed local commits).
- **Working tree:** **clean** (`git status --porcelain` empty). No WIP files, no stray edits.
- **Branches:** ~40+ feature/chore/claude branches exist; several are active **worktrees** (`+` prefix): `chore/211-cd-trial-ubuntu-latest`, `feat/113-api-endpoints`, `feat/114-rule-params`, `feat/115-frontend-clone`, `feat/116-frontend-delete-tests`, `feat/275-llm-mode-ui`. None are ahead of `dev` in a way that blocks today.
- **origin/dev — last 5:**
  - `24466de3e` [#502] fix(open15): source 09:15 candle from broker, drop from vol baseline (#505)
  - `33bf36a79` [#503] feat(open15): UI-configurable trade side (#504)
  - `36a74e088` @ (#499)
  - `094191f9f` [#496] feat(accounts): capital-per-trade mirror sizing (#498)
  - `3ba88925f` [#492] fix(accounts): TOTP copy button + child-credential guard (#493)

---

## OpenAlgo health

| Signal | Value | Read |
| --- | --- | --- |
| `log/errors.jsonl` last write | **2026-08-03 08:02:30** | App **is running** now |
| `log/openalgo_*.err.log` | not present | (no separate err log file) |
| `db/historify.duckdb` last write | **2026-07-31 15:30** (Fri) | Weekend gap; today's convergence not yet run |
| Errors last 4h | **16** | 100% `thread_watchdog: notification failed` |
| Errors today (total) | **32** | Same single cause (the item-2 bug) |

Error rate is otherwise quiet — the only error class today is the watchdog notification `AttributeError`. No feed, order, or DB errors in the file today.

---

## Today's schedule (Monday — trading day)

- **09:15 IST** — market open
- **15:17/15:18 IST** — `sector_follow_cap5_vol` pre-entry + smoke check
- **15:20 IST** — `sector_follow` + `futures_follow_cap50` entry evaluation
- **15:25 IST** — exits (T+1)
- **15:30 IST** — EOD summary
- **15:45 IST** — `scanner_comparison_eod`
- **16:00 IST** — scanner history refresh
- **16:30 IST** — `sector_follow` data-health check

---

## ⚠️ Telegram delivery

**Blocked.** The Cowork sandbox cannot reach `api.telegram.org` (tunnel 403 Forbidden), same as the Sunday checklist run. The bot token/chat-id are configured and the legacy bot `is_active=1` (so the *running app* can still push its own alerts) — but this scheduled task can't. **Please open this journal directly in the Cowork app.** To get morning alerts on your phone, either allowlist `api.telegram.org` for the sandbox, or have the running OpenAlgo instance emit this via its own `notification_service`.
