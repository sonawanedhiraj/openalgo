# R37 — Defined-Risk Monthly Index Premium Selling (REAL bhavcopy premiums)

**Date:** 2026-06-09 · **Verdict: REJECT across all 6 variants.** Not a structural
edge problem — a **frequency-starvation** problem. The premium-selling edge (IV > RV)
*is* visible at the trade level, but the spec's entry gates fire so rarely (1–6 trades
over the 23-month OOS) that the strategy cannot reach any deploy bar. Real
NIFTY/BANKNIFTY monthly CE+PE premiums from `index_options_eod` (1.64M rows,
2022–2026), faithful 4-leg costs, per-side condor management.

---

## 1. TL;DR

- **No deployable variant.** Best OOS is **B/F** (NIFTY+BANKNIFTY iron condors, 0.16Δ):
  Sharpe 0.25, WR 50%, **N = 4**, green months **8.7%**, return +0.55% over ~2 years.
  Every gate (N≥18, green%≥60, Sharpe≥1.3) is missed by a wide margin.
- **The binding constraint is the 25–30 DTE entry window.** Over 476 OOS trading days,
  142 pass vix≥60, 49 also pass the range-bound regime, but only **9** also have a
  monthly expiry sitting in 25–30 DTE. After OI + sizing, **6** become positions.
  "High realized-vol percentile **AND** range-bound regime **AND** a 6-day-per-month
  DTE window" almost never co-occur.
- **Premium selling works per-trade, fails per-portfolio.** Of the ICs that aren't
  breached, nearly all hit the 50% profit target. But N is too small for green-month
  consistency, and ~40% of trades have a short tested → breach losses.
- **The one real success mode is event vol-crush capture** (NIFTY entered 2024-05-30
  pre-election, +₹6,816 as IV collapsed after the June-4 result). That is an
  opportunistic event trade, not a systematic monthly condor program.
- **Does NOT become a 5th sleeve.** V_BLD_B (Sharpe 1.41, 70% green, payoff 1.67)
  dominates on every axis. **REJECT, structurally.**

---

## 2. Variant matrix (OOS 2024-01-01 → 2025-11-30, 30 bps/leg)

| V | Universe / knobs | N | Sharpe | WR% | Payoff | Green% | MaxDD% | Ret% | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| **A** | NIFTY only, 0.16Δ, vix≥60 | 2 | 0.33 | 50 | 2.0 | 4.3 | −0.67 | +0.68 | REJECT |
| **B** | +BANKNIFTY, 0.16Δ, vix≥60 | 4 | 0.25 | 50 | 1.51 | 8.7 | −0.67 | +0.55 | REJECT |
| **C** | looser: vix≥50, 0.20Δ | 6 | 0.25 | 33 | 2.81 | 8.7 | −0.80 | +0.57 | REJECT |
| **D** | tighter: vix≥75, 0.10Δ | 1 | 0.74 | 100 | ∞ | 4.3 | 0.0 | +0.72 | REJECT (N=1) |
| **E** | higher risk: 0.30Δ | 4 | 0.51 | 50 | 2.35 | 8.7 | −0.19 | +0.26 | REJECT |
| **F** | adaptive IC/BPS/BCS | 4 | 0.25 | 50 | 1.51 | 8.7 | −0.67 | +0.55 | REJECT |

4-year (2022–2026) trade counts: A=4, B=8, C=11, D=1, E=9, F=8. **Max N anywhere = 11**
(C, over 4 full years) vs the N≥18 DEPLOY / N≥12 PROMISING bars. Every variant is
net-positive at both 30 bps and 60 bps — but on negligible capital deployment.

---

## 3. The 5 critical experiments

