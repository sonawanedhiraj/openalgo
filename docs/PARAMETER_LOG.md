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

### Live-mode broker-position reconciliation (issue #265, proposed 2026-07-01)

> Proposed by feature branch `feat/265-live-position-reconciliation` (staged,
> operator-reviewed order path). Land the entry on `dev` at merge time.

#### LIVE_POSITION_RECONCILE_ENABLED
- **Current value:** unset → defaults **`true`**.
- **Set in:** env; read by
  `services/live_position_reconciliation_service.is_enabled()` inside
  `reconcile_exit`. The two engine call-sites additionally gate the whole guard
  on their own **LIVE** mode, so in sandbox the guard is never even invoked.
- **What it does:** master gate for the LIVE-mode exit reconciliation guard. When
  ON (and the strategy is in `live` mode), every live exit reconciles its
  journalled/in-memory close quantity against the real broker net position
  (`services/openposition_service.get_open_position`) before the exit order is
  placed:
  - broker flat (net 0) → **SUPPRESS** the exit (phantom);
  - broker holds fewer than journaled, or sits on the opposite side → **CLAMP**
    to the broker qty (opposite side → clamp to 0 = suppress);
  - broker consistent → **PROCEED** with the journalled qty (never more);
  - broker fetch fails / no api key → **FAIL CLOSED for reverse-risk** (proceed
    with the journalled qty, never an unbounded one) + drift alert.
  On any mismatch it emits a position-drift alert via
  `services.source_divergence_alerts.check_and_alert` (`journal_qty` vs
  `broker_qty`, per-(strategy, symbol, IST-day) dedup).
- **Wired at:** `services/futures_follow_service.py`
  (`place_exit` → covers `run_exit` / `run_eod_watchdog` / `close_all_positions`,
  plus a live-only boot `rehydrate_paper_book_from_broker`) and
  `services/simplified_stock_engine_service.py` (`_flatten_for_api_key`
  engine-known qty reconcile + phantom suppress, and `flatten_strategy_positions`
  broker-aware clamp/suppress).
- **Sandbox invariant:** in `sandbox` mode the guard is a strict no-op — the
  broker positionbook is NEVER consulted; sandbox keeps reading `sandbox.db` and
  `engine_eod_reconciliation_service` is unchanged. Proven by regression tests
  that assert the positionbook mock is not called in sandbox.
- **Why default true:** live money. The broker must be the source of truth at
  exit; a journal↔broker mismatch (manual/partial exit, restart-lost `paper_book`,
  phantom) could otherwise double-SELL into a net-short overnight future or fire a
  reversing exit. Set `false` only as an emergency disable to fall back to the
  legacy journal-driven exit path.
- **Safety guarantee:** `test/test_live_position_reconciliation_service.py`
  (helper semantics) + live/sandbox exit + boot-rehydrate cases in
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
  watchdog **15:14**, daily reset 09:00, EOD summary 15:30 IST (all `mon-fri`,
  `Asia/Kolkata`). The watchdog at 15:14 fires before any auto-square-off window.
- **Product/exchange:** `product` NRML (futures carry — not MIS/CNC), `exchange`
  NFO, MARKET orders. `cost_pct_round_trip` 0.030 (~₹530/lot).
- **Who changes:** operator, recorded in
  `strategies/futures_follow_cap50/VERSION_LOG.md`.
- **Shared flag:** the data-freshness gate reuses `DATA_FRESHNESS_VALIDATION_ENABLED`
  + `MAX_STALENESS_BUSINESS_DAYS` (documented under sector_follow) since the futures
  sleeve fires on the sector_follow signal feed.

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
- **Effect:** the per-symbol staleness threshold (business days behind the
  reference trading day) above which a symbol is flagged stale. Weekend-aware;
  market holidays are NOT modelled (a mid-week holiday inflates measured staleness
  by one business day — the default-1 threshold absorbs the common case).
- **Who flips:** operator only.
- **History:**
  - **2026-06-10:** Introduced with `DATA_FRESHNESS_VALIDATION_ENABLED`. Default 1.

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

## Other tunables (placeholder — populate as discovered)

The following are known tunables that should be cataloged in subsequent commits
as they're touched:
- `SIMPLIFIED_ENGINE_MODE` (sandbox / live / disabled)
- `SIMPLIFIED_ENGINE_*` parameters (ATR mult, max trades, cooldown, etc.)
- `OPENALGO_BOOT_DIRTY_CHECK_ENABLED` (default True)
- Sector rotation ETF params: `capital_inr`, `mode`, `deployable`, window times
- Various others in `.sample.env`

This list is not exhaustive — add entries as you touch parameters.
