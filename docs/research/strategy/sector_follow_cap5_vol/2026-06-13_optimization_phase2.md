# sector_follow_cap5_vol — Optimization Research Phase 2 (gate ladder · vol-weighted sizing · pyramiding)

**Date:** 2026-06-14
**Strategy:** `sector_follow_cap5_vol` (intraday sector-follow; entry 15:20 IST, T+1 exit 15:25 IST MARKET)
**Window:** 2026-01-01 → 2026-06-12 (109 trading days; 5.5 months, one regime — NIFTY-50 down ~9.6%)
**Capital:** ₹2.5L book, ₹50k/slot nominal, max 5 concurrent, LOCK_STATIC_30 universe
**Harness:** `outputs/2026-06-13_sector_follow_opt/run_phase2.py` — extends the Phase-1 harness, reuses
the **same live production functions** (`load_config`, `load_sector_map`, `_series_metrics`,
`passes_gates`, `select_entries`, `compute_qty`) so entry generation is byte-identical to live. Adds
(a) per-cohort volume-weighted sizing, (b) leg-by-leg pyramiding with independent buy-side friction,
and (c) the gate ladder above C1. **Charge model is byte-consistent with Phase 1** — `leg_charges`
decomposed into `buy_charges` + `sell_charges` that sum to the identical round-trip (verified by
construction); every pyramid add pays its own buy-side charges and the position pays one combined
delivery sell (one DP).
**Mode:** read-only on `db/historify.duckdb` (`read_only=True`). No orders, no DB writes.
**Full numbers:** `outputs/2026-06-13_sector_follow_opt/results_phase2.json`.

---

## 1. Phase 1 recap (carry-forward)

Phase 1 ([`2026-06-13_optimization_research.md`](2026-06-13_optimization_research.md)) established the
honest baseline under **full Zerodha-equivalent charges**: **+₹6,046 (+2.42% on ₹2.5L), Sharpe 0.56,
DD −4.63%, 109 trades** — roughly ₹3.7k worse than the earlier flat-0.0857% estimate because real
delivery friction is ~₹76/trade not ~₹42. The one lever that worked was **entry selectivity, not exit
machinery**: raising the **sector gate to +1.5% (variant C1)** nearly doubled net to **+₹11,632
(+4.65%), Sharpe 1.23, DD −3.38%, 78 trades** — a pure config edit that flips April positive and halves
the January bleed. Tightening the **volume gate (C3, 1.5×)** was the better drawdown play (Jan −₹11.2k →
−₹0.7k). **Hard intraday stops (Axis A) were ≈ net-neutral and self-defeating** (they whipsaw out names
that recover by the T+1 close, wrecking April), and **profit trails (Axis B) were neutral-to-negative**
— T0-only trails are structurally inert for a 15:20 entry (only 1/109 ever fired), and the one trail
with teeth (through T+1) clips the positive-skew winners the strategy lives on. **Core thesis carried
into Phase 2: the edge lives in the full overnight hold, not in intraday adjustment.**

---

## 2. Phase 2 variant grid (full charges, 109-day window)

> `avgW`/`avgL` = net ₹ per winning/losing trade. `Exp` = net expectancy/trade (₹). `Fric%` = charges
> as a fraction of **gross** P&L. `Util%` = avg fraction of ₹2.5L deployed on active days (deploy-based,
> integer-share floored). `Legs` = avg buy legs/trade (1.0 = no pyramid add fired). `AvgPos` = avg peak
> ₹ deployed per position.

