# Sector Follow (Cap-5, Volume-Tiebreaker) — Learnings

Cumulative knowledge for the `sector_follow_cap5_vol` strategy. Read this before
making any decision. Most-important file in the strategy folder.

## Validated facts (from backtest research, pre-deployment)

_(populate as Phase 0.5 / shadow-replay / sandbox produce evidence)_

## Implementation notes (scaffold)

_(populate as the service module is built)_

## Live Learnings

### 2026-06-15 — Silent zero-signal incident + Fix 1b (aggregator-source + loud failure)

**Incident.** The first sandbox cycle (Mon 2026-06-15, 15:20 IST) produced **0
signals**. Root cause was a **data-supply gap, not gate logic**: at 15:20
`historify.duckdb` had **no stock 1m bars for today** — the backfill convergence
loop only runs 15:30–17:00 IST, so nothing had landed today's intraday yet. The
live evaluator read *today's* stock return from historify, got `None`,
`passes_gates()` failed closed for every name, and the strategy emitted nothing
**with no alert**. Forensics: `docs/research/strategy/sector_follow_cap5_vol/2026-06-15_gate_forensics.md`.

The cruel part: the WS feed was ticking the **entire `LOCK_STATIC_30` universe**
the whole session — those 1m/5m bars lived in the in-process scanner aggregator
(`ScannerService`, which subscribes all of `SCANNER_SYMBOLS`, and all 30 sector
stocks are in it). sector_follow just never consulted it.

**Fix 1b (this branch).** Split market data by its natural source:

1. **TODAY's intraday close+volume → the in-process scanner aggregator.**
   `production_intraday_provider` → `ScannerService.get_today_ohlcv(symbol, date)`
   aggregates today's closed history + the in-progress bar. The **20-day lookback**
   (prior close, avg daily volume) stays on historify — the correct source for
   *historical* days, which never has today's intraday mid-session anyway.
2. **Loud failure** is the systemic fix (the bug was *silent*, not just wrong):
   - per-symbol historify fallback for today's data logs a **WARNING**;
   - `evaluate_candidates` logs **PASS/FAIL per symbol** with the exact gate or
     `None`-input reason;
   - a **decision-input completeness** metric (`n_on_live_intraday / total`)
     Telegrams **WARNING <50% / CRITICAL <20%** — a degraded pipeline can no
     longer masquerade as a real zero-signal day.
3. **15:18 IST pre-entry smoke check** (`assert_data_pipeline_healthy`): aggregator
   coverage ≥ 50%, historify lookback works, broker session live. On failure it
   writes a self-expiring `pause` runtime override (holds the 15:20 entries) +
   alerts. Flags: `SECTOR_FOLLOW_SMOKE_CHECK_ENABLED` / `SECTOR_FOLLOW_SMOKE_MIN_COVERAGE`.

**Known limitation (load-bearing — do not lose):** 6 of the 8 mapped sector
indices (NIFTYAUTO/FMCG/IT/METAL/PSUBANK/PVTBANK) are **not** in `SCANNER_SYMBOLS`,
so their `sector_ret` still comes from **historify** via the fallback (logged as
`intraday_source='historify'`), not the aggregator. Only NIFTY + FINNIFTY are in
the scanner universe. The stock side — the part that actually broke on 06-15 — is
now fully live-sourced; the index side depends on the index 1m feed being fresh at
15:20 (it was on 06-15: NIFTY +0.991% was computed). If a future incident is an
*index* staleness, the fix is to add the sector indices to the scanner universe
(or give sector_follow its own index aggregator), not to touch the stock path.

**Also note:** the scanner tracks `SCANNER_INTERVALS` (default **5m**, not 1m).
`get_today_ohlcv` reads the primary interval — 5m bars give the same daily
aggregates (today close = last bar close; today volume = sum of today's bar
deltas), so the "1m" in the original plan is satisfied by the 5m series without
forcing a scanner-interval change.

### 2026-06-10 — Strategy spawned from R40 winner V_SF_CAP5_VOL

