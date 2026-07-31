# open15_vol_breakout — Version log

| Version | Date | Change | Evidence |
|---|---|---|---|
| 0.1.0 | 2026-07-20 | Initial sandbox implementation: top-3 gap selection, mid-bar tick trigger (cumvol ≥ 1.5× running-avg minute volume + level break), MARKET MIS ₹150k notional, hard 09:30 flatten. Modes sandbox/observe. | Round 58 research doc (`docs/research/strategy/open15_vol_breakout/2026-07-19_...md`); issue #425. Deployed as a measurement: no honest bar-level edge exists; this quantifies mid-bar capture. |
| 0.1.1 | 2026-07-31 | Trade-side option: `trade_side` = `both` (default, unchanged behavior) / `long_only` / `short_only`, editable on `/open15_vol_breakout/logs` and via `POST /api/config`, env default `OPEN15_TRADE_SIDE`. Enforced in `Open15Core._finalize_selection` — an excluded side is never selected, so it is never watched, never triggers and never journals. Recorded in the day's `armed` decision-log event. | Issue #503. No parameter change to the default configuration; the parity targets remain both-sides numbers and a one-sided day is flagged as not comparable on the logs page. |
