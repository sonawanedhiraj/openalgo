# R81 — pre-registration of new-entry hypotheses (written 2026-11-15, BEFORE any test)

- **Issue:** #785.
- **Status: NOT RUN.** No hypothesis below has been computed on any data. They run only after Dheeraj approves this list (or an amended one). An amendment is a new commit, made before any result.
- **Survey and cost floors** that motivate the list: `docs/research/strategy/r81_new_entry/2026-11-15_r81_survey_cost_floors_preregistration.md`.

## Common protocol (applies to every hypothesis)

1. **Windows and sealed holdout.**
   - For stock, ETF and futures candidates that need tuning context: TRAIN 2010-01 → 2017-12, VALIDATE 2018-01 → 2021-12, **SEALED HOLDOUT 2022-01 → 2026-11**.
   - The holdout is not computed, plotted or summarised until every candidate's spec is frozen, by a commit after VALIDATE. Each candidate's own window is stated below.
2. **Execution timing.**
   - The signal uses closes up to and including the signal date.
   - The trade executes at the **close of the next trading day** (one-day lag; no same-close fills).
   - H4 is the exception: it is defined on close → next open.
3. **Costs.** Exact Zerodha charges as quoted by Kite's `/charges/orders` on 2026-11-15 (`data/cost_floors*.json`). Every year is priced at today's rates (the question is "does it work now").

   | Vehicle | Charges per round trip |
   |---|---|
   | Equity CNC | 22.25 bps + ₹15.34 DP per scrip per sell day |
   | Equity ETF CNC | 2.25 bps + ₹15.34 DP |
   | NIFTY futures | 5.94 bps (+ one extra round trip per roll) |
   | Stock futures | 6.4–6.6 bps |

4. **Slippage.** Explicit, per side.

   | Vehicle | Slippage per side |
   |---|---|
   | Large-cap stock CNC | 5 bps |
   | Tier-1 ETF (BANKBEES, ITBEES, NIFTYBEES, PVTBANIETF, AUTOBEES) | 5 bps |
   | Thin ETF (PSUBNKBEES, PHARMABEES, METALIETF, FMCGIETF, HEALTHIETF) | 15 bps |
   | NIFTY futures | 1 bp |

   A **zero-slippage upper bound** is reported beside every result.
5. **Statistics.**
   - Monthly (or nightly for H4) net returns, with a Newey-West t-stat (3 lags monthly, 5 nightly) and a block-bootstrap 95% CI (2,000 resamples, 3-month or 20-day blocks).
   - **Nothing with |t| < 2 is called a result.**
   - Six primary hypotheses, so a family-level CONFIRMED needs |t| ≥ 2.64 (Bonferroni 0.05/6) in the holdout. A result between 2 and 2.64 is reported as "significant alone, not family-wise".
6. **Regime rule.** The primary metric must have the same sign in TRAIN, VALIDATE and HOLDOUT (and in each sub-period listed for H1). A candidate that works in one period or one regime is reported as **noise**, whatever its t.
7. **Plateau.** The pre-declared neighbours listed per hypothesis must all have a holdout primary metric of the same sign. A lone peak fails.
8. **Placebo (R67 method).**
   - Portfolio candidates: at each rebalance, draw a random portfolio of the same size from the same eligible universe (2,000 draws), cost it identically, and report p = share of draws whose net result ≥ the candidate's.
   - H4 instead uses a 2,000-draw shuffle of the night/day labels within each month.
   - Placebo p must be < 0.05.
9. **Ledger.** Every variant run, every failure included, goes to `data/variant_ledger.csv`. Each row carries:
   - code hash and window;
   - n periods and n trades;
   - gross, net and every charge component;
   - the slippage assumption and the zero-slip result;
   - net mean and annualised return, Sharpe, NW t, CI;
   - max drawdown, turnover;
   - placebo p and verdict.
10. **Universe for stock candidates (H2, H3, H6): a DECISION FOR DHEERAJ.** The only point-in-time F&O list on disk is 2024-03-01.
    - **Option A (default):** stocks in that list with ≥ 252 prior daily bars at each rebalance. This is survivorship-biased: names that failed before 2024 are absent and names that later grew are present. The control is an equal-weight portfolio of the same eligible universe, so the bias largely cancels in the long-minus-universe spread but inflates absolute returns.
    - **Option B:** download NSE's public month-end F&O bhavcopy files (about 200 files) to build point-in-time membership. This needs approval because it is a download.
    - The choice is recorded by amendment before any run.
