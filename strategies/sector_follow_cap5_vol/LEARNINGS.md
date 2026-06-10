# Sector Follow (Cap-5, Volume-Tiebreaker) — Learnings

Cumulative knowledge for the `sector_follow_cap5_vol` strategy. Read this before
making any decision. Most-important file in the strategy folder.

## Validated facts (from backtest research, pre-deployment)

_(populate as Phase 0.5 / shadow-replay / sandbox produce evidence)_

## Implementation notes (scaffold)

_(populate as the service module is built)_

## Live Learnings

### 2026-06-10 — Strategy spawned from R40 winner V_SF_CAP5_VOL

Strategy spawned from R40 winner `V_SF_CAP5_VOL`. Carrying the R40 backtest as
the truth source for parity checks (Sharpe 2.37 daily / 1.92 monthly, payoff
1.39, EV +0.63%/trade, MaxDD −8.76%, 434 trades over 2.4 yr, 2026-YTD +12.9%,
max concurrent positions = 5). Operator decisions locked the same day — see
[`PLAN.md`](PLAN.md) "Operator decisions". Phase 0 starting next.

### 2026-06-10 — Phase 0.5 decision: universe LOCK_STATIC_30 (re-rank loses)

Resolves operator decision #2 (universe re-rank was **conditional on Phase 0.5
showing re-rank ≥ static**). It does not. **Universe = static top-30, locked.**

A/B on the R40 cap5_vol harness using the **complete Phase-0 sector map** (19/30
mapped to a real sectoral index vs R40's 14), 2024-01→2026-06, identical
entry/exit/cap/cost. Static = top-30 by full-window traded value, locked.
Rerank = top-30 by trailing-90d traded value, recomputed 1st-of-month from the
147-symbol liquid pool.

| Metric | STATIC30 | RERANK_M |
|---|---|---|
| N trades | 625 | 622 |
| Win rate | 56.3% | 57.2% |
| Payoff | 1.44 | 1.13 |
| EV/trade | 0.454% | 0.329% |
| Sharpe (daily) | 2.19 | 1.20 |
| Sharpe (monthly) | 2.49 | 1.50 |
| Sortino (monthly) | 9.01 | 1.21 |
| Max DD (daily) | −8.8% | −15.2% |
| Calmar | 2.79 | 1.09 |
| Green months | 83.3% | 76.7% |
| 2026 YTD | +5.96% | +6.20% |

**Decision: LOCK_STATIC_30.** Static dominates on every risk-adjusted metric
(Sharpe(d) +0.99, Sortino 9.0 vs 1.2, Calmar 2.8 vs 1.1, half the drawdown,
green-months 83 vs 77%). Re-rank only nudges win-rate (+0.9pp) and YTD (+0.24pp),
both swamped. Fails both promotion gates (needed ΔSharpe ≥ +0.30 & ΔEV ≥ +0.10pp;
got −0.99 & −0.124). Monthly re-rank churns ~3.3/30 names/month (11% turnover, 72
distinct stocks over the window) toward recently-liquid momentum names with a
weaker sector-follow edge — complexity that actively hurts. Not borderline; no
operator confirmation needed.

**Sector-map note:** completing the map (RELIANCE→OILANDGAS, INFY/TCS→IT,
M&M/MARUTI→AUTO, AXISBANK/INDUSINDBK→PVTBANK, BSE/BAJFINANCE/JIOFIN→FINNIFTY,
DIXON→CONSRDURBL; JIOFIN re-tagged off PVTBANK) raised candidate trades 434→625 and
moved Sharpe 2.37→2.19 vs the R40 partial map. More real sector signals fire more
often; 2.19 is the honest baseline (R40's 16 NIFTY-defaults made half the "sector
signal" a market-day signal). 11 names still default to broad NIFTY — no
representative index in our data (telecom, defence-PSU, aviation, retail, infra).
See [`sector_map.json`](sector_map.json). Artifacts:
`outputs/sector_follow_cap5_vol_phase05_2026-06-10/`.
