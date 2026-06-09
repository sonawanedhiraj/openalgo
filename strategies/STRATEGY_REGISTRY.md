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

## Cross-Cutting Findings

### Long-gross-positive as a viability screen for intraday strategies (2026-06-07)

After 28 rejected intraday rounds + the R28b breakdown analysis:

Across the 10 intraday rounds with trade-level CSVs, longs beat shorts gross 7 of 10 times — but the actionable discriminant is NOT long-vs-short. It is whether the **long book has positive gross-before-costs**. Where long gross is positive, stripping shorts + correcting slippage to the platform's 0.035%/side default flips net green; where it is negative (R1, R2, R13 bull-flag, R15 mean-reversion, R28 v1), nothing rescues the round.

**Re-examine under a "long-only + score-positive" lens:**
- R3, R4, R5 (sector-engine variants — small samples, ~10-13 trades).
- R28b v2 Sneaky Pivot (73-trade long book, +24k gross → +5k net at 0.035% slip, tightening to ~+2-3k net after this PR's brokerage cap fix).

**Confirmed dead — long gross already negative:** R1, R2, R13, R15, R28 v1.

**Caveats:**
1. The per-leg brokerage cap fix in PR #2 adds ~25% cost on mid-size intraday trades; estimates above use the corrected formula.
2. None of the re-examination candidates clear the deployable bar — they are statistical noise (+2 to +7k over 5 months). This is a screening rule, not a strategy.
3. Slippage remains unverified; `ltp_at_signal` capture (this PR) enables validation once ~10 real trades accumulate.

**Action for future intraday rounds:** report BOTH long-only and short-only gross/net in the trade summary so the long-gross sign is immediately visible.

### Options leverage-rescue does NOT save low-WR *intraday* strategies (2026-06-07)

Tested the operator's hypothesis that a low-win-rate equity book (R29 v2: OOS WR 34%, payoff 1.10, net -45k) could be rescued by trading **ATM calls** instead of stock — the idea being that options' asymmetric payoff (capped premium loss, open upside via delta/gamma) makes low WR survivable if payoff clears ~2.33.

**It made things worse.** Replaying all 90 signals as intraday ATM monthly calls (entry 9:45, exit 15:15, same day), priced from real NSE bhavcopy daily open/settle anchors with Black-Scholes intraday interpolation (87/90 mapped, calibration clean, median IV 30%, open-vs-settle IV spread 2%), the options book posts **OOS payoff 0.95 — below the 2.33 needed and below equity's own 1.10** — and is net negative at 10/25/50 bps (−97.6k/−102.2k/−109.8k). WR 32.8%, Sharpe −1.49.

**Why:** an intraday ATM call (~0.5 delta) held ~5.5h captures only ~half of an already-small underlying move while paying full time premium and option-specific costs (STT on sell premium, slippage on premium). Convexity needs a *large* move to bend the payoff above linear; intraday wins here are ~0.3–0.5%. So the option just tracks spot at higher cost.

**Rule:** options leverage cannot rescue an *intraday* signal with no gross edge — there is no time for gamma to work. **It CAN partially rescue a multi-day swing with real gross edge — but fragile.** Same test on **R8** (longs-only +4% swing, equity payoff 1.51, net +43k) replayed as ATM monthly calls (85/86 mapped): under faithful intraday execution the BS model lifts payoff 1.51→2.30 and net to +205k @25bps → **PROMISING** (just shy of the 2.33 DEPLOY bar). BUT a both-legs-real-EOD-settle cross-check collapses the hold≥1 edge to ≈₹0 — the result hinges on capturing premium at the *exact* intraday target instant plus constant-IV; theta/IV-path over a multi-day hold eats it otherwise. So the swing-vs-intraday distinction is the real finding (convexity needs a big move), but R8 options is execution-fragile, not deployable — needs ~10 real paper fills to measure intraday-exit slippage + IV crush before trusting the magnitude.

New reusable asset: `fo_bhavcopy_eod` in `historify.duckdb` (4.7M expired-option EOD rows; 30-symbol R29 universe over 2024-01→2025-11 + 2026-01→05, plus all-symbol coverage on R8's 55 dates) recovers expired-contract daily prices that Kite's master cache purges. Pipeline: `outputs/r29v2_options_hybrid_2026-06-07/`.

### The monthly-consistency wall is signal FREQUENCY, not signal QUALITY (R30 -> R34, 2026-06-08)

Across R30–R34 every *single-signal* strategy carrying a real edge fails the strict monthly cash-flow bar for the same structural reason: edge-bearing Indian F&O signals are **lumpy by construction** (top-2/3 sectors, pyramided winners, episodic reversions). Widening the universe 4.7x (R31) did not raise trade frequency. Market-neutralizing (R33) stripped out the directional sector edge that made the signal worth trading. Mean-reversion (R32) has no structural single-name edge to begin with. The *only* thing that cleared the strict bar (R34 V_BLD_B, Sharpe 1.41 / 70% green) was **portfolio-layer blending of 4 uncorrelated lumpy sleeves** — monthly consistency is manufactured at the portfolio layer, not discovered in a better single signal. Next lever is portfolio-layer construction, not another signal hunt.

### Synthetic Black-Scholes pricing is systematically OPTIMISTIC for option BUYING (R36-real, 2026-06-09)

Real-data Sharpe was 0.4-1.1 worse than synthetic BS-pricing on every variant. Synthetic used IV=RV×1.10; real market IV runs above realized vol because of the implied-volatility floor, and buyers pay it. **Implication: any prior round that used synthetic BS pricing for option BUYING (R8 PROMISING, R29 v2, R35) needs an "optimistic-pricing" caveat until re-tested with real bhavcopy.** Premium SELLING (R37/R38) is unaffected — those tests used real bhavcopy throughout. Backfilled asset: `index_options_eod` in `historify.duckdb` (1.64M rows, NIFTY+BANKNIFTY, 2022-01 -> 2026-06).

### Premium selling edge exists at trade level but dies at portfolio level due to entry-gate co-occurrence (R37, 2026-06-09)

Iron condors with disciplined exits and tight strike selection produce real trade-level edge (un-breached condors hit 50% profit consistently). But the natural conjunction of entry conditions ("vix high AND range-bound AND fresh DTE window") fires far less often than the binding-evidence threshold requires (N=4 / 23 months). Weeklies / continuous-DTE / looser regime could fix it but become different strategies. SPAN margin (~5-15× max-loss) caps real-capital deployment regardless.

### Delta-roll adjustments make defined-risk condors strictly worse, not nicer (R38, 2026-06-09)

The textbook "roll the untested side when tested-delta hits 0.30" discipline manufactures a fresh directional breach risk (re-centered untested side, no longer delta-neutral). In whipsaw or vol-expansion, both sides breach → double-loss tail manufactured. Cosmetic-to-harmful on gap days specifically (0 of 13 gap-day prevents in audit). Net credit added by rolls is positive everywhere yet realized P&L falls.

### Scanner rule validation must verify against live .env, not code defaults (2026-06-09)

Historical validation of the `fno_intraday_buy_chartink` rule used the 3.0% code default for the gap-up gate; the actual `.env` value is 1.5% (`CHARTINK_RULE_BUY_GAP_PCT=1.5`). Diagnosis was wrong on a real axis. Same lesson is now codified in [`docs/PARAMETER_LOG.md`](../docs/PARAMETER_LOG.md). When a task evaluates a rule with parameterized thresholds, always read the env override before walking through the rule logic.

### Scanner rule diagnosis — g13 5m-volume gate is the dominant filter (2026-06-09)

Of the 12 gates in `fno_intraday_buy_chartink`, gate 13 (`5m vol > 2 × SMA(5m vol, 10)`) is the single biggest killer. TCS on 2026-06-02 passed 11 of 12 gates (gap +6.5%, RSI 76.8, all daily vol/trend) but failed g13 (5m vol 206k vs required 402k). Plus the gap gates (g1/g9/g10) use a COMPLETED daily candle while Chartink fires on LIVE intraday price — three symbols (NATIONALUM −5.2%, SAIL −3.2%, TRENT −0.03%) closed red daily but were green intraday at Chartink alert time (two real bugs in the transcription). Historical Jaccard 0.062 against Chartink's actual alerts confirms the rule is too tight on the wrong axes. Report: `outputs/screener_historical_validation_2026-06-09/REPORT.md`.

## Active Deployable Strategies

### Sector Rotation ETF (Combined Momentum + Low-Vol)
- **Status:** SCAFFOLD COMPLETE — not yet live. Live seed entry moved up to 2026-06-15 (operator-manual, per `strategies/sector_rotation_etf/DEPLOYMENT_CHECKLIST_2026-06-15.md`); first sandbox rebalance still planned 2026-07-01.
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

### Deploy candidates queued (not yet live)
- **V_BLD_B portfolio blend (R34)** remains DEPLOY_CANDIDATE — slated for sandbox paper-trade starting 2026-07-01 (Sharpe 1.41, 70% green months; promote to paper, not live). Report: `BACKTEST_R34_PORTFOLIO_BLEND_REPORT_2026-06-08.md`.
- **Sector Rotation ETF** live seed entry 2026-06-15 per `strategies/sector_rotation_etf/DEPLOYMENT_CHECKLIST_2026-06-15.md` (operator-manual, see Active entry above).

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
| 29 | Long-only + positive-sector-momentum gate (in-sample) | 2026-01 -> 05-29 (5 mo) | R8-style long setups gated to sectors with positive 5d return; ATR stop, RR trail; intraday | INSUFFICIENT | In-sample only: 20 trades, WR 60%, payoff 0.85, gross +4.8k but net ~breakeven (+0.9k @1.88bps -> -5.6k @10bps). 20 trades / 5 mo is below the 30-trade/6-mo bar. Required true OOS -> R29 v2. |
| 29 v2 | Same gate, true 22-month OOS hold-out | 2024-01 -> 2025-11 (22 mo) | Identical gate/params replayed over 2024-25 hold-out, 70 trades | REJECT | OOS WR 34.3%, payoff 1.10, gross -31.8k, net -45.4k @1.88bps / -68.0k @10bps, Sharpe -1.53, MaxDD -8.57%. The in-sample edge did not generalize. |
| 29 v2 Opt | Leverage-rescue: replay 90 signals as intraday ATM monthly calls | 2024-01 -> 2026-05 (in-sample + OOS) | bhavcopy daily anchors + Black-Scholes intraday interpolation; ATM CE, entry 9:45 exit 15:15 same day; 10/25/50 bps slip on premium | REJECT | Options WORSE than equity, not a rescue: OOS 67 trades, WR 32.8%, **payoff 0.95** (vs 2.33 required), net -97.6k/-102.2k/-109.8k @10/25/50 bps, Sharpe -1.49, edge ratio 0.47. Intraday holding gives gamma/convexity no time to work; an ATM call just tracks spot at higher cost. 96.7% signal-map rate; calibration clean (87/87, median IV 30%, open-vs-settle IV spread 2%). Report: `BACKTEST_R29V2_OPTIONS_HYBRID_REPORT_2026-06-07.md`. |
| 8 Opt | Leverage-rescue on R8 swing (longs +4%) as ATM monthly calls | 2026-01 -> 05 (in-sample) | Same bhavcopy+BS method; multi-day hold (0-3d); buy at signal spot, sell at target/stop spot; 10/25/50 bps slip | PROMISING (fragile) | Rescue partially works on swing (unlike intraday R29): 85/86 mapped, BS payoff 1.51->2.30, net +211k/+205k/+196k @10/25/50 bps. BUT a both-legs-real-EOD-settle cross-check collapses the hold>=1 edge to ~0 — hinges on exact intraday-target execution + constant IV; theta/IV-path eats multi-day holds. Not deployable; needs ~10 real paper fills. Report appendix: `BACKTEST_R29V2_OPTIONS_HYBRID_REPORT_2026-06-07.md`. |
| 31 | V9-F sector swing + pyramiding on widened 141-stock universe | OOS hold-out | 6 variants; Supertrend sector swing + pyramiding, universe widened 30 -> 141 stocks | REJECT | All 6 variants reject. Pyramiding is the *entire* edge; cutting losers HURTS returns. Widening the universe 4.7x did NOT fix the breadth/frequency wall (~same ~1.9 trades/mo, edge still only in top-2/3 sectors). Best V9-F: ~110k OOS, 26% green months, payoff 4.34 — lumpy, fails strict monthly bar. Report: `BACKTEST_R31_V9_EXPANDED_REPORT_2026-06-08.md`. |
| 32 | Single-name mean reversion (Connors / Bollinger / sector RW) | OOS hold-out | 6 MR variants on Indian F&O large-caps | REJECT | All 6 reject; WR never crossed 50% (sub-50, not the hoped 55-70%). STT is not the killer; any apparent "winner" is concentrated momentum-dip-buying in defence/infra, not a reversion edge. No structural single-name mean-reversion edge in Indian large caps. Report: `BACKTEST_R32_MEAN_REVERSION_REPORT_2026-06-08.md`. |
| 33 | Index reversion + market-neutral pairs + options income overlay | OOS hold-out | 6 variants: NIFTY Connors reversion, beta-hedged sector pairs, covered-call overlay | REJECT/MARGINAL | Market-neutrality KILLS the sector edge (V_PAIR_A perfectly neutral, beta -0.01, but zero alpha); covered-call overlay DEGRADES rotation by capping winners; NIFTY Connors V_IDX_A best monthly Sharpe (1.45) but tiny N. bhavcopy has NO index/ETF options to extend the overlay. Report: `BACKTEST_R33_INDEX_PAIRS_OPTIONS_REPORT_2026-06-08.md`. |
| 34 | Portfolio blend of 4 validated uncorrelated components | 2024-01 -> 2025-11 (23 mo OOS) | 6 blend variants; V_BLD_B = inverse-vol blend of 4 sleeves (pairwise corr -0.01) | DEPLOY (candidate) | **V_BLD_B clears the strict bar — first since R26**: Sharpe 1.41, 70% green months, payoff 1.67, MaxDD -3.2%. Mechanism: blend 4 uncorrelated *lumpy* sleeves. Caveats: 23-mo window luck (dodges Sleeve A's -7% DD), vol-scalability assumption on small-N sleeves, low-corr partly a zero-padding artifact. Promote to PAPER-TRADE, not live. Report: `BACKTEST_R34_PORTFOLIO_BLEND_REPORT_2026-06-08.md`. |
| 35 | Ankit Chaudhary BB Squeeze + highest-OI strike options (synthetic BS pricing) | OOS hold-out (27 dense F&O names) | 6 variants; BB squeeze entry, buy ATM vs highest-OI strike, synthetic BS premiums | REJECT | All 6 reject. Best V_ABCO_E (ATM control): Sharpe 0.76, 50% green, +₹427k OOS — but only 1 of 8 quarters carries it; lopsided. **The OI thesis is REFUTED:** ATM beats highest-OI strike on every metric (V_ABCO_E vs V_ABCO_A, same 558 trades, different strike → no order-flow/squeeze alpha). Data caveat: synthetic BS pricing was systematically optimistic for buying — see R36-real. Report: `BACKTEST_R35_BB_SQUEEZE_OI_OPTIONS_REPORT_2026-06-08.md`. |
| 36 | Monthly directional index option buying (synthetic BS pricing) | OOS hold-out | 6 variants; NIFTY/BANKNIFTY monthly CE/PE, IV proxied by RV × 1.10, ₹5L cap + 1.5%/35% rule | REJECT | All 6 reject; Sharpe −0.91 to +0.59. Premiums 100% synthetic BS (`fo_bhavcopy_eod` had no index options); SENSEX dropped (no data). Tightening filters HURT (V_MIOB_D, N=4); loosening flips Sharpe negative. ITM vs ATM a wash on Sharpe, capital-efficiency favors ATM; partial booking caps the convex tail; ₹5L+rule lets only 1 of 8 entries fit. **All findings reopened in R36-real after backfilling actual NSE bhavcopy.** Report: `BACKTEST_R36_MONTHLY_INDEX_OPTIONS_REPORT_2026-06-08.md`. |
| 36-real | Monthly index option buying with REAL NSE bhavcopy premiums | 2022-01-03 -> 2026-06-04 | Same 6 variants re-priced on backfilled `index_options_eod` (1.64M rows: NIFTY 918,895 + BANKNIFTY 718,604) | REJECT | All 6 still reject, decisively worse. **Headline: real premiums are uniformly 0.4-1.1 Sharpe worse than synthetic on every variant.** Mechanism: synthetic IV=RV×1.10=9.57% priced contracts ~38% cheaper than real market IV=15.5%; the IV floor above realized vol is structural, buyers pay it. Variants B/E/F flipped positive→negative; ITM vs ATM still a wash. Hand-validated to the rupee (NIFTY 2024-02-27 22100CE ₹513.30→₹250.50, net −₹17,285). **Downstream: R8's PROMISING (synthetic BS) needs an optimistic-pricing caveat.** Report: `BACKTEST_R36_REAL_MONTHLY_INDEX_OPTIONS_REPORT_2026-06-08.md`. |
| 37 | Defined-risk monthly index premium selling (iron condors) | 23 OOS months | 6 variants; NIFTY+BANKNIFTY iron condors, VIX≥60th-pct gate, 0.15-0.30Δ shorts, 50% TP / 2× credit SL / DTE≤8 time stop; real bhavcopy | REJECT | All 6 reject. Best V_SELL_B/F (identical): Sharpe 0.25, WR 50%, **N=4 over 23 mo, only 8.7% green months**. **Binding constraint is FREQUENCY, not cost/pricing:** gate funnel (NIFTY 476d) vix≥60 →142 → range-bound →49 → 25-30 DTE window →9 → 6 positions; "high vol-pct AND range-bound AND ~6-day/mo DTE window" structurally don't co-occur. Trade-level edge IS real (un-breached condors hit 50% TP consistently; election vol-crush trade +₹6,816 hand-validated). Would need weeklies/continuous-DTE/looser regime (a different strategy); SPAN margin ~5-15× max-loss caps real-capital deployment. Report: `BACKTEST_R37_PREMIUM_SELLING_REPORT_2026-06-09.md`. |
| 38 | Delta-roll adjustment module for R37 condors | 23 OOS months | 6 ON variants vs OFF; roll the untested side at 0.30Δ; real bhavcopy | REJECT | All 6 ON variants reject. **OFF (roll disabled) reproduces R37 bit-for-bit** (harness fidelity). Every ON variant LOSES Sharpe and return vs OFF and turns negative at 60bps where OFF was positive. **Counterintuitive: the roll makes it strictly worse.** `credit_added − roll_cost` is net-positive everywhere (E: +₹33k) but realized P&L FALLS — rolling re-centers the untested side closer to spot and assumes fresh directional breach risk; in whipsaw/vol-expansion both sides breach → double-loss tail. **Gap-day audit: roll prevented a gap loss in 0 of 13 cases.** N(ON)=N(OFF) for all 6 → cadence starvation intact. Report: `BACKTEST_R38_DELTA_ROLL_REPORT_2026-06-09.md`. |

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
| Round 29 — long-only sector gate (in-sample) | `BACKTEST_ROUND29_LONG_ONLY_SECTOR_GATE_REPORT_2026-06-07.md` |
| Round 29 v2 — long-only sector gate (OOS) | `BACKTEST_ROUND29V2_LONG_ONLY_SECTOR_GATE_OOS_REPORT_2026-06-07.md` |
| Round 29 v2 Options Hybrid — bhavcopy+BS leverage-rescue test | `BACKTEST_R29V2_OPTIONS_HYBRID_REPORT_2026-06-07.md` |
| R31 — V9-F sector swing + pyramiding, widened universe | `BACKTEST_R31_V9_EXPANDED_REPORT_2026-06-08.md` |
| R32 — single-name mean reversion | `BACKTEST_R32_MEAN_REVERSION_REPORT_2026-06-08.md` |
| R33 — index reversion + market-neutral pairs + options income | `BACKTEST_R33_INDEX_PAIRS_OPTIONS_REPORT_2026-06-08.md` |
| R34 — portfolio blend (V_BLD_B deploy candidate) | `BACKTEST_R34_PORTFOLIO_BLEND_REPORT_2026-06-08.md` |
| R35 — BB squeeze + highest-OI strike options | `BACKTEST_R35_BB_SQUEEZE_OI_OPTIONS_REPORT_2026-06-08.md` |
| R36 — monthly index option buying (synthetic BS) | `BACKTEST_R36_MONTHLY_INDEX_OPTIONS_REPORT_2026-06-08.md` |
| R36-real — monthly index option buying (real bhavcopy) | `BACKTEST_R36_REAL_MONTHLY_INDEX_OPTIONS_REPORT_2026-06-08.md` |
| R37 — defined-risk premium selling (iron condors) | `BACKTEST_R37_PREMIUM_SELLING_REPORT_2026-06-09.md` |
| R38 — delta-roll adjustment module | `BACKTEST_R38_DELTA_ROLL_REPORT_2026-06-09.md` |
| Screener historical validation (chartink rule) | `outputs/screener_historical_validation_2026-06-09/REPORT.md` |
| Consolidated deployment plan | `SECTOR_ROTATION_DEPLOYMENT_PLAN_2026-06-06.md` |
| Quick-reference summary table | `STRATEGY_SUMMARY_TABLE_2026-06-06.md` |
