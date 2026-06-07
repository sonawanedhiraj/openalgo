# Strategy Registry — Master Index

**This is the canonical record of every trading strategy tested in this project.**

When the user says "trading strategy" — including questions like "what should I try next", "how did X do", or "is Y worth testing" — Claude should READ THIS FILE FIRST. It contains: (1) the active deployable shortlist, (2) every strategy ever rejected with WHY, (3) currently-in-flight experiments, (4) the backlog of untested ideas, and (5) the standard testing protocol so new rounds are comparable.

This is a LIVING document. After every new backtest round, add an entry. After every weekly trading session, update active strategies' learnings.

## How to use this file

**Finding past work:** Search by strategy name (e.g. "bull flag") or round number ("round 13"). If a strategy was tried before, it will be in "Rejected" or "Active." If it is not in any list, it is a fresh idea — see "Backlog" for queued ideas, and consider testing.

**Adding a new round:** Copy the template from "## Entry Template" at the bottom. Fill it in. Add the report path. Save and commit.

**Updating an active strategy after a live session:** Open the strategy's own `strategies/<name>/LEARNINGS.md` and append daily/weekly observations there. Then update this registry's "Status / Latest Note" for that strategy.

A note on numbering: the backtest reports are numbered from Round 2 onward. There is no file named `BACKTEST_ROUND1`; the round-1-equivalent work is the initial F&O screener sweep captured in `BACKTEST_RESEARCH_REPORT_2026-06-05.md` (its cost model and harness are referenced by later reports as "round-1"). Two early experiments — the research sweep and the sector-movers study — predate the numbered series but are recorded here as full rows.

## Standard Testing Protocol

These parameters apply to ALL new strategy backtests unless overridden in the entry. They make rounds comparable.

| Parameter | Default |
|---|---|
| Capital | INR 10 lakh (1,000,000) |
| Per-trade risk | INR 10,000 (1% of capital) for stop-based; equal-weight 1/N for portfolio strategies |
| Cost model (delivery) | Zerodha 0% brokerage + 0.1% STT (sell) + ~0.002% exchange + 18% GST + 0.002% stamp duty + 0.10%/side slippage |
| Cost model (intraday) | Zerodha INR 20 brokerage/order + 0.025% STT (sell intraday) + 0.10%/side slippage stocks + 0.02%/side futures |
| Cost model (futures) | INR 20 brokerage + 0.0125% STT (sell) + ~0.002% exchange + GST + 0.002% stamp buy + 0.02%/side slippage |
| Cost model (ETF) | Delivery + 0.15%/side slippage + 0.25% annual expense ratio absorbed by NAV |
| Universe (Indian F&O) | ~200 F&O stocks from `historify.duckdb` |
| Universe (sector strategies) | 15 NSE sector indices (BANKNIFTY, FINNIFTY, IT, AUTO, PHARMA, METAL, FMCG, REALTY, MEDIA, ENERGY, PSUBANK, PVTBANK, OILANDGAS, HEALTHCARE, CONSRDURBL) |
| Universe (ETF deployment) | 11 sectors (9 ETFs: BANKBEES, ITBEES, PSUBNKBEES, PHARMABEES, METALIETF, PVTBANIETF, FMCGIETF, AUTOBEES, HEALTHIETF + FINNIFTY futures and NIFTYOILANDGAS top-5 basket proxies) |
| Benchmark | NIFTY 50 buy-and-hold over the same window |
| Min sample size | If trades < 30 OR test period < 6 months, flag verdict as "INSUFFICIENT EVIDENCE" |
| Hand-validation | At least 1 trade reconciled to-the-rupee manually before reporting positive |
| Reporting | Save report at repo root as `BACKTEST_ROUND<N>_<NAME>_<DATE>.md`, also copy to `outputs/backtest_round<N>_<name>_<date>/REPORT.md` |

## Active Deployable Strategies

