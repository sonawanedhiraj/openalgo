# R53 — Loss-Month Diagnosis & Win-Rate Filters (findings)

**Task:** diagnose the loss-making months of the R53 *intraday pullback top-2* winner and test
filters that raise the win rate in those months without breaking the good months. Research-only —
the strategy implementation was **not** modified.

**Inputs:** handoff `2026-07-08_r53_loss_month_analysis_handoff.md`, spec
`strategies/intraday_pullback_top2/SPEC.md`, winner harness `userstrat_20mo_6535.py`.
**Branch/issue:** `strategy/388-r51-simplified-engine-improvements`, #389.
**Data:** 20 months (2024-11-01 → 2026-07-06), full 163-stock/15-sector universe, real NIFTY+15
sector indices, real Zerodha MIS charges. **Slippage still unmodeled** (standing caveat). Long-only.

## TL;DR

- The loss months are **not** a regime/VIX story and **not** an intraday-market-reversal story
  (both hypotheses rejected — NIFTY was *up* in 3 of the 4 robust bad months and held green into
  the close in bad months just as in good months). The loss is **stop-out clustering**, concentrated
  in (a) **bad-month morning entries** and (b) **doubling-down re-entries** on a name that already
  stopped out the same day.
- **Two causal, good-month-preserving filters** fix most of it and **validate on both halves**:
  1. **`nf_mom`** — at the entry candle, require **NIFTY still ≥ its own 9:30 gain** (momentum
     confirmation), on top of the existing NIFTY ≥ +0.3% gate.
  2. **`noreentrySL`** — once a stock's attempt exits at its stop, **take no further attempt** on
     that stock that day (stop doubling down).
- **Recommended = `nf_mom + noreentrySL`.** On the handoff's 65/35 compounding book it takes the
  20-month result from **+88.6% → +102.7%, PF 1.45 → 1.68, Sharpe 2.59 → 3.42, MaxDD −11.8% → −9.5%,
  positive months 13/21 → 16/21** — i.e. it **strictly dominates** the baseline on this sample
  (more return, higher PF, *lower* drawdown). Good-month P&L retained ≈ 98–101%.
- **Rejected as fixes:** static **stop-distance caps** and **extension-from-open caps**. They *do*
  cut the bad months, but they gut the good months too (retain only ~40–64% of good-month P&L) — the
  tail losers and the good winners share the same "wide stop / extended entry" features, so a static
  cap can't separate them. Only the *market-momentum* and *no-double-down* gates separate cleanly.
- **Irreducible residual: Jun 2025 (−5.6k).** Neither filter helps it — it is genuine *stock-level*
  trend failure (oil names HINDPETRO/BPCL faded, ADANIGREEN failed) while NIFTY **and** the mapped
  sector stayed green. There is no index/re-entry signal to catch it.

## 1. Baseline reproduced (exact)

Winner harness `userstrat_20mo_6535.py` reproduces to the rupee: **197 trades, WR 45%, PF 1.45,
Sharpe 2.59, +88.6%, MaxDD −11.8%, 13/21 months +.** Loss months match the handoff:
25-06 −5.6k, 25-08 −2.5k, 26-02 −9.1k, 26-07 −3.3k (robust), plus 24-12 −0.9k, 25-11 −1.3k, 26-05 −1.4k.

For clean, sizing-stable filter analysis the diagnostics below use **equal-weight fixed sizing**
(C=₹60k, 2 slots, notional 0.5·C·5 = ₹150k/pos, no compounding) so per-trade nets are additive:
that frame gives **197 trades, WR 44%, PF 1.47, net +₹39.1k** and reproduces the same loss-month signs.

## 2. Per-month baseline vs NIFTY regime

