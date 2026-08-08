# Futures Follow CAP50 — Version Log

## Data repair — 2026-08-08: the 7 lots #507 stranded were closed by backfill

**The `futures_follow_trades` exit rows dated 2026-07-28 .. 2026-07-31 were
written by an operator backfill, not by the running strategy.** They are
deliberately indistinguishable from real exits in the journal (`note='t+1_exit'`,
strategy tag `futures_follow_cap50`, sandbox order-id shape) — an explicit
operator decision on 2026-08-08 — so **this entry is the only record that they
are reconstructed.** Any performance study of this sleeve that treats
2026-07-28..31 as measured fills is overstating its evidence.

Why they were needed: issue #507 (the analyze-overlay position read) suppressed
every T+1 exit from 2026-07-17, stranding 7 NIFTY lots (455 qty) and consuming
the whole 50% margin cap, which then blocked all entries after 2026-07-30. The
code fix is PR #569.

How the prices were derived — real, not modelled:
- Exit marks are the **OPEN of the 15:25 IST 1m bar** for `NIFTY25AUG26FUT`,
  pulled from the broker's own history API (`history_service.get_history`).
  15:28.1 → `2026-07-28=24115.1`, `2026-07-29=24305.0`, `2026-07-30=24362.0`,
  `2026-07-31=24455.1`.
- Calibrated against the four real 15:20 entry fills: the bar open tracks an
  actual fill to **±4 index points (~₹260/lot)**. That is the residual error in
  these four rows.
- Entry prices are the **actual sandbox fills** (`sandbox_orders.average_price`),
  not the journal's pre-fill quote.
- Charges use the strategy's own `compute_futures_charges` — not a
  reimplementation.
- Exits are **merged per (exit_date, symbol)**, as the overnight rehydrate would
  have merged the positions (cf. the real qty=130 rows). This is not cosmetic:
  brokerage is a flat ₹40/round-trip, so 7 separate exits would have overstated
  charges by ~₹142 against the 4 the strategy would really have placed.

Result — 4 exits, 455 qty, **+₹43,706.44 net**. Sleeve total after repair:
**11 round-trips, +₹11,431.17 net, 7/11 wins**, versus −₹32,275.28 over 7
round-trips before it.

⚠ **This is NOT the sleeve's true P&L for the period.** Had the exits fired,
the freed margin would have admitted further entries from 2026-07-31 onward
(the log shows `CAP HIT — skipping signal` daily). Those trades never happened
and are not reconstructable, so the repaired series still understates activity.

Backups: `db/openalgo.db.bak.20260808_211821`, `db/sandbox.db.bak.20260808_211821`.