### Sector Rotation ETF (Combined Momentum + Low-Vol)
- **Status:** SCAFFOLD COMPLETE — not yet live. First sandbox rebalance planned 2026-07-01.
- **Last validated:** 2026-06-06 (Round 26)
- **Spec:** 50% momentum (top-3 sectors by trailing 6M return) + 50% low-vol (bottom-3 by trailing 60d return vol), risk-parity inverse-vol weighting between legs, monthly rebalance on the first trading day. Long-only, unleveraged, at most 6 sectors held. Equal-weight within each leg, no per-position stops — the monthly rebalance is the risk-recycling mechanism.
- **Universe:** 11 sectors — 9 traded via NSE ETFs (BANKBEES, ITBEES, PSUBNKBEES, PHARMABEES, METALIETF, PVTBANIETF, FMCGIETF, AUTOBEES, HEALTHIETF) + 2 index proxies (FINNIFTY via futures, NIFTYOILANDGAS via RELIANCE/ONGC/IOC/BPCL/GAIL top-5 basket).
- **Backtest (Round 26, actual ETF NAVs, Aug 2022 -> Jun 2026, 47 rebalances):** variant 26d risk-parity Sharpe 1.17, CAGR 14.8%, total +69.8%, Max DD -16.9%, +34.8 pp total-return alpha vs NIFTY (NIFTY Sharpe 0.69, CAGR 8.1%, +35.0%). On index series (Round 21b/24) the same book reads Sharpe 1.11, MaxDD -17.0% — the ETF and index numbers agree within rebalance noise.
- **Why it works:** Sector momentum captures multi-month dispersion (4-year walk-forward index alpha +42.8 pp, barely degrading out of sample to +16.9 pp). The low-vol sleeve is a genuine diversifier — correlation 0.69 to momentum — and lost less than momentum in 2 of 3 known correction months (saved ~3-5 pp). The feared ETF "friction cliff" did not appear: ETF NAVs capture ~1.0-1.5%/yr in dividends that price-only indices exclude, offsetting expense ratio and slippage almost exactly.
- **Known weakness:** Not a hedge against a fast broad selloff — in the -11.3% March-2026 month even low-vol sectors fell hard and lagged NIFTY. The -17% max drawdown is accepted as the price of the alpha; there is no intra-month intervention.
- **Files:** `strategies/sector_rotation_etf/`, `services/sector_rotation_etf_service.py`, `services/sector_rotation_etf_cli.py`. Reports: `BACKTEST_ROUND21B_SECTOR_4YEAR_REPORT_2026-06-06.md`, `BACKTEST_ROUND24_LOW_VOL_REPORT_2026-06-06.md`, `BACKTEST_ROUND25_ETF_REPLICATION_REPORT_2026-06-06.md`, `BACKTEST_ROUND26_ETF_COMBINED_REPORT_2026-06-06.md`. Consolidated plan: `SECTOR_ROTATION_DEPLOYMENT_PLAN_2026-06-06.md`.
- **Latest LEARNINGS:** `strategies/sector_rotation_etf/LEARNINGS.md`

### Simplified Stock Engine (Chartink-driven Intraday)
- **Status:** LIVE in sandbox mode (orders flow to `sandbox.db`, virtual INR 1 Cr). Operational since 2026-Q2.
- **Spec:** Chartink screener-driven intraday breakout system — armed by webhook, fires market orders on 5-min candle breakouts, ATR-based stop loss, RR trailing. Independent mode flag `SIMPLIFIED_ENGINE_MODE`.
- **Universe:** F&O stocks matching the buy/sell Chartink screeners.
- **Why it exists:** Pre-existing operator strategy, integrated with the platform. Note: the standalone intraday breakout edge tested in the research sweep and Rounds 2-6 was structurally unprofitable on its own; the live engine is operated as an armed, screener-gated, sandbox-monitored system rather than a proven standalone alpha.
- **Files:** `services/simplified_stock_engine_service.py`, `services/simplified_stock_engine_core.py`, `strategies/simplified_engine/LEARNINGS.md`. Handoff: `docs/SIMPLIFIED_ENGINE_HANDOFF.md`.

## Currently Testing (In-Flight Experiments)

