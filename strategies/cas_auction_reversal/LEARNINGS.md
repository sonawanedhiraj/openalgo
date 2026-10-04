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
