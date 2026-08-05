# Parameter Log

Canonical history of tunable parameters across the system. Every parameter change
(env var, DB row, config flag, threshold default) MUST get an entry here in the
same commit that makes the change.

**This file lives on `dev` and is updated by direct commits — never via feature
branches.** This guarantees every fresh branch and every spawned task inherits
the latest decisions automatically.

## How to use this file

- **Before changing any parameter:** add the entry here in the same commit
- **Before any parameter-dependent work:** read this file AND verify against `.env`
  (or the DB row, or wherever the parameter lives). The doc records intent; the
  live source records reality. Mismatches are real and must be resolved.
- **Spawned tasks:** include "read PARAMETER_LOG before parameter work" in the brief

## Active parameters

### Scheduler + daemon-thread registry (issue #539, added 2026-08-03)

Tunables introduced with Phase 1 of the scheduler/thread inventory. All are code
defaults — none is set in `.env`, so the shipped behaviour is the default. The
registries themselves are **read-only**: these flags govern *alerting*, never
whether a job or thread runs.

#### THREAD_REGISTRY_ENABLED
- **Value:** code default `true`. Consulted **per check**, inside
  `thread_registry.check_and_alert()`, so a flip takes effect on the next tick
  of the thread watchdog's 30 s loop without a restart.
- **What it gates:** only the Telegram/alert publish for a stale or dead thread.
  `snapshot()` and `GET /admin/api/schedulers` keep working when it is `false` —
  the page must never go blank because alerting was silenced.

#### THREAD_HEARTBEAT_STALE_MULTIPLIER
- **Value:** code default `3.0`. Values `<= 1` fall back to `3.0` (a multiplier
  of 1 would flag a loop stale the instant it is one tick late, which is normal
  jitter, not a fault).
- **What it does:** a loop thread is `stale` once its last heartbeat is older
  than `cadence_sec * multiplier`. At the default that is 90 s for a 30 s
  watchdog and 90 min for the 30 min backfill convergence loop.
- **Why a multiplier and not a fixed deadline:** cadences in the catalog span
  5 s to 30 min. One absolute deadline would either spam on the slow loops or
  never fire on the fast ones.

#### THREAD_REGISTRY_ALERT_DEDUP_MIN
- **Value:** code default `30` (minutes), per thread.
- **What it does:** a thread that stays wedged re-alerts at most once per window
  — a reminder rather than a storm. Same policy shape as
  `THREAD_WATCHDOG_DEDUP_WINDOW_MIN`.

#### NOTIFY_THREAD_REGISTRY
- **Value:** per-event Telegram toggle consumed by
  `notification_service.notify("thread_registry", ...)`. Unregistered event
  types fall through the `NOTIFY_UNKNOWN_EVENTS` fail-open path, so the alert is
  delivered either way; register it to control it explicitly.

**Not a parameter, but load-bearing:** alerts fire *only* for threads that beat
at least once and then went silent or vanished. A thread that never started is
reported as `not_started` and never alerts, because that is the normal state for
most catalog entries on a normal install (no broker session, outside the window,
flag off, bot not configured). Widening this is how the channel becomes noise.

### Daily post-market review + job-run audit (issue #511, added 2026-08-02)

New tunables introduced with Phase 1 of the post-market review scheduler. All are
code defaults — none is set in `.env`, so the shipped behaviour is the default.

#### POSTMARKET_REVIEW_ENABLED
- **Value:** code default `true`. Per-fire gate, read **inside** the job body, so
  flipping it needs no re-registration.
- **What it gates:** whether the 17:15 IST `postmarket_review` job does anything.
  `false` makes it a logged no-op; the job stays registered.

#### POSTMARKET_REVIEW_TIME
- **Value:** code default `17:15` (IST, `HH:MM`, mon-fri). Malformed values fall
  back to `17:15`.
- **Why 17:15 and not "right after the close":** the deterministic EOD chain runs
  15:35 (trading-day funnel) → 15:45 (scanner comparison) → 16:00 (historify) →
  16:30 (sector_follow data health), and the scanner backfill **convergence loop
  runs until 17:00** (`SCANNER_BACKFILL_PERIODIC_END_TIME`). Reviewing before
  that window closes reads half-written data and reports phantom staleness.
  **If `SCANNER_BACKFILL_PERIODIC_END_TIME` is moved later, move this too.**

#### NOTIFY_POSTMARKET_REVIEW
- **Value:** code default `true` (still subject to the master
  `NOTIFY_TELEGRAM_ENABLED`).
- **What it gates:** the daily Telegram summary. Default ON because a silent
  review is indistinguishable from a review that never ran — precisely the
  failure mode that hid `journal_reflection`'s dead schedule for two months.

#### JOB_RUN_AUDIT_ENABLED
- **Value:** code default `true`. Per-fire gate read inside each listener handler.
- **What it gates:** whether APScheduler fires are recorded to `job_run`. Off
  means the review's job section reports zero recorded jobs — which looks
  identical to "no jobs fired", so leave it on unless write volume is actually a
  problem.

#### JOB_RUN_RETENTION_DAYS
- **Value:** code default `90` (IST days).
- **What it does:** `job_run` rows older than this are pruned by the
  `postmarket_review` job. Without pruning the table grows on every scheduler
  tick forever. Lower it only if the table is measurably large — Phase 2's
  expectation contracts want enough history to spot a job that *stopped* firing.

### Post-market expectation contracts (issue #532, added 2026-08-03)

#### POSTMARKET_CONTRACTS_ENABLED
- **Value:** code default `true`. Read at evaluation time.
- **What it gates:** whether the post-market review evaluates expectation
  contracts at all. `false` keeps the digest and the Telegram summary, but drops
  the verdict section — the report degrades to Phase 1 behaviour.

#### POSTMARKET_CONTRACTS_DISABLED
- **Value:** code default empty. Comma-separated `contract_id` or
  `strategy:contract_id` (e.g. `futures_follow_cap50:t1_exit_for_carry`).
- **What it does:** silences individual contracts **surgically** — every other
  contract keeps evaluating. Exists so a rule that starts crying wolf can be
  muted the same day without a code change and without disabling the layer.
  A contract silenced here should get an issue to fix or delete it; a permanently
  disabled contract is worse than no contract, because the report still looks
  complete.

### Post-market investigating agent + issue filing (issue #536, added 2026-08-03)

#### POSTMARKET_FILING_MODE
- **Value:** code default **`dry_run`**. Only the literal `live` enables writes.
- **What it does:** whether confirmed findings actually become GitHub issues.
  Default-deny on purpose: the first week should show what it *would* have filed
  while the findings are still being tuned. An automated reporter that files
  wrong issues gets muted, and a muted reporter is worse than none.

#### POSTMARKET_MAX_ISSUES_PER_DAY
- **Value:** code default `3`.
- **What it does:** caps issues created per run. Overflow is appended to
  `audit/proposed_fixes.jsonl` and named in the result — the cap bounds issue
  noise, never the record.

#### POSTMARKET_INVESTIGATION_ENABLED
- **Value:** code default `true`.
- **What it gates:** the tool-enabled investigating agent. When `false` the review
  falls back to the #534 no-tools triage, which still produces a day assessment.

#### POSTMARKET_INVESTIGATION_TIMEOUT_SECONDS
- **Value:** code default `600`.
- **Why so much larger than triage's 240:** tool use means several model turns
  (Grep, then Read, then reason). Runs on a real OS thread and is killed at this
  budget, so a hang cannot wedge the scheduler.

#### POSTMARKET_REPO_ROOT
- **Value:** code default = the repo containing `services/`.
- **What it does:** the directory the agent's read-only tools are rooted at.

#### GH_CMD
- **Value:** code default `gh` (resolved on PATH).
- **Note:** auth is the operator's ambient `gh auth` (keyring on this host);
  `GH_TOKEN` is honoured if set. **If this install ever moves to a service
  account or Docker, the keyring is unavailable and filing goes dark** — set
  `GH_TOKEN` there.

**Security note (not tunable, deliberately):** the agent may use only `Read`,
`Grep`, `Glob`; `Bash`/`Write`/`Edit`/`WebFetch` are denied; and `.env*`, `db/`,
`.git/`, `*.key`, `*.pem` are unreadable via the CLI's own deny rules. `.env`
holds `API_KEY_PEPPER` and `FERNET_SALT`, and every encrypted secret in
`openalgo.db` is sealed against them. Everything the agent writes also passes a
`detect-secrets` gate before it can reach GitHub, and that gate **fails closed**
if the scanner cannot run.

### Post-market LLM triage (issue #534, added 2026-08-03)

#### POSTMARKET_TRIAGE_ENABLED
- **Value:** code default `true`.
- **What it gates:** the `claude -p` triage pass over the Phase 2 violations.
  `false` keeps the deterministic report intact and skips the LLM entirely.

#### POSTMARKET_TRIAGE_ON_CLEAN_DAYS
- **Value:** code default **`false`**.
- **What it does:** whether to spend an LLM call on a day with zero violations.
  Off by default because with nothing proven broken the output is speculative by
  construction, and Phase 4 would not file it anyway. Turn on if the day
  assessment is wanted every day regardless of cost.

#### POSTMARKET_TRIAGE_TIMEOUT_SECONDS
- **Value:** code default `240`.
- **Why so much higher than the veto's 25-60s:** the triage prompt carries the
  violations, prior-occurrence history, day context and error templates, and
  asks for several paragraphs plus draft issue bodies. The call runs on a real
  OS thread and is killed at this budget, so a hang cannot wedge the scheduler.

**Operator prerequisite:** triage needs the `claude` CLI **logged in** on the
box running OpenAlgo (it uses the CLI's own subscription auth — there is no API
key anywhere in this codebase). As of 2026-08-03 it is **not** logged in: the
review will report `llm_status='not_logged_in'` until someone runs `claude
login`. Everything deterministic still runs and reports.

### Multi-account mirror trading (issues #468/#474/#476/#478/#482, added 2026-07-28)

#### MULTI_ACCOUNT_ENABLED
- **Value:** `true` (in `.env`; code default `false`) — enabled 2026-07-28 by the operator.
- **What it gates:** the Phase-2 order fan-out to child broker accounts
  (`services/account_fanout_service.py`) and the Phase-3 observability jobs
  (login reminders 09:00/15:00 IST, EOD mirror summary 15:35 IST). Account
  setup/login on `/accounts` is never gated.
- **CORRECTION (same day):** the first version of this entry said enabling was
  inert because "every strategy runs sandbox" — WRONG. Verified against
  `strategy_mode`: `open15_vol_breakout` is **live** (operator flip 2026-07-24)
  and `sector_follow_cap5_vol` is **live** (2026-06-24). With the flag on,
  **mirroring is ARMED from the next trading day**: open15's live orders mirror
  into Swapna-zerodha (₹15,000, factor 0.015, open15 selected — ~₹2,250/slot).
  sector_follow is live but NOT selected by any child → no mirrors for it.
- **Rollback:** set `false` + restart (or delete the line — code default is false).

#### PRIMARY_BOOK_CAPITAL
- **Value:** `1000000` (₹10L, in `.env`; matches the code default — pinned
  explicitly so sizing intent is visible).
- **What it does:** denominator for child mirror sizing —
  `factor = child.capital_inr / PRIMARY_BOOK_CAPITAL`, floored to shares/lots.
  Only read when `MULTI_ACCOUNT_ENABLED=true`.
- **Why ₹10L:** the consolidated primary book target (see the 2026-06 consolidated
  ₹10L research). A ₹15k child ⇒ factor 0.015.