(none active)

## Rejected Strategies — Full Audit Trail

For each, the row records: round, name, period tested, key params, verdict, why. Sorted by round number. Negative and inconclusive results are kept permanently — re-doing rejected work wastes time.

Verdict legend: REJECT (no edge / loses) · INSUFFICIENT (sample too small to conclude) · PROMISING (signal but not deployable) · INFRA (data/diagnostic, not a strategy) · BLOCKED (cannot run as specified) · DEPLOY (cleared every gate — see Active).

| Round | Strategy | Period | Key Params | Verdict | Why |
|---|---|---|---|---|---|
| R1 (research sweep) | Intraday 5m engine param sweep on F&O | 2026-05-04 -> 05-29 (19d) | gap 1.5/2.0, ATR 1.0-2.5, cooldown 3/6, BUY/SELL; ~500/trade risk | REJECT | Every variant loses gross AND net; 100% of exits are stops/EOD, win~25%, avg win ~76 vs avg loss ~575 (4.5:1). Underperforms +1.05% buy-hold. |
| Sector-movers | Sector-aligned movers + low-vol-base breakout | May 2026 (19d) | Top-5 gainers up-sectors long / losers down-sectors short, 2x vol confirm, ATR2 trail | REJECT | Net -26,791; PF 0.28; costs 4.9x gross. Sector data degraded to NIFTY/BANKNIFTY 2-bucket proxy (fidelity gap). |
| 2 | Exact Chartink FnO rule on real 5m bars | 2026-01-16 -> 05-29 (89d) | The live 12-gate buy / 10-gate sell rule, validated 92% vs `_evaluate`; ATR2.5 stop, RR trail | REJECT | Removed the data-fidelity excuse; win rose 25%->49% but still loses. Avg win 118 vs avg loss 540 (4.5:1), PF 0.21. Structurally no edge. |
| 3 | Sector movers, fixed sizing + real sector indices | May 2026 (19d) | ATR-floor on stop, INR 2 min-risk filter, 18 real sector indices backfilled at 1m | REJECT | Net -13,702; PF 0.69; cost/gross improved to 1.6x but still loses. Real sectors helped fidelity, not profit. |
| 4 | Wyckoff no-supply/no-demand color filter | May 2026 (19d) | LONG needs RED low-vol candle, SHORT needs GREEN; else identical to R3 | REJECT | Cut 68->24 trades, halved loss to -6,891 — but the "improvement" is just trading less, not an edge. Abandon as a quality signal. |
| 5 | Regime sensitivity + doji-inclusive color filter | May + Mar 2026 | r3 (no filter) vs r5 (doji eligible), across chop and trend months | REJECT | Every cell loses; cost/gross 1.6-3.4x. Intraday Zerodha costs annihilate a razor-thin/absent gross edge. |
| 6 | Risk-amount invariance test (500 -> 10,000) | May + Mar 2026 | Only RISK_PER_TRADE changed, 20x larger | INFRA | Diagnostic: gross scaled ~20x, costs ~19.2-19.5x (brokerage capped at INR 20/side). Cost/gross essentially invariant to risk size — losing strategies stay losing at scale. |
| 7 | Wyckoff SWING (daily bars, 1-3 day hold) | 2026-01-01 -> 05-29 (99d) | Low-vol day + 2x vol confirm, 3% target, ATR/low stop, max 5 concurrent, delivery costs | PROMISING | Cost wall cleared (cost/gross 1.07 vs 1.56 intraday) but net -10,067, PF 0.98. Daily-bar class is the right direction; not yet profitable. |
| 8 | Wyckoff swing, longs-only + 4% target | 2026-01-01 -> 05-31 (99d) | R7 base, drop shorts, target 3%->4% | PROMISING | First net-positive: +43,121, PF 1.14, cost/gross 0.68. BUT entire profit is April (+141k); strip April -> -98k. Concentration risk, not stable edge. TUNE MORE. |
| 9 | Indicator-based long-only swing | 2026-01-01 -> 05-31 (99d) | EMA50>EMA200 trend, EMA20 pullback, MACD/RSI momentum, 1.2x vol, +4% target | REJECT | Negative gross (-6,825) — entries do not clear before costs. Net -47,881, PF 0.75. Classic indicators were a worse selector than the volume anomaly. |
| 10 | Wyckoff long + 3x volume + per-stock sector filter | 2026-01-01 -> 05-31 (99d) | R8 base, vol mult 2->3, real per-stock sector trend gate (163/212 mapped) | PROMISING | Net +36,361 (slightly below R8), but max DD halved -123k->-64k and Feb flipped +10k. Better risk profile, still April-dependent. No net gain. |
| 11 | Broad-market regime gate on Path 8 | 2026-01-01 -> 05-31 (99d) | NIFTY>EMA200 + EMA20>EMA50 + 5d-ret>0 + breadth>=50% GO/STOP gate | REJECT | Gate fires GO on only 5/99 days, all in warm-up -> 0 trades. Worse: all 86 profitable R8 trades occur on STOP days. The gate is anti-correlated with where money is made. |
| 12 | 3-state Markov-chain entry signal | Train 2025, test 2026-01-01 -> 05-31 | 224 valid per-stock transition models, E[r|state] > +0.2% entry, daily | REJECT | Net -213,323, PF 0.78. Model leans mean-reversion (122/224 stocks), fires 219 uniform trades; gross ~0 and costs (208,741) bury it. |
| 13 | Bull-flag pattern, intraday (Ross Cameron) | May 2026 (19d) | Pole >=1.0% on >=1.5x vol, flag 3-8 candles retrace <=50%, breakout entry, LONG only | REJECT | Win 9.5% (2W/19L), gross/net deeply negative. US small-cap pattern does not translate to Indian F&O large-caps intraday. |
| 14 | Bull-flag SWING (daily bars) | 2026-01-01 -> 05-31 (~100d) | Pole >=3% over 1-3 green days on 1.5x vol, flag retrace <=50%, target min(1x pole, +5%) | REJECT | Net -232,116, PF 0.40, win 35.2%. Negative gross (-170,722). Loses every month. Pattern has no swing edge here. |
| 15 | Mean-reversion fade (fade 3%+ moves) | May 2026 (19d) | SHORT up-extremes / LONG down-extremes after 2 rejection closes, 50% reversion target | REJECT | Net -843,101, win 22.1%. The opposite signal to breakout also fails -> F&O underlyings have no exploitable intraday directional edge net of costs. |
| 16 | NIFTY <-> BANKNIFTY cointegration pair | 2026-01-01 -> 05-31 (99d, 60d warm-up) | Walk-forward Engle-Granger, z-score entry, beta-hedged | REJECT | Cointegration broke: in-sample p=0.077 (already >0.05), rolling beta 0.029->0.373 (13x swing). No equilibrium to revert to; only 13 trades, 15.4% win. |
| 17 | Sector momentum rotation (top-3, 6M) | 2026-01-01 -> 05-29 (5 rebalances) | Top-3 of 15 NSE sector indices by trailing 6M return, monthly, equal-weight, long-only | PROMISING | First positive in 19 rounds: +2.58% vs NIFTY -9.94% (+12.5 pp), Sharpe +0.40 vs -1.41. Small sample (5 rebalances) — needed extended validation (R20/21b). |
| 18 | F&O ban-list event study | Jan-May 2026 | Trade entries/exits around NSE securities-in-ban list (98 day-files) | REJECT | Ban list extremely sparse in 2026-H1: avg 1.4 stocks/day. Too few events to build a tradable book. |
| 19 | Path 8 Wyckoff swing on index futures | (could not run) | Re-run R8 logic on NIFTY/BANKNIFTY futures for ~5x cheaper execution | BLOCKED | Index series in DuckDB have ZERO volume. R8's setup is 100% volume-gated (low-vol day, 2x confirm) -> strategy collapses to nothing without volume. |
| 20 | Sector rotation extended validation | 2025-07 -> 2026-05 (8-11 rebalances) | Lookback sweep (3M/6M/12M), top-3, monthly, equal-weight | PROMISING | Extends R17 out-of-sample over the added Jul-Dec 2025 stretch; confirms the sector-momentum edge persists. 12M lookback not testable (only 290 daily bars on disk pre-backfill). |
| 21a | Sector index historical backfill | (data infra) | Backfill NIFTY + 15 sectors 2022-01 -> 2025-03 via Zerodha API into `historify.duckdb` | INFRA | Extended the window from 14 months to 4.4 years (1095 daily bars each) for multi-regime validation. Auth gotcha: Auth table keys on username, not "zerodha". Enabler for R21b+. |
| 21b | Sector rotation 4-year validation | 2022-08 -> 2026-06 (47 rebalances) | Lookback sensitivity on 4y index data; 6M chosen over 3M/12M | DEPLOY (candidate) | 6M: Sharpe 0.92, +77.86% (+42.82 pp vs NIFTY), MaxDD -19.62%. Walk-forward alpha barely degrades OOS (+18.3->+16.9 pp). 6M picked for Sharpe-to-DD balance and OOS stability over 3M's peak in-sample return. Feeds Active. |
| 22 | Indian calendar effects (TOM + pre-expiry) | 2022-01 -> 2026-06 (4.42y) | A: turn-of-month ~5d hold; B: pre-expiry ~3d hold; C: union; NIFTY long | REJECT | TOM Sharpe 0.03 (t 0.06), Pre-Expiry Sharpe -0.46, Combined -0.21. All underperform NIFTY B&H (+32.9%). No exploitable calendar edge net of 0.30% round-trip. |
| 23 | 200DMA crash filter overlay | 2022-2026 | Gate sector rotation off when NIFTX < 200DMA (strict-dominance overlay) | REJECT | The naive 200DMA filter DROPPED Sharpe rather than improving it; strict dominance fails. Strategy has no fast downside gate — flagged for future VIX-spike work. |
| 24 | Low-vol diversifier + combined variants | 2022-2026 (index series) | 24a low-vol (bottom-3, 60d vol); 24c 50/50; 24d risk-parity inverse-vol | DEPLOY (candidate) | Low-vol Sharpe 1.04, corr 0.69 to momentum (genuine diversifier). Risk-parity 24d Sharpe 1.11, MaxDD -17.0% — best risk-adjusted on indices. Defensive vs momentum in 3 known crash months. Feeds Active. |
| 25 | ETF / basket replication and tracking error | 2022-2026 | Map 15 sectors to tradable ETFs/baskets; measure corr, beta, TE, ADV | INFRA | Established the deployable instrument set: 8 liquid Tier-1 ETFs (TE <=4.5%), 1 thin Tier-2 (HEALTHIETF), 2 index proxies (FINNIFTY futures, OILGAS basket); excluded MEDIA/ENERGY/REALTY/CONSRDURBL. TE 3-6%/sector. Enabler for R26. |
| 26 | ETF combined portfolio (4 variants), actual NAVs | 2022-08 -> 2026-06 (47 rebalances) | 26a low-vol, 26b momentum, 26c 50/50, 26d risk-parity — on real ETF NAV returns | DEPLOY | 26d Sharpe 1.17 / CAGR 14.8% / +69.8% (+34.8 pp vs NIFTY) / MaxDD -16.9%. Friction cliff did not materialize (dividend offset). Cleared the final gate -> promoted to Active (Sector Rotation ETF). |