Strategy spawned from R40 winner `V_SF_CAP5_VOL`. Carrying the R40 backtest as
the truth source for parity checks (Sharpe 2.37 daily / 1.92 monthly, payoff
1.39, EV +0.63%/trade, MaxDD −8.76%, 434 trades over 2.4 yr, 2026-YTD +12.9%,
max concurrent positions = 5). Operator decisions locked the same day — see
[`PLAN.md`](PLAN.md) "Operator decisions". Phase 0 starting next.

### 2026-06-10 — Phase 0.5 decision: universe LOCK_STATIC_30 (re-rank loses)

Resolves operator decision #2 (universe re-rank was **conditional on Phase 0.5
showing re-rank ≥ static**). It does not. **Universe = static top-30, locked.**

A/B on the R40 cap5_vol harness using the **complete Phase-0 sector map** (19/30
mapped to a real sectoral index vs R40's 14), 2024-01→2026-06, identical
entry/exit/cap/cost. Static = top-30 by full-window traded value, locked.
Rerank = top-30 by trailing-90d traded value, recomputed 1st-of-month from the
147-symbol liquid pool.

| Metric | STATIC30 | RERANK_M |
|---|---|---|
| N trades | 625 | 622 |
| Win rate | 56.3% | 57.2% |
| Payoff | 1.44 | 1.13 |
| EV/trade | 0.454% | 0.329% |
| Sharpe (daily) | 2.19 | 1.20 |
| Sharpe (monthly) | 2.49 | 1.50 |
| Sortino (monthly) | 9.01 | 1.21 |
| Max DD (daily) | −8.8% | −15.2% |
| Calmar | 2.79 | 1.09 |
| Green months | 83.3% | 76.7% |
| 2026 YTD | +5.96% | +6.20% |

**Decision: LOCK_STATIC_30.** Static dominates on every risk-adjusted metric
(Sharpe(d) +0.99, Sortino 9.0 vs 1.2, Calmar 2.8 vs 1.1, half the drawdown,
green-months 83 vs 77%). Re-rank only nudges win-rate (+0.9pp) and YTD (+0.24pp),
both swamped. Fails both promotion gates (needed ΔSharpe ≥ +0.30 & ΔEV ≥ +0.10pp;
got −0.99 & −0.124). Monthly re-rank churns ~3.3/30 names/month (11% turnover, 72
distinct stocks over the window) toward recently-liquid momentum names with a
weaker sector-follow edge — complexity that actively hurts. Not borderline; no
operator confirmation needed.

**Sector-map note:** completing the map (RELIANCE→OILANDGAS, INFY/TCS→IT,
M&M/MARUTI→AUTO, AXISBANK/INDUSINDBK→PVTBANK, BSE/BAJFINANCE/JIOFIN→FINNIFTY,
DIXON→CONSRDURBL; JIOFIN re-tagged off PVTBANK) raised candidate trades 434→625 and
moved Sharpe 2.37→2.19 vs the R40 partial map. More real sector signals fire more
often; 2.19 is the honest baseline (R40's 16 NIFTY-defaults made half the "sector
signal" a market-day signal). 11 names still default to broad NIFTY — no
representative index in our data (telecom, defence-PSU, aviation, retail, infra).
See [`sector_map.json`](sector_map.json). Artifacts:
`outputs/sector_follow_cap5_vol_phase05_2026-06-10/`.

### 2026-06-10 — Phase 1: core service module + scheduler hooks shipped on feat/sector_follow_cap5_vol_phase1

- `services/sector_follow_service.py` (700 lines) — config/sector-map loaders,
  pure gate evaluator (`passes_gates`), `select_entries` (cap-5 + vol-ratio-desc
  tiebreaker + skip-open), `compute_qty` (floor to integer shares), mode-aware
  `place_entry`/`place_exit`, kill switch (3% of capital, blocks entries not exits),
  `run_entry`/`run_exit`/`run_daily_reset` job bodies, `register_jobs` on the shared
  historify APScheduler (15:20 / 15:25 / 09:00 IST cron, mon-fri, replace_existing),
  idempotent `seed_strategy` into the `strategies` table, production DuckDB metrics
  provider (read-only, derives daily from 1m per data_coverage.md), Telegram notifier.
  All I/O injected (mirrors `scanner_ws_watchdog.py`) so tests need no broker/DuckDB.
- `database/sector_follow_db.py` (new file) — `sector_follow_trades` journal table
  in `openalgo.db` (additive; existing schema untouched). Phase 0's strategy_id_design
  flagged that the live order/trade journal has no `strategy_id` column — rather than
  modify it, Phase 1 owns its own attributable table. `strategy_id` FK is nullable
  until `seed_strategy` resolves it.
- `test/test_sector_follow_service.py` (19 tests) — all 13 required cases + 6 extra
  (fail-closed-on-None, remaining-slots, above-threshold no-fire, daily-reset,
  qty-uses-max-position, end-to-end run_entry cap). Fully stubbed I/O.
- `app.py` hook (~11 lines) — `init_sector_follow_service(app=app)` after the
  historify scheduler block. Default mode=scaffold → zero live behavior change.
- `.sample.env` — `SECTOR_FOLLOW_CAP5_VOL_MODE=scaffold`.
- `config_snapshot.json` — extended with the locked static-30 universe, time
  windows, vol lookback, and the parity_target block.

Tests written but **NOT executed** (market-hours pytest ban — pollutes the live
journal). Operator runs `uv run pytest test/test_sector_follow_service.py -v`
post-close to verify before merging to dev.

Parity target from Phase 0.5: Sharpe(d) 2.19, payoff 1.44, EV 0.454%/trade,
win-rate 56.3%, 625 trades (2024-01..2026-06). Phase 4 shadow-replay verifies the
live signal path reproduces these against the backtest.

**Production metrics-provider caveat (Phase 4 to confirm):** sector return is
computed from the mapped index's **1m** bars; if an index has no 1m history,
`sector_ret=None` and the stock fails the gate (fail-closed). data_coverage.md
confirms stocks have full 1m but did not audit index 1m — shadow-replay must verify
index intraday coverage or the live sector gate silently never fires for those names.

Phase 2 ready to spawn after this branch merges to dev + tests pass.

## 2026-06-10 — Phase 2: observability + index coverage shipped on feat/sector_follow_cap5_vol_phase2

- `blueprints/sector_follow.py` — API-key-auth endpoints at `/sector_follow_cap5_vol`:
  `GET /api/status`, `GET /api/positions`, `POST /api/pause`, `POST /api/resume`,
  `POST /api/close_all` (requires `{"confirm":"yes"}`). Registered in `app.py`.
- `services/sector_follow_service.py` extended: `manual_pause` flag + `pause()`/
  `resume()`, `kill_switch_reason`, intraday `today_entries`/`today_exits` journals,
  `get_status()`, `open_positions_view()`, `close_all_positions()`,
  `build_eod_summary()`/`run_eod_summary()`. New 15:30 IST EOD-summary scheduler job.
- `index_data_coverage.md` (Deliverable 1): **8 of 10 mapped indices have 1m, 2 do
  not** (NIFTYCONSRDURBL→DIXON, NIFTYOILANDGAS→RELIANCE — daily-only, status
  `1M_MISSING_USE_DAILY`, Phase 4 work item). **0 NO_DATA → no `sector_map.json`
  edit needed.** Big finding: even the 8 "READY" indices have a **freshness gap** —
  index 1m is stale (last bar 2026-05-29 vs stock 1m 2026-06-08, ~12 days behind in
  the snapshot). Index 1m is a one-off partial backfill, not a daily-maintained feed;
  if it isn't backfilled/subscribed daily, **every** stock fails-closed (no
  `sector_ret`), not just the two daily-only names. Must wire index 1m into the daily
  historify backfill before sandbox go-live — flagged Phase 3/4.
- Audit method: live `historify.duckdb` is exclusively write-locked by the running
  app during market hours (read-only connect still fails), so the audit ran against a
  3.6GB file-copy snapshot. `index_1m_audit.py` takes `--db <path>` / `HISTORIFY_DB_PATH`
  for exactly this; operator re-runs against the live file post-close to refresh.
- EOD Telegram summary at 15:30 IST (reuses Phase 1 `telegram_notifier`; silent if TG off).
- Tests: 5 added (total: 19+5=24). Written, NOT executed (market hours / pollutes journal).

**Scope adjustment:** Original PLAN.md Phase 2 included "wire strategy_id tagging in
order_router" — **DEFERRED** to a separate platform-wide task. Phase 1's isolated
`sector_follow_trades` table is sufficient for this strategy. Cross-strategy
attribution wiring in the platform router is its own work (benefits the 5-sleeve
blend, not just this strategy) and modifying the shared router during one strategy's
build-out is scope creep.

Phase 3 ready: risk + monitoring (much of which Phase 1 + Phase 2 already cover —
kill switch, pause/resume, EOD summary all shipped). Phase 3 likely = Telegram polish,
operator dashboard, and wiring the index-1m daily backfill surfaced above.

### 2026-06-10 — Discipline miss: architecture docs lagged the Phase 1+2 merge

Phase 1+2 (`3266858f`) shipped a new strategy, a new `sector_follow_trades` DB
table, a new service with 4 APScheduler jobs, a new `/sector_follow_cap5_vol/api/*`
blueprint, and a new `SECTOR_FOLLOW_CAP5_VOL_MODE` env var — **without** the
matching updates to `docs/SYSTEM_MAP.md`, `CLAUDE.md`, and `docs/PARAMETER_LOG.md`
that `CLAUDE.md` "Documentation discipline — change docs WITH code, not after"
requires in the **same** commit. Caught and back-filled the next day (this commit,
direct to dev), with the Phase 3 index-backfill docs landing on the Phase 3 branch.

**Lesson:** for this strategy specifically, an architecture surface (DB table /
scheduled job / endpoint / env var / new service) is added in almost every phase —
so the SYSTEM_MAP + CLAUDE.md + PARAMETER_LOG triplet must be part of each phase's
diff, not a follow-up. When opening the next phase's PR, the checklist item "does
this add/rename a table, job, endpoint, env var, or process? → update the three
canonical docs in THIS commit" is non-optional. Note also: the brief described the
service as registering 3 jobs; it actually registers 4 (an EOD-summary job at 15:30
IST was added) — the docs now reflect the code, which is the source of truth.
## 2026-06-10 — Phase 3: data feed wiring + live MTM shipped on feat/sector_follow_cap5_vol_phase3

- Wired sector indices into the daily 1m backfill: new `_register_sector_follow_index_job`
  in `services/historify_scheduler_service.py` (~33-line additive method + a 4-line
  call in `init()`) registers an APScheduler job at **16:05 IST** (after the 16:00
  scanner refresh + market close). Gated by `SECTOR_FOLLOW_INDEX_BACKFILL_ENABLED`
  (default true), `replace_existing=True`. Additive — routes index 1m through the
  same `create_and_start_job` pipeline as the stock backfill; never touches the
  watchlist schedules.
- One-shot catch-up script: `services/sector_follow_index_backfill.py` —
  `uv run python -m services.sector_follow_index_backfill --from 2026-05-29 --to 2026-06-10`.
  Symbol set = unique `sector_map.json` index values UNIONed with the two known
  1m-missing indices (defensive — feed populates automatically if the broker ever
  returns them). Exchange `NSE_INDEX`.
- Re-mapped **DIXON, RELIANCE -> NIFTY** (broad_market) in `sector_map.json`.
  Rationale: NIFTYCONSRDURBL + NIFTYOILANDGAS have NO 1m feed (daily only), so the
  intraday sector gate could never fire for those two stocks — fail-closed forever.
  Option A (PLAN). Backtest-neutral: Phase 0.5 showed completing the map was a slight
  drag (Sharpe 2.37->2.19) because more stocks correctly fail the sector gate;
  re-mapping 2 names back to NIFTY does not materially change strategy economics.
  Revisit if those indices ever get a 1m feed.
- Live MTM in the status endpoint: `production_price_fetcher` (broker quote LTP) +
  injectable `price_fetcher` + `_compute_mtm()` in `services/sector_follow_service.py`.
  `open_positions_view()` now returns `mtm_pnl_gross` / `mtm_pnl_net` (net charges the
  0.0857% round-trip once on entry notional — the rate is already both-legs) /
  `current_price` / `mtm_error`; `mtm_pnl` kept as a legacy alias of net.
  `get_status().today_pnl_net` = realized (closed exits) + unrealized (open MTM net),
  with `today_pnl_realized_net` / `today_pnl_unrealized_net` broken out. Defensive:
  a price-fetch failure leaves P&L None + sets `mtm_error`, never crashes the endpoint.
- Tests: 4 added (total 28). All pass.

Phase 4 ready: shadow-replay last 30 trading days against R40 backtest. Parity
target: Sharpe 2.19, payoff 1.44, EV 0.454%/trade.

Open question: how often does the operator run the one-shot index backfill?
Recommend: once now (catch up the ~12-day gap), then trust the 16:05 daily
scheduler. After the re-map no stock depends on NIFTYCONSRDURBL/NIFTYOILANDGAS, so
the broker failing to deliver their 1m affects nothing. The new 16:05 job is an
in-app APScheduler job (like scanner_history_refresh) — not tracked in SYSTEM_MAP
(that table is host-side Cowork tasks only).

## 2026-06-10 — Phase 4: shadow-replay parity check

Window: last 30 trading days, 2026-05-05 .. 2026-06-08 (30 days).
Harness: `outputs/sector_follow_cap5_vol_phase4_2026-06-10/run_phase4.py` (force-added;
CSVs gitignored). Production track = real `duckdb_metrics_provider`/`passes_gates`/
`select_entries` at 15:20 IST with T+1 carryover; R40 track = backtest full-day gate
screen + top-5 vol_ratio, no carryover. Both use post-Phase-3 sector_map + static-30.

Entries: Jaccard 0.50 (production-only 2, r40-only 13, matched 15)
P&L: mean abs diff 0.0000pp on 15 matched trades, max diff 0.0000pp
Verdict: NEEDS_INVESTIGATION

Production CODE is sound — exact P&L parity, correct gates + carryover. The 15
entry mismatches attribute to: 10x **backtest look-ahead** (R40 evaluates gates on
the realized full-day CLOSE; the live design + production evaluate at 15:20, before
the final ~10min — DIXON -0.18%@15:20 -> +4.23%@close flips the gate; KOTAKBANK
+1.6%@15:20 -> -0.5%@close is the false-positive flip side); 3x **NIFTYIT 1m gap** on
2026-06-01/06-02 (production sector_ret=None -> fail-closed, skips TCS/INFY; backtest
used daily-native NIFTYIT which had data — same failure mode as the RELIANCE/DIXON
re-map); 1x T+1 carryover (M&M re-entry suppressed correctly); 1x vol-avg lookback
window edge (IRFC).

**Phase 5 go-live NOT cleared.** Blocker is the parity TARGET, not the code: the
Sharpe-2.19 baseline was computed with closing-print look-ahead and overstates the
live 15:20 edge (~10/28 backtest entries/30d would not fire at 15:20). Before go-live:
(1) re-derive the backtest baseline with 15:20-snapshot gates for an honest expected
Sharpe/EV; (2) confirm the 1m index feed (esp. NIFTYIT) is current each session +
monitor `sector_ret is None`. No production code changes recommended. See
`outputs/sector_follow_cap5_vol_phase4_2026-06-10/REPORT.md`.

## 2026-06-10 — Phase 4.5: honest 15:20-snapshot baseline (NEW OFFICIAL BASELINE)

Harness: `outputs/sector_follow_cap5_vol_phase4_5_2026-06-10/run_phase4_5.py`
(force-added; CSVs gitignored). Built the honest baseline by calling the
**production provider** per day (gates+entry @15:20; R40 look-ahead snapshot
@15:30; exit = T+1 full close). Used the provider — not an independent
reimplementation — because the 1m `timestamp` column has an inconsistent epoch
convention that splits a single IST session across naive `(ts+19800)/86400`
day-buckets (2026-05-29 diverged on all 30 symbols), so naive daily grouping
mis-aggregates. The provider's `fromtimestamp(ts,IST).date()` + `ts<=as_of` is
authoritative and is what live trading sees. **Flag for a data-integrity pass on
the 1m timestamp column before scaling capital.**

**HONEST BASELINE — this replaces R40's 2.19 for all future references.**
NIFTY-1m window (2025-12-02..2026-05-29, 120 eval days, 76 trades):
Sharpe(d) **1.70**, Sharpe(m) 1.71, payoff **1.42**, EV **+0.248%/trade**,
MaxDD −4.65%, win **48.7%**. Sector-1m window (22 days, all 30 names, 22 trades):
Sharpe(d) 1.77, payoff 1.56, EV +0.051%, win 40.9%.

**Look-ahead delta (15:20 vs 15:30, same days/structure):** SMALL and FAVOURS
15:20 (Sharpe +0.34, EV +0.137pp, win +4.6pp) — entering before the closing
run-up is a cheaper fill. Phase 4's "Sharpe 2.19 is look-ahead-inflated" thesis
is only partly right: the snapshot-timing component is minor; the **dominant** gap
2.19→~1.5 is the SAMPLE WINDOW. R40's 625 trades span 2.4y using **daily** index
data; the production 15:20 path only has index 1m from 2025-12-01 (NIFTY/FINNIFTY)
/ 2026-04-27 (sectoral), so at most ~76 trades / ~6 months are honestly
evaluable. **2.19 is unreproducible intraday and is NOT a deployable target.**

**Production parity vs honest baseline (last 22 sector days):** matched-entry P&L
**0.000000pp** (bit-identical arithmetic), `production_only=0` (production ⊆
no-carryover baseline), Jaccard 0.864. The 3 `honest_only` names (M&M 05-07,
HDFCBANK/VEDL 05-14) are 100% **T+1 carryover** (prior-day positions occupying
slots at 15:20), NOT bugs. **Verdict: PASS** — production matches the honest
baseline exactly once carryover is accounted for. No code changes needed.

**Phase 5 readiness — GREEN LIGHT, small & exploratory.** Code is correct/safe;
edge MAGNITUDE is unvalidated (thin sample; sector sleeve has ~22 days of intraday
history only). Treat the paper-trade as the real validation that accrues sector-
index 1m history. **Updated Phase 5 targets (honest, provisional):** Sharpe(d)
~1.5–1.7, payoff ~1.4, EV ~+0.20–0.25%/trade, win ~48%, tolerate MaxDD ~−6%,
~12–15 trades/mo (broad; sector sparse). Open items: monitor `sector_ret is None`
(~82% of days now), data-integrity pass on 1m timestamps, accumulate ≥3mo
sectoral 1m before judging the sector sleeve. See
`outputs/sector_follow_cap5_vol_phase4_5_2026-06-10/REPORT.md`.

## EOD reports (Day-N markdown mirror) — added 2026-06-10

The 15:30 IST APScheduler EOD job now writes a markdown Day-N report to
`strategies/sector_follow_cap5_vol/eod_reports/YYYY-MM-DD.md` **in addition to**
sending the Telegram summary. The file holds the **same information** as the
Telegram message (date/mode, signals fired, positions opened, capital deployed,
realized P&L, sector breakdown, per-position table, kill-switch state) so review
doesn't depend on Telegram delivery or phone access. The two sinks are
independent — if either the file write or the Telegram send fails it is logged
and the other still runs. The directory is git-ignored (reports are
observational, not source) and the path is hardcoded (no env var). Generated by
`SectorFollowService._write_eod_report` / `_format_eod_report_markdown` in
`services/sector_follow_service.py`.

## Unified daily intent — pre-market control moved to a table (added 2026-06-10)

Pre-market control of this strategy now lives in the unified
`strategy_daily_intent` table (`db/openalgo.db`), read at `run_entry`/`run_exit`
via `services.mode_service.resolve_strategy_mode("sector_follow_cap5_vol")`.
A row's `intent` gates the jobs (`pause` = no new entries but T+1 exits still
run; `halt` = skip entries AND exits), its `mode` (`live`/`sandbox`/`skip`,
mapped `skip`→`scaffold`) overrides the routing **only when `source='unified'`**,
and `daily_capital_cap` caps the slot count. With **no row** the resolver falls
through to `SECTOR_FOLLOW_CAP5_VOL_MODE` (env) → behavior is unchanged, so the
2026-06-11 sandbox fire is unaffected (verified: `source='env'`, `mode=sandbox`,
`intent=run`). The `/api/pause|resume` REST endpoints are kept as **runtime
emergency overrides** (in-memory `manual_pause`), not the planning surface. The
service takes an injectable `intent_resolver` for hermetic tests. Design:
`docs/design/strategy_daily_intent.md`.

