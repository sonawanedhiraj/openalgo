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
  (09:00 reset / 15:14 watchdog / 15:20 entry / 15:25 exit / 15:30 EOD). Mode-aware
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

## Live Learnings

_(populate as the sandbox pilot produces evidence)_
