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

### 2026-07-25 (Sat) — Round 59: July stock-vs-options backtest fills the dashboard Backtest column
Full-July (07-01..24, 18d) production-faithful replay (issue #453; harness
`backtest/options_open15/july_full_run.py`, untracked-local): production
`resolve_day_config` defaults, production charge models, honest next-minute-open
entry. **Stock: 15 trades, 60% win, +Rs2,564 net (+2.85% on the 90k margin
base, maxDD -0.77%). Options (production option-mode, real 28-JUL premiums):
13 trades (2 unaffordable), 62% win, +Rs11,195 (+12.44%, maxDD -2.18%)** —
but NATIONALUM 07-23 alone is +7,583 of it, AND that pick came from a
bar-vs-tick selection divergence (the live day traded OIL instead: pick overlap
4/6 on 07-22, 3/6 on 07-23). Executed-trade parity where both fired is tight
(BAJAJ-AUTO 07-22: same qty 14, entry within 2Rs). One green month does NOT
overturn R58's full-history -0.16%/trade honest verdict; `parity_target` now
carries the numbers with that caveat inline. Full doc:
`docs/research/strategy/open15_vol_breakout/2026-07-25_r59_july_stock_vs_options.md`.

### 2026-07-25 (Sat, later) — #456 fix: arm-time prev-close verification vs broker registry
R59's tick-log replay proved the selection code exact (07-22 to 2dp) but found
07-23's gaps shifted by provisional prev-closes: the 09:10 arm raced the
09:08-09:18 daily-D resettle. Fix: `verify_prev_closes` cross-checks every
historify prev-close against the #305 broker prev-close registry at arm time —
divergence > 0.05% -> broker settled value wins (fail-open per symbol when no
registry entry). Provenance in the `armed` event (`prev_close_check`: checked /
no_registry_entry / overridden + per-symbol detail) and each pick's prev-close
in the `selection` event, so this class is diagnosable from the day log alone.
9 unit tests incl. the exact OFSS 07-23 shape. Learning: **a correct selection
rule fed unverified reference data is still wrong** — same lesson as DELHIVERY
2026-07-02 (#305), now enforced at open15's choke point too.
