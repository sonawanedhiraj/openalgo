# R39 Overnight Research Synthesis — 2026-06-10

**Batch:** A (ZENTEC actual target) · C (vol-regime sector rotation) · D (IV-RV
variance premium) · E (setup library + dynamic allocation)
**Pivot under test:** away from "one strategy fits all" → "find conditional edge
per asset/setup." All four ran the NEW v2 gates (frequency × expectancy, NO
monthly Sharpe / NO monthly-consistency gate, Bonferroni p < 0.05/N).
**Completion:** 4 of 4 reports present and read.

---

## 1. Top-line verdict

**Zero of four cleared the DEPLOY_CANDIDATE gates.** Nothing is promotable.

| Task | Verdict | Why it died |
|---|---|---|
| A — ZENTEC per-stock edge | **REJECT** | 0/115 setup×condition tests cleared all six gates; best sleeves fail n<15 + Bonferroni |
| C — vol-regime sector rotation | **KILL** | single-regime (2022–24) artifact; collapses OOS; profit sits in non-tradable sectors |
| D — IV/RV variance premium | **REJECT** | weekly options don't exist in the data; IV/RV>1.5 fires ~2×/4.4yr; zero 2026 signal |
| E — setup library / dynamic alloc | **PIVOT (lean kill)** | no variant beats all three baselines; dynamic allocator loses to one stable single setup |

The gates did their job: they rejected four thin, regime-bound, or
under-powered edges that older monthly-Sharpe framing might have flattered.

## 2. Best discovery

**SECTOR_FOLLOW, surfaced as a by-product of Task E** — the strongest
positive-EV pattern in the batch, even though it was never the thing under test.

- Spec: sector index up >1% **and** stock up >0.5% on >1× volume → enter, exit
  T+1 close.
- **+0.45% per trade after costs across 711 trades** over 2024-10 → 2026-06.
- Monthly Sharpe **3.12** — the best risk-adjusted profile in the entire batch.
- Critically, it produced this **while NIFTY fell 9.2%** over the same window.
  Not a bull-tape artifact.

It is not yet a DEPLOY_CANDIDATE (E tested the *allocator*, not this sleeve
standalone, and the single-setup benchmark is mildly flattered by no overlap
modeling / no position cap), but it is the one genuinely robust, high-frequency,
cost-surviving signal the night produced. **This is the lead.**

Runner-up: **ZENTEC as a breakout/momentum name** (Task A). Its volume-breakout
sleeve (S5) shows +3.40% EV / Kelly +0.39 / WR 58%, and S3_sector + secRS20>0
shows +5.47% EV / Kelly +0.64 / p=0.015. Real-looking edge, but only 8–14 trades
— under-powered, not disproven.

## 3. Side-by-side comparison

| Task | Best variant | Trades | EV/trade (test) | WR | Payoff vs breakeven | 2026-YTD | Best p | Bonferroni bar | Pass? |
|---|---|---|---|---|---|---|---|---|---|
| **A** ZENTEC | S3_sector+secRS20>0 | 12 | +5.47% | 75% | 2.31 vs 0.33 ✅ | +35.7% | 0.015 | 0.00043 | ❌ (n<15, p) |
| A base | S5_volbreak | 12 | +3.40% | 58% | 2.18 vs 0.71 ✅ | +6.7% | 0.120 | 0.00043 | ❌ (n<15, p) |
| **C** vol-regime | B (top-33% RV) | 181 | +0.34% | 54% | ✅ | +₹19.5k | 0.072 | 0.0125 | ❌ (EV<0.5%, p) |
| **D** VRP | A (IV/RV>1.5) | 2 | +2.75%/margin | 50% | 4.41 ✅ | 0 entries | — | — | ❌ (freq, n, 2026) |
| **E** setup lib | V_SLIB_A | 27 | +1.70% | 67% | 1.32 vs 0.50 ✅ | + | 0.0255 | 0.0167 | ❌ (freq, p) |
| E reference | SECTOR_FOLLOW (single) | 711 | +0.45% | — | — | — | — | — | not gated as variant |

Pattern in the failures: **payoff/Kelly is almost always positive — the wall is
either sample size (n) or Bonferroni significance, never the economics.** Every
attractive conditional sleeve is a 8–30-trade specimen over a single 2024–26
regime.

## 4. Cross-cutting insight — conditional edge vs single strategy

The batch was designed to validate "no single strategy has edge all the time →
allocate conditionally." **The evidence points the other way.**

- The **conditional / per-stock** edges (A's ZENTEC sleeves, C's regime-gated
  bounces) all died on n<15 and Bonferroni. Conditioning slices the data so
  thin that nothing survives multiple-testing correction. The edges *look* real
  per-cell but are statistically indistinguishable from selection noise.
