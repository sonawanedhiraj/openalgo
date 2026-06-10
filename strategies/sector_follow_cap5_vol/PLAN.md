# Sector Follow (Cap-5, Volume-Tiebreaker) — Delivery Plan

`sector_follow_cap5_vol` · v0.1.0 · **scaffold-only · deployable: false**

## Overview

This strategy is the deployable extract of **SECTOR_FOLLOW**, the R40 winner
(`V_SF_CAP5_VOL`). It is a daily long-only intraday-confirmation, overnight-hold
system: at the signal window it buys any stock whose **sector index is up >+1%
intraday AND the stock itself is up >+0.5% intraday AND its volume is >1× its
20-day average volume**, entering at the close. Each position is exited at the
**next trading day's close (T+1)**. At most **5 concurrent positions** are held;
when more than 5 names qualify on a given day, the tiebreaker is **volume ratio
descending** (highest relative-volume names win the slots). Costs are modeled at
**0.0857% per round-trip**.

**Verbatim verdict carried from R40 (`V_SF_CAP5_VOL`):** Sharpe 2.37 (daily) /
1.92 (monthly), payoff 1.39, EV +0.63%/trade, MaxDD −8.76%, 434 trades over
2.4 yr, 2026-YTD +12.9%, max concurrent positions = 5 (vs 30 uncapped).

**Verdict tier:** ADD_AS_5TH_SLEEVE in the `V_BLD5_B` blend (R41). This plan
ships the **standalone** version first; sleeve integration into `V_BLD5_B`
follows as a separate later plan, after this strategy clears sandbox validation.

## Operator decisions (locked 2026-06-10 with Dheeraj)

Quoted verbatim — these are the binding choices made with the operator:

1. **Capital:** ₹2,50,000 initial (₹50k per position × 5)
2. **Universe:** top-30 F&O stocks by traded value, monthly re-rank
   **conditional on Phase 0.5 backtest showing re-rank ≥ static**
3. **Daily loss kill switch:** enabled, threshold 3.0% (halt new entries for the
   day, hold existing positions to scheduled T+1 exit)
4. **Post-loss cooldown:** NONE (pure rule per backtest)
5. **Signal evaluation window:** 15:20 IST (leaves 5 min to place ≤5 MARKET
   orders before 15:25 close)

## Readiness checklist (11 pieces)

For each: **owner** (engineering / operator), **decision** authority, and current
**status** (TODO / DONE / GATED-ON-X).

