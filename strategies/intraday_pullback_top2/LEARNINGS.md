# Intraday Pullback Top-2 — Learnings

Cumulative knowledge. Newest first. Read before any change.

## 2026-07-09 — Backtest → sandbox (issue #394)
- **Edge (long):** mid-strength band [+1.0,+2.5%) IS the edge — extended top gainers mean-revert.
  Adding `nf_mom` (NIFTY at entry ≥ its 09:30 gain) + `noreentrySL` (no double-down after a stop)
  lifted the long book to PF 1.72; both validate on both halves and retain ~100% of good-month P&L.
- **Edge (short):** the profitable short is **deep losers (−3..−5%)**, NOT the mid-strength mirror
  (−1..−2.5%, which loses). Bounded on both sides: >−5% (capitulation) snaps back. `nf_mom` and
  `noreentrySL` HURT the short; the 1pm re-selection HURT it (PF→1.01). Deep-loser short PF 1.40.
- **Combined (fixed ₹60k):** PF 1.60, +97.6%, Sharpe 2.96, MaxDD −8.9%, 15/21 months. Compounding
  (net capital carried forward): +162.1%, DD −11.6%. Long is best risk-adjusted; short adds return
  but lowers Sharpe and is the most slippage-fragile leg.
- **Exit management all REJECTED:** 1:2 partial, breakeven-at-1R (deepens DD — shakes out winners on
  the pullbacks the edge is built around), trailing/targets. Full-size hold to 15:15 is optimal.
- **Irreducible:** Jun 2025 loss — genuine stock-level trend failure while NIFTY + sector held green.
- **Open risk #1: slippage is unmodeled.** The whole point of the sandbox run is to measure realized
  fill slippage per sleeve. At ~0.05%/side the combined PF likely drifts to ~1.3–1.4. Treat +97.6% as
  an upper bound. Deep-loser shorts (selling into a falling book) are where slippage bites hardest.

## Daily results
(append per sandbox trading day: date, book run, signals, fills, realized-vs-backtest slippage, net)
