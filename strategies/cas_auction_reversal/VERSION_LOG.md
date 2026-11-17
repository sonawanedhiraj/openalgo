# cas_auction_reversal — Version Log

## v0.1.1 — 2026-10-04

- **`deployable: true`.** `false` rendered a "Scaffold" badge on /strategies
  although the strategy routes to sandbox by default.
- **`parity_target` now carries the Backtest column's keys** from a
  strategy-faithful replay (`backtest/close15/cas_ar_parity.py`):
  280 trades, 61.8% net win, Rs38,599 net,
  max DD -0.61%, CAGR 54.6% / Sharpe 6.02 (2-month window, indicative).
- **Threshold tiers** (net, capped, and R71b's gross uncapped) are recorded under
  `backtest_threshold_tiers*`.

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

## 2026-11-12 — breadth gate `min_candidates` = 10 (issue #777)

- **Change:** when fewer than `min_candidates` names cross the threshold at the 15:23:30
  decision, no order is placed that day. Candidates are still journalled
  (`skip_reason='breadth_gate'`). UI row → `CAS_AR_MIN_CANDIDATES` → default 10; 0 = off.
- **Why:** the first 19 sandbox sessions (2026-10-05..11-05, 125 trades) were break-even:
  gross +₹17,160, CNC charges ₹15,378, net +₹1,782 (+0.275% gross per trade, 44% net win).
  Split by candidates at the decision: >= 10 → 8 days, 80 trades, **+₹11,759**;
  < 10 → 11 days, 45 trades, **−₹9,977**.
- **Independent check** (R71b bars, 2026-08-03..09-29, final-close dislocation >= 0.5%,
  top 10/day, 0.25% cost): < 5 names → −0.21%/trade; 5–9 → −0.04%; >= 10 → +0.57%
  (H1 +0.44, H2 +0.65; 16 days, 160 trades).
- **Caveats:** the gate was found on the forward sample and confirmed on the backtest
  sample; 8 kept forward days is small. The original pre-registered gate (>= +0.40% gross,
  >= 70% win at 20 sessions) FAILS ungated (0.275% / 44%); gated it is ~0.54% gross and
  62% win — the win-rate leg still fails. Stays sandbox, `deployable: false`.
- **Pre-registered check:** after 10 more gated-in days, net of the kept trades must be
  > 0, else remove the gate and reject the strategy.
