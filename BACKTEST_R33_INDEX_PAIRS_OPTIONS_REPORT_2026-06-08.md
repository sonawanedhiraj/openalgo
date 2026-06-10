# R33 — Index Reversion / Sector Pairs / Options Overlays

**Date:** 2026-06-08 · **Hypothesis:** edge on Indian F&O lives in index/sector/market-neutral
constructs, not single names (R32 rejected all 6 single-name MR variants). Hunt
monthly-consistent cash flow (≥70% green, Sharpe ≥1.3, ≤25% concentration).

**Outcome: NO DEPLOY_CANDIDATE, NO PROMISING.** Best is V_PAIR_B (MARGINAL). Two structural
findings worth the spend (below).

## 1. Variant matrix (OOS 2024-01-01 → 2025-11-30 @ 1.88 bps)

| variant | construct | verdict | N | Sharpe | green% | payoff | WR | net@1.88 | net@3.5 |
|---|---|---|---|---|---|---|---|---|---|
| V_IDX_A | NIFTY Connors RSI(2) via NIFTYBEES | **MARGINAL** | 20 | 1.45 | 62.5% | 1.85 | 0.65 | ₹3,303 | ₹3,023 |
| V_IDX_B | BANKNIFTY Bollinger fade via BANKBEES | REJECT | 8 | −2.28 | 14% | 0.80 | 0.25 | −₹4,230 | −₹4,358 |
| V_PAIR_A | Sector-ETF rotation pair (mkt-neutral) | REJECT | 23 | 0.06 | 48% | 1.13 | 0.48 | ₹2,096 | ₹602 |
| V_PAIR_B | Stock-vs-sector beta-hedged pair | **MARGINAL** | 110 | 0.75 | 50% | 1.52 | 0.46 | ₹28,974 | ₹25,435 |
| V_OPT_A† | Weekly CSP on NIFTY, Mon→Thu (BS) | REJECT | 100 | −0.26 | 56% | 0.42 | 0.68 | −₹58,442 | −₹58,651 |
| V_OPT_B† | NIFTY 0-DTE short strangle, Thu (BS) | REJECT‡ | 95 | −0.28 | 4% | 6.96 | 0.10 | −₹4,945 | −₹4,973 |

† Both options variants REDIRECTED per the 2026-06-08 regulatory update (BankNifty/FinNifty
weeklies discontinued → NIFTY-only; SEBI ELM-2%-on-expiry + stamp 0.003% added to cost model;
NIFTYBEES/sector-ETFs have no options). ‡ V_OPT_B's −₹4,945 / 10% WR is a **modelling artifact**
(see §4/§5), not a reliable edge estimate.

Gates: DEPLOY needs Sharpe ≥1.5, ≥70% green, payoff ≥1.2, ≤25% any name/month, WR ≥50%.
None clears. The hard wall everywhere is **green% (monthly consistency)** — same wall as R30/R31/R32.

## 2. Data-quality / tradability disclosures (read before trusting any number)

- **NIFTYBEES & GOLDBEES are absent** from `historify.duckdb`. V_IDX_A proxies NIFTYBEES with
  NIFTY-index OHLC (returns identical bar ~1.2%/yr dividend yield that we OMIT → result is
  conservatively low). Verified clean (0 zero closes, 1 flat bar / 1095).
- **`fo_bhavcopy_eod` holds STOCK options ONLY** — re-verified after the 2026-06-08 redirect:
  **zero symbols contain "NIF"**, option_type ∈ {CE,PE} are all single-stock, and NIFTY index
  options exist at **no interval, no date**. The redirect's instruction to "use bhavcopy NIFTY
  weekly options 2024+" is therefore **not satisfiable on this warehouse.** NIFTY 1m spot exists
  only from **2025-12-01** — no intraday NIFTY on the OOS window either. So **V_OPT_A and V_OPT_B
  premium is Black-Scholes MODELLED** (IV = realized-20d-vol × 1.15) on real *daily* NIFTY spot.
  Prior rounds noted BS interpolation "failed us before"; we flag it loudly: **neither options
  variant can be a DEPLOY_CANDIDATE on modelled premium.**
