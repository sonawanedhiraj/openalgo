# cas_320_expiry_straddle

Expiry-day ATM straddle across the closing auction session (issue #740).

On weekly index-option expiry days — NIFTY on NFO (Tuesdays), SENSEX on BFO
(Thursdays); the day comes from the master contract, never from `weekday()` —
the strategy buys the ATM call and the ATM put at **15:20:00 IST** and sells
both when the combined bid reaches `target_mult ×` the premium paid (default
2×), or at the hard exit time (default 15:28:00). The premium paid is the
maximum loss.

| When (IST) | What |
|---|---|
| 09:00 | daily reset |
| 15:12 | arm: trading day + expiry day per underlying, config snapshot, ATM ±2 ladder, monitor thread, hard-exit job |
| 15:14–15:41 | monitor thread polls spot + ladder every `poll_interval_s` on ONE batched quote call, journals every poll, evaluates the target |
| 15:15:00 | ATM fixed from the last continuous spot print |
| 15:20:00 | entry: BUY CE + PE, NRML, `lots × lotsize` per underlying toggled on |
| `hard_exit_time` | SELL any leg with a bid; a leg with no bid is left to cash-settle at 0 |
| 15:38 | protected fallback flatten from the position book |
| 15:45 | EOD summary + session digest + settlement counterfactual |

**Controls (and nothing else):** the `/strategies` Live/Sandbox toggle, the
Settings card on `/strategies/cas_320_expiry_straddle` (per-underlying trade
toggles, lots, premium cap, target multiple, hard exit time, poll interval),
and `pause`/`resume` on the control API. No env flags.

Files: `services/cas_straddle_service.py`, `database/cas_straddle_db.py`,
`blueprints/cas_straddle.py` (`/cas_320_expiry_straddle/api/*`),
`frontend/src/pages/strategies-dashboard/CasStraddleCard.tsx`. Plan and
verified exchange facts: [`PLAN.md`](PLAN.md). Learnings: [`LEARNINGS.md`](LEARNINGS.md).
