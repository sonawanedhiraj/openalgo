# Morning Status — Friday 2026-08-28 (08:45 IST)

**Dheeraj — nothing is blocking today's open.** OpenAlgo restarted at 08:44 IST, the
primary Zerodha session plus all three child accounts auto-logged in without you, and
the boot backfill convergence is already running.

---

## 🔴 Stuck / Action Required

**None.** No stuck dispatch tasks, no un-FF'd work, no dead broker session.

Two low-priority items worth knowing about, neither needs pre-market action:

1. **`fix/684-open15-today-pnl-by-mode` is 1 commit ahead of `origin/dev`** and its merge
   does not appear in the last 5 `origin/dev` commits (#689, #687, #683 are there; #684's
   is not). Either the PR is still open or the branch carries a stray commit. Worth a
   30-second check when you're at the laptop.
2. **The Cowork↔Claude Code bridge on port 5001 is down.** Every `Fno scan cycle` session
   in the last 24h reported "bridge unreachable — error check skipped". Long-standing;
   scan cycles still complete, they just skip their error-triage step.

---

## 🟢 Dispatch tasks — last 24h

The 30 most recent sessions are **all `Fno scan cycle`** scheduled runs, and **all are
idle** (none running, none errored, none awaiting input). No interactive code/dispatch
sessions were opened in this window.

Sampled three transcripts for verdict:

| Session | Verdict | Latest message |
|---|---|---|
| `local_73aa4bd0…` | **DONE** | "Outside market hours — skipping" (16:47 IST, past the 16:30 cutoff) |
| `local_8fcdbf32…` | **DONE** | "Outside market hours — skipping" (16:32 IST) |
| `local_15d97248…` | **DONE** | Full EOD summary for the simplified engine (see below) |

**Yesterday's EOD (from the 16:17 scan cycle):** simplified engine in sandbox, 4 trades
(cap 6), 1W/3L, **net −₹1,467.27** (gross −₹1,162.60 + ₹304.67 charges). ADANIPOWER
(+₹282.93) the lone winner over a ~3h40m hold; GVT&D, LICHSGFIN and TATAPOWER were fast
stop-outs. All flat at close, 13 armed watches carried, tick log clean (~3.45M ticks,
0 drops).

## ⏳ Dispatch tasks running

**None.**

---

## Git state

**`dev` is exactly in sync with `origin/dev`** — `git rev-list --left-right --count
origin/dev...dev` → `0  0`. Nothing local waiting to be pushed.

**Un-FF'd branches:** of the 10 most recently committed feature branches, **9 are at 0
commits ahead** of `origin/dev` (all merged and safe to delete). The exception:

| Branch | Commits ahead of origin/dev | Last commit |
|---|---|---|
| `fix/684-open15-today-pnl-by-mode` | **1** | 2026-08-27 15:52 |

Recent `origin/dev` history:

```
34052675c Merge PR #689 from fix/688-auto-login-notify-importerror
e008a7dfb [#688] fix(notify): silently-dropped alerts — three sites imported a `notify` that never existed
c6bdd8b31 Merge PR #687 from feat/686-per-mode-realized-metrics
4d2fef1cd [#686] feat(strategies-ui): realized CAGR/Sharpe/MaxDD for open15, futures_follow, intraday_pullback
3f70434be Merge PR #683 from feat/682-open15-waiting-and-holiday-gate
```

**Working tree — read this with a caveat.** `git status` from the Cowork Linux sandbox
reports ~10 tracked files as modified, but `git diff --cached` came back empty and the
diff output is flooded with `CRLF will be replaced by LF` warnings. That signature means
the "modifications" are almost certainly **line-ending artifacts of the sandbox mount
reading a Windows checkout**, not real edits. I could not distinguish real WIP from
artifact from here — verify on the laptop before trusting it.

Files flagged: `.gitignore`, `blueprints/strategies_dashboard_api.py`,
`services/broker_auto_login_{service,watcher}.py`, `services/notification_service.py`,
`services/option_liquidity_service.py`, `strategies/simplified_engine/LEARNINGS.md`, and
their four matching test files.

**97 untracked files** — all research/backtest scratch (`backtest/options_open15/*`,
`backtest/open15_rolling/`, `backtest/inhouse_scanner/r60/`,
`backtest/news_event_study/*`, `backtest/open15_missed_days/`). Expected, not a problem.

A zero-byte `.git/index.lock` exists (timestamped 08:42:02). It was most likely created
by my own `git status` read from the sandbox, which then couldn't unlink it
(`Operation not permitted` on the mount). **If a git command on the laptop complains
about an existing index.lock, delete it** — no git process is holding it.

