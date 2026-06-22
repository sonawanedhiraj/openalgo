# Futures Follow CAP50 — Version Log

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
