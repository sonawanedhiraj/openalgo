# R81 — before searching for a new entry: what we already have, what it must clear, and what to test

- **Issue:** #785.
- **Stage:** survey + cost floors + pre-registration. **No hypothesis has been tested.** The candidate list in §4 waits for Dheeraj's approval.
- **Pre-registration:** `backtest/r81_survey/PREREGISTRATION.md`, committed with this doc before any result.
- **Sources:**
  - `strategies/STRATEGY_REGISTRY.md`, read end to end.
  - Byte copies of `db/openalgo.db` and `db/sandbox.db` taken 2026-11-15.
  - Kite `/charges/orders` (a calculator; it places no orders) and Kite daily history probes, saved under `backtest/r81_survey/data/`.

## 0. The most important finding

**The sector-rotation book (momentum + low-vol, risk-parity) has never been run in any form.** It was validated on 2026-06-06, and it is still the best-validated idea in the registry. Meanwhile ten rounds went into intraday entries that earn nothing.

| Round | What | Sharpe | Window |
|---|---|---|---|
| R21b | Sector momentum, top-3, 6M, 11–15 sector indices | **0.92** (+42.8 pp vs NIFTY; walk-forward alpha +18.3 → +16.9 pp) | 4.4 years of index history |
| R24 | Low-volatility sector tilt, bottom-3 by 60-day vol | **1.04** (corr 0.69 to momentum) | same |
| R24 | Risk-parity combination | **1.11** | same |
| R26 | Same book on real ETF prices, 2022-08 → 2026-06, 47 rebalances | **1.17**, CAGR 14.8%, MaxDD −16.9% (NIFTY 0.69) | 3.8 years |

These are the three results in your notes, and R26 is the ETF confirmation of them.

**Is it still standing?** It has never been re-tested, so it has neither held up nor failed. What the 2026-06-06 work did and did not establish:
- **Costs: conservative, not optimistic.** R26 charged 0.17% buy + 0.27% sell, 44 bps a round trip. Kite's calculator today prices a ₹1 lakh ETF round trip in delivery at **2.3 bps + ₹15.34 DP**; equity ETFs pay STT of 0.001% on the sell side only, not 0.1%. With 5–15 bps slippage per side, the real cost is 14–34 bps a round trip: a third to three-quarters of what R26 assumed.
- **No look-ahead beyond a same-close fill.** The signal at rebalance-day close was filled at that close (`run_etf_combined.py`).
- **Never statistically tested.** There is no t-stat or confidence interval. A Sharpe of 1.17 over 3.8 years is t ≈ 2.3 on *absolute* return, but what matters is the *excess* over NIFTY (Sharpe 0.69 on the same window), and its t was never computed.
- **Chosen in-sample.** The 6-month lookback (3M had the higher return), top-3, the 4-variant pick and the 11-sector universe (R25 dropped four sectors on tracking error) were all chosen on the same 2022–2026 window. That is one regime, mostly a bull market with strong sector dispersion.
- **Never re-validated.** Five months of data since 2026-06-04 exist and nobody has looked.
- **Never deployed.**
  - `strategies/sector_rotation_etf/config_snapshot.json` still says `mode: scaffold-only, deployable: false`.
  - The 2026-06-15 seed entry in `DEPLOYMENT_CHECKLIST_2026-06-15.md` never happened: the LEARNINGS line *"(populate with actual fills after the 2026-06-15 entry)"* is still a placeholder.
  - No commit has touched the strategy since June.
  - The registry's "Active" entry still says the seed entry moved up to 2026-06-15; it is corrected in this commit.

**Next step:** it is H1 below. The frozen spec, run on 15 years of index history it was never fitted to (2007-01 → 2022-07), is a test it could fail, and the cheapest decisive test available.