## v0.4.0 — 2026-07-14
OPTION_C same-minute@15:25 entry, flag-gated (issues #405/#406).
Mode: **sandbox (default)** · Deployable: **true** · Default behavior: **unchanged**

- Finding (#405): the 15:20 entry seeds its 50% cap from `lots_held()`, which
  counts the still-open prior-day lot (it exits at 15:25). This under-sizes carry
  days — production was running the *more conservative* sizing than its own
  validating backtest, which sizes each day against a fresh cap.
- Backtest (four-way, `docs/research/strategy/futures_follow_cap50/2026-07-14_entry_cap_carry_sizing.md`):
  CONTROL 13.12% / Sharpe 1.19 · OPTION_A (fresh cap, entry 15:20) 14.44% / 1.27
  but peak margin 98.7% · OPTION_B (same-min@15:20) 13.75% (loses the exit edge)
  · **OPTION_C (same-min@15:25) 14.31% / 1.26, peak margin 49.8%** — the winner:
  recovers +1.19pp/+0.07 Sharpe over CONTROL with no margin overlap.
- Key asymmetry: exit-timing carries the edge (moving it earlier costs 0.69pp),
  entry-timing does not (moving it later costs 0.13pp).
- Change: `FUTURES_FOLLOW_ENTRY_MODE` (default `legacy`). `same_minute` swaps the
  15:20 entry for a 15:20 *signal snapshot* and the 15:25 exit for a *15:25
  exit-then-entry* job — exit first frees margin, so `run_entry` sizes against a
  fresh (empty) book with no code change to the cap math. Selection stays at
  15:20; execution moves to 15:25.
- **Default is `legacy` — no behavior change on merge.** Operator flips to
  `same_minute` after review. Live needs exit-fill confirmation before entry
  (documented in PLAN.md); sandbox ₹1Cr book is unaffected.

## v0.3.1 — 2026-07-05
EOD watchdog moved 15:14 → 15:28 so the 15:25 T+1 exit is primary (issue #334).
Mode: **sandbox (default)** · Deployable: **true**

- Bug: the watchdog and the 15:25 exit share the predicate `entry_date != today`;
  firing at 15:14 the watchdog flattened every prior-day position FIRST, making
  the de-facto exit 15:14 — an 11-minute divergence from the backtested/
  documented 15:25 exit. The 15:14 slot was inherited from the simplified
  engine's MIS constraint; futures_follow is NRML (accepted till the 15:30 NFO
  close), so it never applied.
- `futures_follow_eod_watchdog` cron now 15:28 IST (after the 15:25 primary
  exit, 2 min before close, matching the entry-deadline buffer). Selection
  predicate UNCHANGED — with correct ordering it is exactly right.
- Supporting change: a REJECTED/exception exit SELL now KEEPS the position in
  `paper_book` (was: silently dropped) so the 15:28 watchdog retries it; the
  #265 store-reconcile guard suppresses the retry if the order actually filled.
- The simplified engine's MIS watchdog (`services/eod_watchdog_service.py`,
  15:14 cap) is untouched — its constraint is load-bearing there.

## v0.3.0 — 2026-07-03
Stage-1 LLM veto wired, strategy-aware (issue #318).
Mode: **sandbox (default)** · Deployable: **true**

- `run_entry` now reviews every in-cap signal via
  `signal_review_service.review_signal(strategy_name='futures_follow_cap50')`
  BEFORE `place_entry`. The prompt is strategy-aware: a STRATEGY CONTEXT block
  (sourced from this folder's `config_snapshot.json` `llm_context` key, code
  fallback in `services/signal_review_service.py` — keep in sync) frames the
  review as **overnight-regime fit for a leveraged long NIFTY carry**, carrying
  the honest caveat (hit-rate 53.4%, corr 0.295 — leveraged beta, not alpha).
  The review combines BOTH the source stock signal (vol_ratio/stock_ret/
  sector_ret) and the resolved NIFTY-future/book state from `get_status()`
  (locked operator decision).
- Enforcement mode resolution: `strategy_llm_config` row → `VETO_LAYER_MODE`
  env → mode-aware default where **sandbox = active/enforcing** (B4). With no
  row and no env, the veto ENFORCES on the sandbox book from the first cycle
  (R2 — intended; disable via the dashboard LLM toggle / `VETO_LAYER_MODE=off`).
- An enforcing `skip` drops that lot WITHOUT consuming the 50% margin cap
  (later signals may use the freed slot), journals `status='veto_skip'`
  (no order id, no margin, no phantom position), and records
  `actually_taken=false` on the `signal_decision` audit row.
- R3 latency bound: cumulative review wall-time per entry batch is capped at
  180s (`VETO_REVIEW_BUDGET_SECONDS`, code constant); beyond it the remaining
  signals place UNREVIEWED with a WARNING. Per-review latency is logged.
  Any reviewer failure fails OPEN (take), mirroring the simplified engine.
- Dashboard: `futures_follow_cap50` added to `_VETO_ENABLED_STRATEGIES`; its
  LLM decisions view filters to `source='futures_follow_cap50'`; the simplified
  engine's view now EXCLUDES futures rows (R1, `exclude_sources`).
- Simplified-engine veto path unchanged (`strategy_name=None` default is
  byte-for-byte the old prompt/context).

## v0.2.0 — 2026-06-15
Sandbox is the structural default — scaffold mode dropped entirely (operator
redirect: must trade in sandbox from Monday 2026-06-15 open).
Mode: **sandbox (default)** · Deployable: **true**

- `VALID_MODES = ("sandbox", "live")`; default `FUTURES_FOLLOW_MODE=sandbox`;
  unknown value force-falls-back to `sandbox` (was `scaffold`).
- `place_entry`/`place_exit` no longer have a scaffold "no-order" branch — they
  always route via the order placer (sandbox → `sandbox.db`, live → broker). Journal
  statuses are `placed`/`rejected`/`exception` only (the `scaffold` status is gone).
- `config_snapshot.json`: `mode: "sandbox"`, `deployable: true`.
- All safety rails unchanged: cap-50%-margin/day HARD enforcement, 15:14 EOD
  watchdog, 3% daily kill switch, data-freshness gate, runtime-override pause.
- Tests reworked: sandbox-mode tests assert actual order placement; the scaffold
  no-order tests are removed; a default-mode test confirms boot defaults to active
  sandbox trading.
- First sandbox cycle: Monday 2026-06-15 15:20 IST.

## v0.1.0 — 2026-06-15
Initial scaffold from the 2026-06-14 NIFTY-only CAP50 leverage research.
Mode: scaffold-only · Deployable: false

- `services/futures_follow_service.py` — `FuturesFollowService`: reuses the
  sector_follow_cap5_vol signal evaluator; resolves the NIFTY near-month future
  dynamically; sizes 1 lot/signal greedy-in-vol-ratio under a HARD 50%-of-capital
  overnight-margin cap; T+1 15:25 MARKET exit; NO stop loss; 3% daily-loss kill
  switch; modelled ~₹530/lot round-trip charges; 5 APScheduler jobs (09:00 reset /
  15:14 watchdog / 15:20 entry / 15:25 exit / 15:30 EOD summary). All I/O injected.
- `database/futures_follow_db.py` — `futures_follow_trades` journal (additive).
- `blueprints/futures_follow.py` — control API at `/futures_follow_cap50/api/*`.
- `app.py` — service + DB + blueprint wired. Default mode=scaffold → zero live
  behavior change.
- Backtest reference (NIFTY-only CAP50): CAGR 14.44%, Sharpe 1.27, MaxDD −8.0% on
  ₹10L, 2024-01..2026-06.
- **Caveat carried:** leveraged broad-market beta, NOT stock-selection alpha
  (signal→NIFTY hit-rate 53.4%, corr 0.295). Sector-matched routing rejected.
- Decision: NIFTY index futures are MONTHLY — the resolver uses the near-month
  (front) contract, not a "weekly" future (which does not exist for NIFTY).