- **Regulatory facts applied to the redirect** (confirmed by the update): BankNifty/FinNifty/
  MidcapSelect/NiftyNext50 weeklies discontinued (SEBI 2024) → only NIFTY 50 has a weekly, so
  both variants trade NIFTY only; ELM 2% on short options on expiry day (2024-11-20) is added —
  but as a *carry on blocked collateral*, not a full-2%-of-notional fee (ELM is returned margin;
  booking the gross 2% as a cost would be wrong accounting and would swamp every trade). Stamp
  0.003% sell-side and brokerage on buy-back legs added. NIFTY lot = **75** (post-Nov-2024), not
  50; all verdict metrics are lot-invariant, absolute ₹ scale ×1.5.
- **Short legs of V_PAIR_A/B are not deliverable-shortable** overnight via CNC. Among these
  sector ETFs only BANKBEES has a liquid index future (BANKNIFTY); the rest need SLB, which is
  illiquid. So both pair variants are **PAPER market-neutral**: long leg tradable, short leg not.
  V_PAIR_A measured beta-vs-NIFTY = **−0.012** (corr −0.014) → neutrality is real, but so is the
  short-leg infeasibility.

## 3. Hand-validated trade (to the rupee) — V_IDX_A trade #12, 2024-01-04→05

Buy NIFTYBEES(=NIFTY) @ 21605.8 ×2 = ₹43,211.6; sell @ 21710.8 ×2 = ₹43,421.6.
- gross = (21710.8−21605.8)×2 = **₹210.00**
- STT (0.1% sell, delivery) = 0.001×43,421.6 = **₹43.42**
- stamp (0.015% buy) = 0.00015×43,211.6 = ₹6.48 · exch (0.00345% turnover) = ₹2.99 ·
  SEBI = ₹0.87 · GST 18%(exch+sebi) = ₹0.69 → **total charges ₹54.45**
- slippage 1.88bps × ₹86,633 turnover = **₹16.29**
- **net = 210 − 54.45 − 16.29 = ₹139.26** ✓ (matches `trades.csv`). At 3.5bps → ₹125.23.

Note what this single trade exposes: ₹70.74 of cost on a ₹210 gross (0.49% move) = **34% cost
drag**. STT-on-delivery is the dominant line. This is why V_IDX_A nets only ₹3.3k/23mo.

## 4. Per-variant verdicts & why

- **V_IDX_A — MARGINAL.** The cleanest signal here (Sharpe 1.45, payoff 1.85, WR 65%) but in
  the market only ~92 of 580 days → **₹3,303 net over 23 months on ₹50k ≈ 1.7%/yr, below
  risk-free.** Statistically fine, economically a rounding error. Index averaging *does* smooth
  the signal (vs R32 single names) but the trade count collapses to 20 and STT eats a third of
  each thin win. Not a cash-flow engine.
- **V_IDX_B — REJECT.** Bollinger fade on BANKNIFTY: 6/8 trades hit the 1.5×ATR stop. Bank
  index trends through its lower band; fading it is standing in front of a train.
- **V_PAIR_A — REJECT.** Market-neutrality is *perfect* (β −0.012) but that's exactly the
  problem: long-top / short-bottom 60d-Sharpe sector rotation **cancels to zero alpha**
  (Sharpe 0.06, ₹2,096/23mo). Sector momentum is a directional, low-frequency, top-2-sector
  effect (per R30/R31 memory) — strip the direction and nothing is left.
- **V_PAIR_B — MARGINAL (best variant).** Stock-minus-sector alpha is *real* (payoff 1.52,
  ₹28,974/22mo) — picking high-RoC stocks in strong sectors and hedging the sector beta
  isolates genuine idiosyncratic alpha. But it fails on **consistency (50% green) and
  concentration (FORTIS alone = 50% of net; top-2 names = ~85%).** It's the same lumpy
  high-payoff signature as the sector sleeve, not a monthly annuity.
- **V_OPT_A (redirected) — REJECT, model-dependent.** Proper NIFTY weekly CSP, Monday→Thursday
  expiry with the corrected cost model, **nets −₹58,442** (payoff 0.42, Sharpe −0.26, green 56%).
  WR 68% (puts expire worthless) but the textbook short-vol asymmetry — avg win ₹7.7k vs avg
  loss ₹18.3k — plus the heavier corrected costs (STT+stamp+ELM-carry, brokerage on 4 legs) and
  the shorter 3-day theta capture push it underwater. The original 7-day version looked positive
  only because it captured more theta and under-costed; the regulatorily-correct version loses.