- The **dynamic cell-switching allocator** (E) was *strictly better than naive
  equal-weight* and *crushed NIFTY*, but **lost to simply running the single
  best stable setup** (SECTOR_FOLLOW, Sharpe 3.12 vs allocator's 1.74). The more
  cells it chased (N=10), the worse it got. Trailing-3M EV is too sparse and
  noisy to out-select a stable standalone edge.

So the operative lesson is almost the inverse of the hypothesis: **a single,
stable, high-frequency setup beat every clever conditional construction.** The
conditional framing's failure mode is statistical — it manufactures fragile
small-n cells faster than it finds durable edge. Where real edge exists
(SECTOR_FOLLOW), it's broad and unconditional, not a narrow per-asset slice.

## 5. Honest negative findings — what definitely did not work

1. **Broad sector mean-reversion (C).** Fourth independent confirmation
   (R30/31/32/33/39) that high-vol oversold-bounce edge lives only in the
   2022–24 stress regime and evaporates OOS (variant A: +0.92% train →
   −0.11% test, WR 68→49%). Worse, the profit concentrates in REALTY / ENERGY /
   OIL&GAS — sectors with **no tradable ETF**; the genuinely tradable ETF
   sectors were net-negative. Disqualified on instrument reality alone.
2. **Weekly variance-risk-premium (D).** The instrument literally **does not
   exist** in our data — `index_options_eod` holds only monthly expiries. The
   real IV/RV premium is modest (median 1.14, not the synthetic 1.10×+ the
   thesis assumed); IV/RV>1.5 fires ~twice in 4.4 years and gave **zero 2026
   signals**. The edge is "did spot pin near ATM," which IV/RV does not predict.
3. **The "defence-PSU proxy" assumption (A).** BDL (the R39 proxy) is a
   **mean-reverter** (+1.9% EV on dips); ZENTEC, the actual name, is a
   **breakout** name where mean-reversion bleeds −2% EV. Same sector, opposite
   micro-structure. The proxy got the deploy verdict right but the *shape* of
   the edge completely wrong. **Retire sector proxies for per-stock work.**
4. **MEAN_REVERSION_OS never fired once (E)** across 30 names / 2.4 years —
   the "6-setup library" is really 5.

## 6. Recommended next 1–3 experiments

1. **Promote SECTOR_FOLLOW to a standalone-sleeve stress test (highest
   priority).** Re-run it with proper position sizing, an overlap-aware capital
   curve (it currently assumes no position cap), explicit regime splits, and an
   OOS extension as deeper daily history accrues. This is the one signal worth
   the next backtest round. Rationale: only robust, high-n, cost-surviving,
   regime-independent edge found.
2. **PANEL test the breakout/sector-momentum sleeve across several defence
   names (BEL/HAL/MAZDOCK/ZENTEC together).** Pool trades to clear n≥15 and the
   Bonferroni bar, which single-name ZENTEC structurally cannot (it's a
   ~6–12 trade/yr setup). Rationale: A's residual edge is real-looking but
   under-powered; pooling is the honest way to test it.
3. **(Optional, low priority) Scope unconditional monthly premium selling with
   wider wings (±300/±400) as its own round — R37 territory, not VRP.** The ±200
   wings were the killer in D (7 of 9 losers pinned at max loss). Only if there
   is appetite; do not chase it on synthetic premiums.

## 7. Risk note — before market open (09:15 IST)

- **No config was touched.** atr_sl_mult, daily_intent, VETO_LAYER_MODE,
  SIMPLIFIED_ENGINE_MODE all unchanged. No trades placed. Read-only on
  services/, broker/, database/, blueprints/. No pytest run.
- **Nothing from this batch is going live.** All four are REJECT/KILL/PIVOT. The
  live simplified engine and any scheduled scans are unaffected by this work.
- **OpenAlgo + bridge were stopped only for the git push** and restarted
  immediately after. System-state confirmation (up/down) is in the morning
  message — **check it before 09:15.** If either failed to come back, the
  message will say so explicitly.
- Task C noted the live DuckDB (`historify.duckdb`) was under an exclusive
  Windows lock by the running OpenAlgo process overnight; it used a cached
  snapshot. Nothing to action, but worth knowing the live DB was never the
  read source for C.

---

*Source reports: `outputs/r39a_zentec_actual_2026-06-10/report.md`,
`outputs/r39c_vol_regime_sector_2026-06-10/report.md`,
`outputs/r39d_vrp_2026-06-10/report.md`,
`outputs/r39e_setup_library_2026-06-10/report.md`. Synthesis is read-only on all
four; no source artifacts modified.*
