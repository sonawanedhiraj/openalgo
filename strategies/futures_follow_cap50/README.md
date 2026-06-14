# Futures Follow CAP50

`futures_follow_cap50` · v0.1.0 · **scaffold-only · deployable: false**

A **leveraged broad-market-beta** sleeve built on the `sector_follow_cap5_vol`
signal set. At **15:20 IST** it reuses the sector_follow C1×W2+E4 evaluator to find
today's ≤5 gate-passing stock signals, and for each — greedily in vol-ratio order —
buys **one NIFTY near-month index future lot**, HARD-CAPPED at **50% of capital as
overnight SPAN margin**. Positions are held to the **next trading day 15:25 IST**
(T+1) and sold MARKET. **No stop loss** (Phase-1 proved hard stops are net-negative
on this signal class); the **15:14 IST EOD watchdog** is the only safety backstop.

## ⚠️ Honest classification — leveraged beta, NOT alpha

The backtest clears the 12% floor (CAGR **14.44%**, Sharpe **1.27**, MaxDD **−8.0%**
on ₹10L), but **the signal does NOT predict NIFTY** — directional hit-rate **53.4%**
(below the 55% line), stock↔NIFTY correlation **0.295**. The return is the small
positive broad-market drift on bullish signal-days, amplified ~2× by futures
leverage. **In a flat or bear NIFTY year this sleeve has no stock-selection edge to
fall back on.** Keep `sector_follow_cap5_vol` (the CNC T+1 equity book) as the alpha
primary; run this as a *separate, leverage-bounded* beta bet.

Sector-matched routing (banking → BANKNIFTY) was tested and **REJECTED** — it costs
0.74pp CAGR vs NIFTY-only and doesn't improve correlation. NIFTY-only is the chosen
vehicle.

## Mode

`FUTURES_FOLLOW_MODE` (env) = `scaffold` (default) | `sandbox` | `live`

- **scaffold:** compute signals, log, write the trade journal — **NO orders placed.**
- **sandbox:** orders route to `db/sandbox.db` (virtual ₹1Cr).
- **live:** real broker orders. **Operator-only flip** — never automated.

## Key files

- `services/futures_follow_service.py` — evaluator + scheduler glue (5 jobs).
- `blueprints/futures_follow.py` — control API at `/futures_follow_cap50/api/*`.
- `database/futures_follow_db.py` — `futures_follow_trades` journal.
- `test/test_futures_follow_service.py`, `test_futures_follow_db.py`,
  `test_futures_follow_blueprint.py`.

## Scheduled jobs (IST, mon-fri)

| Job | Time | What |
| --- | --- | --- |
| `futures_follow_daily_reset` | 09:00 | clear kill switch + daily P&L + journals |
| `futures_follow_eod_watchdog` | 15:14 | flatten any still-open T+1 position (backstop) |
| `futures_follow_entry` | 15:20 | evaluate signals, buy 1 NIFTY lot/signal up to 50% cap |
| `futures_follow_exit` | 15:25 | square off positions opened on a prior trading day |
| `futures_follow_eod_summary` | 15:30 | Telegram summary + Day-N markdown report |

See `PLAN.md` for the delivery plan and `LEARNINGS.md` for the cumulative record.
