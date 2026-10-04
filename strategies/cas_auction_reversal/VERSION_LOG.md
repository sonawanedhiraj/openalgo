# cas_auction_reversal — Version Log

## v0.1.0 — 2026-10-04

Initial ship (issue #752), sandbox-only (`deployable: false`).

- **Arm 15:14:30:** universe = tradeable F&O stocks on NSE; monitor polls every
  15 s to 15:33, journaled to `cas_ar_polls`.
- **Decide 15:23:30:** buy where the indicative close is ≥ 0.5% below the frozen
  last continuous print. Top 10 by dislocation, ₹50k each.
- **Entry:**
  - live: a CNC LIMIT into the auction at decide time;
  - sandbox: a CNC LIMIT at the post-auction LTP at 15:32.
- **Exit:** T+1 09:16 CNC SELL MARKET, retry 09:20, boot catch-up until 15:10.
- **EOD 15:45:** the final auction close for every candidate.
- **Tunables (env, clamped):** `CAS_AR_THRESHOLD_PCT` 0.5,
  `CAS_AR_MAX_POSITIONS` 10, `CAS_AR_CAPITAL_PER_TRADE_INR` 50000,
  `CAS_AR_POLL_INTERVAL_S` 15.
