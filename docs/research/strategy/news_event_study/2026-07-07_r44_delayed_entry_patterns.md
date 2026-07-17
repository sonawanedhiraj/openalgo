# R44 — Delayed-Entry Patterns on News-Event Stocks

**Date:** 2026-07-07 · **Issue:** #363 · **Prior round:** R43 (REJECT chase entries) · **Verdict: REJECT all 5 patterns**

## Question

R43 killed *chasing* news spikes. Does *waiting* rescue the news tape — enter
only after the spike digests, on a defined-risk pattern? Operator-proposed:
(1) pullback-to-support → bull-flag → RR-feasibility gate; (2) the simplified
engine's no-supply/demand-return entry (lowest-volume red candle → green
volume-surge candle). Plus three added patterns: VWAP reclaim, opening-range
breakout, and pre-close-strength overnight hold (the wrapper every prior round
says is the only one that works on spike-adjacent signals).

## Data & method

Same R43 assets (12-month NSE announcement tape × Zerodha 1m bars; 13,590
eligible symbol-days), 5m resampled patterns, long-only (R42: shorts bounce),
entry next 5m open + 0.25%/leg slippage, real MIS/CNC charges, penny floor
₹50, circuit-lock excluded, stops/targets on 5m high/low crossings
(stop-priority). Max hold T+1 15:15 (data window). Simulator:
`backtest/news_event_study/simulate_entries.py` (detectors unit-validated on
synthetic paths before the real run). 9,034 trades, 66,847 no-trade rows.

## Results — every pattern net-negative, gross-negative

| Strategy | n | Hit | Avg net/trade | Median | Total net (₹50k/pos) |
|---|---|---|---|---|---|
| S1 bull_flag_pullback (RR-gated ≥1.5/2.0) | 616 | 25% | −0.48% | −1.29% | −145k |
| S5 preclose_strength (overnight, no stop) | 1,686 | 39% | −0.55% | −1.03% | −463k |
| S2 no_supply_reversal (engine mirror) | 4,189 | 21% | −0.61% | −0.88% | −1,249k |
| S3 vwap_reclaim | 1,476 | 26% | −0.79% | −1.40% | −580k |
| S4 orb (at-open events) | 1,067 | 28% | −0.83% | −1.23% | −435k |

- **The RR gate cannot rescue a bad win rate.** S1 at RR≈2 needs ~33% wins to
  break even; it gets 25% (416 stops vs 120 targets). Checking feasibility
  before entry sizes the risk correctly — it does not change which side the
  market resolves.
- **The engine's no-supply entry does not transfer.** S2 wins only 21% at
  fixed RR 2. The 5m structure that works on F&O large caps in the simplified
  engine fails on news-day names — the news-day tape is crowded and
  mean-reverting at every 5m structure we tested.
- **Loss is gross, not costs**, in essentially every cell (best cells' gross
  also negative or ~0) — same signature as R43.
- **Best cell overall is exactly zero:** S5 positive-news, ret≥3%, close
  within 2% of high, T+1 exit: **+0.017% net (n=135, hit 49%)**. The
  known-good pre-close-strength overnight wrapper — which clears 12%+ CAGR on
  sector-confirmed F&O signals — dies when conditioned on news instead.
  **A news catalyst is adverse selection relative to sector confirmation**:
  the stock is up *because of the crowd*, not because its sector is carrying it.
- INSUFFICIENT (n<30, not evidence): S5 tape_decide cells at +0.96–0.98%
  (n=22–23). Recorded, not actionable.

## Combined R43+R44 conclusion

The NSE announcement tape is not a tradable long-alpha source at
retail-reachable horizons — neither at detection (R43) nor via delayed
pattern entries with defined risk (R44). Entry *timing* was not the problem;
the underlying drift after news is negative-to-zero at every structure
tested. The live news-scanner **trading** path stays DO-NOT-BUILD;
informational alerting remains the only defensible use. Remaining untested
lead from R43: fading the spike (short side) — a different strategy class
with real execution risk, parked.

## Caveats

- Patterns evaluated on 5m closes with next-bar-open entries; sub-5m
  variants untested (unlikely to flip a −0.5%..−0.8% per-trade deficit).
- Support in S1 defined as pullback-low/VWAP/prev-close structure; other
  support definitions (daily pivots, multi-day levels) would need T−k data
  not in the price store.
- Max hold T+1 (data window); multi-day swing holds untested — but S5's T+1
  failure and R43's T+1 event-study decay argue against longer holds helping.