### futures_follow OPTION_C same-minute@15:25 entry (issue #406, added 2026-07-14)
The 15:20 entry seeds its 50% margin cap from `lots_held()`, which counts the
still-open prior-day lot (it exits at 15:25) — under-sizing carry days (issue #405).
Backtest (`docs/research/strategy/futures_follow_cap50/2026-07-14_entry_cap_carry_sizing.md`):
running the T+1 exit FIRST at 15:25 and placing entries in the same minute recovers
**+1.19pp CAGR / +0.07 Sharpe** vs current production while keeping peak margin at
**49.8%** (no overlap). Selection stays at 15:20 (backtest-faithful); only execution
moves to 15:25. New tunable proposed in PR #406; lands on dev with the merge.

#### FUTURES_FOLLOW_ENTRY_MODE (NEW)
- **Current value:** unset → **`legacy`** (default = current behavior: entry 15:20,
  T+1 exit 15:25). Set to **`same_minute`** to enable OPTION_C.
- **Set in:** env; read by `services.futures_follow_service.futures_entry_mode()`,
  consulted at `register_jobs` (takes effect on the next restart).
- **What it does:** `same_minute` replaces the 15:20 entry job with a 15:20 *signal
  snapshot* and the 15:25 exit job with a *15:25 exit-then-entry* job (exit first →
  fresh cap → margin never overlaps). Any value other than `same_minute` resolves to
  `legacy` (fail-safe). **Live caveat:** in live the exit fill must confirm before the
  entry places, else a tight-margin broker could reject; the sandbox ₹1Cr book is
  unaffected. Ships default-`legacy`; operator flips to `same_minute` after review.

### VETO_CLAUDE_TIMEOUT_SECONDS 25 → 60 (added 2026-07-14)
The Stage-1 LLM veto invokes `claude -p` in-process
(`services/llm_review_client.invoke_claude_review`) with a wall-clock budget from
`VETO_CLAUDE_TIMEOUT_SECONDS` (`signal_review_service._claude_timeout_seconds`,
code default 25). Measured on 2026-07-14: a real veto prompt on the default model
(Opus) takes **~19–22s warm** and more cold — the CLI cold-loads ~75K tokens of
system/tool context per call, and live vetoes fire only a few times a day so they
are always cold. Result: **100% of fresh reviews timed out** (`review_failed` /
`claude_timeout`), failing safe to `take` — the guardrail was silently inert
(2026-07-14 CONCOR / KALYANKJIL rows). Benchmarks also showed a model swap on the
CLI path does NOT help (Haiku was slower, ~27–29s) and the `stdin=DEVNULL` fix saves
at most ~3s — the CLI transport is the bottleneck, not the model. Bumping the budget
is the correct hotfix and keeps the veto on the Claude subscription (OAuth) rather
than a metered Messages-API bill.

#### VETO_CLAUDE_TIMEOUT_SECONDS
- **Current value:** `60` (in `.env`; code default remains `25` when unset).
- **Set in:** env; read live per call by
  `services.signal_review_service._claude_timeout_seconds` via `os.getenv` →
  **takes effect on the next OpenAlgo restart** (the process env is loaded from
  `.env` at boot).
- **What it does:** wall-clock ceiling for the `claude -p` veto reasoning
  subprocess. 60s gives comfortable headroom over the ~19–22s warm / higher-cold
  latency so cold calls complete instead of timing out. Vetoes fire only a few
  times/day, so the extra seconds are operationally immaterial. Lower it only if a
  faster transport (direct Messages API) later replaces the CLI.

### futures_follow big-loss news-context alerts (issue #399, added 2026-07-13)
The 3% kill switch is same-day and blind to T+1 overnight losses (the 2026-07-07
war-day gap read ₹0). New: on a big **realized** T+1 loss (and on a kill-switch
fire) the operator alert is enriched with recent market headlines — already
ingested by `news_ingest_service` into `market_intel(kind='news')` — so a large
loss is *explainable* (war / macro / broad sell-off). **Strictly informational +
human-in-the-loop: no order is ever placed from this path** (R54/R55 proved
reacting is net-negative on this leveraged-beta sleeve). Pure DB read, fail-open.
Proposed in PR for #399; lands on dev with the merge.

#### FUTURES_FOLLOW_BIG_LOSS_ALERT_PCT (NEW)
- **Current value:** unset → **2.0** (percent of capital; ₹20,000 on the ₹10L book).
- **Set in:** env; read by `services.futures_follow_service.big_loss_alert_pct`,
  consumed by `_maybe_alert_big_loss` (called from `run_exit`).
- **What it does:** realized daily-loss magnitude that triggers the news-enriched
  big-loss alert (at most once/day, reset at the 09:00 daily reset). Does NOT gate
  trading — alert only.

#### NEWS_CONTEXT_ON_ALERTS_ENABLED (NEW)
- **Current value:** unset → **`true`**. Master switch for attaching news context
  to big-loss / kill-switch alerts. When `false`, `get_recent_news_context`
  returns "" and alerts fire unchanged (no news block).
- **Set in:** env; read by `services.news_context_service`.

#### NEWS_CONTEXT_LOOKBACK_MIN / NEWS_CONTEXT_MAX_ITEMS / NEWS_CONTEXT_HIGHLIGHT_TERMS (NEW)
- **Defaults:** `720` (12h lookback), `6` (headlines shown), and a built-in
  geopolitics/macro highlight list (war/attack/missile/sanction/RBI/Fed/crude/…)
  overridable as a comma-separated list. Highlighted headlines get a ⚠️ marker —
  a hint to the operator, **never** a trading trigger.
- **Set in:** env; read by `services.news_context_service`.

### futures_follow quotes-snapshot data source (issue #332, added 2026-07-05)
`futures_follow_cap50` makes exactly one decision per day (15:20 IST); PR #333
moved its decision snapshot (today's per-symbol close + cumulative volume) off
the all-day WS-fed scanner aggregator onto a point-in-time broker quote call,
and added a wall-clock lateness guard on the entry job. Merged 2026-07-05
(`6ea8fe322`); entries below are the immediate follow-up direct commit proposed
in the PR body.

#### FUTURES_FOLLOW_INTRADAY_SOURCE (NEW)
- **Current value:** unset → defaults **`quotes`** (unknown values fall back to
  `quotes` with a WARNING).
- **Set in:** env; read by `services.futures_follow_service.futures_intraday_source`
  (consumed by `production_signal_evaluator` and `assert_data_pipeline_healthy`);
  provider lives in `services.sector_follow_service.make_quotes_intraday_provider`.
- **What it does:** selects the source of the 15:20 IST decision snapshot.
  `quotes`: ONE batched `get_multiquotes` call at decision time (universe stocks
  on NSE + mapped sector indices on NSE_INDEX), memoized per eval cycle, with
  fail-safe fallback quotes → aggregator → historify (WARNING per hop) and a
  15:18 dry-run quote probe in the smoke check. `aggregator`: the pre-#332
  WS-fed path, byte-identical (regression-tested) — the rollback switch.
- **Why:** depending on the all-day WS tick stream being healthy at the single
  decision moment was the 2026-06-15 "aggregator empty → silent 0-signal day"
  failure class. A REST snapshot through the same broker session removes that
  dependency. Scope: futures_follow_cap50 ONLY — sector_follow_cap5_vol (the
  equity alpha book) still uses the aggregator; accepted divergence is a
  marginally different `t_close` (quote at 15:20:xx vs last aggregator bar close).

#### FUTURES_FOLLOW_ENTRY_DEADLINE_IST (NEW)
- **Current value:** unset → defaults **`15:28`** (HH:MM IST); consult-time
  (re-read on every entry fire). Malformed value falls back to `15:28` with a
  WARNING — a typo can never disable the guard.
- **Set in:** env; read by
  `services.futures_follow_service.futures_entry_deadline_ist` (`run_entry`).
- **What it does:** wall-clock lateness deadline for the 15:20 IST entry job,
  checked FIRST in `run_entry` — before the override gate, kill switch,
  freshness gate, evaluation, or any order. A fire past the deadline skips ALL
  of today's entries, logs an ERROR, and Telegrams the operator. Exits
  (`run_exit`), the EOD watchdog, and `close_all` are NEVER gated (repo
  invariant: a held T+1 position is riskier than a rejected exit order).
- **Why:** the entry cron can misfire LATE (app down at 15:20 → APScheduler
  fires on restart). NFO hard close is 15:30: at ~15:35 the exchange rejects
  (noisy but harmless), but after ~15:45 the MARKET order could queue as an AMO
  and execute at tomorrow's open — an unintended, unmonitored overnight entry.
  15:28 leaves the normal 15:20 fire (plus jitter) untouched while guaranteeing
  no order is dispatched into/after the close.

### LLM health probe timeout (issue #297, added 2026-07-02)
The Strategies-page LLM health chip (`GET /strategies/api/llm/health`) spawns a
lightweight `claude -p` liveness probe on demand (operator clicks the chip's
refresh icon — never auto-polled). This bounds that subprocess.

#### LLM_HEALTH_PROBE_TIMEOUT_SECONDS (NEW)
- **Current value:** unset → defaults **`12`** (seconds).
- **Set in:** env; read by
  `blueprints.strategies_dashboard_api._llm_health_probe_timeout`. Clamped to
  `[3, 60]`; malformed → `12`.
- **What it does:** wall-clock budget passed to
  `services.llm_review_client.probe_claude_health`. On expiry the probe reports
  `reason='timeout'`, `reachable=false`. Distinct from
  `VETO_CLAUDE_TIMEOUT_SECONDS` (25s, the real veto call) — the probe is a
  smaller "is claude alive/logged-in" check and should return fast.

### Scanner daily-D re-settle (issue #299, added 2026-07-02)
Once-per-day non-incremental overwrite re-fetch of the trailing settled daily-D
window for the scanner universe, run before the stale-check at boot + post-close
convergence (`services.scanner_universe_backfill.resettle_recent_daily`, wired via
`scanner_backfill_scheduler._maybe_resettle_daily`). Corrects a daily bar written
intraday as a provisional/running close (the #277 freeze class) that the
incremental convergence can never fix (it skips a day whose bar already exists),
which otherwise persists into the scanner's `yest_d` gate and fires phantom gap
signals (2026-07-02 DELHIVERY false BUY: stored 07-01 close 475.4 vs settled
507.7). Also refreshes `ScannerHistoryProvider` so the corrected close reaches
the live scanner without a restart. Additive, idempotent, fail-graceful.

#### SCANNER_DAILY_RESETTLE_ENABLED (NEW)
- **Current value:** unset → defaults **`true`**.
- **Set in:** env; read by
  `services.scanner_universe_backfill._daily_resettle_enabled`. When off,
  `resettle_recent_daily` returns `status="disabled"` and no re-fetch happens.

#### SCANNER_DAILY_RESETTLE_DAYS (NEW)
- **Current value:** unset → defaults **`2`** (trailing settled trading days).
- **Set in:** env; read by
  `services.scanner_universe_backfill._daily_resettle_days`. Bounded to >= 1;
  malformed → falls back to `2`.

### Pre-entry data refresh (sector_follow + futures) (issue #237, added 2026-07-02)
A 15:17 IST APScheduler job (`sector_follow_preentry_refresh`) that runs the
existing `run_backfill_checks` (fetch stale index+stock intraday tail) and waits
`_PREENTRY_WAIT_SEC` (90s, bounded so it can't overrun the 15:20 entry) so
today's bars land in historify before the 15:18 smoke + 15:20 entry. Closes the
mid-day gap that produced the 06-29/06-30 zero-order days (boot check runs hours
earlier; the periodic loop only runs 15:30–17:00). Benefits futures_follow too
(shared sector_follow data). Additive, idempotent (fresh → no-op), fail-graceful.

#### SECTOR_FOLLOW_PREENTRY_REFRESH_ENABLED (NEW)
- **Current value:** unset → defaults **`true`**.
- **Set in:** env; read by
  `services.sector_follow_backfill_scheduler.preentry_refresh_enabled`. When off,
  the 15:17 job is not registered and `run_preentry_backfill_checks` no-ops.

#### SECTOR_FOLLOW_PREENTRY_REFRESH_TIME (NEW)
- **Current value:** unset → defaults **`15:17`** (IST; must be < 15:18 smoke).
- **Set in:** env; read by
  `services.sector_follow_backfill_scheduler.preentry_refresh_time`. Malformed →
  falls back to `15:17`.

### `_CALENDAR_BUFFER` — scanner daily-history query window (issue #280, changed 2026-07-01)
- **Current value:** **`1.6`** (was `1.4`).
- **Set in:** code constant `services/scanner_history_provider.py:_CALENDAR_BUFFER`
  (not env/DB — a threshold default in code).
- **What it does:** `ScannerHistoryProvider._fetch` sizes its DuckDB date-range
  query by CALENDAR days = `daily_lookback_bars(205) × _CALENDAR_BUFFER`, then
  takes the last 205 rows. It's a heuristic proxy: it must span enough calendar
  days to contain ≥200 *trading* bars for the BUY rule's SMA(200) volume-gate
  warm-up (`fno_intraday_buy_chartink`).
- **Why 1.4 → 1.6:** Indian NSE trading days are only ~0.67 of calendar days
  (weekends + ~14–16 holidays/yr), not the naive 5/7≈0.71. At 1.4 → 288 cal days
  → only ~193 trading bars → below 200 → the guard silently rejected the WHOLE
  F&O universe (0 BUY hits). 1.6 → 329 cal days → ~220 trading bars (~15-bar
  margin over 205, 20 over the 200 guard). Verified arithmetically
  (GODREJPROP/RECLTD/JSWSTEEL → 220); all NSE F&O share one trading calendar so
  the ratio is universe-wide. Committed direct to dev `1f5d53e22` (non-order-path
  scanner read-path). **Note:** heuristic, not a hard bar-count guarantee — a
  holiday-heavy window could still dip; the robust hardening (bar-count-aware
  fetch / re-widen-on-short) is a follow-up. #281 added the loud <200 WARNING.

### Position-store reconciliation at exit (issue #265, proposed 2026-07-01)
### LLM veto — in-process `claude -p` transport (#266 Phase 1 / #267, added 2026-07-01)

Phase 1 of #266 moved the Stage-1 LLM veto's reasoning call from an httpx POST
to the Claude Bridge (`http://127.0.0.1:5001/review-signal`) to an **in-process**
`claude -p "<prompt>" --output-format json` subprocess run on a dedicated real
OS thread (`services/llm_review_client.invoke_claude_review`, eventlet-safe). The
bridge was never auto-started, so every real veto call failed `ConnectError` and
fell safe to 'take' — the veto never fired. It now runs directly.

#### VETO_CLAUDE_TIMEOUT_SECONDS (NEW)
- **Current value:** unset → defaults **`25.0`**.
- **Set in:** env; read by
  `services/signal_review_service._claude_timeout_seconds` and passed as
  `timeout_s` to `invoke_claude_review`.
- **What it does:** wall-clock budget for the `claude -p` subprocess. On expiry
  the subprocess is killed and the veto fails safe to `decision='take'`,
  `reasoning='claude_timeout'`. Matches the bridge's old 25s wall-clock.

#### CLAUDE_CMD (NEW, optional)
- **Current value:** unset → defaults **`claude`** (resolved against `PATH`).
- **Set in:** env; read by `services/llm_review_client._claude_cmd`.
- **What it does:** overrides the `claude` binary path for the veto subprocess
  (e.g. an absolute path to a non-PATH install). The operator's install is
  authenticated by the Claude subscription CLI login — no `ANTHROPIC_API_KEY`.

#### VETO_BRIDGE_URL (RETIRED)
- **Was:** bridge endpoint, default `http://127.0.0.1:5001/review-signal`.
- **Now:** removed from `signal_review_service`. The veto no longer uses the
  bridge. (`bridge/server.py`'s `/review-signal` endpoint and the Cowork
  automation endpoints are untouched, but the veto path no longer calls them.)

#### VETO_REQUEST_TIMEOUT_SECONDS (RETIRED)
- **Was:** httpx read timeout for the bridge POST, default `30.0`.
- **Now:** removed. Superseded by `VETO_CLAUDE_TIMEOUT_SECONDS` (the subprocess
  budget). `VETO_CACHE_TTL_SECONDS` and `VETO_LAYER_MODE` are unchanged.

### LLM control — per-strategy `llm_mode` (#266 Phase 2 / #275, proposed 2026-07-01)

> Proposed by feature branch `feat/275-llm-mode-ui` (staged, operator-reviewed;
> order-path-adjacent — the veto blocks orders in `active` mode). Raised as a PR,
> NOT merged. The entry lands on `dev` at merge or as an immediate follow-up.

Replaces the hidden `VETO_LAYER_MODE` env flag as the *operator* control with a
single per-strategy toggle on `/strategies` (issue #266). New table
`strategy_llm_config` (`db/openalgo.db`, `database/strategy_llm_config_db.py`):
one row per strategy, `llm_mode ∈ {off, veto, delegate}`.

#### strategy_llm_config.llm_mode (NEW)
- **Current value:** no rows → the resolver falls through to `VETO_LAYER_MODE`
  env, then the mode-aware default (sandbox→active, else shadow) — so **existing
  behavior is preserved until the operator sets a UI value** (#162 Phase-4
  pattern).
- **Set via:** the guarded writer `services.strategy_llm_config_service.flip_llm_mode`
  (audits + publishes `StrategyLLMModeChangedEvent`), fronted by
  `POST /strategies/api/<name>/llm-mode {"llm_mode":"off|veto|delegate"}`.
- **What it does / how it maps to enforcement** (in
  `signal_review_service.get_veto_layer_mode(effective_mode, strategy_name)`,
  DB-row-first resolution):
  - `off` → `off` (no reviewer runs).
  - `veto` → `active` (a `skip` verdict BLOCKS the order).
  - `delegate` → stored, but resolved as `active` for now — the LLM-decides
    engine path isn't built (a later phase); shown disabled/"coming soon" in the
    UI.
- **Fixes #274 item 2:** the DB row makes the sandbox enforcement explicit, so
  the fire that resolved `shadow` when sandbox should have been `active` is now
  unambiguous once the operator sets `veto`.
- **`shadow` is env-only:** it stays available via `VETO_LAYER_MODE=shadow`
  (observe-only) but is **not** operator-selectable from the UI.
- **Rollback:** delete the strategy's `strategy_llm_config` row → instant
  env/default fall-through. `DELETE FROM strategy_llm_config WHERE
  strategy_name='<name>'` or `database.strategy_llm_config_db.delete_llm_mode`.
- **Scope today:** only `simplified_engine` actually calls the veto
  (`_run_pre_order_review`), so only its row has runtime effect; other strategies
  can hold a row but it is inert until Phase 3 wires the veto into them.

### Live-mode broker-position reconciliation (issue #265, proposed 2026-07-01)

> Proposed by feature branch `feat/265-live-position-reconciliation` (staged,
> operator-reviewed order path). Land the entry on `dev` at merge time.
>
> **Revised 2026-07-01:** the guard now runs in **BOTH sandbox AND live** modes,
> sourced from the mode-appropriate position store (was live-only). The flag was
> renamed `LIVE_POSITION_RECONCILE_ENABLED` → `POSITION_RECONCILE_ENABLED`.

#### POSITION_RECONCILE_ENABLED
- **Current value:** unset → defaults **`true`**.
- **Set in:** env; read by
  `services/live_position_reconciliation_service.is_enabled()` inside
  `reconcile_exit`. The engine call-sites run the guard in both `sandbox` and
  `live` mode (only `disabled` mode — which never sends orders — skips it).
- **What it does:** master gate for the exit reconciliation guard. When ON (and
  the strategy is in `sandbox` or `live` mode), every exit reconciles its
  journalled/in-memory close quantity against the **mode-appropriate position
  store** — the `sandbox.db` virtual book in sandbox, the real broker positionbook
  in live — via the mode-aware `services/openposition_service.get_open_position`,
  before the exit order is placed:
  - store flat (net 0) → **SUPPRESS** the exit (phantom);
  - store holds fewer than journaled, or sits on the opposite side → **CLAMP**
    to the store qty (opposite side → clamp to 0 = suppress);
  - store consistent → **PROCEED** with the journalled qty (never more);
  - store fetch fails / no api key → **FAIL CLOSED for reverse-risk** (proceed
    with the journalled qty, never an unbounded one) + drift alert.
  On any mismatch it emits a position-drift alert via
  `services.source_divergence_alerts.check_and_alert` (`journal_qty` vs
  `broker_qty`, per-(strategy, symbol, IST-day) dedup).
- **Wired at:** `services/futures_follow_service.py`
  (`place_exit` → covers `run_exit` / `run_eod_watchdog` / `close_all_positions`,
  plus a both-mode boot `rehydrate_paper_book_from_store`) and
  `services/simplified_stock_engine_service.py` (`_flatten_for_api_key`
  engine-known qty reconcile + phantom suppress, and `flatten_strategy_positions`
  store-aware clamp/suppress). The mode-aware store routing is done by
  `get_open_position` / `get_positionbook`, so the same call reads sandbox.db in
  sandbox and the broker in live.
- **Sandbox complement (unchanged):** the sandbox EOD journal reconciliation
  (`services/engine_eod_reconciliation_service`, sandbox.db → `trade_journal`) is
  left untouched and is **complementary** — it stamps sandbox MIS square-offs into
  the journal, while the #265 per-exit guard clamps/suppresses over-exits against
  the sandbox store. The two do not overlap.
- **Why default true:** the mode-appropriate store must be the source of truth at
  exit; a journal↔store mismatch (manual/partial exit, restart-lost `paper_book`,
  phantom) could otherwise double-SELL into a net-short overnight future or fire a
  reversing exit — in the sandbox virtual book as well as live. Set `false` only
  as an emergency disable to fall back to the legacy journal-driven exit path.
- **Safety guarantee:** `test/test_live_position_reconciliation_service.py`
  (helper semantics) + sandbox/live exit + boot-rehydrate cases in
  `test/test_futures_follow_service.py`, `test/test_simplified_stock_engine_service.py`,
  `test/test_eod_watchdog_service.py`.

### Runtime source-divergence alerts (issue #231, added 2026-06-29)

#### SOURCE_DIVERGENCE_ALERTS_ENABLED
- **Current value:** unset → defaults **`true`**.
- **Set in:** env; read by
  `services/source_divergence_alerts._flag_enabled` on every call to
  `check_and_alert`.
- **What it does:** master gate for the runtime divergence-alert helper
  used by three integration sites — `services/scanner_aggregator_seeder.py`
  (historify vs broker most-recent close), `services/engine_eod_reconciliation_service.py`
  (journal-expected closed quantity vs sandbox covering-fill quantity), and
  `services/scan_rules/fno_intraday_{buy,sell}_chartink.py` (`bars_daily`
  today's close vs live 5m last close). When ON, a divergence above the
  threshold emits `logger.warning` AND a Telegram alert via
  `notification_service.notify('source_divergence', ...)` with per-(service,
  symbol, IST-day) dedup so the operator gets one notification within
  seconds instead of finding the discrepancy in `errors.jsonl` after EOD.
- **Dedup table reset behaviour:** in-process dict, cleared at boot AND on
  IST date rollover. A restart re-arms every dedup key (a genuine
  cross-restart regression alerts immediately on the next divergent read).
- **Why default true:** this is the runtime sibling of the PR #227 contract
  tests — the catch-at-PR-time pattern only catches *new* divergence bugs;
  this catches *operational* divergence (stale historify slot, partial
  sandbox fills, frozen daily cache) in production. The 2026-06-29 41-SELL
  false-positive storm is the canonical case where same-day operator
  visibility would have prevented the recurrence.
- **Set false to:** silence ALL three integrations from one switch (e.g.
  during a known-noisy backfill window). The helper short-circuits before
  the threshold check, so no log + no Telegram fires.
- **Related:** `NOTIFY_SOURCE_DIVERGENCE` (per-event toggle inside
  `services/notification_service.py`, default true) gates only the Telegram
  delivery layer; set it false to keep the `logger.warning` and silence
  just the Telegram channel.

#### SOURCE_DIVERGENCE_THRESHOLD_PCT
- **Current value:** unset → defaults **`0.5`** (percent).
- **Set in:** env; read by
  `services/source_divergence_alerts._threshold_pct` on every call.
- **What it does:** the divergence threshold above which the helper fires
  an alert. The relative divergence is computed as
  `abs(a - b) / max(|a|, |b|, 1e-9) * 100`. Below this percentage the
  helper returns silently.
- **Why default 0.5:** matches the existing `SCANNER_RULE_DIVERGENCE_WARN_PCT`
  default (the scanner rule's `logger.warning` predates issue #231; this
  threshold keeps the new alert path consistent with what was already
  considered "stale source" in the rule layer).
- **Set higher to:** suppress noise during a volatile / illiquid window
  where 0.5% drift is plausible without indicating a stale source.
- **Set lower to:** catch finer divergences (rarely useful; expect false
  positives at <0.2% on bid/ask noise).
- **Junk values** (non-numeric, blank) fall back to the 0.5 default rather
  than crashing the helper.

### Trading-day funnel diagnostic (issue #159, added 2026-06-28)

#### TRADING_DAY_FUNNEL_ENABLED
- **Current value:** unset → defaults **`true`**.
- **Set in:** env; read each fire in
  `services/trading_day_funnel_service._funnel_job`.
- **What it does:** master gate for the daily 15:35 IST end-of-session funnel
  summary that walks the signal → engine → order → journal pipeline and
  Telegrams the per-layer counts (scanner hits, engine signals taken/vetoed,
  per-strategy orders attempted/filled/open-EOD, sandbox cross-check) plus a
  drop-off verdict naming the first layer where `K < M`. The next "zero
  trades on a healthy-looking day" surfaces as a single Telegram message at
  15:35 IST instead of empty-journal forensic-SQL the next morning.
- **Why default true:** the failure class this catches is **silent**
  (2026-06-26 produced 0 trades while every individual subsystem reported
  healthy). The funnel itself is read-only and never raises into the
  scheduler, so the operational cost of being on is zero and the diagnostic
  payoff is large.
- **Set false to:** suppress the daily Telegram (e.g. during a scheduler dev
  window or to silence noisy notifications); the service still registers but
  the per-fire body is a no-op.
- **Related:** `NOTIFY_TRADING_DAY_FUNNEL` (per-event toggle inside
  `services/notification_service.py`, default true) gates the Telegram
  delivery layer; set it false to silence only the Telegram while still
  letting the structured INFO log fire.

#### TRADING_DAY_FUNNEL_TIME
- **Current value:** unset → defaults **`15:35`** (IST).
- **Set in:** env; read by
  `services/trading_day_funnel_service.register_jobs` at boot.
- **What it does:** the IST fire time `HH:MM` for the daily funnel job. Sits
  between the 15:14 EOD watchdog / 15:25 sector_follow exit / 15:30 sandbox
  MIS auto-square-off / 15:30 EOD reconciliation, and the 15:45 IST
  `scanner_comparison_eod` job, so the funnel reads a fully-settled day.
- **Why default 15:35:** late enough to capture every entry/exit/journal
  write the EOD reconciliation made (15:30 trigger window), early enough
  that the Telegram lands before the 15:45 comparison alert so the operator
  reads them in causal order.
- **Set to a different `HH:MM` to:** shift the slot. Junk values fall back
  to the default rather than crashing boot.
### Telegram inbound poller — disabled (Conflict fix, added 2026-06-30)

#### TELEGRAM_INBOUND_ENABLED
- **Current value:** `.env` → **`false`** (was `true`).
- **Set in:** env; read in `services/telegram_inbound_service.py._inbound_enabled`
  (master gate on `init_telegram_inbound_service`).
- **What it does:** master on/off switch for the Phase-6 inbound Telegram service.
  `false` means `init_telegram_inbound_service` is a no-op at boot (no poller, no
  send-fallback registration).
- **Why changed `true→false`:** issue #238. With it `true`, the inbound service
  started a second `getUpdates` poller on the SAME bot token the UI-toggled
  interactive bot (`telegram_bot_service`, `bot_config.is_active`) already polls,
  producing a persistent `telegram.error.Conflict: terminated by other getUpdates
  request` — ~3856 occurrences (~200/hour all day) on 2026-06-30. The operator
  decision is that the UI bot is the single poller and single sender. The env was
  flipped to `false` as the immediate fix; a durable **single-poller guard** also
  landed in code (`telegram_inbound_service.start()` refuses to poll whenever
  `bot_config.is_active` is true, even if this flag is `true`), so the bug is
  structurally impossible regardless of the env value. Operator lands the `.env`
  edit + this log entry direct to `dev`.

### Preflight error gate — per-signature cap (added 2026-06-19)

#### PREFLIGHT_ERROR_PER_SIGNATURE_CAP
- **Current value:** unset → defaults **`5`**.
- **Set in:** env; read in `services/preflight_service.py._check_recent_errors`
  (constant `PREFLIGHT_ERROR_PER_SIGNATURE_CAP_DEFAULT`), applied in
  `_count_recent_errors` via `_error_signature`.
- **What it does:** caps how much any single error *signature* — `(logger,
  source file:line)` — contributes to the gate's **effective** count. The gate
  compares `effective_count` (each signature capped at this value) to
  `PREFLIGHT_MAX_ERRORS_LAST_HOUR` (default 10). `count_last_hour` still reports
  the raw total; the response also carries `effective_count` and
  `distinct_signatures`. Entries with no logger can't be attributed to one fault
  and are counted individually (not capped). `0`/negative disables capping
  (effective == raw, the legacy behavior).
- **Why added:** the 2026-06-19 TCS incident — a single per-tick exit storm
  (~1600 identical `services.simplified_stock_engine_service:453` lines in 30
  min) single-handedly tripped the error gate and **aborted every scan cycle**.
  Capping each signature means one runaway code path can't DOS the whole scan
  pipeline; a genuinely broad problem (many distinct signatures) still aggregates
  over the threshold and aborts. Default `5` (≤ the abort threshold) so one
  signature alone can never abort. Pairs with the P0 engine fix that stops the
  storm at its source (`fix(simplified-engine): stop orphan-position exit storm`).

### In-house screener — Tier-1 observability hardening (added 2026-06-15)

All default-on and additive — they change what is observed/skipped, never which
signals fire. Source: `services/scanner_service.py` + the two
`services/scan_rules/fno_intraday_*_chartink.py` rule modules. Plan:
`docs/research/strategy/screener/2026-06-15_inhouse_deep_analysis.md` (Tier 1).

#### SCANNER_POSTCLOSE_GATE_ENABLED
- **Current value:** unset → defaults **`true`**.
- **Set in:** env; read in `services/scanner_service.py`
  (`_postclose_gate_enabled()`), gating the market-hours guard in
  `_evaluate_definitions`.
- **What it does:** when `true`, the scanner skips rule evaluation (INFO log)
  outside `[09:15, 15:30]` IST. `false` → evaluation runs at any wall-clock time
  (the pre-Tier-1 behavior).
- **Why added:** the 2026-06-15 post-close spurious-SELL incident (17 AUROPHARMA
  SELL fires at 16:10–17:30 IST on a stale daily bar, FM-6).

#### SCANNER_DBAR_DATE_VERIFY_ENABLED
- **Current value:** unset → defaults **`true`**.
- **Set in:** env; read in both `fno_intraday_buy_chartink.py` /
  `fno_intraday_sell_chartink.py` (`_dbar_date_verify_enabled()`).
- **What it does:** **Reframed for Issue #197 (2026-06-29).** The rule now
  derives today's running daily snapshot from today's 5m bars when
  `bars_daily.iloc[-1]` is dated before today (the production state during
  the trading session), so the original AUROPHARMA-style "fire on
  stale-as-today" bug class is structurally impossible. The guard now
  defends against the LATEST SETTLED bar being more than **5 calendar days**
  behind today (backfill broken across multiple sessions), in which case
  the rule aborts with a WARNING. Only fires when the daily frame carries
  a `timestamp` column (production reads); `false` → no staleness check.
- **Why added:** original Tier-1 defense for FM-6. Threshold widened to 5
  days as part of Issue #197 because `iloc[-1]` is naturally 1-4 days
  behind today during normal Mon-Fri operation (post-weekend / post-holiday).

#### SCANNER_COMPLETENESS_ENABLED
- **Current value:** unset → defaults **`true`**.
- **Set in:** env; read in `services/scanner_service.py`
  (`_completeness_enabled()`), gating `_record_completeness`.
- **What it does:** when `true`, the scanner accumulates which symbols produced a
  live bar per rolling window and emits a decision-input completeness metric.
  `false` → no window accumulation, no metric.
- **Why added:** ends the "0 hits == no data == failure" ambiguity (DP-4) by
  reporting `n_live/total` and alerting on partial feed degradation.

#### SCANNER_COMPLETENESS_WINDOW_MIN
- **Current value:** unset → defaults **`5`** (minutes, ~one 5m bar cycle).
- **Set in:** env; read in `services/scanner_service.py`
  (`_completeness_window_min()`).
- **What it does:** the rolling window over which symbol liveness is accumulated
  before the completeness metric is emitted + reset.

#### SCANNER_COMPLETENESS_WARN_PCT
- **Current value:** unset → defaults **`50`** (percent).
- **Set in:** env; read in `services/scanner_service.py`
  (`_completeness_warn_pct()`).
- **What it does:** a live fraction below this threshold sends a 🟠 WARNING
  Telegram alert (`scanner_completeness` event).

#### SCANNER_COMPLETENESS_CRIT_PCT
- **Current value:** unset → defaults **`20`** (percent).
- **Set in:** env; read in `services/scanner_service.py`
  (`_completeness_crit_pct()`).
- **What it does:** a live fraction below this threshold sends a 🔴 CRITICAL
  Telegram alert. Per-severity once-a-day dedup prevents spam.

#### NOTIFY_SCANNER_COMPLETENESS
- **Current value:** unset → defaults **`true`**.
- **Set in:** env; read in `services/notification_service.py` (`per_event`).
- **What it does:** per-event toggle for the `scanner_completeness` Telegram
  alert. `false` → the metric still logs but no Telegram is sent. (Master switch
  `NOTIFY_TELEGRAM_ENABLED` still applies.)

### scanner_aggregator_seeder — broker fallback (issue #199, added 2026-06-29)

#### SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED
- **Current value:** unset → defaults **`true`**.
- **Set in:** env; read in `services/scanner_aggregator_seeder.py`
  (`_broker_fallback_enabled()`), gating the broker-history fetch in
  `_read_1m_bars_for_symbol`.
- **What it does:** when historify returns < `lookback_min / 3` 1m bars for a
  scanner symbol at boot, the seeder falls back to
  `services.history_service.get_history` (broker API, `source='api'`) to fetch
  the missing window. `false` → broker fallback disabled; the seeder uses only
  historify (pre-#199 behaviour — leaves ~195/227 scanner symbols un-seeded
  on a mid-session restart because the scanner-side 1m backfill only runs in
  the 15:30-17:00 IST window).
- **Why added:** Issue #199. On 2026-06-29 the seeder reported only `32/227
  symbols seeded` at the 12:45 IST restart (boot log:
  `aggregator_seeder: seeded 32/227 symbols, 6752 bars total (avg 211.0/symbol,
  195 empty, 0 errors)`). The 195 empty symbols had no recent 1m bars in
  historify because the scanner-universe 1m backfill is post-close only. With
  the broker fallback, every scanner symbol gets ~500 min of 1m bars seeded —
  enough to clear the 15m RSI(14) warm-up (needs 14×15m = 210 min) so the
  rules can evaluate from the first 5m bar close after a mid-session restart.

### Scanner rule-vs-broker observability (issue #205, added 2026-06-29)

Follow-up to the four scanner-rule fixes shipped 2026-06-29 (#198 / #200 /
#202 / #204). 147+ unit tests verified gate logic on internally-consistent
synthetic data; none caught the class of bug where two data sources for the
same value DISAGREE (a frozen historify daily snapshot vs the live 5m
aggregator). These knobs gate the three observability additions that surface
the next regression of that class in minutes instead of hours.

#### SCANNER_RULE_DIVERGENCE_WARN_ENABLED
- **Current value:** unset → defaults **`true`**.
- **Set in:** env; read in both
  `services/scan_rules/fno_intraday_buy_chartink.py` and
  `services/scan_rules/fno_intraday_sell_chartink.py`
  (`_divergence_warn_enabled`), gating the WARNING that fires when
  `today_d.close` drifts from the latest 5m close by more than
  `SCANNER_RULE_DIVERGENCE_WARN_PCT`.
- **What it does:** the 2026-06-29 41-SELL false-positive storm was caused by
  `today_d.close` being a frozen 14:28 snapshot while live 5m closes had
  advanced ~3%. With this guard on, the same condition logs a WARNING into
  `errors.jsonl` on every evaluation (per-symbol, per-bar-close) — a `grep
  diverges log/errors.jsonl` becomes the first-look diagnostic.
- **Set false to:** silence the WARNING during a known stale-data window
  (post-close backfill catching up) without disabling the rule.

#### SCANNER_RULE_DIVERGENCE_WARN_PCT
- **Current value:** unset → defaults **`0.5`** (%).
- **Set in:** env; read in both rule modules (`_divergence_warn_pct`).
- **What it does:** the divergence threshold above which the WARNING fires.
  0.5% is calibrated to TCS-class stocks where intraday drift between
  back-to-back 5m bars rarely exceeds 0.3%; tune up on high-vol names if the
  WARNING fires routinely.

#### SCANNER_RULE_DIVERGENCE_BLOCK_ENABLED (NEW, 2026-07-01)
- **Current value:** unset → defaults **`true`**.
- **Set in:** env; read in both
  `services/scan_rules/fno_intraday_buy_chartink.py` and
  `services/scan_rules/fno_intraday_sell_chartink.py`
  (`_divergence_block_enabled`).
- **What it does:** when on, a `today_d.close` that diverges from the latest 5m
  close beyond `SCANNER_RULE_DIVERGENCE_WARN_PCT` REJECTS the symbol (returns
  `False`, no scan hit) — not just a WARNING. Defense-in-depth for the
  `ts`-vs-`timestamp` Path-B fix in
  `services/scan_rules/_today_running.py::derive_today_and_yest`: the live
  scanner's 5m frame carries a `ts` column (naive IST datetimes), but Path B
  was gated on `"timestamp"` only, so it never engaged and the rules read a
  FROZEN ~09:45 historify daily bar all session (BUY hits empty, SELL misfired
  on the frozen morning crash). With the primary fix landed, `today_d.close`
  now IS the live 5m close so divergence should vanish — but if that path ever
  regresses, this gate guarantees a stale-data signal can never fire an order.
- **Set false to:** revert to WARNING-only (observe divergence without blocking
  the hit) during a known stale-data window, at the cost of the safety net.

#### SCANNER_CONTRACT_TEST_ENABLED
- **Current value:** unset → defaults **`false`**.
- **Set in:** env; read by
  `test/test_scanner_rule_vs_broker_contract.py` as a pytest module-level
  `skipif` gate.
- **What it does:** opt-in for the live-data contract test. When true, the
  test reads recent in-house `scan_results` rows directly from
  `db/openalgo.db` (read-only `file:` URI, bypassing the conftest temp-DB
  redirect), re-fetches broker bars via `services.history_service.get_history`,
  re-invokes the rule, and fails if divergence rate > `SCANNER_CONTRACT_TEST_MAX_DIVERGENCE_PCT`.
- **Why default false:** the test depends on a live broker session and live
  in-house fires. Default-off keeps unit CI hermetic and fast; the operator
  runs it manually after a session or wires it into an hourly cron.

#### SCANNER_CONTRACT_TEST_WINDOW_MIN
- **Current value:** unset → defaults **`60`** (minutes).
- **Set in:** env; read in
  `test/test_scanner_rule_vs_broker_contract.py`.
- **What it does:** look-back window for in-house fires the contract test
  will verify. 60 min covers a normal manual run; a 5-min cron loop should
  set it to `10` so signal-expiry false positives are minimized.

#### SCANNER_CONTRACT_TEST_MAX_DIVERGENCE_PCT
- **Current value:** unset → defaults **`5`** (%).
- **Set in:** env; read in
  `test/test_scanner_rule_vs_broker_contract.py`.
- **What it does:** divergence-rate ceiling for a passing contract test.
  Below ceiling → test passes (some signal expiry is normal); above → test
  fails with a per-row breakdown naming the symbol, today_d.close, latest
  5m close, and the offending `scan_results.id` for triage.

### sector_follow_cap5_vol — Fix 1b smoke check (added 2026-06-15)

#### SECTOR_FOLLOW_SMOKE_CHECK_ENABLED
- **Current value:** unset → defaults **`true`**.
- **Set in:** env; read in `services/sector_follow_service.py`
  (`smoke_check_enabled()`), gating the 15:18 IST `sector_follow_smoke_check`
  APScheduler job (`assert_data_pipeline_healthy`).
- **What it does:** when `true`, a 15:18 IST pre-entry smoke check verifies the
  data pipeline (aggregator coverage ≥ `SECTOR_FOLLOW_SMOKE_MIN_COVERAGE`,
  historify lookback works, broker session live) and — on failure — writes a
  self-expiring `pause` `strategy_runtime_override` that holds the 15:20 entries +
  Telegram-alerts. `false` → the job is a no-op (`ok=True`, no override written).
- **Why added:** the 2026-06-15 silent zero-signal incident — historify had no
  today stock 1m at 15:20 and the strategy failed closed with no alert. The smoke
  check catches a degraded pipeline 2 minutes before the entry window.

#### SECTOR_FOLLOW_SMOKE_MIN_COVERAGE
- **Current value:** unset → defaults **`0.5`**.
- **Set in:** env; read in `services/sector_follow_service.py`
  (`smoke_min_coverage()`).
- **What it does:** the minimum fraction of the `LOCK_STATIC_30` universe that
  must have **live aggregator** data for smoke-check Check 1 to pass. Below this,
  the 15:18 check fails and holds the 15:20 entries. Same threshold the
  `evaluate_candidates` completeness metric warns at (a separate hard-coded
  CRITICAL floor at 20% lives in `_emit_completeness_metric`).

### Logging / observability (added 2026-06-15)

#### LOG_TO_FILE
- **Current value:** `True` (was `False`).
- **Set in:** `.env`; read in `utils/logging.py` `setup_logging()`. Writes
  daily-rotated `log/openalgo_YYYY-MM-DD.log` (dir `LOG_DIR='log'`, retained
  `LOG_RETENTION=14` days).
- **Why changed:** the live Windows instance captures runtime INFO logs *only*
  via the operator's `Start-Process` stdout/stderr redirect, which is fragile —
  on 2026-06-15 the current instance (started 08:25) wrote to no captured file at
  all (`openalgo_stderr.log` froze at 08:20 = the prior instance), leaving the
  15:20 futures_follow cycle un-observable in any log. Enabling `LOG_TO_FILE`
  gives a durable, rotation-managed file log independent of the launch redirect.
  `errors.jsonl` (ERROR-only) is unchanged. Pairs with the restart now using a
  timestamped `log/openalgo_<ts>.out/.err.log` redirect.

### futures_follow_cap50 — strategy (added 2026-06-15)

#### FUTURES_FOLLOW_MODE
- **Current value:** unset → defaults **`sandbox`** (`.sample.env` not modified — add
  `FUTURES_FOLLOW_MODE=sandbox` there at the next convenient operator edit).
- **Set in:** env; read in `services/futures_follow_service.py`
  (`FuturesFollowService.__init__`).
- **Values:** `sandbox` | `live` — **there is NO scaffold / observe-only state.**
  - `sandbox` (default): orders routed to `db/sandbox.db` (virtual ₹1Cr) — **the
    strategy actively trades from boot.**
  - `live`: real broker orders.
  - Any unknown value force-falls-back to `sandbox` (logged WARNING).
- **Who flips to live:** **operator only** — `sandbox`→`live` is a deliberate
  operator decision (env or a persistent `strategy_mode` row,
  `strategy_name='futures_follow_cap50'`), never automated. The env/default source
  can NOT escalate to live; only a `strategy_mode` row can. Active sandbox trading
  can be paused without changing mode via `POST /futures_follow_cap50/api/pause`.
- **History:**
  - **2026-06-15 (v0.1.0, scaffold):** Introduced with default `scaffold` (compute +
    log only). **Superseded same day — see below.**
  - **2026-06-15 (v0.2.0, sandbox-default — operator redirect):** Default flipped to
    **`sandbox`** and the scaffold mode dropped entirely (`VALID_MODES =
    ("sandbox","live")`). The strategy now places real orders into `sandbox.db` from
    boot; first sandbox cycle Monday 2026-06-15 15:20 IST. `config_snapshot.json`:
    `mode: "sandbox"`, `deployable: true`. Rationale: get the strategy actively
    paper-trading the virtual book before the operator evaluates a live flip.
    Backtest reference (NIFTY-only CAP50): CAGR 14.44%, Sharpe 1.27, MaxDD −8.0% on
    ₹10L. **Caveat:** leveraged beta, not alpha (signal does not predict NIFTY —
    hit-rate 53.4%, corr 0.295).

#### config_snapshot.json (non-env tunables — NOT environment variables)
- **File:** `strategies/futures_follow_cap50/config_snapshot.json` — canonical
  source for the strategy's non-env tunables. Loaded by `load_config()`; the
  `FuturesFollowConfig` dataclass mirrors it. **The task brief named these as
  `FUTURES_FOLLOW_*` env vars; in the shipped code they live in config (or are
  scheduler-fixed cron times), NOT env — documented here accurately so the
  intent/reality match holds.**
- **Cap (was: `FUTURES_FOLLOW_CAP_MARGIN_PCT`):** `cap_margin_pct` = **0.50** —
  HARD cap, max 50% of capital as overnight SPAN margin (the other 50% is the
  gap-crash buffer — do NOT raise without a fresh tail-risk study). `capital_inr`
  ₹10,00,000, `nifty_lot_margin_inr` ₹2,50,000 (per-lot SPAN estimate used for the
  cap decision; operator refreshes from the broker), `nifty_lot_size` 75,
  `lots_per_signal` 1, `max_signals_per_day` 5.
- **Daily loss kill (was: `FUTURES_FOLLOW_DAILY_LOSS_KILL_PCT`):**
  `daily_loss_kill_pct` = **3.0** (halt new entries, hold open positions to T+1).
- **Times (was: `FUTURES_FOLLOW_ENTRY_TIME_IST` / `..._EXIT_TIME_IST` /
  `..._EOD_WATCHDOG_TIME_IST`):** scheduler-fixed cron times in
  `FuturesFollowService.register_jobs` — entry **15:20**, exit **15:25**, EOD
  watchdog **15:28** (was 15:14 — moved by #334 so the 15:25 exit is primary;
  the watchdog is the post-primary-exit retry backstop before the 15:30 NFO
  close), daily reset 09:00, EOD summary 15:30 IST (all `mon-fri`,
  `Asia/Kolkata`).
- **Product/exchange:** `product` NRML (futures carry — not MIS/CNC), `exchange`
  NFO, MARKET orders. `cost_pct_round_trip` 0.030 (~₹530/lot).
- **Who changes:** operator, recorded in
  `strategies/futures_follow_cap50/VERSION_LOG.md`.
- **Shared flag:** the data-freshness gate reuses `DATA_FRESHNESS_VALIDATION_ENABLED`
  + `MAX_STALENESS_BUSINESS_DAYS` (documented under sector_follow) since the futures
  sleeve fires on the sector_follow signal feed.

#### FUTURES_FOLLOW_SMOKE_CHECK_ENABLED (added 2026-07-02, #292)
- **Current value:** unset → defaults **`true`**
- **Set in:** env; read in `services/futures_follow_service.futures_smoke_check_enabled()`.
- **What it gates:** the 15:18 IST pre-entry smoke check
  (`FuturesFollowService.assert_data_pipeline_healthy`). When `true`, an
  APScheduler job fires at 15:18 IST (mon-fri) and verifies two things before the
  15:20 entry job runs:
  1. The sector_follow_cap5_vol feed is fresh (via `DATA_FRESHNESS_VALIDATION_ENABLED`
     + the existing `_data_health_checker` — the same check the entry gate calls
     but 2 minutes earlier, giving time to alert).
  2. A broker session (API key) is live.
  On failure it writes a self-expiring `pause` runtime override (expires 15:30 IST,
  honored by `_entry_held_by_override`) and Telegrams the operator with the reason.
  A passing check logs `INFO futures_follow 15:18 smoke check PASSED`.
- **When to set `false`:** only to silence the guard entirely (e.g. during
  non-market testing). The entry gate (`_data_is_fresh_for_entry`) still blocks on
  stale data; the smoke check just adds an early-alert + proactive pause 2 min
  before the trade window.
- **History:**
  - **2026-07-02 (introduced, #292):** Mirrors the `SECTOR_FOLLOW_SMOKE_CHECK_ENABLED`
    pattern (issue #237 / 2026-06-30 observed silent 0-lot day when sector_follow
    feed was stale).

### Build/runtime environment

#### `.python-version` = `3.12` (new file, 2026-06-13)
- **Current value:** `3.12` (single-line file at repo root)
- **Set in:** `.python-version` (new tracked file)
- **What it gates:** the Python interpreter `uv run` selects for every command.
  `uv` honors `.python-version` and pins the project to 3.12 even when a newer
  interpreter (3.14) is installed system-wide.
- **Why:** eventlet has no wheels for Python 3.14, so OpenAlgo cannot boot under
  3.14 (Flask-SocketIO falls back to the threading async-mode and the Werkzeug
  guard kills the server — port 5000 never binds). On 2026-06-13 `uv run`
  defaulted to system-newest 3.14 and the parallel app+bridge launches deadlocked
  uv's lock while building a fresh 3.12 env on demand → a 41-min restart outage
  (21:11→21:53 IST) instead of the usual ~2 min.
- **Effect:** `uv run` now auto-selects 3.12; the explicit `--python 3.12` flag is
  no longer needed, and a cold restart no longer races to build the wrong env.
- **Related:** boot-fail learning (memory `py314-eventlet-werkzeug-boot-fail`) —
  no eventlet on 3.14 → threading async-mode → Werkzeug guard kills boot; needs
  `allow_unsafe_werkzeug` or Py3.12. `pyproject.toml` already requires
  `>=3.12`; this file makes uv's default match that floor.

### Notifications — task_complete event

#### NOTIFY_TASK_COMPLETE
- **Current value:** unset → defaults `true`
- **Set in:** env var (read in `services/notification_service.NotificationService.__init__`
  via `_env_bool("NOTIFY_TASK_COMPLETE", default=True)`)
- **Code default:** `true`
- **What it gates:** the per-event toggle for the `task_complete` notification
  event. When `true`, `notify("task_complete", summary)` routes through the same
  Telegram path as other events (legacy outbound bot → Phase 6 inbound fallback);
  when `false`, those pushes are silently suppressed (master switch
  `NOTIFY_TELEGRAM_ENABLED` still applies on top).
- **Why (2026-06-13):** `task_complete` was never a registered event type, so
  every `notify("task_complete", …)` hit the unknown-event-type gate and was
  warned-and-dropped — forcing spawned code tasks to fall back to direct Telegram
  Bot API calls. Registering the event type (with this toggle, default ON) makes
  the documented completion-push path actually deliver.
- **Test coverage:** `test/test_notification_service.py`
  (`test_notify_task_complete_routes_through_telegram`,
  `test_notify_task_complete_enabled_by_default`,
  `test_notify_task_complete_respects_per_event_toggle`).

### Notifications — unknown event_type fallback (issue #243)

#### NOTIFY_UNKNOWN_EVENTS
- **Current value:** unset → defaults `true`
- **Set in:** env var (read in `services/notification_service.NotificationService.__init__`
  via `_env_bool("NOTIFY_UNKNOWN_EVENTS", default=True)`)
- **Code default:** `true`
- **What it gates:** when `true` (default), a `notify(event_type, …)` call where
  `event_type` is NOT in the registered `per_event` dict still DELIVERS the message
  (fail-open) — a WARNING is logged to prompt the operator to register the event
  type, but the alert is never silently dropped. When `false`, the old
  warn-and-drop behaviour is restored (useful in high-noise dev environments where
  new event_types are being iterated on).
- **Why (2026-07-02, issue #243):** on 2026-06-30 three operator alerts were
  silently dropped because `orphan_exit_reconciliation` and `scanner_aggregator_seed`
  were not in the registry. A silently-dropped alert is irrecoverable; a
  misrouted-but-delivered one is not. The flag defaults to the safer
  (fail-open) direction.
- **Related:** `NOTIFY_ORPHAN_EXIT_RECONCILIATION`, `NOTIFY_SCANNER_AGGREGATOR_SEED`
  (the two event types that were missing and triggered this fix).
- **Test coverage:** `test/test_notification_service.py`
  (`test_notify_unknown_event_type_delivers_with_warning`,
  `test_notify_unknown_event_type_drops_when_flag_off`).

#### NOTIFY_ORPHAN_EXIT_RECONCILIATION
- **Current value:** unset → defaults `true`
- **Set in:** env var (read in `services/notification_service.NotificationService.__init__`
  via `_env_bool("NOTIFY_ORPHAN_EXIT_RECONCILIATION", default=True)`)
- **Code default:** `true`
- **What it gates:** per-event toggle for the `orphan_exit_reconciliation` event
  type. When `true`, the reconciliation summary (N orphans found/reconciled/errored)
  is delivered via Telegram each time the orphan-exit reconciliation service runs.
  Caller: `services/orphan_exit_reconciliation_service.py`.
- **Why (2026-07-02, issue #243):** this event type was missing from the registry;
  its alert was silently dropped on 2026-06-30 at 08:31:50 IST next to a "9
  orphan(s) found, 9 reconciled" log line. Registered here with default ON.
- **Test coverage:** `test/test_notification_service.py`
  (`test_notify_orphan_exit_reconciliation_routes_through_telegram`,
  `test_notify_orphan_exit_reconciliation_per_event_toggle`).

#### NOTIFY_SCANNER_AGGREGATOR_SEED
- **Current value:** unset → defaults `true`
- **Set in:** env var (read in `services/notification_service.NotificationService.__init__`
  via `_env_bool("NOTIFY_SCANNER_AGGREGATOR_SEED", default=True)`)
- **Code default:** `true`
- **What it gates:** per-event toggle for the `scanner_aggregator_seed` event type.
  When `true`, the aggregator-seeding completion/failure notice is delivered via
  Telegram at boot and on reconnect. The operator needs this to know whether the
  scanner's in-process bar aggregator has warm bars before the trading session.
  Caller: `services/scanner_service.py` (aggregator-seeding path).
- **Why (2026-07-02, issue #243):** this event type was missing from the registry;
  its alerts were silently dropped on 2026-06-30 at 08:34:26 (boot) and 15:26:33
  (restart) IST while the operator was actively investigating an empty-aggregator
  failure. Registered here with default ON.
- **Test coverage:** `test/test_notification_service.py`
  (`test_notify_scanner_aggregator_seed_routes_through_telegram`,
  `test_notify_scanner_aggregator_seed_per_event_toggle`).

### Strategy control — unified daily intent

#### STRATEGY_DAILY_INTENT_ENABLED
- **Current value:** `true` (default; ships hot)
- **Set in:** env var (not yet in `.sample.env` — operator WIP held that file;
  add `STRATEGY_DAILY_INTENT_ENABLED=true` there at next convenient edit). Read
  with a safe default in `services/mode_service.py:_flag_enabled` (default
  `true`).
- **Code default:** `true` (`services/mode_service._flag_enabled`)
- **What it gates:** when `true`, `resolve_strategy_mode(strategy_name)` consults
  the new `strategy_daily_intent` table (`db/openalgo.db`) first, then falls
  through to the legacy `daily_intent` table (simplified only) → env mode flag
  (`SIMPLIFIED_ENGINE_MODE` / `SECTOR_FOLLOW_CAP5_VOL_MODE`) → `sandbox/run`
  default. When `false`, the unified-row step is skipped (pure legacy behavior).
- **History:**
  - **2026-06-10:** Introduced with the unified `{mode, intent}` control surface
    (feat/sector-rotation-etf → `206a5d14`). Default `true`, but **deploy is a
    no-op**: with no `strategy_daily_intent` row for `(strategy, today)` the
    resolver falls through to each strategy's existing env/legacy behavior. The
    operator opts a strategy in by inserting a row (`set_intent`); rolls back by
    deleting it. Migration backfills legacy `daily_intent` rows into the unified
    table once at boot (idempotent, `updated_by='migration'`, `intent='run'`).
    `place_order_service`'s global `resolve_effective_mode()` floor is unchanged
    — the intent gate lives in the engines. Design:
    `docs/design/strategy_daily_intent.md`.
- **Related state:** `db/openalgo.db` → `strategy_daily_intent` table
  (`strategy_name`, `intent_date`, `mode` live/sandbox/skip, `intent`
  run/pause/halt, `daily_capital_cap`). Live env at time of ship:
  `SECTOR_FOLLOW_CAP5_VOL_MODE=sandbox`, `SIMPLIFIED_ENGINE_MODE=live`.
- **Test coverage:** `test/test_strategy_daily_intent.py` (flag-on/off,
  fall-through, migration), plus intent-gate tests in
  `test/test_sector_follow_service.py` and
  `test/test_simplified_stock_engine_service.py`.

#### Mode-only architecture (`strategy_mode` + `strategy_runtime_override`) — 2026-06-12
- **What changed:** the per-strategy control collapses from `{mode, intent,
  daily_capital_cap}` to a single **persistent `mode ∈ {live, sandbox}`** (table
  `strategy_mode`, `database/strategy_mode_db.py`), **default `sandbox`**. The
  run/pause/halt intent axis and the daily-capital cap are retired; automated,
  self-expiring safety guards move to `strategy_runtime_override`
  (`database/strategy_runtime_override_db.py`).
- **Resolver:** `services.mode_service.resolve_mode(strategy_name)` →
  `(mode, source)` with fall-through **`strategy_mode` row → env flag → `sandbox`**.
  `resolve_strategy_mode` / `resolve_effective_mode` remain as **deprecated shims**.
- **Global-gate default change (behavioral):** `resolve_effective_mode()` (the
  external `/api/v1` place/cancel/close gate) **no longer returns `DISABLED` when
  no config exists — it returns `SANDBOX`.** External callers with no setup route
  to the virtual ₹1Cr book instead of being refused. Live external orders now
  require an explicit persistent `strategy_mode` row for the reserved
  `__global__` key (+ `analyze_mode` off). The change only ever makes the path
  *more* sandboxy, never more live. Authorized by the operator ("apply the same
  default-sandbox policy globally").
- **Defaults to know:** `strategy_mode.mode` default `sandbox`; `resolve_mode`
  fall-through default `sandbox`; legacy `mode='skip'` migrates to `sandbox`.
- **Migration:** `scripts/migrate_strategy_daily_intent_to_strategy_mode.py`
  (idempotent; ran on the live DB 2026-06-12 → `simplified_engine=sandbox`).
- **`STRATEGY_DAILY_INTENT_ENABLED`** (above) is superseded — `resolve_mode` does
  not consult it; it is slated for removal as the engines migrate (B3).
- **Test coverage:** `test/test_strategy_mode.py`, `test/test_strategy_runtime_override.py`,
  `test/test_mode_service.py` (mode-only resolver + shim + global-gate-default tests).
- **Ops note — Windows Defender exclusion:** on this dev host, Defender real-time
  scanning intermittently stalls loads of SQLAlchemy's Cython extensions
  (`.venv/.../sqlalchemy/cyextension/*.pyd`), which hangs `pytest` and pre-commit
  hooks. Add a Defender exclusion (elevated PowerShell) to prevent recurrence:
  `Add-MpPreference -ExclusionPath "C:\workspace\ai-trade-agent\openalgo\.venv"`
  (and optionally `-ExclusionExtension pyd`). Not a code parameter — recorded
  here so the operator can configure it.

#### VETO_LAYER_MODE — mode-aware default (B4, 2026-06-12)
- **Current value:** unset → **mode-aware default**: `active` (enforce) when the
  strategy routes to `sandbox`; `shadow` (observe-only) when `live`.
- **Set in:** env var (optional). Read in `services/signal_review_service.get_veto_layer_mode(effective_mode)`.
- **What it gates:** the Stage-1 LLM veto layer that reviews each entry candidate
  before order dispatch (`off` = skip the reviewer; `shadow` = log the verdict
  but always take; `active` = a `skip` verdict blocks the entry).
- **Change:** previously a flat default of `shadow` in every mode. Now, with the
  env var unset, **sandbox enforces by default** so the veto is exercised for
  real on the virtual ₹1Cr book before it ever gates live money; **live is
  unchanged** (`shadow`). An explicit `VETO_LAYER_MODE` wins in every mode and is
  the single emergency disable (`VETO_LAYER_MODE=off`). The simplified engine
  passes its routing mode to `get_veto_layer_mode(self.mode)`; callers without
  mode context still get the safe `shadow` default.
- **Test coverage:** `test/test_signal_review_service.py`
  (`*_sandbox_defaults_to_active`, `*_live_defaults_to_shadow`,
  `*_env_overrides_mode_aware_default`, plus the existing off/shadow/active env tests).
- **.sample.env:** not added (operator WIP holds that file); document
  `VETO_LAYER_MODE` there at the next convenient edit. The mode-aware default
  needs no env entry to function.

#### TELEGRAM_INBOUND_ENABLED
- **Current value:** `false` (default; ships cold)
- **Set in:** env var (not yet in `.sample.env` — operator WIP held that file;
  add `TELEGRAM_INBOUND_ENABLED=false` there at next convenient edit). Read with
  a safe default in `services/telegram_inbound_service.py:_inbound_enabled`
  (default `false`).
- **Code default:** `false` (`services/telegram_inbound_service._inbound_enabled`)
- **What it gates:** when `true`, `init_telegram_inbound_service` (called at boot
  from `app.py`) starts the Phase-6 INBOUND Telegram poller and registers the
  08:45 IST `telegram_inbound_morning_prompt` APScheduler job. The bot lets the
  operator set the unified `strategy_daily_intent` row (run/pause/halt + capital
  cap) from the phone. When `false` (default) the whole module is a no-op — no
  poller, no scheduler job. **Mode flips are never exposed via Telegram** (intent
  axis + cap only); a Telegram intent change preserves the row's existing routing
  mode. Authorization gates on the `bot_config.telegram_chat_ids` allowlist.
- **History:**
  - **2026-06-10:** Introduced with the Phase-6 inbound bot
    (feat/sector-rotation-etf → `00737983`). Default `false` so deploy starts no
    poller; operator opts in by adding their chat_id to
    `bot_config.telegram_chat_ids` (or `add_authorized_chat_id`) and flipping the
    flag to `true`, then restarting. Single-poller-per-token caveat: do not run
    the full interactive `telegram_bot_service` poller on the same bot token
    while this is enabled. Design: `docs/design/telegram_inbound.md`.
- **Related state:** `db/openalgo.db` → `bot_config.telegram_chat_ids` (new column,
  comma-separated allowlist; idempotent ALTER-TABLE migration adds it) and the
  reused Fernet-encrypted `bot_config.token`; writes `strategy_daily_intent`.
- **Test coverage:** `test/e2e/test_critical_flows.py`
  (`TestTelegramInboundEndToEnd`, `TestChatAllowlist`).

### Scanner — Chartink BUY rule

#### CHARTINK_RULE_BUY_GAP_PCT
- **Current value:** `1.5` (1.5% gap-up vs previous daily close)
- **Set in:** `.env` line `CHARTINK_RULE_BUY_GAP_PCT=1.5`
- **Code default:** `3.0` in `services/scan_rules/fno_intraday_buy_chartink.py:113`
- **History:**
  - **2026-06-?? (verified 2026-06-09):** Operator lowered to 1.5 from 3.0 default. Reason: collect more signal data to validate the rule on a wider historical window. The 3.0 default in code matches the original Chartink screener formula; .env override is the working value.
- **Related state:** `db/openalgo.db scan_definitions.id=1.rule_module = fno_intraday_buy_chartink` (set 2026-06-09; was `fno_intraday_buy_20` placeholder)
- **Test coverage:** `test/test_fno_intraday_buy_chartink.py` covers both 1.5 and 3.0 thresholds via monkeypatch

### Scanner — Chartink SELL rule

#### scan_definitions.id=2.rule_module
- **Old value:** `fno_intraday_sell_20` (placeholder rule)
- **New value:** `fno_intraday_sell_chartink`
- **Set in:** `db/openalgo.db scan_definitions.id=2.rule_module` (DB row, not env)
- **Date:** 2026-06-10 (post-close, ~17:08 IST)
- **Why:** Today's scanner-vs-Chartink comparison showed the in-house SELL leg
  fired on 209 of ~220 F&O stocks vs Chartink's 5 (Jaccard 0.024) — the
  `fno_intraday_sell_20` placeholder is far too lenient. Swap to the
  Chartink-equivalent mirror rule `fno_intraday_sell_chartink`. Mirror of the
  BUY-side fix applied this morning (id=1 → `fno_intraday_buy_chartink`).
- **Effective:** immediately. `ScannerService._evaluate_definitions`
  (`services/scanner_service.py:901`) calls `get_scan_definitions(enabled_only=True)`
  on every bar evaluation, and `get_scan_definitions` opens a fresh DB session
  per call (`scanner_service.py:199`) — no boot cache. Rule
  `fno_intraday_sell_chartink` is registered (verified via `get_rule`). No restart
  required.

### Scanner — legacy `_20` rule files removed

#### services/scan_rules/fno_intraday_{buy,sell}_20.py
- **Change:** removed (file deletion). Dropped the two import lines from
  `services/scan_rules/__init__.py` so the package no longer registers them.
- **Date:** 2026-06-10 (post-close)
- **What:** deleted `services/scan_rules/fno_intraday_buy_20.py` and
  `services/scan_rules/fno_intraday_sell_20.py` (the lenient placeholder rules:
  volume surge ≥2× 20-bar avg + close vs 20-EMA).
- **Why:** both were replaced earlier today by their Chartink-mirror equivalents
  (`fno_intraday_buy_chartink` / `fno_intraday_sell_chartink`) and the live DB
  `scan_definitions.id=1/2.rule_module` no longer points at either (see the BUY
  and SELL rule entries above). The dead files were a source of confusion — a
  registered-but-unused rule that looked active. No other production code
  imported them (only the `scan_rules` package self-registration).
- **Test coverage:** the chartink mirrors keep their dedicated tests
  (`test/test_fno_intraday_{buy,sell}_chartink.py`). `test/test_scanner_service.py`
  was decoupled to use self-contained test rules instead of the deleted `_20`
  rules; `test/test_scan_rules.py` now covers only generic registry mechanics.
- **Backout plan:** revert this commit — the rule files remain in git history at
  their last commit on `dev`.

### Scanner — EOD Chartink-vs-inhouse comparison job

#### SCANNER_COMPARISON_EOD_ENABLED
- **Current value:** `true` (default; ships hot)
- **Set in:** env var (not yet in `.sample.env` — operator WIP held that file;
  add at next convenient edit). Read with a safe default in
  `services/scanner_comparison_eod_service._eod_comparison_job`.
- **Code default:** `true`
- **What it gates:** the per-fire body of the `scanner_comparison_eod`
  APScheduler job (15:45 IST mon-fri). When `true`, the job computes the
  in-house-scanner-vs-Chartink comparison for the day, writes one
  `scanner_comparison` row per side, and Telegrams the verdict. When `false`,
  the job is registered but the body is a no-op (so flipping the flag needs only
  a restart, not a re-registration).
- **History:**
  - **2026-06-12:** Introduced with the EOD comparison job that retires the
    Cowork-side `scanner-vs-chartink-daily-comparison` scheduled task (which ran
    read-only but silently failed in the sandbox — no repo/folder access). The
    in-process job is durable: it persists a row AND Telegrams every trading day.

#### SCANNER_COMPARISON_EOD_TIME
- **Current value:** `15:45` (default)
- **Set in:** env var; read in
  `services/scanner_comparison_eod_service.register_jobs` at boot.
- **Code default:** `15:45` (matches the retired Cowork task's cron)
- **What it controls:** the `HH:MM` IST fire time of the `scanner_comparison_eod`
  cron job. Junk values fall back to the default. Changing it requires a restart
  (the trigger is built at registration).
- **History:**
  - **2026-06-12:** Introduced alongside `SCANNER_COMPARISON_EOD_ENABLED`.

#### NOTIFY_SCANNER_COMPARISON
- **Current value:** `true` (default)
- **Set in:** env var; snapshotted at `NotificationService` construction
  (`services/notification_service.py`), so a change needs a process restart.
- **Code default:** `true`
- **What it controls:** whether the `scanner_comparison` notification event is
  delivered to Telegram. When `false`, `notify("scanner_comparison", …)` no-ops
  (the DB row is still written; only the Telegram send is suppressed).
- **History:**
  - **2026-06-12:** Introduced with the EOD comparison job's Telegram summary.

### sector_follow_cap5_vol — strategy

#### SECTOR_FOLLOW_CAP5_VOL_MODE
- **Current value:** `sandbox` (operator `.env`; `.sample.env` still ships `scaffold` default)
- **Set in:** env; read in `services/sector_follow_service.py` (`SectorFollowService.__init__`)
- **Values:** `scaffold` | `sandbox` | `live`
  - `scaffold` (default): compute signals, log, write trade journal — **NO orders placed**
  - `sandbox`: orders routed to `db/sandbox.db` (virtual ₹1Cr)
  - `live`: real broker orders
  - Any unknown value force-falls-back to `scaffold` (logged WARNING).
- **Who flips:** **operator only** — the strategy ships scaffold; `sandbox`/`live` is a deliberate operator decision, never automated.
- **History:**
  - **2026-06-10 (Phase 1+2, merged `3266858f`):** Introduced with the SectorFollowService core + observability endpoints. Default `scaffold` so wiring the service into boot changes no live trading behavior.
  - **2026-06-10 (Phase 5 kickoff):** Operator flipped `scaffold → sandbox` in `.env` (not committed; `.env` is gitignored). Orders now route to `db/sandbox.db` (virtual ₹1Cr) — no live broker orders. First scheduled fire: 2026-06-11 15:20 IST. No engine config changed.

#### config_snapshot.json (locked Phase-0.5 decisions)
- **File:** `strategies/sector_follow_cap5_vol/config_snapshot.json` — canonical source for the strategy's non-env tunables. Loaded by `load_config()`; the `SectorFollowConfig` dataclass mirrors it.
- **Locked values:** `capital_inr` 250000, `max_position_inr` 50000, `max_concurrent_positions` 5, `daily_loss_kill_pct` 3.0, `cooldown_days` 0, entry/exit window 15:20–15:25 IST, daily reset 09:00 IST, gates (sector >1.0%, stock >0.5%, vol >1.0×20d), tiebreaker `volume_ratio_desc`, universe `LOCK_STATIC_30` (30 names), `mode: scaffold-only`, `deployable: false`.
- **Who changes:** operator, recorded in `strategies/sector_follow_cap5_vol/VERSION_LOG.md`.

#### SECTOR_FOLLOW_INDEX_BACKFILL_ENABLED
- Env var (default `true`) gating the daily 16:05 IST sector-index 1m refresh job. Introduced on the Phase 3 branch — full entry lands with that merge.
### sector_follow_cap5_vol — sector-index 1m refresh

#### SECTOR_FOLLOW_INDEX_BACKFILL_ENABLED — RETIRED
- **Current value:** **no longer read** (the 16:05 cron job it gated was removed).
- **Effect:** previously gated registration of the daily 16:05 IST
  `sector_follow_index_backfill` APScheduler job. That cron is gone — the index 1m
  feed is now kept fresh by the boot+periodic state-convergence check (see
  `SECTOR_FOLLOW_PERIODIC_CHECK_ENABLED` below), which is unconditional (no
  per-feed enable flag).
- **History:**
  - **2026-06-09 (Phase 3):** Introduced with the sector-index feed wiring (`feat/sector_follow_cap5_vol_phase3`, commit `3bfa4a08`). Default `true` so a fresh deploy keeps the feed current without extra config.
  - **2026-06-13:** RETIRED. The 16:05/16:10 cron jobs were replaced by a
    boot-time + periodic stale-check (state-convergence pattern); this env var is
    no longer referenced anywhere. Setting it has no effect.

#### sector_follow_stock_backfill (was: daily 16:10 IST cron — RETIRED)
- **Current value:** the 16:10 cron is **removed**; the stock 1m feed is now kept
  fresh by the boot+periodic convergence check (see
  `SECTOR_FOLLOW_PERIODIC_CHECK_ENABLED` below). No env flag (the convergence
  check is unconditional per universe).
- **Effect (historical):** kept the 30 `LOCK_STATIC_30` universe stocks' 1m feed
  fresh in `db/historify.duckdb`. CLI still available for manual multi-day
  catch-up: `uv run python -m services.sector_follow_stock_backfill --from … --to …`.
- **History:**
  - **2026-06-13:** Introduced to close the manual-backfill gap (daily 16:10 IST
    cron). Before this, only the sector **indices** had a daily refresh; a missed
    catch-up held all entries on 2026-06-12 (every stock 2 business days stale).
  - **2026-06-13 (same day):** RETIRED the cron in favor of the state-convergence
    pattern — see below. The directive: *"start once OpenAlgo starts every time
    and start the task based on the last backfill timestamp only if required, for
    index and stocks both, instead of dependency on a scheduler."*

### sector_follow_cap5_vol — boot+periodic 1m feed convergence

Replaces the retired 16:05/16:10 IST backfill crons. On boot (after a broker
session appears) and periodically in the post-close window, the system reads
`MAX(timestamp)` per index + stock from `db/historify.duckdb` and incrementally
fetches only the symbols behind today's expected 15:30 IST close. See
`services/sector_follow_backfill_scheduler.py` (wired in `app.py` via
`init_sector_follow_backfill`).

#### SECTOR_FOLLOW_PERIODIC_CHECK_ENABLED
- **Current value:** unset → code default `true`
- **Set in:** env (not in `.sample.env`); read in
  `services/sector_follow_backfill_scheduler._periodic_enabled`.
- **Values:** `true` / `false` (any value other than `true`, case-insensitive, disables).
- **Effect:** master gate for the **periodic** re-check daemon thread. When
  `false`, only the **boot-time** convergence check runs (the boot check is never
  gated — it is the self-healing replacement for the missed cron catch-up). The
  boot check alone covers the common restart-after-relogin case; the periodic loop
  adds the after-close catch-up on a day OpenAlgo stayed up.
- **Who flips:** operator only.
- **History:**
  - **2026-06-13:** Introduced with the state-convergence refactor (direct to `dev`).

#### SECTOR_FOLLOW_PERIODIC_INTERVAL_MIN
- **Current value:** unset → code default `30` (minutes)
- **Set in:** env; read in `services/sector_follow_backfill_scheduler._interval_seconds`
  (clamped to a 60s floor).
- **Effect:** how often the periodic loop re-checks staleness inside the post-close
  window. 30 min comfortably covers Zerodha's ~5–15 min current-day historical lag
  without hammering the broker's 3 req/sec limit.
- **History:**
  - **2026-06-13:** Introduced with the state-convergence refactor.

#### SECTOR_FOLLOW_PERIODIC_END_TIME
- **Current value:** unset → code default `17:00` (IST, `HH:MM`)
- **Set in:** env; read in `services/sector_follow_backfill_scheduler._end_time`.
- **Effect:** the close of the periodic re-check window (the window opens at the
  fixed `15:30` IST market close). After this time the loop stops checking for the
  day and backs off until tomorrow's window. 17:00 gives ~90 min past close for
  Zerodha to finish publishing the day's post-close 1m bars.
- **History:**
  - **2026-06-13:** Introduced with the state-convergence refactor.

### Scanner universe — boot+periodic feed convergence (1m + daily)

The scanner-side analogue of the sector_follow convergence above, fixing the two
supply bugs the 2026-06-13 Friday-screener replay surfaced (the `SCANNER_SYMBOLS`
F&O universe was never backfilled; the stored `D` interval was universally stale).
On boot (after a broker session appears) and periodically in the post-close
window, it reads `MAX(timestamp)` per symbol for each interval from
`db/historify.duckdb` and incrementally fetches only the symbols behind today's
close — for BOTH `1m` and daily (`D`). See
`services/scanner_backfill_scheduler.py` (+ `services/scanner_universe_backfill.py`),
wired in `app.py` via `init_scanner_backfill_scheduler`.

#### SCANNER_BACKFILL_ENABLED
- **Current value:** unset → code default `true`
- **Set in:** env (not in `.sample.env`); read in
  `services/scanner_backfill_scheduler._backfill_enabled`.
- **Values:** `true` / `false` (any value other than `true`, case-insensitive, disables).
- **Effect:** master gate for the whole scanner convergence (boot hook AND periodic
  loop). When `false`, `init_scanner_backfill_scheduler` is a no-op — the scanner
  universe is not auto-refreshed and the operator must use the CLI. Default-on so a
  fresh deploy self-heals.
- **Who flips:** operator only.
- **History:**
  - **2026-06-13:** Introduced (worktree branch; FF to `dev`).

#### SCANNER_BACKFILL_PERIODIC_CHECK_ENABLED
- **Current value:** unset → code default `true`
- **Set in:** env; read in `services/scanner_backfill_scheduler._periodic_enabled`.
- **Values:** `true` / `false`.
- **Effect:** gate for the **periodic** re-check daemon thread only. When `false`,
  only the boot-time convergence runs (the boot check is never gated). Mirrors
  `SECTOR_FOLLOW_PERIODIC_CHECK_ENABLED`.
- **History:**
  - **2026-06-13:** Introduced.

#### SCANNER_BACKFILL_PERIODIC_INTERVAL_MIN
- **Current value:** unset → code default `30` (minutes)
- **Set in:** env; read in `services/scanner_backfill_scheduler._interval_seconds`
  (clamped to a 60s floor).
- **Effect:** how often the periodic loop re-checks staleness inside the post-close
  window. 30 min covers Zerodha's current-day historical lag without hammering the
  broker's 3 req/sec limit (the larger ~200-symbol universe × 2 intervals takes
  longer per pass than sector_follow's 38).
- **History:**
  - **2026-06-13:** Introduced.

#### SCANNER_BACKFILL_PERIODIC_END_TIME
- **Current value:** unset → code default `17:00` (IST, `HH:MM`)
- **Set in:** env; read in `services/scanner_backfill_scheduler._end_time`.
- **Effect:** close of the periodic re-check window (opens at the fixed `15:30` IST
  market close). After this the loop backs off until tomorrow's window.
- **History:**
  - **2026-06-13:** Introduced.

#### SCANNER_BACKFILL_INTERVALS
- **Current value:** unset → code default `1m,D`
- **Set in:** env; read in `services/scanner_backfill_scheduler._intervals`.
- **Values:** comma-separated subset of `1m,D`. Unknown tokens are dropped; an
  empty/garbage value falls back to both.
- **Effect:** which storage intervals the convergence keeps fresh. Default refreshes
  both the intraday tape (`1m`) and the daily gates (`D`). Set to `1m` only to drop
  the daily arm if the `D` download adds undesirable broker load (the daily gates
  would then revert to whatever else refreshes stored `D`).
- **History:**
  - **2026-06-13:** Introduced.

### Simplified engine — EOD watchdog timing

#### SIMPLIFIED_ENGINE_EOD_WATCHDOG_ENABLED
- **Current value:** unset → code default `true`
- **Set in:** env; read in `services/eod_watchdog_service.py.start_eod_watchdog`
  (via local `_env_bool`)
- **Values:** `true` / `false` (any value other than `1/true/yes/on`, case-insensitive, disables)
- **Effect:** master on/off switch for the APScheduler EOD watchdog (the
  tick-independent backstop that flattens open `trade_journal` rows at end of day
  via `place_order`). When `false`, `start_eod_watchdog` returns early and
  registers no jobs (app boot logs the disable). When `true` (default), one daily
  mon-fri job is registered per intraday strategy. Belt to the tick-driven
  `_maybe_flatten_eod` and the 15:30 reconciliation.
- **Who flips:** operator only (leave `true` — disabling re-opens the
  stranded-position risk the watchdog exists to cover).
- **History:**
  - **2026-06-11:** Introduced alongside the fire-time cap. Default `true`.

#### SIMPLIFIED_ENGINE_EOD_WATCHDOG_TIME
- **Current value:** unset → code default `15:14` (IST, `HH:MM`)
- **Set in:** env; read in `services/eod_watchdog_service.py.start_eod_watchdog`
- **Values:** `HH:MM` 24h IST. Invalid values log an error and fall back to `15:14`.
- **Effect:** caps each strategy's watchdog fire time. The job fires at
  `min(strategy.eod_exit_time, SIMPLIFIED_ENGINE_EOD_WATCHDOG_TIME)` — it honors an
  earlier declared cut-off but never runs later than the cap. The default `15:14`
  is deliberately **one minute before** the 15:15 sandbox/broker MIS
  auto-square-off: sandbox *rejects* MIS orders placed at/after 15:15, so the old
  behavior of firing at the declared `eod_exit_time` (15:20) was always too late
  and stranded positions (the 2026-06-10 OIL/HINDZINC/TATAELXSI orphans, only
  recovered by the 15:30 reconciliation). **Do not set ≥15:15.**
- **Who flips:** operator only.
- **History:**
  - **2026-06-11:** Introduced. Default `15:14` — fixes the 15:20 → post-square-off
    race for the simplified engine's intraday EOD flatten.

### Data-freshness validation (sector_follow_cap5_vol)

#### DATA_FRESHNESS_VALIDATION_ENABLED
- **Current value:** unset → code default `true`
- **Set in:** env; read in `services/sector_follow_service.py.data_freshness_enabled()`
- **Values:** `true` / `false` (any value other than `true`, case-insensitive, disables)
- **Effect:** master switch for the freshness layer — the daily 16:30 IST
  `sector_follow_data_health` APScheduler job (alert + auto-pause on stale data),
  the pre-entry gate in `run_entry` (aborts entries on stale data), and the
  exit-job staleness warning. When `false`, all three are no-ops (pure legacy
  behavior). The `/sector_follow_cap5_vol/api/data_health` endpoint always works
  (it just queries, never gates).
- **Who flips:** operator only.
- **History:**
  - **2026-06-10:** Introduced after the 2026-05-29→06-10 index-feed staleness
    incident (the daily index backfill job did not exist until that day's Phase 3
    commit, so the feed silently sat 12 days stale). Default `true` — ships hot,
    behavior additive (read + alert; auto-pause only on confirmed staleness).

#### MAX_STALENESS_BUSINESS_DAYS
- **Current value:** unset → code default `1`
- **Set in:** env; read in
  `services/data_freshness_service.py.default_max_staleness_business_days()`
- **Values:** non-negative integer. `1` == "yesterday's close is acceptable" (the
  realistic state at 15:20 IST, before today's after-close backfill runs);
  day-before-yesterday is stale.
- **Effect:** the per-symbol staleness threshold (trading days behind the
  reference trading day) above which a symbol is flagged stale. Weekend- and
  NSE-holiday-aware since issue #253 (`is_trading_day()` consults
  `database.market_calendar_db.is_market_holiday()`; fails open to the prior
  weekday-only behavior with a once-per-year WARNING if the calendar has no
  rows for a given year — e.g. a future year before its yearly seed lands).
- **Who flips:** operator only.
- **History:**
  - **2026-06-10:** Introduced with `DATA_FRESHNESS_VALIDATION_ENABLED`. Default 1.
  - **2026-07-06 (#253):** Staleness math became holiday-aware (previously
    weekend-only) — no default/threshold change, just a more accurate
    trading-day count. No new env flag; fail-open is behavior, not a toggle.

### Simplified engine — EOD journal reconciliation

#### ENGINE_EOD_RECONCILIATION_ENABLED
- **Current value:** unset → code default `true`
- **Set in:** env; read in
  `services/simplified_stock_engine_service.py.SimplifiedStockEngineService._maybe_reconcile_eod_journal`
  (via `_env_bool`)
- **Values:** `true` / `false` (any value other than `true`, case-insensitive, disables)
- **Effect:** master switch for the EOD reconciliation step. When `true` (and the
  engine is in `sandbox` mode), the engine — right before it fires the Telegram
  EOD summary — calls
  `services/engine_eod_reconciliation_service.reconcile_engine_journal(today)`,
  which closes any open `trade_journal` row whose sandbox position was already
  flattened by sandbox's MIS auto-square-off (writing the missing exit row with
  `exit_reason='sandbox_eod_squareoff'`). When `false`, the step is a no-op and
  the journal under-reports square-off closures (the 2026-06-10 bug). Read-only on
  `sandbox.db`; idempotent. No effect outside sandbox mode (live/disabled skip it).
- **Who flips:** operator only (rollback lever — leave `true` for correct Telegram
  EOD counts).
- **History:**
  - **2026-06-11:** Introduced. Default `true`.

### Preflight — recent-errors gate noise immunity

#### PREFLIGHT_REQUIRE_PRODUCTION_LOGGER
- **Current value:** unset → code default `false`
- **Set in:** env; read in
  `services/preflight_service.py._count_recent_errors` (via `_env_bool`)
- **Values:** `true` / `false` (default `false`)
- **Effect:** opt-in defense-in-depth for the `recent_errors` preflight gate
  (Failure 4, 2026-06-11). When `true`, an errors.jsonl entry is counted toward
  the abort threshold only if its `logger` field names a known OpenAlgo
  production namespace (`_PRODUCTION_LOGGER_PREFIXES`: services, blueprints,
  database, broker, restx_api, websocket_proxy, sandbox, utils, app, …). An entry
  with a present-but-non-production logger is treated as noise and ignored; an
  entry with no logger field is still counted (real prod entries always carry a
  logger). This makes a pytest-polluted errors.jsonl unable to brick preflight
  even if test DB isolation regresses. When `false` (default) the gate behaves
  exactly as before — every non-test-origin ERROR counts.
- **Caveat:** some legitimate prod errors log under non-namespace names (e.g.
  `zerodha_websocket`); enabling this trades catching those against stronger
  noise immunity. Leave `false` unless a pollution incident recurs.
- **Who flips:** operator only.
- **History:**
  - **2026-06-11:** Introduced. Default `false`. (`.sample.env` doc line deferred —
    that file was operator WIP at commit time; add the documented default there in
    a follow-up.)

> Note: the separator-agnostic fix in the same gate (Windows `\test\` traceback
> paths now match the `test/` marker) is **not** a tunable — it is an always-on
> correctness fix in `_is_test_source_entry`, so it has no PARAMETER_LOG knob.

### Broker WebSocket — event-driven session reconnect (no tunable)

#### ~~BROKER_SESSION_AUTO_RECONNECT_ENABLED~~ (removed — now unconditional default)
- **Status:** **Removed 2026-06-13.** There is no env var. Event-driven WS reinit
  on a broker re-login is the **default, unconditional behavior** — the safety
  guarantee is carried by the hermetic E2E suite
  (`test/test_broker_session_auto_reconnect.py`), not by a flag.
- **What happens (no knob):** the WebSocket proxy reacts to the ZMQ
  `CACHE_INVALIDATE` event that `database.auth_db.upsert_auth()` publishes after
  every broker re-login. `WebSocketProxy._reconnect_broker_adapter(user_id)`
  snapshots the adapter's current symbol subscriptions, disconnects, re-reads the
  new token via `adapter.initialize()`, reconnects, and re-subscribes the held set,
  so the market-data feed resumes **without an OpenAlgo restart**. On reconnect
  failure the snapshot is retained (`_last_known_subscriptions`) and the dead
  adapter is dropped for the next client auth to rebuild. Indian broker tokens
  expire daily ~3 AM IST; this is what lets a morning Zerodha re-login restore the
  WS feed without bouncing the process. The login path also emits a
  `broker_session_refreshed` SocketIO event for UI/observability (not the trigger —
  the proxy is a separate subprocess that can only be reached over ZMQ).
- **History:**
  - **2026-06-13 (AM):** Introduced as `BROKER_SESSION_AUTO_RECONNECT_ENABLED`
    (default `false`) in `feat(broker): event-driven WS reinit on Zerodha session
    refresh` (dev `60ac04546`).
  - **2026-06-13 (PM):** Flag **removed** per operator direction — once the E2E
    tests proved it works, the behavior became the unconditional default
    (`feat(broker): event-driven WS reinit on Zerodha session refresh (no restart
    required, no flag)`). No migration needed; nothing read the env var in
    production yet.

### WS-reconnect historical replay (Fix B-prime)

#### WS_RECOVERY_LOOKBACK_MIN
- **Current value:** `20` (default; not in `.sample.env` — operator WIP holds that
  file. Add `WS_RECOVERY_LOOKBACK_MIN=20` at the next convenient edit.)
- **Set in:** env var. Read with a safe default in
  `services/ws_recovery_service.WSRecoveryService.__init__`
  (`int(os.getenv("WS_RECOVERY_LOOKBACK_MIN", 20))`).
- **Code default:** `20` (minutes of 1m bars fetched per symbol on a WS reconnect).
- **What it controls:** how many minutes of 1m history the WS-reconnect recovery
  service (`ws_recovery_service.py`) pulls per tracked symbol from the broker
  historical API before folding them into the live scanner aggregator via
  `MultiIntervalAggregator.replay_bars`. 20 min comfortably covers a typical WS
  hiccup while staying inside one 1m page. Larger values lengthen the catch-up
  (broker 3 req/sec limit → ~85s for ~250 symbols already).
- **No feature flag for the service itself** — recovery always registers at boot;
  this is the only tunable. The behavior goes live on the next OpenAlgo restart.
- **History:**
  - **2026-06-13:** Introduced with Fix B-prime
    (`feat(broker): historical-API replay on WS reconnect…`, builds on the
    event-driven WS reinit `c5f88a8cf`). Closes the scanner tick-starvation gap
    (the 2026-06-11/12 "1944→7 hits/day" collapse) by replaying the bars missed
    while the socket was down. Test: `test/test_ws_recovery_service.py`.

## `SCANNER_SMOKE_CHECK_*` — scanner pre-entry smoke check (Tier 2, issue #32)

- **Files:** `services/scanner_smoke_check_service.py`, `app.py` (wire-in),
  `test/test_scanner_smoke_check.py`.
- **What it controls:** the 09:18 IST pre-entry smoke check for the in-house
  scanner. Closes the gap CLAUDE.md acknowledges in the Tier-1 hardening
  section — a total feed outage produces no bar closes, so the per-cycle
  completeness metric never fires.
- **Knobs:**
  - `SCANNER_SMOKE_CHECK_ENABLED` (default `true`) — master gate. When false
    the job still registers (so toggling at runtime takes effect without
    re-init) but the check returns `(True, {"skipped": True})` immediately.
  - `SCANNER_SMOKE_CHECK_TIME` (default `09:18`) — cron fire time, `HH:MM` IST.
  - `SCANNER_SMOKE_MIN_COVERAGE` (default `0.5`) — minimum fraction of
    `SCANNER_SYMBOLS` that must have produced at least one live bar today
    via the in-process aggregator.
- **Gates checked:** (1) aggregator coverage ≥ min, (2)
  `data_health_check.latest('scanner_universe_1m').overall_ok`, (3)
  `data_health_check.latest('scanner_universe_D').overall_ok`, (4) broker
  session live.
- **Failure path:** writes a `data_health_check` row with
  `strategy_name='scanner_smoke_check'`, CRIT Telegram via
  `notify('scanner_smoke_check_fail', …)`. **No runtime override is written
  for the scanner** (unlike sector_follow which holds a single entry-job, the
  scanner is a passive consumer with no entry-job to gate).
- **Dedup:** at most one CRIT per `(date, instance)` — second fire on the
  same day is silent. Process restart resets dedup intentionally.
- **History:**
  - **2026-06-21:** Introduced as the upstream gate for the Friday
    2026-06-19 silent-pipeline failure mode (issue #32). Mirrors
    `sector_follow_service.assert_data_pipeline_healthy` (15:18 IST). 12
    hermetic E2E tests in `test/test_scanner_smoke_check.py`.

## `SCANNER_DRY_*` — scanner zero-results tripwire (issue #33)

- **Files:** `services/scanner_dry_tripwire_service.py`, `app.py` (wire-in),
  `test/test_scanner_dry_tripwire.py`.
- **What it controls:** the downstream silent-failure detector for the
  in-house scanner. Catches the Friday 2026-06-19 gap that the Tier-1
  completeness metric missed — completeness was 56% (above the 50% WARN
  floor) while the scanner produced 0 BUY hits all day because the stored
  daily gates ran against ~6-day-old bars.
- **Knobs:**
  - `SCANNER_DRY_TRIPWIRE_ENABLED` (default `true`) — master gate. When
    false the job still registers but `check_dry_scanner` returns
    `{"status": "flag_off"}` immediately without provider calls.
  - `SCANNER_DRY_THRESHOLD_MIN` (default `30`) — gap in minutes from the
    latest `scan_results` row with `source='inhouse'` before the tripwire
    fires. Friday's gap was 6h+; 30 min catches a real silent-failure
    within one full bar window after the 09:30 warm-up.
  - `SCANNER_DRY_CHECK_INTERVAL_MIN` (default `5`) — APScheduler firing
    cadence during market hours (09:30-15:30 IST).
- **Severity logic:** at fire time the tripwire probes `scan_cycle` for
  any `cycle_kind='chartink'` rows within the threshold window. If
  Chartink is producing rows but in-house is silent → **CRIT** (pipeline
  degraded). If Chartink is also dry → **WARN** (market is genuinely
  quiet — visibility only, not a page). A failing Chartink probe defaults
  to **WARN** (don't escalate on telemetry hiccups).
- **Skips that never fire:** outside 09:15-15:30 IST market hours,
  weekends, the 09:15-09:30 IST warm-up window (the scanner can't have
  produced anything yet), or when no broker session is live (operator off
  — silence is expected).
- **Dedup:** per-day-per-severity. CRIT and WARN have independent dedup
  keys so a mid-day regime change (Chartink goes dry) still surfaces
  once. Process restart resets dedup intentionally.
- **History:**
  - **2026-06-21:** Introduced as the downstream silent-failure detector
    paired with the smoke check (`SCANNER_SMOKE_CHECK_*` above) for the
    Friday 2026-06-19 outage. 13 hermetic E2E tests in
    `test/test_scanner_dry_tripwire.py`.

## `SCANNER_PREENTRY_REFRESH_*` — scanner pre-entry data refresh + WS nudge (issue #239)

- **Files:** `services/scanner_backfill_scheduler.py` (functions
  `preentry_refresh_enabled`, `preentry_refresh_time`,
  `run_preentry_scanner_refresh`, `init_scanner_preentry_refresh`,
  `_scanner_preentry_refresh_job`), `app.py` (wire-in next to smoke check),
  `test/test_scanner_backfill_scheduler.py`.
- **What it controls:** a daily 09:16 IST APScheduler job that closes the
  cold-boot gap where the in-process scanner aggregator is still 0/216 at the
  09:18 smoke check — the root of the 2026-06-30 5-day signal drought
  (`scanner_subscribed_at=None`, `gap_min=7745`). The job runs the same
  `check_and_refresh_if_stale` convergence the boot/periodic paths use (for
  BOTH `1m` AND `D` intervals), waits up to `_PREENTRY_WAIT_SEC` (120s) for
  the download jobs, and — if `scanner_pre_subscriber.subscribed` is empty —
  nudges the broker WS subscription via `scanner_pre_subscriber.ensure` so the
  tick aggregator starts filling before the first evaluatable 5m bar at 09:30.
- **Knobs:**
  - `SCANNER_PREENTRY_REFRESH_ENABLED` (default `true`) — master gate. When
    false the APScheduler job is still registered so toggling at runtime takes
    effect without a restart; the per-fire `preentry_refresh_enabled()` check
    makes it a no-op immediately.
  - `SCANNER_PREENTRY_REFRESH_TIME` (default `09:16`) — cron fire time in
    `HH:MM` IST. Must be before the 09:18 smoke check (`SCANNER_SMOKE_CHECK_TIME`)
    and early enough to allow the 120s bounded wait to complete before 09:18.
- **Failure path:** always fail-graceful — historify download errors are logged
  and produce anomaly alerts via `notification_service.publish_anomaly`; the WS
  nudge failure is `logger.exception`-logged but never propagates. A fresh → no-op
  (idempotent).
- **History:**
  - **2026-07-02:** Introduced to close the cold-boot gap that produced the
    2026-06-30 5-day signal drought (issue #239). Mirrors the sector_follow
    pre-entry refresh pattern (`SECTOR_FOLLOW_PREENTRY_REFRESH_*`, issue #237).

## `scanner_dry_tripwire` — WS-absence CRITICAL escalation (issue #239)

- **Files:** `services/scanner_dry_tripwire_service.py` (`check_dry_scanner`,
  `_format_alert`), `test/test_scanner_dry_tripwire.py`.
- **What it controls:** an additive severity-escalation path in the existing
  `scanner_dry_tripwire_service`. When `scanner_subscribed_at is None` (the WS
  connect callback from `ws_connect_callbacks` never fired in this process, so
  the broker WS subscription never came up) AND `gap_min > 60`, the severity
  is forced to **CRITICAL** — overriding the normal chartink cross-check — and
  a Telegram alert fires with `escalation_reason='ws_subscription_absent'` in
  the payload. This is the exact condition of the 2026-06-30 tripwire log
  (`gap_min=7745, scanner_subscribed_at=None, severity=WARN`): the WARN meant
  no page fired despite a 5-day drought.
- **No new flags** — the change is purely additive to the existing
  `SCANNER_DRY_TRIPWIRE_ENABLED` and `SCANNER_DRY_THRESHOLD_MIN` flags. The
  chartink cross-check is still used when `subscribed_at is not None`
  (normal path unchanged).
- **History:**
  - **2026-07-02:** Introduced as part of issue #239. Paired with the
    `SCANNER_PREENTRY_REFRESH_*` job above.

## `SCANNER_REFERENCE_*` — reference-data certificate (issue #305)

- **`SCANNER_REFERENCE_CHECK_ENABLED`** (default `true`): master gate for the
  reference-data certificate + rule-side cross-check
  (`services/scanner_reference_data.py`). The scanner validates the rules'
  settled reference close (`yest_d.close`) against the broker prev-close the
  aggregator_seeder records at boot; a confirmed divergence REJECTS the symbol
  (fail-closed) while a missing broker prev-close fail-opens with a dedup'd
  WARNING. `false` -> no verdict computed or consulted anywhere (pre-#305
  behavior).
- **`SCANNER_REFERENCE_DIVERGENCE_MAX_PCT`** (default `1.0`): max settled-reference
  vs broker-prev-close divergence (percent) before the reference is NOT
  certified. The 2026-07-02 DELHIVERY incident divergence was 6.78%.
- **History:**
  - **2026-07-02:** Introduced by issue #305 / PR #312 after the DELHIVERY
    42x false-BUY on a stale historify-D reference (475.4 vs real 510.0).

## `SCANNER_SMOKE_BLOCK_ENABLED` — smoke-fail post-hold enforcement (issue #305)

- **`SCANNER_SMOKE_BLOCK_ENABLED`** (default `true`): consult-time enforcement
  gate for the 09:18 smoke-check post-hold. While a failed smoke check's hold
  is armed, rule PASSes are still logged but hits are NOT persisted to
  scan_results or posted to the engine; the hold releases on a passing
  re-check (wired into the backfill convergence tick) and self-expires at
  15:35 IST. `false` -> the 09:18 FAIL is alert-only (pre-#305 behavior).
  Runtime flips take effect immediately (no restart).
- **History:**
  - **2026-07-02:** Introduced by issue #305 / PR #312 — on 2026-07-02 the
    09:18 check FAILED loudly ("scanner_universe_1m stale; scanner_universe_D
    stale") and 118 BUY rows posted anyway; this flag turns that class of
    failure into enforcement.

## `SCANNER_BACKFILL_MAX_CATCHUP_DAYS` — backfill catch-up ceiling (issue #304)

- **`SCANNER_BACKFILL_MAX_CATCHUP_DAYS`** (default `7`, floor 1): explicit,
  operator-tunable ceiling on the scanner-universe backfill's incremental
  catch-up window, on top of the per-interval `_LOOKBACK_DAYS` floor (4 for
  `1m`, 15 for `D`). When the gap is wider than the cap, the window is
  clamped and a WARNING names the affected symbols and the manual CLI
  (`uv run python -m services.scanner_universe_backfill --from --to
  --interval`) for the deeper backfill.
- **History:**
  - **2026-07-02:** Introduced by issue #304 / PR #311, alongside
    verified-refresh reporting (refreshed counts are post-job verified reads,
    never submission counts).

## `SCANNER_CHARTINK_MISS_DEBUG_ENABLED` — Chartink-miss gate diagnostics (issue #321)

- **`SCANNER_CHARTINK_MISS_DEBUG_ENABLED`** (default `true`): when a symbol on
  TODAY's Chartink webhook lists (`scan_cycle`, cycle_kind='chartink') FAILs an
  in-house scan rule, log `scanner MISS <sym> rule=<rule> failed_gate=<gate>
  <values>` at INFO (dedup'd per symbol/rule/gate/day; non-listed symbols stay
  at DEBUG). Pure observability — zero gate-outcome change. Purpose: one
  trading day of logs pinpoints the exact failing gate per Chartink-parity
  miss (issue #242 diagnosis).
- **History:**
  - **2026-07-03:** Introduced by issue #321 / PR #322 after the first
    fully-healthy data day still showed in-house recall 0 vs 10+ Chartink
    symbols, with FAIL reasons invisible at production LOG_LEVEL=INFO.

## `SCANNER_AGGREGATOR_SEED_LOOKBACK_MIN` — semantics change to trading minutes (issue #340)

- **`SCANNER_AGGREGATOR_SEED_LOOKBACK_MIN`** (default `500`, unchanged): as of
  issue #340 / PR #341 the value means **trading-session minutes** (09:15-15:30
  IST, weekdays), not wall-clock minutes. The boot seeder walks backward
  through prior sessions until the window contains the requested trading
  minutes, so a pre-market boot seeds prior-day bars instead of an empty
  overnight window. Pre-fix, every pre-market boot seeded 0/227 symbols
  (live-log proof 2026-07-06: "seeded 0/227 symbols, 0 bars total"), which
  starved the 15m RSI(14) warm-up gate until ~13:00 IST and rejected the whole
  universe every morning (first #321 MISS-diagnostic finding: GODREJCP
  failed_gate=15m_warmup 7/15 bars at 10:35).
- **History:**
  - **2026-07-06:** Semantics changed wall-clock → trading minutes (issue
    #340 / PR #341). Default value untouched; 500 trading minutes ≈ 33 15m
    bars, ample margin over the 15 needed even across a holiday (holiday
    calendar itself is issue #253).

## `SCANNER_ACTIVE_TTL_MIN` — /scanner currently-matching window (issue #342)

- **`SCANNER_ACTIVE_TTL_MIN`** (default `12`, floor 1, read at request time):
  the /scanner UI's "Currently matching" list shows a symbol while it has an
  in-house scan_results row within the last N minutes. The rules re-fire every
  5m bar close while conditions hold, so ~2 bar intervals means a symbol drops
  off within a couple of cycles of conditions breaking — Chartink-style live
  semantics with no new scanner state. Signal HISTORY is unaffected (scan_results
  is never filtered/mutated by this TTL).
- **History:**
  - **2026-07-06:** Introduced by issue #342 / PR #343 (operator request:
    stocks shown only while conditions are met; fired signals stay in history).

## `TICK_LIVENESS_*` / `WS_PROXY_*` — feed-death watchdog + supervision (issue #376)

Added by PR #391 after the 2026-07-07 libzmq WSAENOBUFS (10055) assertion killed
the WS/ZMQ side while Flask stayed up — 42 min of silent tick outage.

- **`TICK_LIVENESS_WATCHDOG_ENABLED`** (default `true`): master gate for the
  tick-liveness watchdog (CRIT alert when no live bar closes universe-wide for
  a threshold during market hours; the documented total-outage blind spot the
  completeness metric cannot catch).
- **`SCANNER_LIVENESS_MAX_SILENT_MIN`** (default `10`): minutes of universe-wide
  bar-close silence (09:25-15:30 IST, holiday-aware) before the watchdog trips.
- **`SCANNER_LIVENESS_REALERT_MIN`** (default `30`): re-alert cadence while an
  outage persists.
- **`TICK_LIVENESS_AUTOHEAL_ENABLED`** (default `true`): run the in-process
  auto-heal ladder on trip (re-subscribe nudge -> broker adapter reconnect ->
  WS-proxy subprocess restart -> terminal CRIT). `false` = alert-only.
- **`SCANNER_LIVENESS_LADDER_COOLDOWN_MIN`** (default `30`): the whole ladder
  runs at most once per this window (anti-thrash).
- **`WS_PROXY_MAX_RESTARTS_PER_DAY`** (default `3`): supervisor auto-restart cap
  for the WS-proxy subprocess per IST day; beyond it, CRIT and stop trying.
- **History:**
  - **2026-07-08:** Introduced by issue #376 / PR #391. Follow-ups tracked:
    external main-process supervisor (#384), re-subscription seams (post-#376).

## `SCANNER_SMOKE_TOTAL_HOLD_PCT` / `SCANNER_STRAGGLER_RECHECK_*` — per-symbol hold + intraday heal (issue #390)

Added by PR #393 after 2026-07-08, when 3 of 216 stale 1m symbols armed the
smoke post-hold all-or-nothing and suppressed the ENTIRE scanner all session
(0 signals; hold released 15:36 after close because the release re-check only
ran in the 15:30-17:00 periodic window).

- **`SCANNER_SMOKE_TOTAL_HOLD_PCT`** (default `0.5`): on a smoke FAIL from stored
  1m/D staleness, a stale fraction ABOVE this holds EVERYTHING (genuine dead-feed
  / broker-down morning); at or below it, the hold is PER-SYMBOL — only the named
  stale symbols are held, the fresh majority posts. A `None`/symbol-less hold
  (aggregator-coverage gate fail, legacy caller) is always a total hold.
- **`SCANNER_STRAGGLER_RECHECK_ENABLED`** (default `true`): master gate for the
  mid-session straggler-heal loop.
- **`SCANNER_STRAGGLER_RECHECK_MIN`** (default `15`): interval (minutes) of the
  market-hours (09:20-15:30 IST, trading days) tick that re-fetches still-stale
  symbols, persists verified health rows, and re-runs `re_check_and_release` — so
  a handful of morning stragglers self-heal intraday instead of holding until the
  15:30 periodic window.
- **History:**
  - **2026-07-10:** Introduced by issue #390 / PR #393. Fourth layer of the
    smoke-hold saga: #305 (enforce) -> #319 (release wiring) -> #338 (verified
    health rows) -> #390 (per-symbol + intraday heal).

## `OPEN15_*` — open15_vol_breakout mid-bar breakout strategy (issue #425)

- **What:** `OPEN15_ENABLED` (default `true`) master switch; `OPEN15_MODE`
  (`sandbox` | `observe`, default `sandbox`) — observe journals signals without
  orders; `OPEN15_VOL_MULT` (default `1.5`) — cumvol-in-minute must reach this ×
  the running-avg completed-minute volume; `OPEN15_TOP_N` (default `3`) — top-N
  gainers long / losers short; `OPEN15_MARGIN_PER_SLOT` (default `30000`) and
  `OPEN15_LEVERAGE` (default `5`) → ₹150k notional per trade;
  `OPEN15_TICK_CAPTURE` (default `true`) — master switch for persisting ticks
  to `tick_logs/open15/` for backtest replay;
  `OPEN15_TICK_CAPTURE_UNIVERSE` (default `true`, issue #528) — capture EVERY
  universe symbol's ticks across the whole 09:14:50 → `exit_time`+5s window
  instead of only the day's 3 selected symbols. `false` restores the pre-#528
  targeted behaviour (unselected symbols' 09:15 ticks buffered then dropped).
  `OPEN15_SIZING_MODE` (default `fixed`) — `fixed` | `compound` capital sizing.
  `OPEN15_TRADE_SIDE` (default `both`) — `both` | `long_only` | `short_only`;
  which sides the 09:15 selection may pick at all.
  **Rolling additive watch list (issue #529):**
  `OPEN15_ROLLING_WATCHLIST_ENABLED` (default **`false`**) — master switch;
  `OPEN15_ROLLING_CADENCE_S` (default `30`, clamped **10–300**) — how often the
  universe is re-ranked on live LTP inside the entry window;
  `OPEN15_ROLLING_TOP_N` (default `3`, clamped **1–10**) — how many movers per
  side each cycle may append. All three are UI-editable (below); the clamps are
  applied server-side on BOTH the env read and the saved row, so neither a bad
  `.env` value nor a hand-crafted POST can set a 1-second re-rank.
  **UI overrides:** `margin_per_slot`, `sizing_mode`, `vol_mult`, `instrument`,
  `max_trades`, `no_entry_after`, `exit_time`, `trade_side`,
  `rolling_watchlist_enabled`, `rolling_cadence_s`, and `rolling_top_n` are editable
  from `/open15_vol_breakout/logs` (stored in the `open15_config`
  row; NULL = env default; applied at the next 09:10 arm and recorded in the
  day's `armed` decision-log event). The env vars are the DEFAULTS layer.
  **Data-sourcing (issue #502):** `OPEN15_FIRST_CANDLE_SOURCE`
  (`quotes` | `ticks`, default `quotes`) — where the 09:15 candle's
  open/high/low come from. `quotes` = ONE batched broker quote call at 09:16
  (the `open15_first_candles` job); `ticks` restores the pre-#502 tick-built
  candle. `OPEN15_BASELINE_INCLUDE_FIRST_MINUTE` (default `false`) — whether
  the 09:15 minute stays in the volume baseline.
- **Why these defaults:** mirrors the Round 58 research configuration so the
  sandbox measurement is comparable to the backtest grid; sizing mirrors
  intraday_pullback_top2's ₹30k/slot convention.
- **History:**
  - **2026-07-20:** Introduced by issue #425. Strategy is a measurement
    deployment — see `strategies/open15_vol_breakout/SPEC.md` §2 before tuning
    anything (the bar-level signal has NO honest edge; the mid-bar capture
    fraction is what's being measured).
  - **2026-07-31:** `OPEN15_TRADE_SIDE` added by issue #503 (default `both` =
    no behavior change). Gates `Open15Core._finalize_selection`, so an excluded
    side is never selected, watched, entered or journalled. Note the published
    parity targets are BOTH-sides numbers — a one-sided day is not comparable
    to them, and the logs page flags it.
  - **2026-07-31:** Added `OPEN15_FIRST_CANDLE_SOURCE` (default `quotes`) and
    `OPEN15_BASELINE_INCLUDE_FIRST_MINUTE` (default `false`) — issue #502. The
    tick feed is a ~1/sec sample that starts whenever the first tick arrives,
    so it must not define the 09:15 open (wrong selection: MPHASIS 2026-07-31
    ranked #1 short on a phantom −4.15% vs a real −0.94%/#11) or the breakout
    level (high understated 24/24, low overstated 24/24). Keeping the 09:15
    minute in the baseline inflated it 1.06×–1.67×, so the configured
    `OPEN15_VOL_MULT=1.5` behaved like ~2.5× and produced zero entries on 3 of
    4 sessions. **`OPEN15_VOL_MULT` itself is unchanged at 1.5** — the gate
    now simply means what it says. Both new flags are rollback switches.
  - **2026-08-03:** `OPEN15_TICK_CAPTURE_UNIVERSE` added by issue #528 (default
    `true`). Selected-only capture made the strategy's own entry window
    un-backtestable for any symbol outside the 09:16 gap ranking — the only
    full-universe tick source (`tick_logs/`, written by the simplified engine)
    starts ~09:20-09:23, covering <45% of the 09:16-09:29 window on 4 of 19
    days. **No new broker load** (the ticks already arrive on the service's own
    ZMQ SUB and are parsed before the filter); this changes only what is
    written to disk: ~211 symbols × ~0.6 ticks/s × 900 s ≈ **120k ticks/day
    ≈ 10 MB/day** (retention stays 365 days ⇒ ~2.5 GB/year steady state — revisit
    if disk pressure appears). The writer's queue/batch are widened to
    50000/500 in universe mode so the first-minute burst cannot overflow.
    Set `false` to roll back without touching the master switch.
  - **2026-08-03:** `OPEN15_ROLLING_WATCHLIST_ENABLED` / `_CADENCE_S` /
    `_TOP_N` added by issue #529. **Default OFF — the deploy is a no-op** until
    the operator ticks the box on `/open15_vol_breakout/logs`. Rationale: the
    2026-08-03 replay showed the 09:16 gap ranking put the day's four biggest
    movers at ranks #22/#106/#130/#134, so a one-shot snapshot watches the
    wrong names — but the SAME study could NOT show the added names are
    profitable (3 incremental trades, +₹162, 1 win of 3, on 4 usable days).
    This ships as a MEASUREMENT (journal column `open15_trades.watch_source ∈
    {seed, rolling}` scores the two cohorts apart), NOT as a validated edge; a
    promotion decision waits on the #528 sample. The entry gate is untouched —
    added symbols compete for the same `max_trades` slots — and the list is
    strictly additive, so the 09:16 seed picks are never dropped. Cadence
    default 30 s: fast enough to catch a leaderboard that churns within the
    13-minute window, slow enough that the re-rank (a sort over ~211 floats on
    the tick thread) is negligible.

## `INTRADAY_PULLBACK_TRADE_SIDE` — intraday_pullback_top2 trade side (issue #509)

- **Value:** `both` (default) | `long_only` | `short_only`. Env var is the
  DEFAULTS layer; a `trade_side` value in the `intraday_pullback_config` row
  (set from the strategy settings page or `POST
  /intraday_pullback_top2/api/settings`) overrides it. NULL/unset = env → the
  `trade_side` key in `config_snapshot.json` → `both`.
- **What it does:** gates which book may run, enforced in
  `IntradayPullbackService.run_selection` immediately after the 09:30 NIFTY day
  gate. An excluded side is never selected, never watched, never triggers and
  never journals a row — the same shape as open15's `OPEN15_TRADE_SIDE` (#503).
  Applied at the 09:00 daily reset, like the other editable settings.
- **Load-bearing semantics — this is NOT a rebalance.** The long and short
  books are **mutually exclusive by the day gate** (NIFTY up at 09:30 → long
  book only; NIFTY down → short book only). Excluding a side therefore means the
  strategy **does not trade at all** on the days that side would have run:
  `long_only` gives up every NIFTY-down day (~half the calendar), it does not
  run longs on down-days. A skip records
  `skip_reason='trade_side=…'` on `get_status()` / `entry_breakdown()` so it
  stays distinguishable from a data outage.
- **Why default `both`:** it is the backtested configuration. The R53 figures
  (PF 1.60, +97.6% fixed, Sharpe 2.96, MaxDD −8.9%) are **both-sides numbers** —
  a one-sided run is not comparable to them, and the settings page flags this.
  Per-side backtest contribution: long 155 trades / PF 1.72 / +₹44,202; short
  80 trades / PF 1.40 / +₹14,362.
- **Failure mode:** an unrecognised env or stored value falls back to `both`
  with a WARNING rather than darkening a book on a typo.
- **History:**
  - **2026-08-02:** Introduced by issue #509 (default `both` = no behaviour
    change). Motivated by the strategy's own LEARNINGS: *"Long is the validated
    primary; the short is promising-but-unproven"*, with the deep-loser short
    called out as the most slippage-fragile leg — so disabling the short during
    the sandbox measurement phase needed to be an operator control rather than a
    code edit. First tunable cataloged for this strategy.

## `NOTIFY_OPEN15_BREAKOUT`

- **Default:** `true` (gated, as every `NOTIFY_*` is, by the master switch
  `NOTIFY_TELEGRAM_ENABLED`).
- **Where:** `services/notification_service.py` `per_event` registry; caller is
  `services/open15_breakout_service.py` `_alert_rejection`.
- **What it controls:** the operator Telegram alert when the broker REJECTS an
  open15_vol_breakout entry order — no position was taken and the trade is
  recorded as a PAPER fill instead.
- **Deduped once per day, deliberately.** A static-IP or RMS block rejects every
  entry with the identical message; on 2026-08-05 that would have been three
  identical alerts. Repeated identical alerts are how a channel gets muted, and
  a muted channel is worse than no alert.
- **Failure mode:** the alert is fail-open — a dead Telegram bot never blocks or
  delays an entry. The `logger.error` line (which reaches `log/errors.jsonl`)
  fires unconditionally and is the durable record; Telegram is the nudge.
- **History:**
  - **2026-08-05:** Introduced by issue #548. Before it, open15 had **no alert
    path at all** — three live entries were rejected with a static-IP 403 and
    the only trace was an INFO decision-log line reading `order_status: error`,
    with the broker's message discarded entirely. `sector_follow` had logged
    `[live] … ENTRY REJECTED … <message>` at ERROR since its own build-out; this
    brings open15 to parity and adds the Telegram leg.

## Other tunables (placeholder — populate as discovered)

The following are known tunables that should be cataloged in subsequent commits
as they're touched:
- `SIMPLIFIED_ENGINE_MODE` (sandbox / live / disabled)
- `SIMPLIFIED_ENGINE_*` parameters (ATR mult, max trades, cooldown, etc.)
- `OPENALGO_BOOT_DIRTY_CHECK_ENABLED` (default True)
- Sector rotation ETF params: `capital_inr`, `mode`, `deployable`, window times
- Various others in `.sample.env`

This list is not exhaustive — add entries as you touch parameters.