| Month | n | WR | PF | net ₹ | SL/EOD | NIFTY mret% | NIFTY rvol | bad |
|---|--:|--:|--:|--:|:--:|--:|--:|:--:|
| 2024-11 | 10 | 60 | 1.22 | +875 | 4/6 | −0.73 | 1.01 | |
| 2024-12 | 6 | 17 | 0.53 | −2040 | 5/1 | −2.50 | 0.67 | |
| 2025-01 | 14 | 43 | 2.16 | +7126 | 6/8 | −0.95 | 0.85 | |
| 2025-03 | 8 | 38 | 1.28 | +911 | 3/5 | +6.11 | 0.67 | |
| 2025-04 | 6 | 50 | 4.10 | +4908 | 2/4 | +4.60 | 1.32 | |
| 2025-05 | 8 | 50 | 1.74 | +2064 | 4/4 | +1.74 | 1.07 | |
| **2025-06** | 10 | 30 | 0.26 | **−3954** | 6/4 | **+3.31** | 0.64 | ★ |
| 2025-07 | 4 | 50 | 2.02 | +609 | 1/3 | −3.01 | 0.44 | |
| **2025-08** | 11 | 36 | 0.51 | **−2550** | 6/5 | −0.56 | 0.66 | ★ |
| 2025-09 | 15 | 53 | 2.45 | +6272 | 5/10 | +0.04 | 0.45 | |
| 2025-10 | 17 | 53 | 3.10 | +9868 | 4/13 | +3.54 | 0.50 | |
| 2025-11 | 8 | 38 | 0.77 | −503 | 3/5 | +1.67 | 0.51 | |
| 2025-12 | 11 | 55 | 1.30 | +1100 | 4/7 | −0.13 | 0.47 | |
| 2026-01 | 4 | 75 | 3.91 | +2074 | 1/3 | −3.15 | 0.58 | |
| **2026-02** | 10 | 10 | 0.06 | **−6488** | 6/4 | +1.67 | **1.04** | ★ |
| 2026-03 | 13 | 62 | 2.24 | +5622 | 5/8 | −9.94 | 1.58 | |
| 2026-04 | 12 | 33 | 1.52 | +3408 | 7/5 | +5.90 | 1.21 | |
| 2026-05 | 9 | 33 | 0.99 | −67 | 5/4 | −2.13 | 0.82 | |
| 2026-06 | 11 | 45 | 2.26 | +8305 | 5/6 | +2.30 | 0.76 | |
| **2026-07** | 9 | 44 | 0.60 | **−1822** | 4/5 | +1.82 | **0.20** | ★ |

**Read:** the bad months span the full regime range — 25-06 was a strong up-month (NIFTY +3.3%),
26-02 was elevated-vol (rvol 1.04), 26-07 was the *lowest*-vol month in the sample (0.20). NIFTY was
**up** in 3 of the 4. **Regime is not the discriminator.** (VIX proxied by NIFTY realized vol — no
separate VIX series in the caches.)

## 3. The five hypotheses — verdicts

Aggregating the 4 robust bad months (40 trades) vs the good months (157 trades):

| | n | WR | PF | net ₹ | SL% | morning% | avg ext-from-open% |
|---|--:|--:|--:|--:|--:|--:|--:|
| **BAD** | 40 | 30 | 0.33 | −14,815 | 55 | 38 | 0.90 |
| **GOOD** | 157 | 48 | 1.88 | +53,913 | 41 | 32 | 0.56 |

1. **Regime / VIX — REJECTED.** No shared regime signature (§2).
2. **Stop-out clustering — CONFIRMED (this is the mechanism).** Bad-month SL rate 55% vs 41%.
   Bad-month SL trades bled **−19.5k**; bad-month EOD holds were still **+4.7k**. The entire loss is
   whipsaw stops, not EOD fades. The fat-tail losers all carry **wide stops** (PGEL sl_dist 1.54%,
   TECHM 1.12%, SUZLON 1.07%, MOTILALOFS 1.17%).
3. **Entry session — CONFIRMED (where the loss lives).**
   | | n | WR | PF | net ₹ |
   |---|--:|--:|--:|--:|
   | BAD morning | 15 | **13** | **0.08** | **−11,829** |
   | BAD 1pm | 25 | 40 | 0.67 | −2,986 |
   | GOOD morning | 50 | 44 | 2.18 | +33,724 |
   | GOOD 1pm | 107 | 50 | 1.61 | +20,189 |

   Bad-month **mornings** are the disaster (−11.8k of the −14.8k). But mornings are **excellent** in
   good months (PF 2.18) — so a blanket "morning-only-in-loss-months" rule would be pure curve-fit and
   is not usable ex-ante. The fix has to be a *causal* per-trade gate that happens to fire more in the
   bad-month mornings, not a calendar switch.
4. **Gap / extension — partial, but not usable as a static cap.** Bad-month entries are more extended
   from the 9:30 open (0.90% vs 0.56%), and `close>VWAP` is ~86% in both groups (not discriminating —
   a breakout is almost always above VWAP). Capping extension cuts bad months but also removes good
   winners (§4).
5. **Breadth / intraday market reversal — REJECTED.** Per trade-day, NIFTY closed *red* on 5% of
   bad-month days vs 3% of good-month days; average NIFTY EOD +0.71% (bad) vs +0.79% (good). The index
   **held up** in the bad months. The loss is stock-level, not a market roll-over.

## 4. Filter sweep (equal-weight fixed, split-half validated)