1. **Sector map for all 30 universe stocks** — owner: engineering · decision:
   engineering · status: **TODO** (backtest mapped only 14/30; see Risk #6).
2. **Daily-bar coverage verified for all 30 names** (`historify.duckdb`) — owner:
   engineering · decision: engineering · status: **TODO**.
3. **`strategy_id` tagging schema** for per-strategy position attribution —
   owner: engineering · decision: engineering · status: **TODO**.
4. **Universe re-rank validation backtest** (Phase 0.5) — owner: engineering ·
   decision: operator · status: **TODO** (gates checklist item 5).
5. **Universe method locked** (static-30 vs monthly re-rank) — owner: operator ·
   decision: operator · status: **GATED-ON-Phase-0.5**.
6. **Core service module** `services/sector_follow_service.py` — owner:
   engineering · decision: engineering · status: **TODO**.
7. **APScheduler jobs** (15:20 entry eval, 15:25 next-day exit, EOD watchdog) —
   owner: engineering · decision: engineering · status: **TODO**.
8. **Mode flag** `SECTOR_FOLLOW_CAP5_VOL_MODE` wired in `.env` — owner:
   engineering · decision: operator (sets value) · status: **TODO**.
9. **Risk controls** (5-position cap, 3.0% daily-loss kill switch) — owner:
   engineering · decision: operator (thresholds locked) · status: **TODO**.
10. **Monitoring** (Telegram entry/exit alerts + EOD summary) — owner:
    engineering · decision: operator · status: **TODO**.
11. **Shadow-replay parity harness** vs R40 backtest — owner: engineering ·
    decision: engineering · status: **TODO**.

## Delivery plan (7 phases)

### Phase 0 — Prep
- **Deliverables:** complete sector map for all 30 universe stocks; verify
  daily-bar coverage for every name in `historify.duckdb`; design the
  `strategy_id` tagging schema for position attribution.
- **Effort:** ~1.5 days · **Calendar:** Week 1
- **Exit criteria:** all 30 names have a sector mapping and ≥ 2.4 yr of daily
  bars; `strategy_id` schema reviewed.

### Phase 0.5 — Universe re-rank validation backtest
- **Deliverables:** backtest comparing **static top-30** vs **monthly re-ranked
  top-30 by traded value** on the R40 logic, same window/costs.
- **Effort:** ~1 day · **Calendar:** Week 1
- **Exit criteria:** a clear verdict on whether re-rank ≥ static. **Operator
  decision gate** — locks checklist item 5 / the `universe_rerank` config value.

### Phase 1 — Core strategy module
- **Deliverables:** `services/sector_follow_service.py` with the signal
  evaluator (sector +1% / stock +0.5% / vol >1× 20d), position selector (cap 5,
  volume-ratio-desc tiebreaker), APScheduler 15:20 entry eval + 15:25 next-day
  exit jobs, and the `SECTOR_FOLLOW_CAP5_VOL_MODE` flag honored in `.env`.
- **Effort:** ~3 days · **Calendar:** Week 2
- **Exit criteria:** module computes signals in `scaffold` mode and logs the
  recommended ≤5 orders; no orders placed.

### Phase 2 — Order pipeline
- **Deliverables:** `strategy_id` tagging on every order; a per-strategy position
  view so this book is isolated from other strategies.
- **Effort:** ~1.5 days · **Calendar:** Week 2–3
- **Exit criteria:** orders (in sandbox mode) carry the strategy tag and a
  per-strategy position view reconciles.

### Phase 3 — Risk + monitoring
- **Deliverables:** 5-position concurrency cap enforcement, 3.0% daily-loss kill
  switch (halt new entries, hold existing to T+1), Telegram entry/exit alerts,
  EOD summary report.
- **Effort:** ~2 days · **Calendar:** Week 3
- **Exit criteria:** kill switch fires correctly in a forced-loss simulation;
  Telegram + EOD summary verified.

### Phase 4 — Shadow-replay verification
- **Deliverables:** replay the last 30 trading days through the live module in
  `scaffold` mode; diff signals/orders vs the R40 backtest.
- **Effort:** ~2 days · **Calendar:** Week 4
- **Exit criteria:** signal/order parity with R40 within an explained tolerance
  (latency + running-OHLC differences accounted, see Risk #1–3).

### Phase 5 — Sandbox (30 trading days)
- **Deliverables:** run in `sandbox` mode for 30 trading days (~6 calendar
  weeks); daily EOD logging vs backtest expectation.
- **Effort:** ~6 calendar weeks (low touch) · **Calendar:** Weeks 5–10
- **Exit criteria:** 30 trading days completed with EOD records and a realized
  vs backtest comparison.

### Phase 6 — Live decision gate
- **Deliverables:** sandbox vs backtest report (CAGR, Sharpe, payoff, MaxDD).
- **Effort:** ~0.5 day · **Calendar:** Week 11
- **Exit criteria:** **operator decision gate** — if sandbox CAGR/Sharpe is
  within ±30% of backtest, propose `live`. Otherwise diagnose and iterate.

## Mode lifecycle

`SECTOR_FOLLOW_CAP5_VOL_MODE` in `.env` controls execution. Transitions happen
**only on explicit operator approval** — the module never self-promotes.

- **`scaffold`** (default): compute signals + log the recommended ≤5 orders. **No
  orders placed.** Safe to run continuously for parity/observation.
- **`sandbox`**: orders flow into `sandbox.db` only (virtual ₹1 Cr capital),
  isolated from live trading. Used in Phase 5.
- **`live`**: real broker orders via Zerodha. Entered only after Phase 6 clears
  the ±30% gate and the operator approves.

## Risk register

1. **Zero-slippage backtest assumption on stock orders** — real-world slippage
   cap unknown. *Mitigation:* shadow-replay parity (Phase 4) + sandbox-measured
   realized slippage (Phase 5).
2. **15:20 eval → 15:25 execution latency** — 5-minute gap vs the backtest's
   "buy at close" assumption. *Mitigation:* measure entry-price parity in
   shadow-replay; quantify the drift before live.
3. **Running OHLC at 15:20 differs from settled close** — the live signal uses an
   in-progress daily candle. *Mitigation:* re-run the entry decision at 15:24
   with fresher data before placing orders.
4. **30-stock universe can shift** (delistings, F&O additions/removals).
   *Mitigation:* monthly re-rank if Phase 0.5 validates it; otherwise scheduled
   manual review of the universe.
5. **Stranded T+1 holds if broker WS dies** — no exit gets placed. *Mitigation:*
   a separately scheduled APScheduler EOD watchdog that verifies exits fired and
   alerts/force-closes otherwise.
6. **Incomplete sector map in backtest (only 14/30 mapped)** — completing it
   **will change signal frequencies** in sandbox vs backtest. *Mitigation:*
   explicit pre/post signal-frequency comparison after the map is fixed (Phase 0
   → Phase 4), documented so the change is not mistaken for a bug.

## Decision gates

Operator decision is required before continuing at each of these moments:

- **Phase 0.5 verdict** — re-rank yes/no (locks `universe_rerank`).
- **Phase 6 sandbox → live promotion** — the ±30% CAGR/Sharpe gate.
- **Any unexpected daily-loss kill-switch fire** — operator reviews before the
  strategy resumes the next day.
- **Sleeve integration into `V_BLD5_B`** — a separate later plan, only after the
  standalone clears sandbox.

## Files of interest

- **R40 backtest (truth source for parity):**
  `outputs/r40_sector_follow_capped_2026-06-10/REPORT.md` and the variant dir
  `outputs/r40_sector_follow_capped_2026-06-10/v_sf_cap5_vol/`
- **R41 5-sleeve blend report (sleeve-integration target):**
  `outputs/r41_5sleeve_blend_2026-06-10/REPORT.md`
- **R39 synthesis (where SECTOR_FOLLOW emerged):**
  `outputs/overnight_synthesis_2026-06-10/SYNTHESIS_2026-06-10.md`
- **Strategy registry:** `strategies/STRATEGY_REGISTRY.md` (rows R39/R40/R41)
- **Sibling scaffold (structure mirrored here):**
  `strategies/sector_rotation_etf/`
- **Related memory files:** `r41-5sleeve-blend-promotes.md`,
  `r39e-dynamic-allocation-loses-to-sector-follow.md`
