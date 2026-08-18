# Backtesting Framework Plan (2026-08-11)

Operator request: inventory what data exists for backtesting, survey the code already
written, and plan a proper backtesting framework plus other improvements.

## 1. Data inventory (as of 2026-08-11)

### historify.duckdb (4.1 GB, 42.7M bar rows)

| Dataset | Coverage | Depth | Freshness |
|---|---|---|---|
| NSE equity **1m** | 212 symbols | 2024-01-01 → today (~155 syms full depth; 63 added Dec-2025; 12 Apr-2026) | LIVE (convergence loops) |
| NSE_INDEX **1m** | 23 indices | 9 core (NIFTY/BANKNIFTY/FINNIFTY/IT/METAL/AUTO/FMCG/PSU/PVT) 2024→today; MIDCPNIFTY+NIFTYNXT50 Dec-2025→today; 12 sector indices ONLY 2026-04-27→05-29 | Core LIVE; 12 sector idx STALE |
| NSE equity **D** | 221 symbols | 2022-01-03 → today (4.5+ yrs) | LIVE |
| NSE_INDEX **D** | 22 indices | 2022-01-03 → today | LIVE |
| NFO **1m** | 3 ad-hoc option contracts | May 2026 only | effectively none |
| `fo_bhavcopy_eod` (stock options EOD) | 4.66M rows CE/PE; 28 syms 2024, 30 syms 2025, **218 syms 2026** | 2024-01-01 → **2026-05-29** | **STALE ~2.5 months** |
| `index_options_eod` | 1.64M rows, NIFTY + BANKNIFTY | 2022-01-03 → **2026-06-04** | **STALE** |
| `data_catalog` | 481 rows — per-(symbol,interval) first/last/count | — | maintained |

Note: `index_options_eod.underlying` is polluted for post-2024 rows (spot price
stored instead of the underlying name; group by `symbol` instead). `symbol_metadata`
and `watchlist` tables are empty.

### Outside DuckDB

| Source | Coverage | Notes |
|---|---|---|
| `tick_logs/*.jsonl` | 2026-07-13 → today, 4.8 GB (~25 files + open15/ subdir 13 files) | full-tick replay fidelity; no retention policy |
| `backtest/options_open15/data/iv_history.parquet` | 218 syms, 2024-01 → 2026-05-29, per-day ATM IV (CE/PE) + fwd + rv60 | derived from fo_bhavcopy; stale with it |
| Live journals (openalgo.db) | trade_journal 274 rows (06-01→), sector_follow 29, futures_follow 27, open15 24, scan_results 10.2k, scan_cycle 12.1k, signal_decision 4.6k | ground truth for backtest-vs-real calibration |
| sandbox.db fills | since ~June | sandbox fill prices for reconciliation |
| `backtest/*/**.parquet`, results JSONs, `outputs/` | per-round artifacts | gitignored, ephemeral |

### Data gaps that limit backtesting

1. **Options EOD ingestion has no owner.** `fo_bhavcopy_eod` / `index_options_eod`
   were populated by one-off scripts living in gitignored `outputs/` dirs
   (`backfill_index_options.py`, `phase1_backfill.py`, …). Nothing scheduled → stale
   since late May. Every options round since has been re-fetching ad hoc.
2. **No option intraday data** (3 contracts of NFO 1m). Options rounds rely on
   BS interpolation off EOD anchors — the known-optimistic path (R36 finding).
3. **12 sector indices 1m** only exist for a 5-week Apr–May 2026 window.
4. Equity 1m before 2024 doesn't exist (broker API limit); daily-D before 2022
   doesn't exist.

## 2. Existing code survey

~16k lines across `backtest/` (plus per-round pipelines in gitignored `outputs/`).

| Harness | Purpose | Data source | Status |
|---|---|---|---|
| `backtest/run_backtest.py` (1.2k lines) | Replays 5m candles or tick logs through the ACTUAL SimplifiedStockEngine (`--from-engine` live config) | app history API + tick_logs | the one true engine-replay; single-strategy |
| `backtest/inhouse_scanner/` (R56-R60) | scanner→engine replay, entry refinement | duckdb direct | per-round scripts |
| `backtest/open15_rolling/` | rolling watch-list replay vs captured tick logs | tick_logs + app API | per-round |
| `backtest/options_open15/` (16 scripts) | BS option pricing, IV history, July real-vs-BS calibration | fo_bhavcopy + iv parquet + broker | per-round |
| `backtest/news_event_study/` (11 scripts, 7.5k lines) | R43-R46 event studies | duckdb + harvested announcements | per-round |
| `backtest/tod_volume_gate/` (R48) | time-of-day volume gate replay | broker fetch → parquet | per-round |
| `outputs/r*/` (many) | R29-R55 round pipelines | mixed | gitignored, decaying |

