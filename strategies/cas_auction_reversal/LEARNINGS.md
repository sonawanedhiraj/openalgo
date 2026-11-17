# cas_auction_reversal — learnings

## 2026-10-04 — research basis (R71b / R71c, issue #751)

- **The effect.** Post-CAS, the auction close vs the 15:14 print is the
  strongest overnight predictor found:
  - top-minus-bottom decile spread −0.544% (t −6.3), both halves;
  - pre-CAS, on the 15:29 print, −0.388% (t −26).
- **The tradeable side is long only.** The short mirror is equally strong in
  cash, but it cannot be held overnight: there is no CNC short, and stock
  futures capture almost none of the move.
- **Exit timing.** The T+1 09:15 bar close beat the T+1 open (+0.60% vs +0.42%
  at ≥ 0.5%).
- **Execution.** Sandbox MARKET orders fill at the ask, so the sandbox entry is
  a LIMIT at the post-auction LTP.
- **Unmeasured until live data accrues:** how well the 15:23:30 indicative
  price predicts the final close. That is the first thing to read from
  `cas_ar_polls`.

## 2026-10-05 — first sandbox session (issue #759)

- **2 selected, 1 bought.** UNITDSPR (−0.76%) filled at the auction close
  ₹1,369.00 and sold T+1 09:16 at ₹1,359.70: −₹334.80 gross, −₹459.44 net.
- **BANDHANBNK (−0.79%) never filled.** The sandbox LIMIT was rounded to the
  nearest ₹0.05 (168.27 → 168.25), below the market for a BUY. BANDHANBNK's
  real tick is ₹0.01. The order sat pending, the row stayed `placed`, and the
  T+1 exit sent two SELLs for shares never held (refused, harmless).
  Had it filled: about +₹149 net at the 09:16 price, so the day would have
  been about −₹310 net instead of −₹459.
- **Fix (#759):** per-symbol tick with BUY-rounds-up; unfilled entries are
  cancelled at 15:45 and journalled `unfilled`; the exit verifies `placed`
  rows before selling. One session says nothing about the edge — the
  backtest's net win rate is 62%.

## 2026-11-12 — the edge is a BREADTH effect (issue #777)

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