11. **Data.** Kite daily (stocks from 2000–2003, sector indices from 2000–2006, NIFTY continuous futures from 2011-01, ETFs from launch; see `data/data_depth.json`). Read-only; checkpointed to `data/`.

## The hypotheses

### H1 — Sector rotation, momentum + low-vol risk-parity (R26d), re-test of a FROZEN spec
- **Rationale.** Industry momentum: Moskowitz & Grinblatt (1999) find most of stock momentum is industry momentum, and it is slow-moving and cheap to trade. Low-volatility anomaly: Baker, Bradley & Wurgler (2011) attribute it to leverage constraints and benchmark-hugging. Both have NSE index implementations (NIFTY200 Momentum 30, NIFTY100 Low Volatility 30). R17 → R26 found Sharpe 0.92 / 1.04 / 1.11 / 1.17 on 2022-08 → 2026-06 and was never deployed.
- **Spec (copied from `outputs/backtest_round26_etf_combined_2026-06-06/run_etf_combined.py`, NOT re-tuned).**
  - Signals: momentum = top-3 by 126-day return; low-vol = bottom-3 by 60-day daily-return std.
  - Weights: inverse-vol weights over the union.
  - Cadence: rebalance the first trading day of each month, executed at the next day's close (stricter than R26's same-close fill).
  - Universe: the 11 R26 sectors as NSE sector indices (a sector enters when its index has 127 bars).
  - Benchmark: NIFTY 50 price index (price-only vs price-only).
- **Windows.** R26's 2022-08 → 2026-06 is USED and is not re-scored. Out of sample:
  - **(i) BACKWARD 2007-01 → 2022-07** on index proxies, with sub-periods 2007–2011, 2012–2016 and 2017 → 2022-07 for the regime rule.
  - **(ii) FORWARD 2026-06-05 → 2026-11-13** on actual ETF prices. This is about 5 months, so it is informational only.
  - There is no train window (nothing is fitted).
- **Primary metric.** Annualised net active return vs NIFTY over the backward window. Pass needs NW |t| ≥ 2, positive in all 3 sub-periods, and placebo p < 0.05.
- **Pre-declared components (reported, not separately selectable).** Momentum-only (R26b) and low-vol-only (R26a).
- **Plateau.** Momentum lookback 63 / 189 days; top-2 / top-4 per leg.
- **Costs.**
  - Index-proxy turnover is priced as Tier-1/thin ETFs per the table above, plus 0.25%/yr expense drag on ETF-traded weight.
  - Dividends are excluded on both sides (price indices), as in R24.

### H2 — Stock cross-sectional momentum (12-1), monthly, CNC
- **Rationale.** Jegadeesh & Titman (1993); persistent across markets (Asness, Moskowitz & Pedersen 2013). For India, the IIMA factor library (Agarwalla, Jacob & Varma) reports a positive WML premium; NSE runs momentum indices. The mechanism is under-reaction and slow information diffusion, which a monthly holding period exploits without fighting intraday costs.
- **Spec.**
  - Rank the eligible universe by return over t−252 → t−21 days. Long the top 20, equal-weight.
  - Rebalance monthly (signal at the month-end close, trade at the next close); CNC.
- **Primary metric.** Monthly net long-minus-universe-EW spread.
- **Plateau.** Lookback 126-21 / 189-21; top 15 / 30.
- **Windows.** TRAIN / VALIDATE / HOLDOUT as common.

### H3 — Stock low volatility, monthly, CNC
- **Rationale.** The low-volatility anomaly (Ang et al. 2006; Baker et al. 2011), documented in Indian equities by the IIMA library and by NSE low-vol indices. It is independent of H2's mechanism (constraint-driven mispricing rather than under-reaction).
- **Spec.** Rank by 252-day daily-return std; long the lowest 20, equal-weight; monthly; CNC.
- **Primary metric.** Net long-minus-universe-EW spread, plus a Sharpe comparison with the universe.
  - A low-vol book may earn LESS than the universe with a higher Sharpe.
  - Pre-declared: H3 passes on **risk-adjusted** terms (Sharpe difference, bootstrap CI excluding 0, plus the regime and placebo rules), not on raw spread.
- **Plateau.** Volatility window 126 / 504 days; quarterly rebalance.

