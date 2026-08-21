# Intraday Pullback Top-2 — Version Log

## v0.1.1 — 2026-08-02 (issue #509) — UI-configurable trade side
- New `trade_side` ∈ `both` (default) / `long_only` / `short_only`, editable from the strategy
  settings page and `POST /intraday_pullback_top2/api/settings`; env default
  `INTRADAY_PULLBACK_TRADE_SIDE`. Applied at the 09:00 reset like the other editable settings.
- Enforced in `run_selection` right after the 09:30 NIFTY day gate: an excluded side is never
  selected, never watched, never triggers, never journals a row.
- **Day-gate semantics (load-bearing):** the long and short books are mutually exclusive by NIFTY
  direction, so excluding a side is NOT a rebalance — the strategy simply does not trade on the days
  that side would have run. `long_only` gives up every NIFTY-down day (~half the calendar). The UI
  says this on the control and flags a one-sided selection.
- A deliberate skip records `skip_reason='trade_side=…'` on `get_status()` / `entry_breakdown()` so
  it never looks like a data outage. Invalid env/stored values fall back to `both` with a WARNING.
- No strategy-logic change: default `both` reproduces the v0.1.0 behaviour exactly, so the R53
  backtest figures remain valid for the default.

## v0.1.0 — 2026-07-09 (issue #394) — initial sandbox implementation
- Combined long+short book. Long: band [+1.0,+2.5), nf_mom + noreentrySL. Short: deep-loser band
  (−5,−3], no filters. Shared ₹60k / 2-slot pool, equal-weight, `fixed` sizing default.
- Windows: morning 09:30–11:00, no-trade 11:00–13:00, afternoon 13:00–15:00, EOD flatten 15:15.
- Vol multiplier 2.5×, stop floor 0.3%, 5m candles. Mode `sandbox` (default) → sandbox.db.
- Data: aggregator → broker get_multiquotes → historify fallback. 09:18 smoke check holds on failure.
- Editable via UI: base_capital, no-trade window, afternoon window, sizing mode.
- Backtest (20mo, real charges, no slippage): PF 1.60, +97.6% fixed / +162.1% compound, DD −8.9%.
- Source: config_snapshot.json. All parameter values logged to docs/PARAMETER_LOG.md.