**1. Delta sweep — what's the optimal short delta?**
Indeterminate, because all deltas are throttled by N. Directionally: **0.10Δ (D)**
is starved to 1 trade (credit too small to clear the 0.20 credit/width floor under
the vix≥75 gate); **0.30Δ (E)** books more credit but flips the distribution to a
dangerous **left tail** (skew −3.59; its 2026-01-28 BANKNIFTY condor lost −₹4,383
when both shorts were breached, and E's 2026 sanity is **negative**); **0.20Δ (C)**
trades most (11) but has the **lowest WR (33%)**. **0.16Δ (B)** is the least-bad
balance and is the only delta that keeps a *right*-skewed (safe) distribution. No
delta engineers the target 80–85% WR with usable N.

**2. Single (A) vs dual (B) — does the correlation cap manage risk?**
Yes, mechanically: the corr-cap = 1 net short-vega unit holds (never >1 concurrent
position; verified in the trade log). Dual doubles N (2→4 OOS, 4→8 over 4 years) by
adding BANKNIFTY condors. But it cannot fix frequency starvation — 8 trades in 4
years is still unusable.

**3. Static (B) vs adaptive (F) — does regime adaptation help?**
**No — F is bit-for-bit identical to B** (same 8 trades, same P&L). On every day that
clears vix≥60 + event + the 25–30 DTE window, the regime is range-bound → IC is
selected. The trend structures (bull-put / bear-call) **never fire**, because a
high-realized-vol *trending* day plus an in-window monthly expiry never co-occurred.
Adaptation adds zero.

**4. VIX-spike stress (India VIX not on disk → RV20-percentile proxy + known events):**
| Episode | What happened | Result |
|---|---|---|
| 2022-Mar (COVID echo / Ukraine) | gates never armed; no in-window expiry | **no trade** |
| 2022-05/06 (drawdown) | BANKNIFTY IC, put short breached | −₹2,279 (B) |
| **2024-Jun (election)** | NIFTY IC entered 05-30 pre-result, IV crush after | **+₹6,816 (B)** ← best trade |
| 2024-Oct (correction) | BANKNIFTY IC 10-30, both sides 50% | +₹1,347 (B) |
| 2026-01 (vol spike) | BANKNIFTY IC 01-28 | +₹1,975 at 0.16Δ (B) / **−₹4,383 at 0.30Δ (E)** |
The seller's worst enemy (a sharp vol expansion that breaches a short) shows up at
**2026-01 / 0.30Δ** and **2022-05**; the seller's dream (a vol-crush into a pinned
expiry) shows up at **June-2024**. Absolute drawdowns are tiny only because sizing
never exceeds 1 spread (see §7 SPAN caveat).

**5. Tail-loss distribution — is there a single 6%+ month?**
No. Worst single month = **−0.88%** (E), worst quarter = **−0.88%**. But this "pass"
is an artifact of 1-spread sizing and tiny N, not of genuine tail safety. At 0.16Δ the
defined-risk structure is actually **right-skewed** (the +₹6,816 election win is the
fat tail, on the *win* side: B 4-yr skew +2.74). At 0.30Δ it inverts to left-skew.

---

## 4. Hand-validated trade (to the rupee)

**NIFTY iron condor, entered 2024-05-30 (28 DTE), expiry 2024-06-27.** Spot 22,488.
Sized 1 spread = 65 qty. Managed **per side** (each side closes on its own day).

| Leg | Type | Strike | Entry close | Action |
|---|---|---|---|---|
| short put | PE | 21100 | 134.40 | sell-to-open |
| long put | PE | 20850 | 43.15 | buy-to-open |
| short call | CE | 24000 | 114.60 | sell-to-open |
| long call | CE | 24250 | 88.55 | buy-to-open |

Credit = (134.40−43.15) + (114.60−88.55) = **91.25 + 26.05 = 117.30 pts.**

- **Put side** exits **2024-06-03** (V = 50.60−43.15 = 7.45 ≤ 0.5×91.25 → profit_50):
  gross = (91.25−7.45)×65 = ₹5,447.00; 4-leg charges (incl. 30 bps/leg slip) = ₹161.69;
  **put net = ₹5,285.31.**
- **Call side** exits **2024-06-05** (V = max(36.90−40.00,0) = 0 → profit_50; spread
  value clamped ≥0, a conservative ₹201 give-up): gross = 26.05×65 = ₹1,693.25;
  charges = ₹162.75; **call net = ₹1,530.50.**
- **Total: gross ₹7,140.25 − charges ₹324.44 (of which slippage ₹107.51) = NET
  ₹6,815.81.** Engine reports **₹6,815.81** — exact match. Held-ITM-at-expiry audit:
  **0** across all 6 variants (time-stop DTE≤8 always fires first).