- **V_OPT_B (replaced) — REJECT, but result is a MODELLING ARTIFACT, not an edge verdict.** The
  0-DTE Thursday strangle shows WR 10% / 79-of-95 stopped, because the only data available is
  *daily* OHLC: the SL ("premium doubling") is tested against the day's **full high-low range**,
  which almost always doubles the small ATM±200 entry premium even when intraday the position is
  never actually at that extreme when premium peaks. A real 0-DTE strangle is entered/exited and
  SL-managed *intraday*, which we cannot simulate (no NIFTY intraday on OOS, no real premiums).
  **The valid conclusion is "untestable on the available data," not "the strangle has no edge."**
  We report the number for completeness and flag it as unreliable.

## 5. Options path (requested) — both redirected variants REJECT; one is untestable here

The original covered-call overlay was **dropped** (sector ETFs have no options — confirmed). Its
earlier pre-redirect run had already shown the overlay *degrades* the deployable (caps momentum
winners), so dropping it costs nothing. The replacement 0-DTE NIFTY strangle (V_OPT_B) and the
NIFTY weekly CSP (V_OPT_A) are both REJECT, but the **binding constraint is data, not strategy**:
this warehouse has no index option prices at any interval and no OOS-window NIFTY intraday, so
every options number here rests on a Black-Scholes premium assumption (V_OPT_A) or additionally a
daily-resolution intraday proxy (V_OPT_B). **Neither can be trusted as a deploy/no-deploy verdict.**
To test options income honestly the next step is an *infrastructure* task, not a backtest: backfill
real NIFTY weekly option EOD (and ideally 1-min) into `fo_bhavcopy_eod`/`market_data` for
2024-01→2025-11, then re-run. Until that data exists, the options sleeve is **out of scope** —
do not deploy and do not size on these modelled figures.

## 6. V_PAIR_A path (requested) — cleanest neutrality, but no alpha to harvest

β −0.012 confirms the construct is genuinely market-neutral; the problem is there is no spread
edge to monetize, and the short leg isn't deliverable. Not the market-neutral path.

## 7. Nothing cleared — what's structurally missing, and the next experiment to fund

Across R30→R33 the wall is identical: **monthly green-month consistency, not Sharpe or payoff.**
Every edge we find on this universe is the *same animal* — low-frequency, high-payoff,
concentration-driven (sector momentum, stock-minus-sector alpha). It clusters into a handful of
strong months and a handful of names. R33 confirms index averaging (V_IDX_A) and beta-hedging
(V_PAIR_B) **isolate real alpha but cannot manufacture frequency**: you cannot get 70% green
months from a signal that only pays in trending regimes.

**The missing ingredient is a genuinely uncorrelated return stream to stack ON TOP of the
high-payoff sleeve** so that the *portfolio* (not any single signal) is monthly-consistent —
i.e. stop hunting one engine that is both high-payoff and high-frequency (it may not exist on
141 F&O names + sector ETFs) and instead engineer consistency at the portfolio layer.

**Next experiment to fund (R34): a 2-sleeve portfolio overlay, not a new signal.** Combine the
deployable sector-rotation sleeve (V_PAIR_B-style stock-minus-sector alpha as a second sleeve)
with an explicit **volatility-targeted, inverse-correlation weighting** and measure *portfolio*
monthly green%, not per-signal. Concretely: does V_PAIR_B (idiosyncratic alpha, lumpy) + the
existing momentum sleeve (directional, lumpy) — when their monthly returns are
**negatively/zero correlated** — produce ≥70% green months at the blended level even though
neither sleeve does alone? That is the only untested lever left, and it requires correlation
data we already have. If even the blend can't hit 70% green, the strict cash-flow bar is
structurally unreachable on this universe and the bar itself should be renegotiated (e.g.
quarterly consistency) rather than the strategy.

---
*Artifacts: `outputs/r33_index_pairs_options_2026-06-08/v_*/` — run script, trades.csv,
monthly_pnl.csv, summary.json, notes.md each. All read-only on cached parquet; no DB writes, no
code/branch/test changes.*