### H4 — NIFTY overnight premium, unconditional, via futures (and NIFTYBEES)
- **Rationale.**
  - Overnight returns carry most of the equity premium in many markets (Cliff, Cooper & Gulen 2008; Lou, Polk & Skouras 2019).
  - For India, global-session information and foreign flows arrive overnight.
  - It is also a direct test of what futures_follow_cap50 earns: the registry already says its signal does not predict NIFTY direction, so is its return just this premium?
- **Spec.** Every trading day: buy 1 NIFTY front-month future at the close, sell at the next open. Nights across a roll date are excluded. Variant: NIFTYBEES ETF (Tier-1 slippage).
- **Primary metric.** Mean net return per night (NW t, 5 lags).
- **Cost bar.** About 7.9 bps per night (5.94 + 2 × 1). This is stated in advance: the premium must exceed about 20% a year gross to clear it.
- **Windows.** TRAIN 2011-01 → 2017-12 (the continuous series starts 2011), VALIDATE 2018 → 2021, HOLDOUT 2022 → 2026-11.
- **Plateau.** Not applicable (no parameters). The regime rule applies with extra force: the sign must hold in every calendar year with ≥ 75% of years positive.

### H5 — Volatility-managed overlay on H1 and H2
- **Rationale.** Volatility clusters but expected returns do not rise with it, so scaling exposure inversely to recent volatility raises Sharpe (Moreira & Muir 2017). It also specifically mitigates momentum crashes (Barroso & Santa-Clara 2015; Daniel & Moskowitz 2016).
- **Spec.**
  - Each month, set exposure = min(1, 12% / trailing-21-day annualised realised vol of the unscaled strategy). There is no leverage; the remainder earns 0.
  - The extra turnover is costed.
  - It is applied to H1 and H2 as specified. It is evaluated even if H1 or H2 fails, and is reported as conditional.
- **Primary metric.** Sharpe difference vs unscaled (bootstrap CI excluding 0) and a lower max drawdown, in VALIDATE and HOLDOUT.
- **Plateau.** Target vol 10% / 15%; window 63 days.

### H6 — 52-week-high proximity, monthly, CNC
- **Rationale.** George & Hwang (2004): investors anchor on the 52-week high, so news pushing a stock near it is under-reacted to. It subsumes much of 12-month momentum in US data and has replications in emerging markets. It is pre-registered as a distinct anchoring mechanism, and it is reported with its correlation to H2 so that two rewordings of one effect are not counted as two results.
- **Spec.** Rank by close / max(high over 252 days); long the top 20, equal-weight; monthly; CNC.
- **Primary metric.** Net long-minus-universe-EW spread.
- **Plateau.** 126-day high; top 15 / 30.

## Explicitly excluded (already rejected, in flight, or a ChartInk-breakout variant)

| Excluded | Registry rounds |
|---|---|
| ChartInk / in-house-scanner breakout and any variant | R1–R6, R56–R57, R61–R68, R76, R78–R80 |
| Intraday stock signals generally (eight rounds below their own cost floor) | — |
| Calendar effects: turn-of-month, pre-expiry | R22 |
| 200DMA timing | R23 |
| Single-name mean reversion | R15, R32 |
| Markov | R12 |
| Bull-flag | R13, R14 |
| Wyckoff swing | R7–R10 |
| Pairs / cointegration | R16, R33 |
| News momentum and news drift / PEAD | R43–R45 |
| Option buying as a vehicle | R29, R36, R55, R66 |
| Condors | R37, R38 |
| RSI/WMA crossovers | R72, R73 |
| Noise boundary | R77 |
| Short mirror of futures_follow | R42 |
| Promoter-buy (untradeable liquidity) | R46, R49 |
| Closing-auction reversal (in flight as cas_auction_reversal) | — |
| sector_follow / futures_follow (running) | — |
| An SUE/PEAD study in progress elsewhere (`backtest/news_event_study/analyze_sue_pead.py`) | — |

## Order of work after approval (cheapest and highest prior first)

1. **H1:** about 11 index + 9 ETF series, quick.
2. **H2, H3, H6:** one shared daily stock fetch of about 180 symbols, 2009 → 2026 (about 15 minutes at 3 req/s).
3. **H4:** NIFTY continuous futures + NIFTYBEES daily.
4. **H5:** last.

Checkpoint after each. The holdout is opened only after a commit freezing every spec.
