# Futures Follow CAP50 — Deployment Plan

`futures_follow_cap50` · v0.2.0 · **ACTIVE in sandbox · deployable: true**

## Overview

This strategy is the deployable extract of the **NIFTY-only CAP50** futures wrapper
that emerged from the 2026-06-14 leverage research on the `sector_follow_cap5_vol`
signal set. **It trades the virtual ₹1Cr sandbox book by default — from the moment
the service loads — with no scaffold/observe-only state.** It is a daily, long-only,
**overnight-hold leveraged-beta** sleeve: at **15:20 IST** it reuses the
sector_follow C1×W2+E4 evaluator to find today's ≤5 gate-passing stock signals, and
for each signal — greedily in vol-ratio order — buys **one NIFTY near-month index
future lot**, HARD-CAPPED at **50% of capital as overnight SPAN margin**. Signals
beyond the cap are skipped. Each position is exited at the **next trading day's 15:25
IST close (T+1)** MARKET sell. **No stop loss** (Phase-1 proved hard stops are
net-negative on this signal class). Charges are modeled at ~**0.030% of notional**
(~₹530/lot round-trip).

**First sandbox cycle: Monday 2026-06-15 15:20 IST** — the first sector_follow signal
of the session fires → futures_follow places a NIFTY-futures BUY order in
`sandbox.db`. EOD reconciliation/summary: same day 15:30 IST and daily thereafter.

**Verbatim verdict carried from the backtest (NIFTY-only CAP50):** CAGR **14.44%**,
Sharpe **1.27**, MaxDD **−8.0%**, peak overnight margin ~50%, worst overnight day
−₹34,396 (−3.4%), 149 trades over 2.45 yr on ₹10L. Year split even
(110k/133k/147k) — the signature of riding broad-market drift, not stock selection.

## ⚠️ The load-bearing caveat (do not lose this)

**This is leveraged beta, NOT the sector_follow stock-selection alpha.** The signal
does NOT predict NIFTY: directional hit-rate **53.4%** (below the 55% falsification
line), stock↔NIFTY correlation **0.295**, NIFTY captures only ~⅓ of the stock pick's
mean overnight drift (+0.145% vs +0.437%). The 14.44% comes from leveraging the
small positive market drift on bullish-signal days. **In a flat or bear NIFTY year
this sleeve has no edge to fall back on** — unlike the equity book, which owns the
real alpha. Size it as a *separate, leverage-bounded beta bet*, never as
"sector_follow with futures." Sector-matched routing (banking→BANKNIFTY) was tested
and **REJECTED** (costs 0.74pp CAGR, no correlation gain). NIFTY-only is the vehicle.

## Operator decisions (locked, sandbox-active)

1. **Capital:** ₹10,00,000 (the backtest book size).
2. **Cap:** HARD 50% of capital as overnight SPAN margin (~2 NIFTY lots on ₹10L at
   ~₹2.5L/lot). The other 50% is the overnight-gap buffer — do NOT raise it; the
   tail is understated (close-to-close P&L ignores intraday SPAN calls on a gap).
3. **Instrument:** NIFTY **near-month** (front) monthly future — there are no weekly
   NIFTY futures. Resolved dynamically from the master contract.
4. **Product:** NRML (futures carry). **Not MIS, not CNC.**
5. **Stop loss:** NONE (per backtest). EOD watchdog at 15:14 IST is the backstop.
6. **Daily loss kill switch:** 3.0% of capital (halt new entries, hold to T+1 exit).
7. **Signal source:** REUSE the live `sector_follow_cap5_vol` evaluator — do not
   reimplement gates.
8. **Default mode:** **`sandbox`** — active virtual trading from boot. `live` is an
   explicit operator flip only.

## Mode lifecycle

`FUTURES_FOLLOW_MODE` in `.env` (or a persistent `strategy_mode` row) controls
execution. There are only two modes — **no scaffold/observe-only state.**

- **`sandbox`** (default): orders flow into `sandbox.db` (virtual ₹1 Cr), isolated
  from live trading. **Active from boot.**
