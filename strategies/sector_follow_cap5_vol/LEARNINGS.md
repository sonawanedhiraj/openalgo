# Sector Follow (Cap-5, Volume-Tiebreaker) — Learnings

Cumulative knowledge for the `sector_follow_cap5_vol` strategy. Read this before
making any decision. Most-important file in the strategy folder.

## Validated facts (from backtest research, pre-deployment)

_(populate as Phase 0.5 / shadow-replay / sandbox produce evidence)_

## Implementation notes (scaffold)

_(populate as the service module is built)_

## Live Learnings

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
