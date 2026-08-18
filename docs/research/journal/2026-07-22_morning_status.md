# Morning Status — Wednesday, 2026-07-22 (08:46 IST)

**Dheeraj — headline:** The Zerodha token is dead again (daily ~3 AM expiry) and 47 auth errors have already fired this morning — **re-login to Zerodha before the 09:15 open** or the feed, backfill, and both 15:20 strategies will run blind.

---

## 🔴 Stuck / Action Required

1. **Zerodha token expired — RE-LOGIN NEEDED before 09:15.** (Priority 1)
   - `errors.jsonl` shows **47 errors in the last ~7 min** (08:39–08:40), all one root cause: `Incorrect api_key or access_token` / `authorization value should atleast be…`.
   - Breakdown: 28× `broker.zerodha.api.funds` (margin poller), 4× websocket_client, 4× zerodha data, 2× WebSocket `403 Forbidden`, plus `Auth/token failure detected — will not retry. Refresh token`.
   - This is the routine daily-token expiry. Fix: log in to Zerodha (Kite) via the OpenAlgo broker-auth page. Must be done manually — the Chrome extension can't reach kite.zerodha.com.
   - **Same condition took down yesterday's 09:32 scan cycle** (session `9164ca78`): 1020 errors, cycle correctly aborted at the preflight error-rate gate and logged audit trace `scan_cycle #5775`. So this has been the blocker two mornings running.

2. **Two scheduled reports ERRORED (infra, not code) — no output produced.**
   - `Morning status report` (session `f91b3de6`) → `API Error: Stream idle timeout - no chunks received`.
   - `Weekday trading standup` (session `e0c6834f`) → same stream-idle timeout.
   - These are harness/stream flakiness, not something in the repo to fix. Flagging only so you know those briefs didn't generate. Nothing actionable beyond a re-run if you want them.

---

## 🟢 Dispatch tasks complete / benign in last 24h

- **Fno scan cycle** (running, `e1be6579`) — idle, "Outside market hours — skipping" (08:40, before 09:30 gate). Benign.
- **Fno scan cycle** `9164ca78` — yesterday 09:32, **DONE**: aborted at preflight on the dead-token error storm, no scan/POST, audit trace recorded. Correct behavior.
- **Fno scan cycle** `cf1f27e0`, `06b7e670` — benign "outside market hours" skips (Jul 21 ~09:10–09:17).
- Remaining ~24 sessions in the list are older idle "Fno scan cycle" runs — no stuck/errored states among the ones sampled.

## ⏳ Dispatch tasks running

- Only `e1be6579` "Fno scan cycle" shows **running**, and it has already resolved to a benign pre-market skip. Nothing genuinely in-flight.

---

## Git state

- **`dev` is in sync with `origin/dev`** — 0 un-FF'd commits on dev.
- **Working tree: clean of tracked edits.** 32 entries, all untracked (`??`) scratch: journal `.md` files (Jul 08–21), `backtest/` scratch dirs, two `db/*.bak.20260714` backups, rotated `log/*.log.*` files. No WIP tracked modifications.
- **Stale local branches:** most local `feat/*`, `chore/*` branches show `[gone]` upstreams (their remote was deleted after merge) — safe to prune (`git branch --merged dev` / `git fetch --prune`). `chore/remove-deprecated-fno-rules` is `[behind 327]` (stale, behind dev). None carry un-merged work that needs FF'ing into dev.
- **origin/dev recent history:**
  - `5ee162d4b` [#430] fix(open15): Sandbox badge + UI-reachable decision log/settings + mode-toggle wiring (#431)
  - `d5af68349` [#428] fix(open15): module-level scheduler callables — unpicklable jobs killed first session (#429)
  - `7bafdad7b` [#425] fix(strategies-dashboard): guard null parity_target in strategy detail
  - `d4fa3b024` docs(open15): embed look-ahead entry-mistake diagram in R58 research doc
  - `4425ba068` [#425] feat(open15_vol_breakout): sandbox strategy — mid-bar volume-surge breakout (#426)

---

## OpenAlgo health (log mtimes as proxy — sandbox can't hit /preflight)

| Signal | Last write | Read |
| --- | --- | --- |
| `log/errors.jsonl` | 2026-07-22 08:40:41 | App **alive & active** this morning |
| `log/openalgo_2026-07-22.log` | 2026-07-22 08:45 (143 KB) | Logging live now |
| `db/historify.duckdb` | **2026-07-20 14:31** | ⚠️ **Not written yesterday or today** |
| `*.err.log` | none present | N/A (dev server, expected) |

- **Error rate (last 4h):** 47 errors, all in the 08:39–08:40 burst — 100% Zerodha auth/token failures (see Action item 1). No other error classes.
- **⚠️ historify.duckdb stale since Jul 20 14:31.** No successful historical backfill in ~2 days. This is very likely a *downstream* symptom of the dead broker token — backfill convergence needs a live Zerodha session, which has been failing. Should self-heal once you re-login (boot/periodic convergence will catch up). Worth a glance at the strategies dashboard data-health card after login to confirm the 30-stock + index feeds refresh.

---

## Today's expected schedule (Wed, trading day)

- **09:15 IST** — market open
- **15:18 IST** — sector_follow_cap5_vol smoke check (auto-pauses 15:20 entries if the feed is degraded)
- **15:20 IST** — sector_follow + futures_follow_cap50 entry evaluation
- **15:25 IST** — exits
- **15:30 IST** — EOD summary
- **15:45 IST** — scanner_comparison_eod
- **16:00 IST** — scanner history refresh

The 15:18 smoke check and both 15:20 strategies depend on a live broker session + fresh feed — all of which hinge on the Zerodha re-login above.

---

## Delivery

⚠️ **Telegram blocked** — `api.telegram.org` does not resolve from the Cowork sandbox (`gaierror: Temporary failure in name resolution`), same as prior runs. No alert was sent. Please open this journal directly in the Cowork app. To enable Telegram delivery, `api.telegram.org` would need to be allowlisted for the sandbox.

*Report is read-only: no DB writes, no git ops, no commits.*
