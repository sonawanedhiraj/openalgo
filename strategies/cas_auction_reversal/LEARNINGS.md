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
