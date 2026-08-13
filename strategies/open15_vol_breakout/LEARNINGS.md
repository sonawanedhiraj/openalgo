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

### 2026-07-25 (Sat, cont.) — #456 commit 2: quote-first prev-closes
Operator question exposed the residual gap in commit 1: the #305 registry is
populated as a SIDE EFFECT (boot seeder's broker-fallback arm — which makes no
broker calls pre-open when historify looks healthy — and the resettle, which is
the very job the arm races). So at 09:10 the registry can be sparse or empty.
Commit 2 removes the dependence: the arm now makes ONE batched quote call
(`fetch_broker_prev_closes`, `prev_close` = settled T-1 close) as the PRIMARY
source, records the values into the registry, and falls back to the commit-1
registry-verified-historify chain per symbol/on failure. Cost: one broker API
call per trading day. Learning: **a verification layer that depends on another
job's side effects inherits that job's timing — fetch the truth at the moment
of use.**

### 2026-08-13 — #595: broker-OI (500-lot) watch-list filter; percentile screens cannot mirror an absolute broker rule
Zerodha rejected 4 of 5 option entries ("MIS LIMIT orders are blocked ... OI
less than 500 lots"): UNOMINDA 282 / KALYANKJIL 433 / ADANIENSOL 339 / BDL 460
lots vs the one fill ASHOKLEY at 2,791 — `opt_entry_oi / opt_lot_size < 500`
separated them perfectly, on data the journal was already capturing. Gate 1
(percentile) is structurally blind three ways: band-SUM OI (BDL band 4,255
lots vs 460 in its single strike), RELATIVE rank (KALYANKJIL at p96 was
blocked), and YESTERDAY's ATM strike (gappers select precisely the strike
where OI hasn't accumulated). Fix: mirror the broker's absolute per-contract
rule at watch-list construction only — seed + rolling candidates get ONE
batched `/quote` (OI in lots via `production_oi_filter`), blocked names skip
and promote the next rank; entry keeps NO check (broker is the authority, #548
paper path is the backstop; a rejection frees its `max_trades` slot —
already pinned by `test_rejection_releases_its_max_trades_slot`). Config
`option_min_oi_lots` (default 500, 0=off, env `OPEN15_MIN_OI_LOTS`).
Learnings: **when the broker enforces an absolute rule, mirror the rule —
don't model it**; and **an excluded-side shadow cohort must pass the same
tradeability filters as the traded side, or it accrues unrealizable P&L**.
⚠ Cohort boundary: from the first armed session after this ships, shadow AND
seed selection exclude sub-500-lot names — shadow/parity P&L is not directly
comparable across this date (same class of note as the #581 start date).