## Telegram pause/resume from phone (Phase 6, added 2026-06-10)

The same `strategy_daily_intent` row can now be set **from the phone** via the
inbound Telegram bot (`services/telegram_inbound_service.py`):
`/pause sector_follow`, `/resume sector_follow`, `/halt sector_follow` (halt asks
for a `YES` confirmation), `/intent sector cap <amount>`, or the 08:45 IST
morning-prompt buttons. The bot writes the unified table that `run_entry`/
`run_exit` already gate on, so a phone command takes effect on the next scheduled
job with no engine change. **Mode flips are NOT available from Telegram** — only
the intent axis (run/pause/halt) + capital cap; a Telegram intent change
**preserves** the row's existing `mode`, so routing never changes silently from
the phone. The bot is feature-flagged off (`TELEGRAM_INBOUND_ENABLED=false`) until
the operator adds their chat_id to `bot_config.telegram_chat_ids` and flips the
flag. The runtime `/sector_follow_cap5_vol/api/pause|resume` endpoints remain the
in-session emergency override. Design: `docs/design/telegram_inbound.md`.

## 2026-06-10 — Data-feed staleness incident + durable validation layer

**Incident.** The night before the first sandbox run, a backtest revealed the
sector-index 1m feed in `historify.duckdb` ended **2026-05-29** — 12 days stale.
Root cause was NOT a silently-failing job: the daily 16:05 IST
`sector_follow_index_backfill` job *did not exist* until that same day's Phase 3
commit (`3bfa4a08`, committed 15:41 IST). The index feed had only ever been a
one-off manual backfill; nothing refreshed it for 12 days because the refresh
mechanism wasn't built yet. The running process (started 22:18 IST) now has the
16:05 job registered, but its first fire was tomorrow — *after* the 15:20 entry.