Split halves: **H1 = 2024-11 → 2025-08**, **H2 = 2025-09 → 2026-07** (2 robust bad months in each).
`gd_ret%` = good-month P&L retained vs baseline (guardrail ≥ ~90%).

| filter | n | WR | PF | net ₹ | gd_ret% | bad net ₹ | H1 PF | H1 net | H2 PF | H2 net |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| **BASELINE** | 197 | 44 | 1.47 | +39,082 | 100 | −14,823 | 1.34 | +11,314 | 1.55 | +27,768 |
| sl_cap=0.7 | 155 | 42 | 1.38 | +20,679 | 50 | −6,357 | 1.11 | +2,633 | 1.60 | +18,047 |
| sl_cap=0.8 | 165 | 45 | 1.42 | +23,837 | 56 | −6,357 | 1.21 | +5,316 | 1.58 | +18,521 |
| ext_cap=1.0 | 133 | 44 | 1.47 | +23,819 | 56 | −6,251 | 1.61 | +10,536 | 1.39 | +13,284 |
| ext_cap=1.5 | 160 | 44 | 1.39 | +25,969 | 64 | −8,756 | 1.40 | +9,082 | 1.38 | +16,888 |
| ext_cap=2.0 | 178 | 44 | 1.44 | +33,413 | 86 | −13,094 | 1.40 | +11,512 | 1.48 | +21,901 |
| **noreentrySL** | 176 | 44 | 1.55 | +41,556 | **100** | −12,300 | 1.38 | +11,587 | 1.65 | +29,969 |
| **nf_mom** | 168 | 47 | 1.67 | +44,043 | **101** | −10,187 | 1.60 | +16,040 | 1.72 | +28,004 |
| sl0.8+ext1.5+norSL | 125 | 47 | 1.60 | +23,643 | 44 | −150 | 1.40 | +5,770 | 1.72 | +17,873 |
| **nf_mom + noreentrySL** | 155 | **47** | **1.72** | +44,202 | **98** | −8,662 | 1.56 | +14,698 | 1.83 | +29,504 |

**Why the static caps are rejected:** `sl_cap`/`ext_cap` drive bad-month losses down (even to breakeven
when stacked) but retain only **40–64%** of good-month P&L — they throw away profitable wide-stop /
extended EOD winners. Wide stops are not intrinsically bad; they are only bad in the whipsaw months,
and a static threshold can't tell the two apart. **They fail the ≥90% guardrail.**

**Why `nf_mom` and `noreentrySL` are accepted:** each retains ~100% of good-month P&L, lifts PF and WR,
and **improves both halves** — so they're general entry-quality gates, not a fit to the 4 bad months.
- `noreentrySL` removes 21 net-**negative** re-entries (baseline net +2.5k when removed): doubling down
  on a name that already stopped you out loses money on balance (SUZLON, POLICYBZR, BAJAJ-AUTO,
  JINDALSTEL, PGEL all stopped out on *both* legs in the bad months).
- `nf_mom` skips 29 net-**negative** entries taken while NIFTY had sagged below its 9:30 strength —
  a momentum-confirmation gate.

## 5. Recommended filter — `nf_mom + noreentrySL`

Per-month net (equal-weight fixed; WR shown as wins/n):

| month | BASE | nf_mom | noreentrySL | nf_mom+norSL |
|---|--:|--:|--:|--:|
| 2024-11 | +870 (6/10) | +1577 (5/8) | −578 (4/8) | +938 (4/7) |
| 2024-12 | −2041 (1/6) | −795 (1/5) | −795 (1/5) | −795 (1/5) |
| 2025-01 | +7125 (6/14) | +7739 (5/10) | +7858 (6/13) | +7739 (5/10) |
| 2025-05 | +2064 (4/8) | +3252 (4/6) | +1235 (3/7) | +2423 (3/5) |
| **2025-06★** | −3956 (3/10) | −3971 (3/10) | −3956 (3/10) | −3971 (3/10) |
| **2025-08★** | −2556 (4/11) | −1570 (4/9) | −1559 (4/9) | −1019 (4/8) |
| 2025-10 | +9869 (9/17) | +9493 (8/15) | +9869 (9/17) | +9493 (8/15) |
| 2025-12 | +1100 (6/11) | +1675 (5/9) | +1134 (5/9) | +1937 (5/8) |
| **2026-02★** | −6488 (1/10) | −5965 (2/9) | −4586 (1/7) | −4062 (2/6) |
| 2026-03 | +5621 (8/13) | +3069 (7/11) | +4792 (6/11) | +2239 (5/9) |
| 2026-04 | +3408 (4/12) | +3408 (4/12) | +4503 (4/10) | +4503 (4/10) |
| 2026-05 | −67 (3/9) | +4133 (3/6) | +482 (3/8) | +4133 (3/6) |
| 2026-06 | +8306 (5/11) | +4031 (3/8) | +8133 (4/10) | +4031 (3/8) |
| **2026-07★** | −1822 (4/9) | +1319 (4/6) | −2198 (3/7) | +391 (3/5) |

