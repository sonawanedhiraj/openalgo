# Futures Follow CAP50 — Learnings

Cumulative knowledge for the `futures_follow_cap50` strategy. Read this before
making any decision. Most-important file in the strategy folder.

## Validated facts (from backtest research, pre-deployment)

### 2026-06-14 — NIFTY-only CAP50 is the chosen vehicle (clears 12% feasibly)

From the full-window leverage study on the `sector_follow_cap5_vol` C1×W2+E4 signal
set (221 signals / 101 signal-days / 2024-01..2026-06, ₹10L, honest decomposed
charges):

| Metric | NIFTY-only CAP50 |
|---|---|
| CAGR | **14.44%** |
| Sharpe | **1.27** |
| MaxDD | **−8.0%** |
| Peak overnight margin | ~50% |
| Overlap (15:20→15:25 roll) margin | ~98% (5-min transient) |
| Worst overnight day | −₹34,396 (−3.4%) |
| Trades | 149 |
| Win% | 52.3 |

**Sizing:** 1 NIFTY lot per signal, greedy in vol-ratio order, day's cumulative
overnight margin capped at 50% × ₹10L. One NIFTY lot ≈ ₹18L notional ≈ ₹2.5L margin
= 25% of capital, the indivisible minimum → ~2 lots fit under the cap. The other 50%
is the overnight-gap buffer.

### 2026-06-14 — THE CAVEAT: this is leveraged beta, NOT alpha (load-bearing)

The signal does **not** predict NIFTY direction:
- Signal→NIFTY directional hit-rate **53.4%** — *below* the 55% falsification line.
- Stock↔NIFTY correlation **0.295** (weak).
- NIFTY captures only ~⅓ of the stock pick's mean overnight drift (+0.145% vs
  +0.437%).
- The 14.44% comes from the small positive broad-market drift on bullish-signal
  days, amplified ~2× by futures leverage.
- Year-by-year P&L is **evenly spread** (2024 +110k / 2025 +133k / 2026½ +147k) —
  the signature of riding the index, not the names. The equity C1×W2 book, by
  contrast, was 82% concentrated in 2025 with a 2024 loss.

**Implication:** in a flat or bear NIFTY year this sleeve has no stock-selection edge
to fall back on. Keep `sector_follow_cap5_vol` (CNC T+1 equity) as the alpha primary.
See memory `futures-sector-follow-leveraged-beta-not-alpha`.

### 2026-06-14 — Sector-matched routing REJECTED (NIFTY-only wins)

Tested routing banking signals to BANKNIFTY instead of NIFTY: overall index
hit-rate 53.4%→54.8% (still <55%), correlation UNCHANGED (0.296 vs 0.295), and it
**costs 0.74pp CAGR** (SM-CAP50 13.70% vs NIFTY-only 14.44%). BANKNIFTY-only =
10.20%, fails 12% — a *worse* vehicle. The expected stock↔BANKNIFTY ≈0.7–0.85
correlation does not exist over a 24h hold (it's ~0.35, no better than vs NIFTY).
**NIFTY-only is the deployable choice.** See memory
`sector-matched-futures-no-better-than-nifty`.

### 2026-06-14 — Why futures beat the other leverage wrappers

| Wrapper | Killer | Result |
|---|---|---|
| MIS-leveraged equity | edge IS the overnight hold; T+0 mean-reverts | net-negative |
| Option buying (weekly CALL) | theta ate 54.5% of premium even on winners | net-negative |
| **Index futures** | delta-1, zero theta, overnight hold preserved | **net-POSITIVE** |

Charges trivial and confirmed: **0.030% of notional** (~₹530/lot round-trip) vs
theta's 54.5% and the equity book's 45–53% friction. Futures give the equity signal
the leverage CNC stock structurally cannot access. No stop loss — Phase-1 proved
hard stops on this signal class are net-negative. No Jan-2026 air-pocket (the
inherited E4 sector-5d-vol p80 catastrophe filter zeroes January entries).

## Implementation notes

### 2026-07-05 — #334: watchdog moved to 15:28 — the 15:25 exit is primary again

The 15:14 EOD watchdog and the 15:25 T+1 exit share the selection predicate
(`entry_date != today`), and the watchdog fired FIRST — so on every normal day
the watchdog flattened the book at 15:14 and the "primary" 15:25 exit found
nothing. De-facto exit 15:14 vs the backtested/documented 15:25 (economically
likely immaterial — both near-close MARKET fills — but a systematic spec-vs-code
divergence). The 15:14 slot was inherited from the simplified engine's MIS
constraint (sandbox rejects MIS at/after 15:15); futures_follow trades **NRML**,
accepted until the 15:30 NFO close, so it never applied here. Fix: watchdog cron
→ **15:28 IST** (after the primary exit, 2 min before close), predicate
unchanged — with correct ordering anything it finds SHOULD be flattened
(rejected exit order, failed/missed 15:25 job). Supporting change: a
rejected/exception exit SELL now KEEPS the position in `paper_book` so the
15:28 watchdog retries it (the #265 store-reconcile guard suppresses a double
SELL if the order actually filled). The simplified engine's own 15:14 MIS
watchdog is untouched — its cap is load-bearing there. See VERSION_LOG v0.3.1.

### 2026-07-04 — #332: 15:20 decision snapshot from broker quotes (get_multiquotes)

futures_follow makes exactly ONE decision per day (the 15:20 IST entry eval), yet
its today's-(close, volume) inputs depended on the **all-day WS-fed scanner
aggregator** being healthy at that moment — a tick-stream dependency a
single-decision strategy doesn't need (the 2026-06-15 "aggregator empty, 0 signals"
failure class). Now `production_signal_evaluator` sources today's per-symbol data
from **one batched broker quote snapshot** at decision time:
`make_quotes_intraday_provider` (in `services/sector_follow_service.py`) fetches
all ~30 universe stocks (NSE) + mapped sector indices (NSE_INDEX, same
symbol/exchange routing as `sector_follow_index_backfill`) in a single
`get_multiquotes` call, memoized per eval cycle. Stocks yield
`(ltp, cumulative_day_volume)`; indices `(ltp, None)` — index volume is unused.