### Per-round metric detail (the four ETF finalists, Round 26)

| Variant | Total | CAGR | Sharpe | Max DD | Trades |
|---|---:|---:|---:|---:|---:|
| 26a Low-vol | +55.2% | 12.1% | 1.03 | -17.6% | 43 |
| 26b Momentum | +91.0% | 18.3% | 1.07 | -18.9% | 36 |
| 26c 50/50 | +74.6% | 15.6% | 1.16 | -16.8% | 55 |
| **26d Risk-parity** | **+69.8%** | **14.8%** | **1.17** | **-16.9%** | 55 |
| NIFTY 50 B&H | +35.0% | 8.1% | 0.69 | -15.8% | — |

### Rounds where metrics could not be cleanly extracted

Honesty note — the following are recorded with verdict only or partial metrics because the source report did not state a complete metric set, or the run was infra/blocked rather than a scored backtest:
- **Round 6** — diagnostic (invariance test), no standalone strategy verdict; cost-ratio deltas only.
- **Round 18** — event study; reported the sparse-ban-list data finding (1.4/day) rather than a full PnL table.
- **Round 19** — BLOCKED before producing metrics (no index volume).
- **Round 20** — lookback-variant table is present in the report but full per-variant Sharpe/alpha values beyond the top-line were not transcribed here; treat R21b as the authoritative 4-year number.
- **Round 23** — reported as "dropped Sharpe vs the unfiltered baseline"; the exact filtered Sharpe value was not captured in the summary sources used to build this registry. Verify against `BACKTEST_ROUND23_CRASH_FILTER_REPORT_2026-06-06.md` before relying on a precise figure.