On the handoff's native **65/35 compounding book**:

| | trades | WR | PF | Sharpe | return | MaxDD | months + |
|---|--:|--:|--:|--:|--:|--:|--:|
| BASELINE | 197 | 45 | 1.45 | 2.59 | +88.6% | −11.8% | 13/21 |
| nf_mom only | 168 | 48 | 1.63 | 3.35 | +100.2% | −10.0% | 16/21 |
| **nf_mom + noreentrySL** | 155 | 48 | **1.68** | **3.42** | **+102.7%** | **−9.5%** | **16/21** |

Bad-month deltas (65/35 book): 25-06 −5.6k→−5.9k · **25-08 −2.5k→−1.2k** · **26-02 −9.1k→−5.8k** ·
**26-07 −3.3k→+1.3k**.

**Honest cost:** `nf_mom` reshuffles *which* good months win — it roughly halves the two biggest
(26-03 +12.0k→+6.7k, 26-06 +13.9k→+3.9k on the compounding book) because its momentum gate skips some
good entries, but it more than pays for it elsewhere (26-05 −1.4k→+7.0k, 26-04 +8.2k→+10.6k, plus the
bad-month repair). Net across all good months ≈ +100% retained, and both halves improve, so the
reshuffle is net-positive and split-robust — but the operator should expect a different month-mix, not
just "same wins + fewer losses."

**Primary lever is `nf_mom`** (single gate, does most of the work). `noreentrySL` is a clean additive
second gate — most of its extra value is the 26-02 repair (−8.8k→−5.8k) and a tighter drawdown.

### 5a. Sizing — fixed vs profit-as-capital (full compounding)

The recommended config (5m · 2.5× · `nf_mom + noreentrySL`) under the three sizing models, ₹60k start:

| sizing model | trades | WR | PF | Sharpe | final ₹ | total | CAGR | MaxDD | mon+ |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| Fixed equal-wt (no reinvest) | 155 | 47 | 1.72 | 3.51 | 104,202 | +73.7% | +39.3% | −6.8% | 16/21 |
| **Compounding equal-wt (profit = capital)** | 155 | 48 | 1.70 | **3.64** | **122,938** | **+104.9%** | **+53.9%** | −8.5% | 16/21 |
| Compounding 65/35 (profit = capital) | 155 | 48 | 1.68 | 3.42 | 121,629 | +102.7% | +52.9% | −9.5% | 16/21 |

- **Reinvesting realized profit** (adding it to the deployable base each day) lifts the 20-month result
  from **+73.7% → +104.9%** (CAGR +39.3% → **+53.9%**), at the cost of a deeper drawdown
  (−6.8% → −8.5%) — the standard compounding trade-off. The equity dips in the bad months (25-06,
  26-02) then recovers to a new high.
- **Equal-weight (50/50) is marginally *better* than the 65/35 tilt** here (final ₹122,938 vs 121,629,
  Sharpe 3.64 vs 3.42, DD −8.5% vs −9.5%), confirming the SPEC's equal-weight default — do not use the
  tilt. Month-end capital (compounding equal-wt): ₹60k → 24-12 60,025 → 25-06 75,823 → 25-12 100,664 →
  26-02 95,862 → 26-07 **122,938**.

## 6. What does NOT get fixed — Jun 2025 (be honest)

`nf_mom`/`noreentrySL` leave 25-06 essentially unchanged (−5.6k → −5.9k). Its losers are single-attempt
trades where NIFTY held ≥ its 9:30 mark **and** the mapped sector index stayed green, but the *stock*
faded: HINDPETRO −831 / BPCL −549 (both NIFTY OIL & GAS, sector green), ADANIGREEN −1391, KALYANKJIL
−739, BAJFINANCE −572. This is the irreducible tail of a delta-1 momentum strategy: no index-level or
re-entry signal distinguishes these from winners. Any "fix" for 25-06 alone (e.g. an oil-sector or
per-name exclusion) would be curve-fitting a single month and is **not** recommended.

## 7. Do-NOT-retest confirmations (re-verified negative here)

- **Static stop-distance cap** (`sl_cap` 0.6–1.0): rejected — retains only 28–56% of good-month P&L.
- **Static extension-from-open cap** (`ext_cap` 1.0–2.0): rejected — 56–86% retention; the only
  variant that keeps ~90% (ext_cap=2.0) barely touches the bad months (−13.1k).
