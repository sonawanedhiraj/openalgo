# open15_vol_breakout — Learnings

Cumulative knowledge for the mid-bar volume-surge breakout. **Read SPEC.md §2
first** — this strategy exists to answer one measured question, not because a
backtest said it works (the backtest, honestly priced, says it does NOT).

## Cumulative knowledge (start here)

1. **The bar-level signal has no honest edge.** Round 58 (2026-07-19): every
   1m-bar variant — 1.5×/1.0× volume gate, close entry, level entry with stop,
   prev-bar gate — converges to ≈ −0.10…−0.16%/trade net. The published
   +0.38%/trade was intra-bar look-ahead (entry priced before the volume gate
   was knowable). The options overlay is worse (spread + theta).
2. **The open question is capture fraction.** The burst between level and
   entry-minute close averages +0.54% (median +0.28%). A tick-driven mid-bar
   entry legally fires part-way through. If the trigger fills early enough to
   keep ≥0.4pp vs the close entry, there is a real strategy; if the volume and
   the move arrive in the same seconds (likely, per HFT priors), there isn't.
3. **Every entry here is a data point.** The journal's
   level / trigger_second / trigger_price / entry_minute_close columns ARE the
   experiment. ~15 signals/month expected → first verdict after ~3-4 weeks.
4. **Boot discipline is load-bearing:** app up before 09:15 IST or the day is
   skipped (the tick_logs archive missed the open on 20/20 days — that's why
   this couldn't be answered offline).

## Daily log

### 2026-07-20 (Sun) — created
Scaffolded + wired (issue #425): service (ZMQ tick sub + 4 APScheduler jobs:
arm 09:10 / exit 09:30 / retry 09:32 / summary 09:35), `open15_trades` journal,
`/open15_vol_breakout/api/*`, sandbox mode default. First armed session
expected Mon 2026-07-21 — REQUIRES pre-09:15 boot.
