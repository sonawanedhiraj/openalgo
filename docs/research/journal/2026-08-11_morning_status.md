# Morning Status — 2026-08-11 (Tuesday)

**Dheeraj — headline:** OpenAlgo is up and running, but the master-contract token map has a gap this morning — ~15 major F&O names (HDFCBANK, APOLLOHOSP, HDFCLIFE, ASHOKLEY…) can't resolve instrument tokens, which spiked the error count and tripped the Fno scan-cycle preflight gate at 11:10 IST, so the scan did not run.

_Report generated 11:16 IST (scheduled 08:00; this run fired late)._

---

## 🔴 Stuck / Action Required

**1. Master-contract instrument-token gap → scan cycle blocked (ACT BEFORE NEXT SCAN)**
- At 11:07 IST, `broker.zerodha.api.data` + `services.history_service` logged **~30 "Could not find instrument token for NSE:<SYM>"** errors for large liquid names: `HDFCBANK`, `APOLLOHOSP`, `HDFCLIFE`, `ASHOKLEY` (and others). These symbols obviously exist — this is a **master-contract load/mapping failure**, not delistings.
- Consequence: the 11:10 IST **Fno scan cycle aborted at preflight** — `recent_errors` check saw 11 effective errors in 30 min (raw 31 across 3 signatures), over the threshold of 10. The gate did its job (trace recorded → `scan_cycle` row 11894, `aborted_preflight`), but **no scan ran**.
- **What to check:** did the master contract download/refresh complete this morning? Re-trigger the symbol-master load (or restart) so token mapping is populated; the error rate should fall back under threshold and the next cycle will proceed.

**2. `historify.duckdb` last written 2026-08-07 (Fri) — Monday Aug 10 appears not to have written**
- Last write **2026-08-07 14:32 IST**. Aug 8–9 were weekend, but **Aug 10 (Mon) was a trading day** and shows no duckdb write. Likely the same root cause as #1 (token/backfill can't fetch → nothing to write).
- Caveat: mount mtimes can lag Windows-side writes / WAL — verify against the app's own backfill health before treating as definitive. But combined with #1, treat as a real pre-market data-freshness concern.

---

## 🟢 Dispatch tasks complete in last 24h

- No discrete "task" completions to report. The recurring **Fno scan cycle** dispatch sessions all ran and self-terminated correctly:
  - **11:10 IST today** — aborted at preflight (see #1 above). *Working-as-designed*, not a failure.
  - Older idle sessions in the list are **previous-day** runs (e.g. one shows a 16:47 IST "outside market hours — skipping" exit) — normal scheduled behaviour, nothing outstanding.

## ⏳ Dispatch tasks running

- **"Weekday trading standup"** (`local_07fa70c3…`) — **running / progressing** (18 assistant turns, mid-bash). This is today's standup task executing concurrently. No action needed; will finish on its own.

---

## Git state

- **Working tree (in the mounted repo):**
  - 2 WIP modified: `.gitignore`, `strategies/simplified_engine/LEARNINGS.md`
  - Many untracked scratch files under `backtest/` (options_open15, inhouse_scanner, news_event_study, open15_rolling) + 2 `db/openalgo.db.bak.*` snapshots — research scratch, not deliverables.
  - `git status` printed a harmless `unable to unlink .git/index.lock: Operation not permitted` — **sandbox read-only artifact**, not a real lock.
- **origin/dev — last 5:**
  - `572cbf6cf` [#585] fix(open15): config UI showed a ticked liquidity gate the engine had switched off (#586)
  - `db44e6873` [#583] feat(open15): per-side option-liquidity score + gates (Gate 1 ships OFF)
  - `4c266eb42` [#581] feat(open15): shadow-log the excluded side
  - `eedd13469` [#579] fix(strategies-ui): report NET P&L
  - `97829d931` [#575][#576] fix(boot): create strategy_mode_audit at boot
- **Un-FF'd branches:** nothing local is ahead on `dev` itself. ~30 stale feature/chore/claude branches sit 1–4 commits ahead of `origin/dev` (largest: `feat/305-reference-data-contract` +4, `feat/231-source-divergence-alerts` +3). **318 local branches total** — a cleanup candidate, but none are today's concern.
  - _(Note: git rev-list is pathologically slow on this mount; ~7 older branches couldn't be counted before timeout. The ahead-counts above are best-effort.)_

## OpenAlgo health

- **App is LIVE** — `log/errors.jsonl` last write **11:13:21 IST** (~2 min before this report).
- **Errors last 4h: 32**, all clustered in the 11:00 IST hour:
  - `broker.zerodha.api.data` — 15 (token-not-found, see #1)
  - `services.history_service` — 15 (same root cause; downstream of the token errors)
  - `database.health_db` — 1 (`sqlite3.OperationalError: database is locked` — known transient, benign)
  - `services.scanner_dry_tripwire_service` — 1
- No `.err.log` matches; latest restart log is `restart_583b_144817.out.err` @ **2026-08-09 14:59** (Sunday maintenance restart, expected).

## Today's schedule (weekday)

- 09:15 — market open ✅ (already open)
- 15:18 — sector_follow_cap5_vol smoke check
- 15:20 — sector_follow + futures_follow_cap50 entry evaluation
- 15:25 — exits
- 15:30 — EOD summary
- 15:45 — scanner_comparison_eod
- 16:00 — scanner_history_refresh

---

## Delivery

⚠️ **Telegram not sent** — `api.telegram.org` is **unreachable from the Cowork sandbox** (HTTP 000, confirmed this run), and the bot token is Fernet-encrypted (needs `APP_KEY`). **Please read this journal directly in the Cowork app.** To get Telegram morning alerts, allowlist `api.telegram.org` for the sandbox.
