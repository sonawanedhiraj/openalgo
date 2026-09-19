# cas_320_expiry_straddle — Version Log

## v0.1.0 — 2026-09-19

Initial ship (issue #740). ATM straddle on NIFTY/SENSEX weekly expiry days:
arm 15:12, ATM from the 15:15:00 spot print, entry 15:20:00 (NRML, MARKET in
sandbox / marketable LIMIT live), target = combined bid net of modelled
charges ≥ `target_mult` × cost for 2 consecutive polls, hard exit
`hard_exit_time` (default 15:28:00), protected fallback flatten 15:38, EOD
summary 15:45. UI config row (`cas_straddle_config`): per-underlying trade
toggles, lots (1–10), `max_premium_inr` (refuses, never trims), `target_mult`
(1.1–5.0), `hard_exit_time` (15:20:30–15:37:00), `poll_interval_s` (2–60).
ATM ±2 ladder recorded every poll for both underlyings regardless of the
toggles. No env flags; live only via the `/strategies` toggle.