4-leg cost is ~4.5% of gross on a clean winner; on the small-credit NIFTY condors
(credit ~50 pts) the same ~₹330 cost is what tips marginal trades negative.

---

## 5. VIX-spike stress results
See §3 experiment 4. Net per high-vol episode (variant B, 0.16Δ): 2022-05 −₹2,279;
2022-10 +₹807; 2024-06 **+₹6,816**; 2024-10 +₹1,347; 2026-01 +₹1,975. The portfolio
never had a 6%+ losing month, but only because each position risks one spread
(~2.3% max loss, rarely realized). **Caveat:** raise size to use the capital and the
2026-01 / 0.30Δ −₹4,383 single-trade loss scales linearly into a single-month tail.

## 6. Loss-distribution stats (4-year monthly returns, 30 bps)
| V | monthly σ% | skew | kurtosis | worst month% |
|---|---|---|---|---|
| A | small | +3.39 | 26.6 | −0.68 |
| B | small | +2.74 | 20.5 | −0.68 |
| C | small | +2.48 | 15.1 | −0.67 |
| E | small | **−3.59** | 26.0 | **−0.88** |
With 4–11 trades these moments are **statistically unreliable** (the verdict gate
"kurtosis > 8" trips on all of them, but it is a small-N artifact, not a real fat
tail). The qualitative read is robust: **0.16Δ → right-skew (safe); 0.30Δ →
left-skew (dangerous).**

---

## 7. Verdict & recommendation

**REJECT all 6. Not a 5th sleeve; does not replace anything.**

Against the deployable bar **V_BLD_B (Sharpe 1.41, 70% green, payoff 1.67)**, the best
premium-seller (B/F) offers Sharpe 0.25, **8.7% green months**, and 4 trades in 2
years. It loses on every axis. Verdict-gate tally: DEPLOY needs N≥18 (have ≤6 OOS);
PROMISING needs N≥12 OOS (have ≤6); MARGINAL needs Sharpe≥0.7 with meaningful N (only
D=0.74 reaches it, on N=1). **REJECT.**

## 8. Honest framing — why this is a *structural* reject

1. **Self-defeating gate intersection.** "vix percentile ≥ 60" (high realized vol)
   and "range-bound: ADX<20 AND inside Bollinger" are near-mutually-exclusive — high
   realized vol usually means trending/expanded range. Layer the **25–30 DTE window
   (~6 trading days/month)** on top and qualifying days collapse to single digits.
   The funnel (NIFTY OOS): 476 → vix 142 → regime 49 → **DTE 9** → built 6.
2. **Frequency, not cost or pricing, is the killer.** 4-leg costs (₹280–640/condor)
   and real-IV strike selection are modeled faithfully (no synthetic-pricing caveat
   this round — premiums are real bhavcopy closes, and the inverse R36 finding that
   *IV > RV* is exactly what makes the un-breached condors win). But you cannot build
   60% green months from 4–11 lifetime trades.
3. **The defined-risk frame is sound; the signal cadence is not.** Per-side condor
   management works, the time-stop prevents any held-ITM expiry, and at 0.16Δ the
   distribution is benign. The strategy *concept* is fine — it just needs a far wider
   entry cadence (weeklies, continuous DTE, looser regime) to be a portfolio. That is
   a different strategy than the one specified, and out of scope here.
4. **Real-world execution risks (had it passed):** SPAN margin per index condor is
   typically **₹1.0–1.5 L (5–15× the ₹8–13k defined max-loss)**, so ₹5 L capital
   supports only ~1 concurrent condor anyway — consistent with the corr-cap, but it
   means the headline "1.5% risk per position" understates **capital consumption** by
   an order of magnitude. Wing liquidity (BANKNIFTY OTM OI is thin: median 280 vs
   NIFTY 2,275) and assignment/pin risk near expiry are additional frictions the EOD
   model cannot see.

---

### Reproduce
`engine.py` (shared: BS + real-IV strike selection, per-side condor management,
4-leg costs), `run_all.py` (6 variants × {30,60} bps, sliced OOS/2026/4-yr),
`diag_funnel.py` (gate funnel), `validate_trade.py` (to-the-rupee check).
Read-only on `index_options_eod`; no DB writes, no live-code edits.
_(Not committed — left on the working tree per the operator's request.)_
