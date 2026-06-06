# Sector Rotation ETF — Version Log

Parameter and logic history. Newest first.

---

## 0.1.0 — 2026-06-06 — Scaffold

**Status:** scaffold-only, `deployable: false`. Signal computation + recommended
orders only. No scheduler, no live mode, no order placement.

### Parameters chosen

| Parameter | Value | Rationale |
|---|---|---|
| `momentum_lookback_days` | 126 | ~6 trading months. 6M chosen over 3M: 3M had higher in-sample return (+63.7%) but generalized poorly OOS (+3.2 pp); 6M was the stable performer in both walk-forward halves (Round 21b). |
| `lowvol_lookback_days` | 60 | Trailing 60-day realized vol; the low-vol diversifier window validated in Round 24 (Sharpe 1.04, 0.69 corr to momentum). |
| `momentum_top_n` | 3 | Top-3 strongest sectors by 6M return. Concentrated rotation that drove the +42.8 pp index alpha (Paths 17/20/21b). |
| `lowvol_bottom_n` | 3 | Bottom-3 calmest sectors by 60d vol. |
| `weight_method` | `risk_parity_inverse_vol` | Variant 26d — best Sharpe (1.17) and lowest MaxDD. Inverse-vol weighting between the two legs. 50/50 (26c) is a near-identical equal-weight fallback (Sharpe 1.16). |
| `rebalance_frequency` | `monthly_first_trading_day` | Monthly cadence validated across 47 rebalances. |
| `capital_inr` | 300000 | Start ₹2–3L (sandbox-first); scale to ₹10L target after 3 clean rebalances. |
| `max_position_inr` | 50000 | Structural cap ≈ 1/3 of a leg at ₹3L. No per-position stops — the monthly rebalance is the risk-recycling mechanism. |

### Within/between-leg allocation

- Within each leg: equal-weight (1/3 each).
- Between legs: inverse-vol weighting from each sleeve's own realized vol.
- Legs may overlap (a sector in both momentum and low-vol baskets) — overlapping
  positions are summed.

### Universe

9 tradeable NSE ETFs (BANKBEES, ITBEES, PSUBNKBEES, PHARMABEES, METALIETF,
PVTBANIETF, FMCGIETF, AUTOBEES, HEALTHIETF). The 2 index proxies (FINNIFTY,
NIFTYOILANDGAS) are recorded in config but excluded from the tradeable universe
until futures/basket execution is built.

### Backtest evidence

- `BACKTEST_ROUND26_ETF_COMBINED_REPORT_2026-06-06.md` — final ETF gate
  (Sharpe 1.17, CAGR 14.8%, +34.8 pp NIFTY alpha, MaxDD −16.9%).
- `SECTOR_ROTATION_DEPLOYMENT_PLAN_2026-06-06.md` — consolidated deployment plan.
- `outputs/backtest_round26_etf_combined_2026-06-06/` — scripts + results dump.