**The other validated-and-undeployed book is not reliable as published.** R41's 5-sleeve blend V_BLD5_B (23-month Sharpe 2.42) was "slated for sandbox paper-trade starting 2026-07-01" and never paper-traded. Its sector_follow sleeve's R40 Sharpe of 2.37 was later shown to be ~0.68 under real CNC charges (flat 0.0857% cost and a geometric fractional book; see the 2026-06-14 full-window study). Do not deploy it as written.

## 1. Survey: every round on one screen

| Theme | Rounds | Verdict | What it taught |
|---|---|---|---|
| Intraday 5m breakout and its filters (ChartInk rule, sector movers, Wyckoff colour, regime/doji, Markov, bull flag) | R1–R6, R11–R13 | REJECT | No intraday directional edge net of Zerodha costs. Cost/gross is invariant to size (R6). |
| Swing on daily bars (Wyckoff, indicators, bull-flag swing) | R7–R10, R14, R8-options | PROMISING then not robust | The daily class clears the cost wall. R8 was +₹43k, but all of it was April. |
| Mean reversion (intraday fade, single-name, index Connors), pairs and cointegration | R15, R16, R32, R33 | REJECT | Win rate never above 50%; market-neutrality kills the sector edge. |
| **Sector rotation (momentum, low-vol, risk-parity, ETF replication)** | **R17, R20, R21a/b, R24, R25, R26** | **DEPLOY candidate — never run** | §0. |
| Calendar (turn of month, pre-expiry), 200DMA crash filter, F&O ban list | R18, R22, R23 | REJECT | No calendar edge; 200DMA lowered Sharpe. |
| Sector-gated intraday | R29 (and v2, options) | REJECT | In-sample edge did not generalise; options were worse. |
| Trailing / pyramiding swing (V9-F) | R30 (no table row), R31 | REJECT | Pyramiding is the whole edge; universe breadth binds. |
| Blends of uncorrelated sleeves | R34, R41 | DEPLOY candidate — never paper-traded | §0: a sleeve is overstated. |
| Options: BB squeeze, monthly buying (synthetic and real), condors, delta roll, put hedge | R35–R38, R55 | REJECT | Real premiums are 0.4–1.1 Sharpe worse than BS; premium selling is frequency-bound. |
| Conditional / vol-regime / setup sweeps | R39 | REJECT/PIVOT | Per-asset framing dies on Bonferroni; spun out sector_follow. |
| **sector_follow (CNC, T+1)** | **R40, R41** | Deployed (sandbox; briefly live, see §2) | Real-cost Sharpe ~0.68, not 2.37. |
| **futures_follow (NIFTY futures, overnight)** | **2026-06-14 study, R42, R54, R55** | Deployed (sandbox) | Leveraged beta, not alpha. Short mirror, stops and put hedges all reject. |
| News: momentum, delayed entries, PEAD drift, quarterly updates | R43–R45, R47 | REJECT / INSUFFICIENT | Spikes mean-revert; PEAD "drift" was a smallcap-regime artifact. |
| Insider: promoter buying | R46, R49 | PROMISING but untradeable | The edge lives in illiquid names (ADV20 < ₹1 Cr). |
| Volume-gate latency | R48 | REJECT | Selectivity, not latency. |
| Scanner → engine; pullback design | R56, R57, R60 | REJECT; PROMISING → `intraday_pullback_top2` sandbox | §2 shows the result. |
| simplified_engine diagnostics (journal, OOS, false positives, options, exits, entry, fresh window, audit doc, signal-to-outcome, backfill) | R61–R68, R76, R78–R80 | REJECT | Gross ≈ 0 (0.3–2.2 bps) against an 8.9 bps floor. Exits and filters are exhausted (R67, R80). |
| open15 opening volume breakout | R58, R59, R62b, R63, R64, R70, R74 (+R75, no row) | REJECT / INSUFFICIENT; **running LIVE** | The published edge was intra-bar look-ahead (R58). |
| Closing session / auction reversal | R71, R71b | PROMISING → `cas_auction_reversal` sandbox | The edge needs ≥ 10 candidates a day (#777). |
| Expiry straddle across the auction | R69 | INSUFFICIENT until ~30 sessions | No history exists; the strategy records its own. |
| RSI/WMA crossovers on index futures (30m proxy, daily real) | R72, R73 | REJECT | Turnover × 0.05% futures STT. |
| Noise-boundary NIFTY intraday momentum | R77 | REJECT at current costs | Real futures cost of 5.95 bps eats a 4.2 bps/day gross. |

**Registry gaps:** rounds 27, 28 (only a mention), 30, 50, 51 (issue #388 open, no row), 52, 53 and 75 have no row in the Rejected table. R30, R53 and R75 exist in memory and research docs. This commit does not backfill them; it flags them.

**Everything that was ever found to WORK and is not running:**
1. Sector rotation momentum + low-vol (R21b/R24/R26), detailed in §0. **Never executed.**
2. V_BLD5_B blend (R41). Never paper-traded, and not reliable as published (§0).
3. Promoter-buy drift (R46). Real but confined to illiquid names (R49), so it does not count.
4. R8 Wyckoff swing as options. "Fragile": the edge collapsed on the both-legs-real cross-check.

Everything else marked PROMISING was either deployed (sector_follow, futures_follow, intraday_pullback, cas_auction_reversal, open15) or later rejected.

## 2. The strategies on the dashboard: mode and what each has actually earned

The dashboard registers seven strategy folders. `sector_rotation_etf` is an eighth, scaffold-only folder. Mode is from the `strategy_mode` table; P&L comes from each strategy's own journal, real fills only, net of modelled charges where the journal carries them.

| Strategy | Mode today | Record | Trades | Gross | **Net** |
|---|---|---|---|---|---|
| simplified_engine | sandbox | 2026-06-01 → 11-13 | 511 | +₹9,752 | **−₹29,480** |
| open15_vol_breakout | **LIVE (real money)** since the 2026-10-21 flip; real rows from 2026-08-06 | 2026-08-06 → 11-05 | 73 real fills | — | **−₹21,515** (parent); **children −₹56,506** (3 accounts, 2026-08-26 → 09-10) |
| futures_follow_cap50 | sandbox | 2026-07-01 → 11-12 (priced exit rows) | 28 | +₹25,639 | **+₹5,800** |
| cas_auction_reversal | sandbox | 2026-10-05 → 11-05 | 125 | +₹17,160 | **+₹1,782** (all profit on ≥ 10-candidate days, #777) |
| cas_320_expiry_straddle | sandbox | 2026-09-22 → 11-05 | 26 legs | −₹15,342 | **−₹16,648** |
| intraday_pullback_top2 | sandbox | 2026-07-17 → 10-19 | 13 | −₹1,377 | **−₹2,696** |
| sector_follow_cap5_vol | sandbox (was LIVE 2026-06-24 → 08-07 by an unintended harness write: 19 real BUYs placed, 9 rejected) | 2026-07-02 → 11-06 | 81 sandbox BUYs | **not measurable** | **not measurable** (see the defect below) |
| sector_rotation_etf | scaffold (not on the order path) | — | 0 | — | — |

- **Real money so far:** open15 parent −₹21.5k plus its three child accounts −₹56.5k (child P&L is captured only for 2026-08-26 → 09-10), plus sector_follow's 10 accidental live fills, whose P&L is unmeasured.
- **The one positive running book** is futures_follow at +₹5.8k net over five months in sandbox. The registry already labels it leveraged beta, not alpha.

**Defect found during the survey (not root-caused here; flagged for its own issue).** `sandbox.db` holds **81 sector_follow BUYs and zero SELLs tagged to it**. Its ₹40 lakh of entries were never matched to an exit. 36 sandbox holdings remain open, with +₹1.72 lakh unrealised across all CNC strategies.

- **Likely cause:** the sandbox moves CNC positions into *holdings* at the 03:00 settlement, while sector_follow's rehydrate (#563) reads the *position book*. cas_auction_reversal hit the same problem and fixed it by reading holdings through `mode_key`.
- **Consequence:** sector_follow's sandbox record is not a T+1 strategy record.

## 3. The bar any new entry must clear

All charges below come from Kite's own calculator on 2026-11-15, with `data/cost_floors*.json` holding every component. A same-day buy-and-sell pair is taxed as intraday, so CNC legs were priced separately. In `cost_floors.json`, the CNC/ETF rows are that same-day (intraday-taxed) pair; the delivery figures are in `cost_floors_cnc.json`.

| Vehicle | Fixed per round trip | Proportional | Example size → charges | Slippage (pre-registered, per side) | **Gross edge needed per trade** |
|---|---|---|---|---|---|
| Equity intraday MIS | ₹47.2 (₹20 × 2 brokerage + GST) | **3.55 bps** (STT 2.5 on sell, exchange, stamp) | ₹86k → 9.1 bps; ₹5L → 4.5 bps | ~2.4 bps measured (R79) | **~14 bps at ₹86k; never below ~8.4 bps** at any size (3.55 floor + slippage) |
| Equity delivery CNC | ₹15.34 DP per scrip per sell day | **22.25 bps** (STT 0.1% each side = 20; stamp 1.5) | ₹50k → 25.3; ₹1L → 23.8; ₹5L → 22.6 bps | 5 bps | **~34 bps at ₹1L** per round trip |
| Equity ETF CNC | ₹15.34 DP | **2.25 bps** (sell STT 0.001%) | ₹50k → 5.3; ₹1L → 3.9 bps | 5 (Tier-1) / 15 (thin) | **~14 bps (Tier-1) / ~34 bps (thin)** |
| NIFTY futures (1 lot ≈ ₹16.5L) | ₹47 | **5.94 bps** (STT 0.05% on sell = 5.0) | ₹979 a lot; +5.94 bps per monthly roll | 1 bp | **~8 bps** per round trip or roll |
| Stock futures (1 lot ₹5–7L) | ₹47 | **6.4–6.6 bps** | ₹340–430 a lot | ~3 bps | **~12.5 bps**; lot sizes force ₹5L+ per name |

**Two ways to read this:**
- **Per trade:** the cheapest vehicle per trade is an ETF in delivery or NIFTY futures. Plain equity delivery is the most expensive per trade, because 0.1% STT is charged on each leg.
- **Per day of holding:** the arithmetic flips.

| Vehicle and hold | Edge needed |
|---|---|
| MIS, held for minutes to hours | ≈ 14 bps (at ₹86k) inside one session |
| CNC, held 1 day | 34 bps a day |
| CNC, held 5 days | 6.8 bps a day |
| CNC, held 20 days (monthly) | **1.7 bps a day** |
| CNC, held 60 days | 0.6 bps a day |
| ETF, monthly hold | ~0.7 bps a day |
| NIFTY futures, overnight every night | ~8 bps a night, ≈ 20% a year gross before it earns anything |

**Annual cost of a monthly-rebalanced book:**
- Equity CNC with 50% monthly turnover: ≈ 12 × 0.5 × 34 bps ≈ **2.0% a year**.
- ETF rotation at the same turnover: ≈ **0.8% a year** (Tier-1) to 2% (thin).

**Stated plainly: lower-frequency ideas start with a structural advantage.**
- Eight-plus rounds measured the intraday equity signals' gross edge at 0.3–2.2 bps per trade against a floor no size can push below about 8.4 bps. That entry would need to be 4–30 times better than anything measured.
- A monthly entry needs only to beat 1–2% a year of drag, against documented premia (sector and stock momentum, low volatility) of several percent a year.
- The overnight-every-night futures trade sits in the worst spot: it pays a full round trip every day for a premium of a few bps.

**Not a trading cost, but real:** short-term capital gains tax on positions held under a year applies to monthly rotation and not to intraday business income in the same way. It belongs in the deployment decision, not the backtest. Rates are to be confirmed with a tax adviser.

## 4. Pre-registered candidates (full spec in `backtest/r81_survey/PREREGISTRATION.md`) — for approval

| # | Candidate | Rationale (stated before testing) | Frequency, vehicle | Out-of-sample window |
|---|---|---|---|---|
| **H1** | Sector rotation momentum + low-vol risk-parity, **R26d frozen, not re-tuned** | Industry momentum (Moskowitz & Grinblatt 1999) + low-vol anomaly (Baker, Bradley & Wurgler 2011); NSE runs both as indices | Monthly; sector ETFs | **Backward 2007-01 → 2022-07** on index proxies (never fitted), with three sub-periods; forward 2026-06 → 11 on ETFs (informational) |
| **H2** | Stock 12-1 momentum, top 20 | Jegadeesh & Titman 1993; IIMA factor library shows a WML premium in India | Monthly; CNC | Train 2010–17 / validate 2018–21 / **sealed 2022 → 2026-11** |
| **H3** | Stock low volatility, lowest 20 | Ang et al. 2006; leverage constraints; NSE low-vol indices | Monthly; CNC | Same |
| **H4** | NIFTY overnight premium (close → open), unconditional | Overnight carries the equity premium (Cliff, Cooper & Gulen 2008; Lou, Polk & Skouras 2019); also tests what futures_follow really earns | Daily; NIFTY futures and NIFTYBEES | Train 2011–17 / validate 2018–21 / sealed 2022 → 2026-11 |
| **H5** | Volatility-managed overlay on H1 and H2 | Moreira & Muir 2017; momentum crash protection (Barroso & Santa-Clara 2015) | Monthly overlay | Same as its parent |
| **H6** | 52-week-high proximity, top 20 | George & Hwang 2004 (anchoring); reported with its correlation to H2 | Monthly; CNC | Same as H2 |

Lower-frequency candidates: five of six (H1, H2, H3, H5, H6), all monthly. **Excluded:** every rejected family in the registry, the ChartInk/scanner breakout and its variants, everything in flight, and an SUE/PEAD study someone has started (table in the pre-registration).

**Protocol, applied to every candidate:**
- **Sealed holdout:** not opened until every spec is frozen by commit.
- **Execution:** at the close one day after the signal.
- **Costs:** Kite-exact, with slippage per vehicle plus a zero-slip upper bound.
- **Significance:** Newey-West t and block-bootstrap CI. |t| ≥ 2 is needed to be called a result; ≥ 2.64 to be confirmed family-wise.
- **Regime:** the same sign in every period or sub-period, otherwise noise.
- **Plateau and placebo:** a pre-declared neighbour plateau, and the R67 random-portfolio placebo (p < 0.05).
- **Ledger:** every variant goes in, failures included.

## 5. Decisions needed from Dheeraj before any test runs

1. **Approve, cut or amend the six candidates.** H1 and H2 carry the most prior evidence. H4 is cheap and is mainly a diagnostic of futures_follow.
2. **Stock universe for H2, H3 and H6.**
   - **(A)** Use the 2024-03-01 F&O list as is. This is survivorship-biased; the same-universe benchmark controls most of it.
   - **(B)** Approve downloading about 200 public NSE month-end F&O bhavcopy files for point-in-time membership.
   - B is cleaner. A is faster.
3. **Separate from R81:** the sector_follow sandbox exit defect (§2) should get its own issue and fix before that book's record means anything.
4. **Separate from R81, and only Dheeraj's call:** open15 is live with real money and has lost −₹21.5k on the parent account plus −₹56.5k across children. Its pre-registered checkpoint is 20 new A-grade trades with net < 0 → sandbox (#777).
