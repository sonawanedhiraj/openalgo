# Sector Rotation ETF Strategy

> **Status: SCAFFOLD ONLY — NOT LIVE.** Signal computation + recommended-orders
> output only. No scheduler, no live mode, no order placement. The operator
> reviews the emitted orders manually for the first 1–2 rebalances.

Monthly long-only rotation across a small universe of liquid Indian sector ETFs.
Capital is split between a **momentum sleeve** (buy the 3 strongest sectors by
trailing 6-month return) and a **low-volatility sleeve** (buy the 3 calmest
sectors by trailing 60-day return volatility), combined with **inverse-volatility
weighting** between the two legs. Long-only, unleveraged, ≤6 sectors held at a
time, rebalanced on the first trading day of each month.

## Headline backtest metrics (variant 26d, ETF NAV, Aug 2022 → Jun 2026)

| Metric | Value |
|---|---:|
| Sharpe | **1.17** |
| CAGR | **14.8%** |
| Total return | +69.8% (vs NIFTY +35.0%) |
| Total-return alpha | **+34.8 pp** over NIFTY |
| Max drawdown | −16.9% |
| Rebalances | 47 monthly |

Robust across all 4 portfolio variants and on both the ETF series and the index
baseline. Friction did NOT degrade the edge — ETF NAV dividend capture
(~1.0–1.5%/yr) offsets expense ratio + slippage almost exactly.

## Entry point

```bash
uv run python -m services.sector_rotation_etf_cli --asof 2026-06-05 \
  --current-positions '{"BANKBEES":100}'
```

Computes signals read-only against `db/historify.duckdb`, prints a human-readable
summary, and writes recommended orders to
`outputs/sector_rotation_etf/rebalance_<asof_date>.json`. It does **not** place
orders.

## Pointers

- Deployment plan: [`SECTOR_ROTATION_DEPLOYMENT_PLAN_2026-06-06.md`](../../SECTOR_ROTATION_DEPLOYMENT_PLAN_2026-06-06.md) (repo root)
- Backtest evidence: [`BACKTEST_ROUND26_ETF_COMBINED_REPORT_2026-06-06.md`](../../BACKTEST_ROUND26_ETF_COMBINED_REPORT_2026-06-06.md)
- Cumulative learnings: [`LEARNINGS.md`](LEARNINGS.md)
- Parameter history: [`VERSION_LOG.md`](VERSION_LOG.md)
- Canonical config record: [`config_snapshot.json`](config_snapshot.json)

## Not yet wired

- No scheduler job (manual CLI only).
- No live mode — `mode: scaffold-only`, `deployable: false`.
- FINNIFTY & NIFTYOILANDGAS are index proxies with no liquid ETF — excluded from
  the tradeable ETF universe until futures/basket execution is built.
- First live sandbox entry planned for **2026-06-15** (moved up from 2026-07-01)
  at ₹3L — operator-manual seed entry. See
  [`DEPLOYMENT_CHECKLIST_2026-06-15.md`](DEPLOYMENT_CHECKLIST_2026-06-15.md).
