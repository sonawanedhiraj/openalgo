# Intraday Pullback Top-2 — Version Log

## v0.1.0 — 2026-07-09 (issue #394) — initial sandbox implementation
- Combined long+short book. Long: band [+1.0,+2.5), nf_mom + noreentrySL. Short: deep-loser band
  (−5,−3], no filters. Shared ₹60k / 2-slot pool, equal-weight, `fixed` sizing default.
- Windows: morning 09:30–11:00, no-trade 11:00–13:00, afternoon 13:00–15:00, EOD flatten 15:15.
- Vol multiplier 2.5×, stop floor 0.3%, 5m candles. Mode `sandbox` (default) → sandbox.db.
- Data: aggregator → broker get_multiquotes → historify fallback. 09:18 smoke check holds on failure.
- Editable via UI: base_capital, no-trade window, afternoon window, sizing mode.
- Backtest (20mo, real charges, no slippage): PF 1.60, +97.6% fixed / +162.1% compound, DD −8.9%.
- Source: config_snapshot.json. All parameter values logged to docs/PARAMETER_LOG.md.
