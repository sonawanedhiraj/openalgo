# Sector Rotation ETF — Learnings

Cumulative knowledge for the `sector_rotation_etf` strategy. Read this before
making any decision. Most-important file in the strategy folder.

## Validated facts (from backtest research, pre-deployment)

### 4-year ETF validation (Round 26, the deployable gate)

On **actual ETF NAV** data over Aug 2022 → Jun 2026 (47 monthly rebalances), the
risk-parity combined book (variant 26d) delivered **Sharpe 1.17, CAGR 14.8%,
+69.8% total return** vs NIFTY's +35.0% — a **+34.8 pp** alpha — with max DD
−16.9%. It beat NIFTY in 4 of 5 calendar buckets. The result is robust across all
4 variants (low-vol, momentum, 50/50, risk-parity) and holds on both the ETF
series and the index baseline.

Walk-forward (Round 21b, 6M momentum) showed alpha barely degrades out of sample
(+18.3 pp in-sample → +16.9 pp OOS) — the edge generalizes, it is not an
in-sample fit. **6-month momentum was chosen over 3-month** for this reason: 3M
had higher in-sample return (+63.7%) but generalized poorly OOS (+3.2 pp);
choose 6M for robustness, not 3M for peak backtest return.

### The dividend-offset insight (why friction did not bite)

The feared post-friction "Sharpe cliff" (Round 25 projected 0.85–0.95) did **not**
materialize. Reason: **ETF NAVs capture the underlying dividend yield
(~1.0–1.5%/yr) that the price-only sector indices exclude.** That dividend
capture offsets the ~0.25%/yr expense ratio + extra ETF slippage almost exactly,
so the ETF-vs-index Sharpe drag netted to roughly zero (−0.01 to +0.05 across
variants — within rebalance noise). Round 25's estimate priced the friction but
ignored the dividend offset baked into ETF NAVs. **Implication for live
monitoring:** judge live performance against the ETF-NAV expectation, not the
price-index expectation — they differ by the dividend yield.

### Crash-month behavior (the low-vol leg's defensive role)

In NIFTY months ≤ −4% (Round 24, sector-index series), the low-vol sleeve lost
**less than the momentum sleeve in all three crashes** — notably −5.8% vs −11.0%
(Oct-2024) and −4.0% vs −7.3% (Feb-2025), saving roughly **3–5 pp** in the
milder corrections. But it is a **complement, not a hedge**: in the sharp
broad −11.3% March-2026 selloff even low-vol sectors (FMCG/Pharma/Consumer
Durables) fell hard and lagged NIFTY (−15.9% combined). Accept the ~−17% max
drawdown as the price of the alpha — the strategy has **no intra-month
intervention** and no fast crash gate (the naive 200DMA filter in Round 23 made
Sharpe worse, not better).

### Instrument-universe caveats

- **FINNIFTY & NIFTYOILANDGAS** have no liquid ETF — modeled as index proxies in
  the backtest. Real execution needs FINNIFTY futures + a 5-stock OILGAS basket.
  The scaffold therefore trades only the 9 ETFs; the 2 proxies are recorded in
  config but excluded from the tradeable universe for now.
- **METALIETF launched 2024-08-20** (only ~445 daily bars) — index-shadowed
  before launch; low weight impact, but its momentum/vol history is short.
- **HEALTHIETF, PSUBNKBEES** are thinner — budget ~15–25 bps slippage vs ~5 bps
  for BANKBEES/ITBEES. Start small to measure realized slippage vs the 0.15%/side
  assumption.

## Implementation notes (scaffold)

- `db/historify.duckdb` `market_data.timestamp` is **epoch seconds (UTC midnight
  per trading day)**, column is `timestamp` NOT `date`. Convert to date in Python
  via `datetime.utcfromtimestamp(ts).date()`; the UTC date equals the trading
  date for daily bars.
- All DB access is **read-only**. The module computes signals + recommended
  orders only — it never places orders and never subscribes to live feeds.

## Live Learnings

_(empty — populate after the first sandbox rebalance on 2026-07-01)_