**Second finding (caught by the new validation).** While building the freshness
checker, it surfaced that the **30 universe stocks** were also stale (last bar
2026-06-08). The task brief only flagged the indices; the validation layer
immediately caught the stock gap too — exactly the environmental-regression class
the hermetic E2E suite can't see.

**Immediate fix (both feeds, via the historify pipeline).** Index 1m:
`uv run python -m services.sector_follow_index_backfill --from 2026-05-29 --to 2026-06-10`
(8 mapped indices → 06-10 15:29; the 2 ETF-less indices fail as designed). Stock
1m: a `historify_service.create_and_start_job` over the 30-name universe
(`exchange=NSE`, `interval=1m`, `incremental=True`, 06-08→06-10; 30/30 success).
Both feeds verified current to **2026-06-10 15:29 IST**. Broker session was alive
(operator had restarted the app), so the catch-up succeeded tonight rather than
waiting for the morning re-login + 16:05 job.

**Durable layer shipped** (see CLAUDE.md "Data freshness validation" + SYSTEM_MAP):
`services/data_freshness_service.py` (pure, business-day-aware staleness),
`database/data_health_db.py` (`data_health_check` table), a daily **16:30 IST**
`sector_follow_data_health` job (alert + auto-pause tomorrow on stale data), a
**pre-entry gate** in `run_entry` (aborts on stale index OR stock feed; `run_exit`
only warns — exits never block), and `GET /sector_follow_cap5_vol/api/data_health`.
Flag `DATA_FRESHNESS_VALIDATION_ENABLED` (default true), threshold
`MAX_STALENESS_BUSINESS_DAYS` (default 1 = "yesterday's close acceptable", which
is the realistic state at 15:20 before the after-close backfill). Tests: 12
freshness-service + 3 sector-follow gate + 2 e2e.

**Lesson.** A scaffold's data pipeline is part of its readiness — wire the
*refresh* job in the same phase as the *consume* path, not after. Hermetic tests
prove logic; they do not prove the feed is fresh. The 1-business-day threshold is
deliberate: it tolerates the pre-backfill state at 15:20 yet catches any multi-day
gap loudly the next morning.