- **`live`**: real broker orders via Zerodha. Entered only on explicit operator
  approval after the sandbox cycle is validated.

Active trading can be paused without changing mode via
`POST /futures_follow_cap50/api/pause` (durable `strategy_runtime_override`); resume
via `/api/resume`. The data-freshness gate and 3% kill switch also gate entries
automatically.

## Deployment timeline

### Day 0 — Monday 2026-06-15 (first sandbox cycle)
- Service loads on the next OpenAlgo restart in `sandbox` mode (default).
- **15:20 IST:** first sector_follow signal of the session → futures_follow resolves
  the NIFTY near-month future, sizes 1 lot/signal under the 50% cap, places BUY
  order(s) in `sandbox.db`.
- **15:30 IST:** EOD summary (Telegram + `eod_reports/2026-06-15.md`).
- Operator verifies: the BUY landed in sandbox.db, lots ≤ 50%-margin cap, the
  journal row is `status='placed'`.

### Day 1+ — daily cycle
- **09:00 IST:** daily reset (kill switch + journals).
- **15:14 IST:** EOD watchdog (flatten any stranded T+1 position).
- **15:20 IST:** entry (new signals).
- **15:25 IST:** T+1 exit (square off prior-day positions) with realized P&L.
- **15:30 IST:** EOD summary + reconciliation report.

### Sandbox validation window → live decision gate
- Run the sandbox cycle for N trading days; compare realized vs backtest
  (CAGR/Sharpe/MaxDD/margin/charges). Validate the contract resolver picks the right
  front-month (especially around monthly expiry), the 50% cap holds on heavy signal
  days, and the charge model matches sandbox fills.
- **Operator decision gate:** propose `live` only if within tolerance, and only with
  the beta caveat understood (size small, never let peak overnight margin exceed
  ~50%).

## Risk register

1. **Leveraged beta, not alpha** (the headline) — no stock-selection edge in a
   flat/bear NIFTY year. *Mitigation:* size small; keep the equity book primary; a
   future VIX/drift regime gate is the only remaining lever (research direction).
2. **Tail understated** — P&L is close-to-close at 15:25; a real overnight gap-down
   triggers an intraday SPAN call and force-liquidation worse than the 15:25 print.
   Leverage fine at 50% overnight can spike past 100% intraday on a 2–3% gap.
   *Mitigation:* the 50% cap keeps a buffer; never raise it.
3. **Contract resolution** — picking the wrong expiry (rolling near the monthly
   expiry) or a stale master contract. *Mitigation:* resolver picks the nearest
   non-expired monthly FUT each run; validate in sandbox around expiry week.
4. **Per-lot margin drift** — the cap uses a fixed `nifty_lot_margin_inr` estimate;
   real SPAN varies intraday. *Mitigation:* operator refreshes the config estimate
   from the broker margin API; the 50% cap is conservative.
5. **Stranded T+1 holds if broker WS dies** — *Mitigation:* the 15:14 IST EOD
   watchdog flattens open positions tick-independently before any auto-square-off.

## Decision gates

- **Sandbox → live promotion** — operator-only, with the beta caveat acknowledged.
- **Any kill-switch fire** — operator reviews before the strategy resumes.
- **Raising the 50% cap** — forbidden without a fresh tail-risk study.

## Files of interest

- **Backtest (truth source for parity):**
  `docs/research/strategy/sector_follow_cap5_vol/2026-06-14_sector_matched_futures_10L.md`
  (NIFTY-only CAP50 control) and `2026-06-14_futures_10L.md`.
- **Signal source (the alpha primary):** `strategies/sector_follow_cap5_vol/`.
- **Strategy registry:** `strategies/STRATEGY_REGISTRY.md`.
- **Related memory files:** `futures-sector-follow-leveraged-beta-not-alpha.md`,
  `sector-matched-futures-no-better-than-nifty.md`,
  `option-buying-sector-follow-theta-kills.md`,
  `mis-leveraged-sector-follow-negative.md`.