---

## OpenAlgo health

**Status: UP.** Booted **08:44:47 IST today**. The app was down overnight — yesterday's
log ends at **18:45:49**, so nothing ran between 18:45 and 08:44. Yesterday's 17:15
post-market review did complete (`log/error_digest_2026-08-27.json` written at 17:15:00).

**Broker sessions — all four green, all headless:**

| Account | Result | Time |
|---|---|---|
| Primary (dheeraj) | ✅ auto-login succeeded | 08:45:17 |
| Child 2 — Mai | ✅ auto-login succeeded | 08:45:50 |
| Child 1 — Swapna | ✅ auto-login succeeded | 08:46:20 |
| Child 3 — Didi | ✅ auto-login succeeded | 08:46:49 |

Master contract downloaded in 19s across all 11 exchanges; **228/228 scanner symbols and
10/10 regime symbols pre-subscribed**; scanner daily-D resettle kicked off at 08:45:38
(216 symbols, 2026-08-26..28) and is running now.

**Today's errors: 11 total, all boot-race, all self-resolved.**

- `08:44:55` — Zerodha WS handshake **403 Forbidden**, "Auth/token failure — will not
  retry." This is the normal daily token expiry, logged *before* the auto-login watcher
  got its turn. The watcher then re-logged in at 08:45:17 and the feed came up. **Not an
  action item** — this is the #654 watcher working exactly as designed.
- `08:45:05–08:45:20` — WS client auth/connect failures, same window, same cause.
- `08:46:02 / :03 / :33` — `account_open15_service: child positions read failed` with
  `403 Incorrect api_key or access_token`. These fired *before* children 1 and 3 finished
  logging in (08:46:20 and 08:46:49). A boot-ordering race in the `/accounts` open15
  verification card, cosmetic — but if it recurs after all children are up, it's worth an
  issue.

**Error volume over the captured window** (`errors.jsonl` holds the last 1000 entries,
currently 2026-08-25 20:24 → 2026-08-28 08:45), by logger:

| Count | Logger |
|---|---|
| 243 | `services.open15_breakout_service` |
| 152 | `blueprints.strategies_dashboard_api` |
| 106 | `services.option_symbol_service` |
| 92 | `services.scanner_dry_tripwire_service` |
| 51 | `services.place_options_order_service` |
| 30 | `broker.zerodha.api.funds` |
| 29 | `database.open15_breakout_db` |

That is a ~2.5-day window, so it is not a today problem — but `open15_breakout_service`
at 243 is the loudest thing in the log and probably deserves a look this weekend.

**One thing from yesterday's shutdown worth noting:** at 18:45:49, right before the app
went down, the thread registry flagged **two convergence threads as STALE** —
`ScannerBackfillPeriodic` (no heartbeat for 10,869s ≈ 3h) and
`SectorFollowBackfillPeriodic` (10,854s). Both should have been ticking inside the
15:30–17:00 window. Today's restart resets them, so it isn't blocking, but if the same
STALE pair appears this evening it's a real bug in the periodic loops.

**Data freshness:** `historify.duckdb` last full write 2026-08-27 15:22 (WAL 16:05).
Today's pre-market backfill is *in progress* as of this report, started 08:45:38.

---

## Today's schedule (Friday — trading day)

| IST | What |
|---|---|
| 09:10 | open15_vol_breakout arm (universe, F&O filter, funds clamp) |
| 09:15 | **Market open** |
| 09:16 | open15 seed selection |
| 09:30 | open15 exit (default) |
| 09:40 | multi_account_fill_reconcile |
| 15:02–15:10 | sector_follow_cap5_vol: refresh → smoke → entry 15:05 → exit 15:10 |
| 15:18 | futures_follow_cap50 smoke check |
| 15:20 | futures_follow_cap50 entry evaluation |
| 15:25 | futures_follow_cap50 T+1 exit |
| 15:28 | futures_follow EOD watchdog |
| 15:30 | EOD summaries |
| 15:35 | multi-account mirror EOD summary |
| 15:45 | scanner_comparison_eod |
| 16:00 | scanner history refresh |
| 17:00 | scanner backfill convergence window ends |
| 17:15 | postmarket_review |

---

## ⚠️ Telegram

**Blocked.** `api.telegram.org` is unreachable from the Cowork sandbox
(`Tunnel connection failed: 403 Forbidden`) — same finding as the standup task. No alert
was sent; **please read this journal directly in the Cowork app**. If you want the
morning ping on your phone, `api.telegram.org` needs allowlisting for the sandbox.