## Backlog — Ideas Not Yet Tested

Document strategies that came up during research but have not been backtested. When testing each, move it to "Currently Testing", then to "Rejected" or "Active."

- **Faster sector-rotation cadence (weekly vs monthly)** — Test whether daily/weekly inverse-vol reweighting captures more of the alpha than the current monthly rebalance. Higher-turnover cost trade-off. (Open question #1 from the deployment plan.)
- **VIX-spike crash filter** — Round 23 found the 200DMA monthly filter inadequate and the strategy has no fast downside gate. An intraday/weekly VIX trigger may catch fast drops the 200DMA misses.
- **Volatility-targeting overlay** — Cap combined-portfolio realized vol at a ~12% target, scaling down in high-vol regimes. Could smooth the equity curve and bound the tail without changing the edge.
- **Global / cross-asset sector rotation** — Extend the universe to global sector ETFs (Hang Seng, S&P sectors via INR-hedged instruments) for genuine diversification beyond a single market. Needs new data feeds.
- **Long-short sector pair** — Pure beta-hedged rotation (long winner sector, short loser sector). Round 16's NIFTY-BANKNIFTY pair broke on cointegration; stronger correlated sector pairs may hold.
- **Cross-asset momentum rotation** — Equity/bond/gold rotation by relative momentum. Needs bond and commodity series.
- **Earnings-drift trade** — Buy stocks 3 days post positive earnings surprise. Needs an earnings-calendar data source.
- **Options selling: covered calls on sector ETFs** — Would need IV history we do not currently have. Earliest workable after ~1 year of IV data collection.
- **Re-include NIFTYREALTY / NIFTYCONSRDURBL** — Excluded from the deployable 11 on tracking-error grounds; reconsider only if a re-backtest clears Sharpe >= 0.8 with them included.

## Entry Template (Copy-Paste When Adding a Round)

When adding a new round, copy this block into the "Rejected" table (or "Active" if it works):

```
| Round | Strategy name | Period | Lookback/Universe/Params | Verdict (REJECT/PROMISING/DEPLOY/BLOCKED/INSUFFICIENT/INFRA) | One-line why |
```

And if the strategy has its own ongoing learnings, create `strategies/<name>/LEARNINGS.md` with the standard scaffold (cumulative knowledge + dated entries), plus `VERSION_LOG.md` and `config_snapshot.json` per the strategy-architecture convention.

## Update Conventions

- **After every backtest round:** Add a row to "Rejected" or move it to "Active." Commit with message `docs(strategy-registry): add round <N>`.
- **After every live trading session:** Update the active strategy's own `LEARNINGS.md` with the day's observations. If an observation overturns a registry conclusion, update this file too.
- **Quarterly review:** Re-read "Backlog" and decide which ideas to promote to "Currently Testing." Re-read "Active" Sharpe/DD to detect strategy decay.
- **Never delete a rejected entry.** Negative results matter — they stop us re-doing failed work.

## Files Referenced

| Report / Doc | Path |
|---|---|
| R1 research sweep (intraday engine) | `BACKTEST_RESEARCH_REPORT_2026-06-05.md` |
| Sector-aligned movers study | `BACKTEST_SECTOR_MOVERS_REPORT_2026-06-05.md` |
| Round 2 — exact Chartink rule on 5m | `BACKTEST_ROUND2_REPORT_2026-06-05.md` |
| Round 3 — sector movers sized + real sectors | `BACKTEST_ROUND3_REPORT_2026-06-05.md` |
| Round 4 — Wyckoff color filter | `BACKTEST_ROUND4_REPORT_2026-06-05.md` |
| Round 5 — regime + doji filter | `BACKTEST_ROUND5_REPORT_2026-06-05.md` |
| Round 6 — risk-amount invariance | `BACKTEST_ROUND6_REPORT_2026-06-05.md` |
| Round 7 — Wyckoff swing | `BACKTEST_ROUND7_SWING_REPORT_2026-06-05.md` |
| Round 8 — swing longs-only +4% | `BACKTEST_ROUND8_LONG_REPORT_2026-06-05.md` |
| Round 9 — indicator swing | `BACKTEST_ROUND9_INDICATORS_REPORT_2026-06-05.md` |
| Round 10 — Wyckoff 3x vol + sector | `BACKTEST_ROUND10_REPORT_2026-06-05.md` |
| Round 11 — regime gate | `BACKTEST_ROUND11_REPORT_2026-06-05.md` |
| Round 12 — Markov chain | `BACKTEST_ROUND12_MARKOV_REPORT_2026-06-05.md` |
| Round 13 — bull flag intraday | `BACKTEST_ROUND13_BULLFLAG_REPORT_2026-06-05.md` |
| Round 14 — bull flag swing | `BACKTEST_ROUND14_BULLFLAG_SWING_REPORT_2026-06-05.md` |
| Round 15 — mean-reversion fade | `BACKTEST_ROUND15_MEANREV_REPORT_2026-06-05.md` |
| Round 16 — NIFTY/BANKNIFTY pair | `BACKTEST_ROUND16_PAIR_REPORT_2026-06-06.md` |
| Round 17 — sector momentum rotation | `BACKTEST_ROUND17_SECTOR_ROTATION_REPORT_2026-06-06.md` |
| Round 18 — F&O ban-list | `BACKTEST_ROUND18_BANLIST_REPORT_2026-06-06.md` |
| Round 19 — index-futures Wyckoff (blocked) | `BACKTEST_ROUND19_INDEX_WYCKOFF_REPORT_2026-06-06.md` |
| Round 20 — sector rotation extended | `BACKTEST_ROUND20_SECTOR_EXTENDED_REPORT_2026-06-06.md` |
| Path 21a — sector backfill | `BACKTEST_PATH21A_BACKFILL_REPORT_2026-06-06.md` |
| Round 21b — sector rotation 4-year | `BACKTEST_ROUND21B_SECTOR_4YEAR_REPORT_2026-06-06.md` |
| Round 22 — calendar effects | `BACKTEST_ROUND22_CALENDAR_REPORT_2026-06-06.md` |
| Round 23 — 200DMA crash filter | `BACKTEST_ROUND23_CRASH_FILTER_REPORT_2026-06-06.md` |
| Round 24 — low-vol diversifier | `BACKTEST_ROUND24_LOW_VOL_REPORT_2026-06-06.md` |
| Round 25 — ETF replication | `BACKTEST_ROUND25_ETF_REPLICATION_REPORT_2026-06-06.md` |
| Round 26 — ETF combined (final gate) | `BACKTEST_ROUND26_ETF_COMBINED_REPORT_2026-06-06.md` |
| Consolidated deployment plan | `SECTOR_ROTATION_DEPLOYMENT_PLAN_2026-06-06.md` |
| Quick-reference summary table | `STRATEGY_SUMMARY_TABLE_2026-06-06.md` |