### What every round rebuilds from scratch (the actual problem)

1. **Data loading** — `duckdb.connect` in 20+ files, each with its own lock-fallback,
   resampling, and calendar handling. Some route via app API, some direct, some
   re-fetch from broker.
2. **Cost model** — Zerodha charges re-implemented in ≥4 places
   (`inhouse_scanner/r60/stage_c_execution.py:33`, `news_event_study/simulate.py:331`,
   `open15_rolling/replay_rolling_gainers.py`, `options_open15/pipeline.py`) plus
   in-app modelled charges (open15 #433, futures_follow ₹530/lot). Divergence risk is
   proven: R40's flat-cost model overstated Sharpe ~3x; #552 found four independent
   P&L derivations giving three answers.
3. **Look-ahead discipline** — the R58 bar-close-gate/trigger-level-entry class is a
   registry-level cross-cutting finding, but nothing structural prevents the next
   harness from repeating it.
4. **Metrics & verdict rules** — Sharpe/MaxDD/WR/payoff/monthly-green%, long-vs-short
   split reporting, the <30-trades INSUFFICIENT EVIDENCE rule, split-half + placebo
   checks (R45/R61 lessons) — all reimplemented or skipped per round.
5. **Report + registry writing** — manual each time.

The Standard Testing Protocol exists ONLY as prose in `strategies/STRATEGY_REGISTRY.md`.
Nothing executable encodes it.

## 3. Framework plan

**Architecture choice: a shared library + thin per-round harnesses, NOT a monolithic
event engine.** The workflow here is research-round-driven (R1→R61, each with a bespoke
question). Zipline-style frameworks optimize for the wrong thing; what compounds is a
trustworthy toolkit every round imports. `run_backtest.py` (engine replay) stays as-is —
it replays production code, which is a different, higher-fidelity tool.

### Phase 1 — `backtest/lib/` foundation (highest value, ~1 week)

1. **`data.py` — one DataStore API.**
   - `bars(symbols, interval, start, end)` → tidy DataFrame from historify.duckdb
     (read-only, with locked-file fallback to the app's history API — the recurring
     lock pain codified once).
   - Resample 1m→5m/15m (5m is computed, never stored), daily-from-1m for deep OOS.
   - `ticks(date)` → tick_logs JSONL loader (shared with run_backtest.py).
   - `option_eod(underlying/symbol, date)` → fo_bhavcopy_eod + index_options_eod
     readers (encapsulating the polluted-underlying quirk).
   - Trading-calendar aware via `database.market_calendar_db` (weekend ≠ stale,
     holiday-aware windows).
2. **`costs.py` — the single canonical Zerodha cost model.** MIS / CNC / FUT /
   options, exactly the registry protocol table, unit-tested against hand-computed
   round trips. All four existing copies get deleted in favor of it; the in-app
   modelled-charges helpers (`open15_breakout_db`, futures_follow) can import it too,
   ending model drift between backtest and live.
3. **`metrics.py` — standard scorecard.** Gross AND net, long-only/short-only split
   (mandatory per registry), Sharpe, MaxDD, WR, payoff, monthly-green %, benchmark
   (NIFTY buy-and-hold), and the verdict gates: auto-flag INSUFFICIENT EVIDENCE
   (<30 trades or <6 months), auto-run split-half stability and a size-matched
   random-benchmark placebo (the R45/R61 false-positive killers).
4. **`sessions.py` — IST session model.** Session times per instrument class incl.
   the post-2026-08-03 CAS regime (F&O cash scrips end continuous 15:15), MIS
   square-off times, expiry helpers (NIFTY monthly = last Tuesday).

### Phase 2 — execution semantics + options (~1 week)

5. **`fills.py` — entry/exit convention enforcement.** The load-bearing piece: a
   `Fill` constructor that REFUSES a same-bar trigger-level entry when the gate uses
   full-bar statistics (must be bar close or next open) — structurally preventing the
   R58 look-ahead class — plus slippage application from `costs.py`. Emits the audit
   stat `mean(entry_bar_close/trigger_level − 1)` automatically.
6. **`options.py` — consolidated BS pricing.** Merge `options_open15/bs.py`,
   iv_history.parquet access, and the real-vs-BS calibration lessons (BS is
   systematically optimistic for buying; convention checks from
   `equity_convention_check.py`). Warn loudly when a result depends on synthetic
   pricing rather than real settles.
7. **`report.py` — round scaffolding.** Emits the `BACKTEST_ROUND<N>_<NAME>_<DATE>.md`
   skeleton with the scorecard, placebo results, and a ready-to-paste
   STRATEGY_REGISTRY entry stub. One command closes the loop the registry demands.

Migration policy: don't rewrite old rounds. New rounds must use `backtest/lib/`;
the next time an old harness is touched, its local copies are swapped out.
`test/test_backtest_lib_*.py` pins costs against hand-validated rupee examples.

### Phase 3 — data completeness (parallel, mostly ops)

8. **Own the options-EOD pipeline.** Promote the bhavcopy ingestion out of
   `outputs/` into `services/` with a scheduled daily job (same convergence-check
   pattern as scanner backfill) + backfill CLI for the missed May→Aug window. Also
   regenerates iv_history.parquet. This unblocks every future options round.
9. **Decide on option 1m capture.** Cheapest path: subscribe/persist 1m bars only for
   contracts the live strategies already touch (open15 option shadow already fetches
   them — currently discarded). Full-chain intraday capture is a much bigger commitment;
   defer unless an options strategy is promoted to live.
10. **Backfill the 12 missing sector indices 1m** (if sector rounds need them) —
    one CLI run with a live broker session.

### Phase 4 — UI (optional, after the library exists)

The Historify UI already exists (`/historify` — catalog, charts, download jobs,
schedules) — the "no UI for DuckDB" perception is mostly a discoverability problem.
Incremental additions rather than a new surface:
11. **Data-coverage dashboard** on the Historify page: per-symbol × interval
    freshness/depth heatmap straight off `data_catalog` + a staleness banner for the
    options EOD tables (they're invisible today, which is why they rotted).
12. **Backtest-runs page** (later, only if the CLI workflow chafes): list past round
    reports + artifacts; possibly "re-run round N". Running backtests from the UI is
    deliberately NOT proposed — rounds are code, and the review loop lives in git.

## 4. Other improvements to consider

- **Tick-log retention policy** — 4.8 GB/month, unbounded. Compress (gzip ≈ 10x on
  JSONL) after N days, archive/delete after M. Cheap win.
- **DB backup hygiene** — 9 `db/*.bak.*` files litter the repo dir (gitignored but
  ~500 MB); adopt a rotate-keep-3 convention in whatever writes them.
- **`openalgo_test_bridge.db` (68 MB)** in db/ — verify still needed; likely stale
  test artifact.
- **Daily-D from 1m (Approach 2)** — single source of truth for daily bars is now
  viable (deep 1m exists for the scanner universe); removes the provisional-close
  re-settle class (#299) for covered symbols.
- **Calibration harness** — we now have ~3 months of real fills (journals + #555
  fill reconciliation). A small recurring report: realized slippage vs the 0.10%/side
  protocol assumption, per strategy. Feeds back into `costs.py` constants.
- **Registry `In-Flight`/`Active` freshness check** — a postmarket contract could
  flag when a round issue is open >14 days with no registry entry.

## 5. Suggested sequencing

| Step | What | Effort | Unblocks |
|---|---|---|---|
| 1 | `costs.py` + `metrics.py` + tests | 2-3 days | every future round; ends cost-model drift |
| 2 | `data.py` + `sessions.py` | 2-3 days | kills the 20-way duckdb.connect duplication |
| 3 | Options-EOD scheduled ingestion + May→Aug backfill | 1-2 days | all options research |
| 4 | `fills.py` + `options.py` + `report.py` | 3-4 days | look-ahead-proof rounds, one-command reports |
| 5 | Coverage dashboard on Historify | 1-2 days | operator visibility |
| 6 | Retention/hygiene items | opportunistic | — |

Each step is its own GitHub issue per the task-tracking policy; steps 1-2 are pure
additive library code and can start immediately.