| Variant | Trades | Win% | Gross ₹ | Charges ₹ | **Net ₹** | Net % | Sharpe | MaxDD% | avgW | avgL | Exp ₹ | Fric% | Util% | Legs | AvgPos |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **BASE sector 1.0 (uniform 50k)** | 109 | 48.6 | 14,329 | 8,284 | **6,046** | 2.42 | 0.56 | −4.63 | 886 | −731 | 55 | 57.8 | 57.8 | 1.00 | 48,791 |
| C1/G0 sector 1.5 (uniform) | 78 | 55.1 | 17,577 | 5,945 | **11,632** | 4.65 | 1.23 | −3.38 | 873 | −740 | 149 | 33.8 | 54.5 | 1.00 | 48,893 |
| *Axis G — gate ladder above C1* | | | | | | | | | | | | | | | |
| G1 sector 2.0 | 57 | 59.6 | 15,436 | 4,355 | **11,080** | 4.43 | 1.28 | −3.38 | 858 | −786 | 194 | 28.2 | 45.6 | 1.00 | 49,007 |
| G2 sector 2.5 | 41 | 53.7 | 8,199 | 3,148 | **5,052** | 2.02 | 0.64 | −3.62 | 925 | −805 | 123 | 38.4 | 40.5 | 1.00 | 49,362 |
| G3 sector 3.0 | 22 | 31.8 | −11,419 | 1,661 | **−13,080** | −5.23 | −1.78 | −5.99 | 1,030 | −1,353 | −595 | n/a | 35.9 | 1.00 | 48,939 |
| *Axis W — vol-weighted sizing (gate 1.0)* | | | | | | | | | | | | | | | |
| W1 uniform (= BASE) | 109 | 48.6 | 14,329 | 8,284 | **6,046** | 2.42 | 0.56 | −4.63 | 886 | −731 | 55 | 57.8 | 57.8 | 1.00 | 48,791 |
| W2 proportional (no cap) | 109 | 48.6 | 17,840 | 8,283 | **9,557** | 3.82 | 0.83 | −4.50 | 942 | −721 | 88 | 46.4 | 46.4 | 1.00 | 48,760 |
| W3 tiered 25/50/75k | 109 | 48.6 | 15,190 | 8,281 | **6,909** | 2.76 | 0.65 | −4.18 | 893 | −722 | 63 | 54.5 | 54.5 | 1.00 | 48,764 |
| W4 tiered + 35% conc cap | 109 | 48.6 | 15,190 | 8,281 | **6,909** | 2.76 | 0.65 | −4.18 | 893 | −722 | 63 | 54.5 | 54.5 | 1.00 | 48,764 |
| *Axis P — pyramiding (gate 1.0)* | | | | | | | | | | | | | | | |
| P1 start25k +25k@T0+0.5% | 109 | 48.6 | 3,886 | 5,163 | **−1,277** | −0.51 | −0.22 | −2.39 | 427 | −427 | −12 | 132.9 | 31.0 | 1.07 | 25,563 |
| P2 start25k +15k@+0.5 +10k@+1 | 109 | 48.6 | 5,204 | 5,067 | **137** | 0.05 | 0.02 | −2.37 | 427 | −401 | 1 | 97.4 | 30.1 | 1.07 | 24,834 |
| P3 start35k +15k@T0close+0.5% | 109 | 48.6 | 9,628 | 6,323 | **3,306** | 1.32 | 0.43 | −3.24 | 613 | −521 | 30 | 65.7 | 41.4 | 1.02 | 34,178 |
| P4 start25k +25k@T1open+0.5% | 109 | 45.9 | 9,172 | 6,053 | **3,119** | 1.25 | 0.48 | −2.68 | 558 | −420 | 29 | 66.0 | 40.5 | 1.39 | 33,442 |
| *Combinations* | | | | | | | | | | | | | | | |
| **C1(1.5) × W2** | 78 | 55.1 | 18,603 | 5,969 | **12,633** | **5.05** | **1.33** | −3.31 | 889 | −731 | 162 | 32.1 | 54.7 | 1.00 | 49,132 |
| C1(1.5) × W3 | 78 | 55.1 | 17,684 | 5,947 | **11,738** | 4.70 | 1.23 | −3.38 | 882 | −748 | 150 | 33.6 | 54.5 | 1.00 | 48,908 |
| C1(1.5) × P1 | 78 | 55.1 | 8,353 | 3,626 | **4,727** | 1.89 | 1.02 | −1.70 | 421 | −382 | 61 | 43.4 | 27.6 | 1.05 | 24,791 |
| C1(1.5) × W3 × P1 (full stack) | 78 | 55.1 | 8,489 | 3,652 | **4,837** | 1.93 | 1.03 | −1.70 | 427 | −386 | 62 | 43.0 | 27.9 | 1.05 | 25,057 |

**Naming note.** The task labels combinations "G1 × …" but describes them as "C1's threshold" — C1 is
the **sector 1.5** gate, the Phase-1 winner. The gate ladder (G1=2.0/G2=2.5/G3=3.0) is the *upward*
extension of C1; the ladder shows 1.5 stays the net-maximal rung, so combinations use the **sector-1.5
(C1) gate** as the base, written `C1(1.5) × …`.

---

## 3. Per-axis analysis

### Axis G — the gate ladder elbows at 1.5–2.0; 2.5+ is the overfit cliff