- **Flag**: `FUTURES_FOLLOW_INTRADAY_SOURCE = quotes (default) | aggregator`.
  `aggregator` restores the pre-#332 path byte-identically (regression-tested).
  Applies ONLY to this sleeve — `sector_follow_cap5_vol` (the equity alpha book)
  is untouched; accepted divergence: the two books may see marginally different
  `t_close` (quote at 15:20:xx vs last aggregator bar close).
- **Fail-safe chain**: quotes → aggregator → historify → none, WARNING per hop;
  the provider never raises. `intraday_source="quotes"` counts as LIVE coverage
  for the <50%/<20% completeness Telegram alerting. One INFO line per eval:
  `futures_follow intraday source=quotes fetched=<n>/<total> ...`.
- **15:18 smoke check reworked** (issue #332 AC-9): the historify-freshness arm
  now guards only the 20-day lookback (prior close, avg vol — reworded message);
  the broker-session arm is load-bearing for DATA, not just orders; NEW dry-run
  quote probe (1 universe symbol via the provider path, assert ltp > 0, only when
  source=quotes) writes the same self-expiring pause override + Telegram alert on
  failure. `_data_is_fresh_for_entry` / `_warn_if_stale_for_exit` stay (they guard
  the historify lookback).
- The 20-day lookback stays on historify; sourcing it from the broker daily API
  was explicitly deferred (breaks the "fires on exactly the signal set the equity
  book sees" invariant). No gate/selection/sizing/contract-resolver change.
- Tests: `test/test_sector_follow_quotes_provider.py` (batched+memoized mapping,
  broker-down fallback chain, index close-only path, source propagation +
  completeness, aggregator regression guard, aggregator-empty-all-day
  independence) + smoke-probe cases in `test/test_futures_follow_service.py`.
- **2026-07-05 follow-up — wall-clock entry-lateness guard**: `run_entry` now
  checks `self._now()` (IST) against `FUTURES_FOLLOW_ENTRY_DEADLINE_IST`
  (default **15:28**, consult-time; a malformed value falls back to 15:28 with
  a WARNING — a typo must never disable the guard) **before** any
  gate/evaluation/order placement. Why: the 15:20 entry cron can misfire LATE
  (app down at 15:20 → APScheduler fires it on restart) — at ~15:35 the
  exchange rejects the order, but after ~15:45 it could queue as an **AMO and
  execute at tomorrow's open**, an unintended overnight entry (NFO hard close
  is 15:30). A late fire skips all entries, logs an ERROR with the actual fire
  time vs the deadline, and Telegrams the operator. Entries ONLY — `run_exit` /
  `run_eod_watchdog` / `close_all` are never gated (repo invariant: a held T+1
  position is riskier than a rejected exit order). No early-side check (cron
  cannot fire early; misfires only fire late).

### 2026-06-15 — v0.2.0: sandbox is the structural default (deployable: true)

Per the operator redirect (must trade in sandbox from Monday's open), the scaffold
mode was **dropped entirely** — there is no observe-only / "log without placing"
state. `VALID_MODES = ("sandbox", "live")`, default `sandbox`; an unknown
`FUTURES_FOLLOW_MODE` force-falls-back to `sandbox`. `place_entry`/`place_exit`
always route via the order placer (sandbox → `sandbox.db`, live → broker); journal
statuses are `placed`/`rejected`/`exception` only. `config_snapshot.json`:
`mode: "sandbox"`, `deployable: true`. The mode-override + runtime-override +
data-freshness + kill-switch rails are unchanged and remain the operational safeties
(they are not "shadow" flags). First sandbox cycle: Monday 2026-06-15 15:20 IST.

### 2026-06-15 — Phase 1: core service shipped

- `services/futures_follow_service.py` — `FuturesFollowService` mirroring
  `sector_follow_service.py`. **Reuses** the sector_follow evaluator
  (`production_signal_evaluator` → `load_config`/`load_sector_map`/
  `duckdb_metrics_provider`/`passes_gates`/`select_entries`) — does NOT reimplement
  gates. Pure functions `compute_lots_to_buy` (the 50%-cap sizing) and
  `compute_futures_charges` (the ~₹530/lot model). Near-month NIFTY contract
  resolved from the master contract (`production_contract_resolver` →
  `fno_search_symbols_db`, nearest non-expired monthly FUT). 5 APScheduler jobs
  (09:00 reset / 15:20 entry / 15:25 exit / 15:28 watchdog (was 15:14 — moved by
  #334) / 15:30 EOD). Mode-aware
  order placement, kill switch, pause/resume/close_all, runtime-override gate
  (entry-only), data-freshness gate (delegates to sector_follow's check). All I/O
  injected → hermetic tests.
- `database/futures_follow_db.py` — `futures_follow_trades` journal (additive table
  in `openalgo.db`; futures-specific columns: `nifty_symbol`, `lots`, `entry_price`,
  `exit_price`, `gross_pnl`, `charges_inr`, `net_pnl`, `margin_inr`, `signal_id`).
- `blueprints/futures_follow.py` — control API at `/futures_follow_cap50/api/*`
  (status / positions / pause / resume / close_all / data_health).
- `app.py` — `init_futures_follow_service(app=app)` + DB init + blueprint register.
  Default mode=sandbox → actively trades the virtual ₹1Cr book from boot.
- **Decision point flagged:** the spec said "NIFTY weekly future" but NIFTY index
  futures are MONTHLY (only options are weekly). The resolver picks the **near-month
  (front) monthly FUT**. Documented in PLAN.md and the resolver docstring.
- **Per-lot margin for the cap:** uses a fixed config `nifty_lot_margin_inr`
  (₹250,000) so the cap decision is deterministic across the entry batch and the
  session. The dynamic `price × lot_size × margin_rate` estimate is shown in
  `/api/status` for observability only — never used for the cap math. Operator
  refreshes the config estimate from the broker SPAN margin.

### 2026-07-05 — R42: the SHORT mirror REJECTS — the overnight edge is long-only

Operator asked whether the opposite trade works (SHORT NIFTY futures when the
market trends down into the close: sector < −1.5%, stock < −0.5%, vol > 1×,
top-5, CAP50, T+1 cover). It does not:

| Variant | CAGR | Sharpe | MaxDD | Worst day |
|---|---:|---:|---:|---:|
| LONG control (proxy, matched window) | 15.48% | 1.27 | −6.1% | −₹46k |
| SHORT mirror +E4 | **+1.05%** | 0.15 | −11.3% | −₹69k |
| SHORT mirror no-E4 | **−7.82%** | −0.24 | −25.1% | −₹111k |

Mechanism: after a down-signal day the signal stock *bounces* (+0.38% mean
next-day, only 45.6% continue down) while after an up-signal day it continues
(+0.51%, 59.2%) — panic closes mean-revert overnight, and NIFTY's upward drift
turns from tailwind to headwind (down only 48.5% of post-signal days). E4 is
the only thing keeping the short above water — "don't trade when the signal is
strongest" as the best filter is the tell that there is no edge underneath.
Run on a public-data daily proxy (historify locked by the live app); the LONG
control reproduced the canonical study's Sharpe **exactly** (1.27), so the
proxy is trusted. With MIS-T+0 and option buying already rejected, every
wrapper except LONG delta-1 overnight has now failed on this signal family.
Report: `docs/research/strategy/futures_follow_cap50/2026-07-05_inverse_short_futures_backtest.md`
(R42, issue #336).

## Live Learnings

_(populate as the sandbox pilot produces evidence)_