- **Breakout volume multiplier ↑ (2.5× → 3.0×/3.5×): rejected.** Pure selectivity, not quality —
  fewer trades lift WR slightly but return falls ~20pp (65/35 book +88.6%→+69.2% at 3.0×) and
  good-month retention drops to 74%/56%. 2.5× is already near-optimal. (`r53_volmult.py`.)
- **Entry candle size ↑ (5m → 10m/15m): rejected.** Fewer/later/chunkier trades; good-month retention
  collapses to 50%/23%, return +88.6%→+35.2%→+12.5%, and 10m *worsens* drawdown (−15.1%) with a
  non-robust split-half (H1 PF 2.05 vs H2 1.01 — curve-fit red flag). 5m is required by the Wyckoff
  no-supply→breakout mechanism. (`r53_candle.py`.)
- **SHORT / SELL mirror WITH the winning filters: rejected (re-confirmed).** The exact inverse (NIFTY
  down → sector red → 9:30 loss in (−2.5%,−1.0%] → top-2 losers → low-vol green no-supply bounce →
  high-vol red breakdown, SL=green-high) loses money: baseline −18.0% / PF 0.86 / DD −27% over 20mo.
  Adding `nf_mom`(mirrored: NIFTY at entry ≤ 9:30 level) + `noreentrySL` only *reduces the bleed*
  (−18.0%→−6.6%, PF 0.86→0.94) — still PF<1, negative Sharpe, and not split-half robust (H1 PF 1.08 vs
  H2 0.83). The filters can't manufacture an edge that isn't there. **Long-only confirmed.**
  (`r53_short.py`.)
- Band-widening, fresh-1pm re-selection, top-3+, trailing/target exits, 65/35 tilt — not re-tested
  (already settled per handoff / §5a).

**Cross-cutting:** every *frequency-reducing* knob (higher vol gate, bigger candles, static stop/ext
caps) trims the bad months but at a disproportionate cost to the good ones — the bad months are **not**
a subset a coarser signal selectively avoids; they get removed only in proportion to the overall trade
cut. Only the *causal* per-entry gates (`nf_mom`, `noreentrySL`) separate losers from winners cleanly.

## 8. Suggested next tests (operator resumes from here)

1. **Sandbox-measure `nf_mom`'s live behavior** — it depends on the *live intraday* NIFTY reading at
   the entry candle; confirm the aggregator delivers a clean 5m NIFTY series at entry time (same source
   the fresh gate already uses, so low risk).
2. **Slippage stress** — re-run `nf_mom + noreentrySL` at 0.03% / 0.05% per side. It trades **fewer**
   times (155 vs 197 fills) so it is *more* slippage-robust than the baseline; quantify the PF floor.
3. **`nf_mom` threshold sweep** — tested at "≥ 9:30 gain". Try "≥ 9:30 gain − 0.1%" (looser, keeps more
   of 26-03/26-06) and "≥ max(9:30, 11:00)" (stricter) to trade off the good-month reshuffle.
4. **25-06 forensics only if a *general* signal emerges** — e.g. does an intraday sector *breadth*
   reading (constituent-share green) separate the 25-06 oil failures? Only pursue if it also helps ≥1
   other month; otherwise leave 25-06 as irreducible.

## 9. Reproducibility

All in the R53 session scratchpad
`…/e1b84d12-9b6d-4345-8481-1146afb6b9b7/scratchpad` (data caches + winner harness live here; not
committed). Scripts added by this task:
- `r53_dump.py` — winner logic + enriched **per-trade CSV** (`r53_trades.csv`): stock, sector, session
  (morning/1pm), entry/exit price & time, SL/EOD reason, 9:30 gain, NIFTY% & sector% (9:30, at-entry,
  11:00, EOD), stop-distance%, extension-from-open%, VWAP, net.
- `r53_analyze.py` — regime table + 5-hypothesis slices + full bad-month trade dump.
- `r53_filters.py` — the split-half filter sweep (§4).
- `r53_compound.py` — the 65/35 compounding-book comparison (§5).
- `r53_grow.py` — profit-as-capital sizing (fixed vs compounding, equal-wt vs 65/35) (§5a).
- `r53_volmult.py`, `r53_candle.py` — the rejected vol-multiplier & candle-size rounds (§7).
Run from the project root with `uv run python <script>` (needs `services.simplified_stock_engine_core`
for real charges; app must not hold the DBs — these read the committed `prices.duckdb` + scratchpad
`prices_ext7.duckdb` + `real_idx*.parquet`, no live session required).
