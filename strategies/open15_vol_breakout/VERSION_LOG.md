# open15_vol_breakout — Version log

| Version | Date | Change | Evidence |
|---|---|---|---|
| 0.1.0 | 2026-07-20 | Initial sandbox implementation: top-3 gap selection, mid-bar tick trigger (cumvol ≥ 1.5× running-avg minute volume + level break), MARKET MIS ₹150k notional, hard 09:30 flatten. Modes sandbox/observe. | Round 58 research doc (`docs/research/strategy/open15_vol_breakout/2026-07-19_...md`); issue #425. Deployed as a measurement: no honest bar-level edge exists; this quantifies mid-bar capture. |