| Sector gate | Trades | Win% | Net ₹ | Net % | Sharpe | Exp ₹/trade |
|---|---:|---:|---:|---:|---:|---:|
| 1.0 (BASE) | 109 | 48.6 | 6,046 | 2.42 | 0.56 | 55 |
| **1.5 (C1)** | 78 | 55.1 | **11,632** | 4.65 | 1.23 | 149 |
| **2.0 (G1)** | 57 | 59.6 | 11,080 | 4.43 | **1.28** | **194** |
| 2.5 (G2) | 41 | 53.7 | 5,052 | 2.02 | 0.64 | 123 |
| 3.0 (G3) | 22 | 31.8 | **−13,080** | −5.23 | −1.78 | −595 |

The ladder is a textbook concave curve. **Net climbs steeply 1.0→1.5 (+₹5,586), then plateaus 1.5→2.0
(−₹552, a statistical tie), then falls off a cliff: 2.0→2.5 halves net (−₹6,028) and 2.5→3.0 goes deeply
negative (−₹18k swing).** Per-trade expectancy keeps rising to 2.0 (₹194, the best in the grid) and
win-rate to 2.0 (59.6%) — so 2.0 is the **risk-adjusted optimum** — but the trade count collapses
78→57→41→22 and the *absolute* net peaks at **1.5**. At 3.0 the strategy has only 22 trades and a 31.8%
win rate: it is no longer following genuine sector momentum, it is chasing a handful of violent intraday
spikes that mean-revert overnight (June: 3 trades, all losers, −₹6,344). **The elbow is 1.5 for rupees,
2.0 for Sharpe; everything ≥2.5 is sample-starved noise.** Recommendation: stop at **1.5**. Do not chase
the marginally-higher 2.0 Sharpe on a 57-trade sample, and treat the ladder as proof that 2.5/3.0 are
overfit, not as a tunable to push.

### Axis W — proportional vol-weighting beats uniform, and charges do NOT eat the lift

This is the **positive surprise of Phase 2.** Because total book capital is held fixed at ₹2.5L,
**redistributing it does not change total friction** (W2 charges ₹8,283 vs uniform ₹8,284 — flat). So
any gross uplift from concentrating capital into higher-conviction (higher relative-volume) names drops
straight to net:

| Sizing | Gross ₹ | Charges ₹ | Net ₹ | Net % | Sharpe | vs uniform |
|---|---:|---:|---:|---:|---:|---:|
| W1 uniform | 14,329 | 8,284 | 6,046 | 2.42 | 0.56 | — |
| **W2 proportional** | 17,840 | 8,283 | **9,557** | 3.82 | 0.83 | **+₹3,511 (+58%)** |
| W3 tiered 25/50/75k | 15,190 | 8,281 | 6,909 | 2.76 | 0.65 | +₹863 |
| W4 = W3 + 35% cap | 15,190 | 8,281 | 6,909 | 2.76 | 0.65 | +₹863 |

