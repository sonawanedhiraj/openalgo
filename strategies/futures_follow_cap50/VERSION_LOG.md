# Futures Follow CAP50 — Version Log

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
