# R60 — Scanner-history strategy design: ideal entry/exit for LONG and SHORT

**Date:** 2026-07-27
**Issue:** [#480](https://github.com/sonawanedhiraj/openalgo/issues/480)
**Verdict:** **PROMISING — sandbox/paper-trade first.** A blank-slate forward-return
mapping over the full scanner signal history finds one coherent, split-half-stable,
parameter-robust structure — **mild gaps (1.5–2.5%) that fire in the afternoon
(13:00–15:00 IST) continue in the signal direction into T+1** — and yields a
concrete long spec AND (for the first time on this signal set) a deployable short
spec. Both clear real Zerodha costs in the year-long replay. **BUT the 3-week live
out-of-sample (2026-07-07..27) is negative on both sides** (small n, earnings
season), so the recommendation is sandbox deployment with a measured go/no-go, not
live capital.

---

## The scanner's filters (what the signals ARE)

Two Chartink-mirror rules over the ~211-symbol F&O universe, re-evaluated every 5m
bar close (`services/scan_rules/`):

- **BUY (12 gates):** price ₹100–5000; today's running close > yest close ×1.015
  (gap-up ≥1.5%, live `CHARTINK_RULE_BUY_GAP_PCT=1.5`); open > yest close; open >
  pivot (H+L+C)/3; **daily volume > SMA50 AND SMA200 of daily volume**; weekly
  ATR(21) > 5% of price; 15m RSI(14) > 50; price above 5m Supertrend(7,3) with
  prior ST ≥ yest close.
- **SELL (9 gates):** the directional mirror — gap-down ≥1.5%, open < yest close,
  open < pivot, **volume > yesterday's volume** (simpler gate), weekly ATR > 5%,
  15m RSI < 50, price below 5m Supertrend.

Structural consequence (R56): the cumulative-volume gates clear **late** — median
first fire ~13:45 IST — so the signal marks a *confirmed, high-volume, trending
mover in the afternoon*, not an open-drive.

## Data & method

- **Signal set:** the R56 full-history replay of the REAL rule functions —
  3,878 first-fires per (symbol, day, direction) over 2025-06-20 → 2026-07-06
  (258 trading days, BUY 1,995 / SELL 1,883), `signals_full.parquet` (restored
  from commit `ee6651283`). Live `scan_results` (2026-06-25 → 07-27, 5,826 hits)
  used for the OOS check.
- **Prices:** `outputs/tod_volume_gate/prices.duckdb` (broker 1m, 20.3M bars +
  daily). OOS T+1 prices via Yahoo (.NS).
- **Blank-slate mapping:** for every signal, raw forward returns at +30/60/120min,
  15:25, day close, T+1 open/close, T+2, T+3, plus MFE/MAE from fire to EOD; then
  signed aggregation over direction × gap-band × fire-window with split-half
  (H1 ≤2025-12, H2 ≥2026-01) discipline; then path-accurate execution sims
  (1m stop paths, gap-through handling, Zerodha MIS/CNC charge models, slippage
  sweeps).

## The structure the grid reveals

| Cut (signed = signal right) | same-day 15:25 | T+1 close | split halves (T+1c) |
|---|---|---|---|
| BUY all (n=1,995) | +0.00% | +0.11% | +0.28 / +0.01 |
| SELL all (n=1,883) | +0.04% | −0.09% | +0.38 / −0.34 |
| **BUY band [1.5,2.5) ∩ 13:00–15:00 (n=272)** | **+0.18%** | **+0.49%** | **+0.54 / +0.46** |
| **SELL band [1.5,2.5) ∩ 13:00–15:00 (n=329)** | **+0.10%** | **+0.36%** | **+0.41 / +0.30** |
| BUY band 5%+ (n=265) | −0.17% | +0.10% | mean-reverts intraday |
| SELL band 3.5–5 (n=340) | −0.00% | −0.52% | deep dips bounce hard |

- **Mild + afternoon = continuation, BOTH directions, both halves.** This extends
  R57 (which found the long side) symmetrically to the short side.
- **Extended moves (≥3.5%, esp. ≥5%) mean-revert** — shorting a deep gap-down
  loses −0.5 to −1.0% by T+1; fading it long is +0.52%/T+1 but H2-concentrated
  (regime-suspect) → not recommended.
- Morning fires (before 11:00) and the 11:00–13:00 window carry no stable edge.
- Not outlier-driven: trimming the 5 best + 5 worst long trades leaves the mean
  unchanged (+0.494%). Robust to every band/window perturbation tested
  ([1.5,2.0), [2.0,2.5), [1.5,3.0), 12:30 start, 15:30 end… all positive,
  +0.27% to +0.57%). LONG cell t-stat ≈ 3.2; SHORT session-variant ≈ 4.

## LONG spec (BUY signals)

**Entry:** at the fire price (5m bar close) of the FIRST BUY fire per symbol-day,
only if gap-at-fire ∈ [1.5%, 2.5%) and fire time ∈ [13:00, 15:00) IST.
**Stop:** 1.0% below entry, same-day only (exit MIS if hit; ~10% of trades).
**Exit:** T+1 15:25 (CNC overnight hold). **~1 trade/day.**

| Exit variant (path-accurate, ₹50k/trade) | gross/trade | net total (269 t) | net avg | green mo | halves |
|---|---|---|---|---|---|
| same-day 15:25 (MIS) | +0.19% | +₹10,793 | +₹40 | 10/14 | + / + |
| T+1 open | +0.35% | +₹14,264 | +₹53 | 10/14 | + / + |
| **T+1 close, 1% stop** | **+0.60%** | **+₹48,459** | **+₹180 (+0.36%)** | **10/14** | **+11.9k / +36.6k** |
| T+2 close, 1% stop | +0.61% | +₹48,737 | +₹182 | 10/14 | H2 weaker |

The 1% same-day stop *improves* gross (+0.49→+0.60%): it cuts the same-day
failures before they gap further against the position overnight. Tighter/ATR-style
stops destroy the edge (R56/R57 — 60–87% of engine trades died on stops).
Costs modeled: CNC ≈ ₹129/₹50k r/t (STT 0.1%×2 dominates); MIS ≈ ₹53. Slippage:
still +₹26k/yr at 5bps/side.

## SHORT spec (SELL signals)

The naive short (enter at fire, exit same-day 15:25) is **gross-positive but
net-negative** (+0.10% vs ~0.11% MIS costs) — do not trade it. Equity shorts
cannot be held overnight (MIS-only). But the return path shows the short edge
lives in the **T+1 session**: t1_open −0.11% (stock gaps *up* slightly against
the short) → t1_close +0.36% → t2 gives it all back. So the deployable variant:

**Signal day:** record symbols whose FIRST SELL fire has gap ∈ [1.5%, 2.5%) and
fire time ∈ [13:00, 15:00). **No position overnight.**
**Entry:** short at T+1 09:15 open (MIS). **Exit:** cover T+1 15:25 (hard;
before the 15:15–15:20 broker auto-square-off cutoffs use 15:10 in practice).
**Stop:** none needed intraday (1–1.5% stop variants tested ≈ neutral-to-worse).
**~1 trade/day.**

| Variant (₹50k/trade) | gross/trade | win | net total | green mo | halves |
|---|---|---|---|---|---|
| **short T+1 open → 15:25 cover** | **+0.455%** | **59.1%** | **+₹57,210 (328 t)** | **11/13** | **+0.56% / +0.36%** |
| band [1.5,3.0) variant | +0.33% | 55.8% | +₹47,185 | 9/13 | + / + |
| (alt) fire → T+1 close via stock futures | +0.36% | 54.3% | n/a (lot sizing) | 8/13 | + / + |

Robust across all band/window shifts (every tested variant positive). The
futures-carry alternative is parked — SSF lots (₹5–15L) don't fit ₹50k sizing.

## Combined book (capped, realistic)

Cap 3 positions/side/day (first by fire time), ₹50k each → max ~₹3L deployed
(₹1.5L CNC longs + ₹1.5L MIS short margin):

- **LONG sleeve:** 230 trades/yr, net +₹35,986, 9/14 green, worst month −₹5.1k
- **SHORT sleeve:** 236 trades/yr, net +₹26,729, 11/13 green, worst month −₹4.5k
- **COMBINED: net +₹62,715/yr (~20% on ₹3L), 13/14 green months, worst month −₹2,731** — the sleeves diversify (long is overnight-drift, short is T+1-session).

## Honest caveats (the reasons this is NOT a live-deploy verdict)

1. **Live OOS is negative.** On live `scan_results` fires 2026-07-07→07-27 (the
   window the replay never saw): LONG −0.38%/trade (n=12, win 42%), SHORT T+1
   session −0.41%/trade (n=25, win 52%). Within ~1.5 SE of the in-sample means
   given n, and mid-July is Q1 earnings season (AXISBANK −5.4%, DALBHARAT −5.5%
   are earnings-day gaps), but it is a warning, not a confirmation.
2. **H2 < H1 in most cuts** — the edge decayed through the sample on the short
   side (+0.56 → +0.36) and OOS continues that direction.
3. Entry at fire-bar close with small slippage is an upper bound (R57's open
   risk); the short's 09:15-open entry has auction slippage the sweep only
   approximates.
4. Multiple-comparison risk is mitigated (the cell was pre-identified by
   R57/R53 independently; symmetric structure; robustness sweeps) but not zero.
5. **Earnings days are unmodeled.** Both OOS blowups were earnings gaps. An
   earnings-calendar exclusion is the single most promising refinement before
   sandbox go-live.

## Recommendation

Deploy **in sandbox** (paper) as a new scaffold strategy (`scanner_t1_continuation`),
LONG + SHORT sleeves as specced, 2–3 positions/side/day, and track live paper
fills vs backtest for ≥2 months (≥60 trades/side). Go live only if the live paper
mean/trade is within noise of the backtest (≳+0.2%/trade net combined). Add an
earnings-day filter before go-live. Do NOT trade the deep-gap fade (H2 artifact
risk) or the naive intraday short (net-negative).

---

# R60b addendum (same day, operator review) — REAL recorded screener history

The operator reviewed R60a and directed the analysis at the **actual recorded
screener output** (no replayed signals, no next-day re-entries, shorts strictly
same-day). Sets: **Chartink webhook first-hits** (`scan_cycle`,
`cycle_kind='chartink'` post-#449 repair: 360 first-hits per symbol-day-side,
2026-05-29→07-27, 35 days) and **in-house scanner first-hits** (`scan_results`:
306, 2026-06-30→07-27); pooled-deduped 527 priced events (BUY 287 / SELL 240).
Prices: broker 1m (`prices.duckdb`) through 07-06, Yahoo 5m after; Yahoo daily
for prev-close/T+1.

## Chasing the hit does NOT work on the real tape

Entering at the hit price (market order when the screener shows the stock):

| Config (₹50k, real charges) | gross | net |
|---|---|---|
| LONG at hit → 15:25 | +0.145% | **+0.04%** (thin) |
| LONG at hit → T+1 close (CNC) | +0.329% | +0.07% (STT eats it) |
| SHORT at hit → 15:25 | +0.015% | **−0.09%** (loses) |
| SHORT at hit → 30/60min | −0.02/+0.01% | −0.13/−0.10% |

Every same-day SHORT chase configuration is net-negative — consistent with the
year-long replay. The R60a mild-band/afternoon cuts do NOT reproduce on this
2-month window (LONG mild+afternoon T+1c net −0.02%); the window even shows the
opposite time-of-day tilt (morning fires better), i.e. those cuts are
regime-sensitive and should not carry live weight.

## The entry IS the edge: retrace-limit, not chase

Limit order at hit_price ∓ X%, valid 60 min, filled only if touched; exit 15:25:

| Side | retrace | fills | net/trade | win | weekly green |
|---|---|---|---|---|---|
| LONG | 0.25% | 65% | +0.17% | 52% | — |
| **LONG** | **0.50%** | **35%** | **+0.263%** | **57%** | 6/10 (W28 = 86% of P&L) |
| LONG | 0.75% | 13% | +0.45–0.77% | 60–74% | n too small |
| SHORT | 0.25% | 67% | +0.02% | 47% | — |
| **SHORT** | **0.50%** | **37%** | **+0.137%** | **51%** | 3/7 (choppy) |
| SHORT | 0.75% | 17% | +0.19–0.34% | 51% | n too small |

Per-source (0.5% config): LONG chartink +0.18% / inhouse +0.63%; SHORT chartink
+0.15% / inhouse +0.04%. Adverse excursion from fill: p25 ≈ −1.1% both sides →
disaster stop ~2%, never tight (tight stops destroyed every prior round).
With a retrace entry the T+1-close CNC exit is WORSE than 15:25 (+0.10–0.13%) —
so on the real tape everything stays intraday; **no overnight positions on
either side**.

## Corrected verdict

- **LONG:** retrace-limit −0.5% / 60min / exit 15:25 is the best real-data spec:
  net +0.26%/trade, win 57%, positive in both sources — but 2 months, 96 fills,
  P&L concentrated in one week. PROMISING, sandbox-first stands.
- **SHORT:** never chase (−0.09% net). The only net-positive same-day short is
  the +0.5% bounce-limit (+0.14%/trade, 51% win, 3/7 weeks green) — MARGINAL;
  paper-only, or treat the SELL list as an avoid/hedge signal rather than a
  trading edge. R60a's T+1-open short re-entry is retired per operator
  direction (trade only what the screener shows, same-day).
- R60a's year-long replay remains the statistical base for the LONG
  continuation edge; R60b is the execution truth on the live tape: **the
  screener's value is the WATCHLIST; the profit mechanic is buying/shorting the
  retrace, not the breakout chase** — R57's pullback conclusion, reconfirmed on
  real data.

## Artifacts

- Analysis scripts + per-signal metrics: `backtest/inhouse_scanner/r60/`
  (stage_a_forward_returns.py, stage_b_aggregate.py, stage_c_execution.py,
  stage_d_robustness.py, stage_e_portfolio_oos.py, metrics.parquet;
  R60b: real_screener_analysis.py, real_entry_refinement.py,
  chartink_first_hits.parquet, inhouse_first_hits.parquet, real_metrics.parquet)
- Signal set: `signals_full.parquet` (from commit `ee6651283`)
- Prices: `outputs/tod_volume_gate/prices.duckdb` (read-only)