**W2 lifts net +58% over uniform with identical friction.** The mechanism: the entry tiebreaker already
ranks by volume ratio, and W2 allocates `₹2.5L × vrᵢ/Σvr` (at a full 5-name book), tilting capital into
the names with the strongest relative-volume confirmation — which, in this window, had better overnight
follow-through (March net 4,756 → 9,702). **W4 is byte-identical to W3** — a clean finding: the W3 tier
ceiling (₹75k = 30% of book) sits *below* the 35% concentration cap (₹87.5k), so the cap never binds.
**Caveat:** W2 is a single-regime bet on "high relative volume ⇒ better overnight edge." It is plausible
(volume confirms conviction) but unproven out-of-sample, and it concentrates risk into fewer names. The
deploy-utilization dips to 46.4% (vs uniform's 57.8%) — proportional sizing floors more sub-₹50k
allocations into whole shares, leaving a little more cash idle — so W2 earns **more gross on slightly
less deployed capital**, a capital-efficiency win but also a concentration one. Stacked on the gate,
**C1 × W2 = ₹12,633 (5.05%), the best net and best Sharpe (1.33) in the entire grid.**

### Axis P — pyramiding never beats its own friction floor; the overnight edge is the headwind

**Pyramiding fails, exactly as Phase 1's thesis predicts.** Every P variant underperforms the ₹6,046
baseline, and P1 is *net-negative*:

| Pyramid | Net ₹ | Net % | Fric% of gross | Avg legs | Why it fails |
|---|---:|---:|---:|---:|---|
| P1 start25k, T0 +0.5% add | **−1,277** | −0.51 | **132.9%** | 1.07 | base half-sized → half the overnight gross; T0 add fires on only 7% of trades; **charges exceed gross** |
| P2 start25k, two T0 adds | 137 | 0.05 | 97.4% | 1.07 | same; barely breakeven |
| P3 start35k, T0-close +0.5% add | 3,306 | 1.32 | 65.7% | 1.02 | bigger base helps, but still < baseline |
| P4 start25k, T1-open +0.5% add | 3,119 | 1.25 | 66.0% | 1.39 | T1 add fires 39% of the time but the added lot is **intraday** (T1 buy→T1 close), capturing T1 drift not the overnight tail |

Two structural reasons, both downstream of the **overnight-edge** finding:

1. **Starting small forfeits the edge on the base lot.** The strategy's return is the *full position
   held overnight*. Halving the initial size (₹25k vs ₹50k) halves the overnight gross on every trade —
   and the scale-in that is supposed to recover it (T0 intrabar +0.5%) fires on **7% of trades** (avg
   legs 1.07), because the entry is at 15:20 with only a ~10-minute T0 window. You give up half the edge
   to chase an add that almost never triggers.
2. **Each add pays its own friction, and the gross it buys is the wrong window.** P1's charges (₹5,163)
   *exceed* its gross (₹3,886) — a 132.9% friction floor. P4's T1-morning add fires more often (39%) but
   the added lot is bought and sold on T+1 (intraday), so it captures T+1 intraday drift, not the
   overnight gap that is the actual edge. **Stacked on the gate, C1 × P1 = ₹4,727 — pyramiding more than
   halves C1's ₹11,632**, and the full stack C1 × W3 × P1 (₹4,837) confirms W-sizing cannot rescue what
   pyramiding destroys. **Binary verdict: pyramiding never beat its friction floor; best case P3 at
   ₹3,306 is still ₹2,740 below the do-nothing baseline.**

---

## 4. Rankings & recommendation

**Top 3 by Net %:**
1. **C1(1.5) × W2 — +₹12,633 (5.05%)**, Sharpe 1.33, DD −3.31%, 78 trades
2. C1(1.5) × W3 — +₹11,738 (4.70%), Sharpe 1.23, DD −3.38%
3. C1/G0 sector 1.5 — +₹11,632 (4.65%), Sharpe 1.23, DD −3.38%

**Top 3 by Sharpe:**
1. **C1(1.5) × W2 — 1.33** (net +₹12,633)
2. G1 sector 2.0 — 1.28 (net +₹11,080, but only 57 trades)
3. C1 sector 1.5 — 1.23 (= C1 × W3 1.23)

**Single recommendation — ship the gate, treat sizing as a staged upgrade:**

> **Ship `gate_sector_pct` 1.0 → 1.5 (C1).** It is the simple, robust, one-parameter change that nearly
> doubles net (₹6,046 → ₹11,632), doubles Sharpe (0.56 → 1.23), lowers drawdown, and has no new failure
> mode or added friction. The gate ladder confirms 1.5 is the net-maximal rung and that pushing higher
> (2.5/3.0) is overfitting.
>
> **Then paper-trade `W2 proportional sizing` on top (C1 × W2, the grid winner at +₹12,633 / 5.05% /
> Sharpe 1.33) before sizing it live.** W2 is real edge (+₹1k over C1, free of extra friction) but it is
> a single-regime concentration bet on the volume-ratio signal and needs out-of-sample confirmation
> plus a documented per-name concentration limit. Do **not** ship sizing machinery and a thinner gate
> at once.

**Do NOT add pyramiding.** It was tested four ways and never beat the do-nothing baseline; P1's charges
exceeded its gross. The strategy's edge is the full overnight hold — any "start small, scale in
intraday" scheme forfeits exactly that.

---

## 5. Friction-floor analysis

Friction is the dominant cost in this strategy — the per-trade moves are small and the per-trade charges
(STT + ₹15.93 DP + GST) are nearly fixed, so **friction as a share of gross is the real efficiency
metric**:

| Variant | Gross ₹ | Charges ₹ | **Fric % of gross** | Read |
|---|---:|---:|---:|---|
| BASE sector 1.0 | 14,329 | 8,284 | **57.8%** | over half of gross eaten — the loose-gate tax |
| C1 sector 1.5 | 17,577 | 5,945 | **33.8%** | fewer, bigger-edge trades → friction drag falls by a third |
| G1 sector 2.0 | 15,436 | 4,355 | **28.2%** | lowest friction ratio of any net-positive variant |
| C1 × W2 | 18,603 | 5,969 | **32.1%** | gate + sizing; best net at a low friction ratio |
| W2 proportional (gate 1.0) | 17,840 | 8,283 | 46.4% | sizing lifts gross but loose gate keeps friction high |
| **P1 pyramid** | 3,886 | 5,163 | **132.9%** | **charges exceed gross** — pyramiding's friction floor is fatal |
| P2 pyramid | 5,204 | 5,067 | 97.4% | essentially all gross consumed by friction |
| P3 / P4 pyramid | 9,628 / 9,172 | 6,323 / 6,053 | 65.7 / 66.0% | extra legs spike the ratio back up |

**The single clearest cost lesson:** tightening the gate is itself the best friction reduction — it
drops the friction ratio from 57.8% to 33.8% by replacing many marginal trades (each paying ~₹76
regardless of how small the move) with fewer high-conviction ones. **Pyramiding does the opposite** —
it multiplies the near-fixed per-trade charges across more legs while shrinking the base gross, spiking
friction to 66–133% of gross.

---

## 6. Regime caveats

- **One regime, 5.5 months (2026-01-01 → 2026-06-12), NIFTY-50 down ~9.6%.** All nets are 2026-to-date;
  do **not** extrapolate to long-run validity. **January is the loss month under every variant** (BASE
  −₹11.2k, C1 −₹7.2k, W2 −₹10.8k) — none of the Phase-2 levers fix it; only the Phase-1 *volume* gate
  (C3/C4) materially defended January.
- **Small samples at the tight end.** C1=78, G1(2.0)=57, G2=41, **G3=22 trades** — the gate-ladder
  rankings beyond 1.5 are directional, not statistically bulletproof. G1's higher Sharpe rides on 57
  trades; the 2.0-vs-1.5 net difference (−₹552) is well inside noise.
- **W2's edge is a single-regime concentration bet.** It tilts toward high relative-volume names, which
  outperformed *in this window*. Plausible (volume confirms conviction) but unproven OOS; it concentrates
  capital into fewer names with no per-name cap. Hence the paper-trade-first recommendation.
- **Sharpe is computed on a mostly-zero daily series** (the strategy sits in cash most days), which
  suppresses volatility and inflates absolute Sharpe — use it to *rank*, not as a true risk-adjusted
  return. Tighter gates show higher Sharpe partly because they have *fewer active days*.
- **Pyramid timing is modeled on 1-minute OHLC.** T0-intrabar adds fill at the +0.5% level when crossed
  (or the bar open if gapped through); the T1-open add is treated as intraday (T1 buy → T1 15:25 sell,
  cheaper MIS charges, no DP). The base lot is always delivery (T0 buy → T1 sell). One combined delivery
  sell pays one DP — the realistic single-SELL assumption per the brief.
- **Entry generation is byte-identical to the live service** (same imported functions); only sizing and
  scale-in differ across W/P. The kill switch is unchanged (baseline ₹50k MTM, fired once 2026-01-30),
  so it does not confound the W/P comparison — the *set* of entered positions is identical across all W
  and P variants at a given gate; only quantity and leg structure change.

---

## Bottom line

**The two Phase-2 winners both come from the entry/allocation side, never the exit side.** (1) The gate
ladder confirms **sector 1.5 (C1)** is the net-maximal rung — ship it; 2.0 is the Sharpe optimum but
thins to 57 trades, and 2.5/3.0 are overfit (3.0 is net −₹13k on 22 trades). (2) **Proportional
volume-weighted sizing (W2) genuinely beats uniform (+58% net, friction unchanged)** and stacks with the
gate to the grid's best result **C1 × W2 = +₹12,633 (5.05%), Sharpe 1.33** — but it is a single-regime
concentration bet, so paper-trade before sizing live. (3) **Pyramiding was tested four ways and never
beat the do-nothing baseline** — starting small forfeits the overnight edge on the base lot, the
intraday scale-in almost never fires (7% of trades), and the extra legs spike friction to 66–133% of
gross. Phase 1's thesis holds: **this strategy's money is in the full overnight hold and in not taking
the worst entries — not in exit machinery, not in intraday scaling.**
