# R68 — simplified_engine entries: what moves P(reach +1R before the stop)? (counter-NIFTY, retrace-limit, full feature scan)

**Date:** 2026-09-18 · **Follows:** R67 (exits exhausted; binding constraint P(1R before stop) = 0.47
vs 0.50 break-even) · **Question:** is there any entry-side lever — R65's counter-NIFTY cell, R60's
retrace-limit entry, or any persisted / bar-derived entry-time feature — that moves P(1R before stop)
in BOTH halves, with net P&L and trade counts that survive a placebo?

**Direct answer.** Across 21 entry-time features, one — and only one — separates the trades that reach
+1R from those that do not in both halves: **NIFTY moving against the trade's direction at review
time**. Entering only when NIFTY is > 0.25% *against* the trade lifts P(1R before stop) from **0.47 to
0.61 in both IS and OOS** (n = 38 / 23; a joint random-subset placebo reaches that in 0.5% of draws),
holds every month (0.50 / 0.58 / 0.67 / 0.62 vs 0.38–0.47 for the rest), and under every exit geometry
from R67 the cell is roughly break-even (+₹1…+₹35 per trade) while the other 82% of trades lose
−₹90…−₹140 per trade. It keeps **18% of the trades (~19 per month)**, its OOS net is +₹139 under the
live rules, and the +₹9.8k the actual journal shows for it in-sample is **eight June trades**. It is a
"lose less" filter with a real mechanism (a stock moving against the tape is an idiosyncratic move),
not a profit engine. **R60's retrace-limit entry is dead on this sample** (it selects the failing
breakouts: P(1R) 0.40 / 0.32 from the fill vs 0.47 for the chase). **Nothing else persisted or
derivable at entry time separates 1R-reachers beyond a shuffle placebo** — the in-sample best feature
is inside the null, the out-of-sample best is the stock's *price level*. P(1R before stop) is 0.467 for
longs and 0.472 for shorts: the signal source produces breakouts with symmetric excursion, and the
engine can only choose which fifth of them to lose less on. **The thing to change is the signal
source (the Chartink screener as consumed), not the engine around it.**

## 1. Setup

- Sample, split, replay, charges: exactly R67 (345 closed sandbox trades 2026-06-01 → 09-18; IS ≤ 08-08
  n=231, OOS 08-09 → 09-18 n=114; the tick/bar replay validated in R67 at 97.6% on ticks).
- **Metric:** `hit1R` = under the live 1.5×ATR stop with a 1R target and no trail, the replay ends on
  the target (0.470 overall, 0.468 IS / 0.474 OOS; longs 0.467, shorts 0.472). P(stop first) 0.394;
  the remainder reach neither by 15:14. Net P&L is the **actual journal net** for keep-subset filters
  and the **replay net** where an exit geometry is changed.
- **Placebo for every filter:** 1,000 random same-size subsets of the same half; `pl%` = percentile of
  the filter's P(1R) and of its net-per-trade among them. A joint both-halves draw is reported for the
  one candidate that passed.
- **Features (21):** persisted at review time — `nifty_pct` (signed by direction → `nifty_vs_dir`),
  `india_vix`, hour, LLM confidence, open positions, trades today, P&L today, Nth trade of day, trend
  label; ATR% (the stop width); and bar-derived at the entry instant from the broker 1m cache —
  gap %, move since open in the trade direction, move vs previous close, position in the day's
  range (directional), 5-/15-minute pre-entry run, day volume so far vs the prior-week average daily
  volume, entry-5m volume vs the prior 30 min, day range %, minutes since open, price level.

## 2. Univariate scan — does ANY entry-time feature separate 1R-reachers?

AUC of each feature for `hit1R` (0.5 = none; both halves shown; the sign must agree to count):

| feature | AUC IS | AUC OOS | consistent? |
|---|---|---|---|
| **nifty_vs_dir** (NIFTY with the trade ↑) | 0.459 | 0.447 | **yes** (against-tape → more 1R) |
| india_vix | 0.409 | 0.460 | yes, weak |
| Nth trade of day | 0.432 | 0.451 | yes, weak |
| entry-5m volume ratio | 0.543 | 0.616 | yes, weak IS |
| day range % | 0.535 | 0.570 | yes, weak |
| gap % | 0.392 | 0.571 | **flips** |
| hour / minutes since open | 0.420 | 0.534 | flips |
| trades today / P&L today | 0.430 / 0.455 | 0.553 / 0.544 | flip |
| ATR % | 0.535 | 0.394 | flips |
| price level | 0.476 | **0.673** | OOS only (spurious) |
| day volume so far ratio | 0.489 | 0.629 | OOS only |
| 5m / 15m pre-entry run | 0.510 / 0.519 | 0.619 / 0.611 | OOS only |
| ctx_nifty (unsigned) | 0.478 | 0.617 | OOS only |
| confidence, positions, pos-in-range, move since open / vs prev close | 0.48–0.51 | 0.41–0.55 | nothing |

**Shuffle placebo** (300 permutations of the outcome, max |AUC − 0.5| over the 21 features): IS real
**0.108 vs placebo p50 0.079 / p95 0.113** — the best in-sample feature is inside the null. OOS real
0.173 vs p95 0.160 — marginally outside, but that maximum is `price` (0.673), followed by
day-volume-so-far (0.629) which runs the *other* way in-sample. Read plainly: **no persisted or
bar-derived entry-time feature separates the trades that reach 1R from those that do not, beyond
what shuffled labels produce** — except the directional NIFTY context, whose separation is small
(AUC 0.46 / 0.45) but same-signed in both halves and shows a usable threshold cell (§3).

Tercile detail for the consistent ones: `nifty_vs_dir` low/mid/high P(1R) 0.49/0.45/0.45 IS and
0.53/0.45/0.45 OOS; VIX high tercile 0.34 IS but 0.46 OOS; Nth-trade high tercile 0.36 / 0.42;
5m-volume low tercile 0.42 / 0.32. Only the NIFTY one has a threshold that also survives §3's
subset placebo.

## 3. Keep-subset filters (actual journal net; pl% = percentile among 1,000 random same-size subsets)

| filter | IS n / P(1R) / net (₹/t) / pl%P1R / pl%net | OOS n / P(1R) / net (₹/t) / pl%P1R / pl%net |
|---|---|---|---|
| baseline | 231 / 0.47 / −8,868 (−38) | 114 / 0.47 / −15,319 (−134) |
| **against NIFTY > 0.25%** | **38 / 0.61 / +9,793 (+258) / 94 / 99** | **23 / 0.61 / −216 (−9) / 90 / 96** |
| against NIFTY > 0.1% | 50 / 0.58 / +8,926 (+179) / 94 / 99 | 33 / 0.58 / −1,690 (−51) / 88 / 92 |
| against NIFTY > 0% | 57 / 0.56 / +7,686 (+135) / 94 / 97 | 39 / 0.51 / −3,578 (−92) / 66 / 79 |
| against NIFTY > 0.5% | 23 / 0.52 / +7,279 / 63 / 98 | **7** / 0.43 / −1,719 / 27 / 20 |
| with NIFTY > 0.25% (control) | 131 / 0.45 / −9,301 (−71) / 21 / 21 | 57 / 0.40 / −11,326 (−199) / 4 / 4 |
| flat tape ≤ 0.25% (control) | 62 / 0.42 / −9,360 / 14 / 5 | 34 / 0.50 / −3,777 / 57 / 65 |
| LONG & against > 0.25% | 13 / 0.54 / +1,188 / 60 / 86 | 17 / 0.53 / −1,063 / 60 / 79 |
| SHORT & against > 0.25% | 25 / 0.64 / +8,605 / 96 / 99 | **6** / 0.83 / +847 / 91 / 97 |
| move since open in dir < 0.5% | 29 / 0.59 / −6,365 / 88 / 4 | 12 / 0.58 / −2,727 / 70 / 22 |
| day volume so far < 0.5× avg | 24 / 0.67 / +4,798 / 97 / 94 | 20 / **0.40** / −4,594 / 14 / 13 |
| gap faded (gap against the trade) | 25 / 0.64 / +1,796 / 95 / 83 | 21 / **0.33** / −3,099 / 5 / 42 |
| ATR% < 0.35 | 124 / 0.42 / −4,145 / 3 / 56 | 79 / 0.53 / −10,657 / 94 / 49 |
| entry not at day extreme | 134 / 0.46 / −6,797 / 28 / 37 | 61 / 0.51 / −7,719 / 75 / 59 |
| morning < 11:00 / afternoon ≥ 13:00 | 49 / 0.53 · 76 / 0.41 | 28 / 0.43 · 38 / 0.50 |

Every bar-derived "not extended / low volume / faded gap" idea that looks like a real P(1R) lift IS
(0.59–0.67) flips OOS (0.33–0.40). The counter-NIFTY cell is the only row above the 85th percentile
on P(1R) in both halves.

**The counter-NIFTY cell, examined properly.**
- P(1R before stop) **0.61 / 0.61** vs 0.47 / 0.47; single-half placebo percentiles 95.2 (IS) and
  88.5 (OOS); **joint** (both halves ≥ observed in one random draw) **0.5% of 5,000 draws**.
- By month (n / P1R / journal net): Jun 8 / 0.50 / **+9,615** · Jul 24 / 0.58 / +285 · Aug 21 / 0.67 /
  −249 · Sep 8 / 0.62 / −73. The rest of the sample: 0.38 / 0.46 / 0.43 / 0.47 and −₹11.6k / −₹7.3k /
  −₹8.9k / −₹5.9k. P(1R) is above the rest in **every** month; the P&L edge in the journal is June.
- Under R67's exit geometries (replay net, cell vs rest, ₹/trade):

| exit | IS cell / rest | OOS cell / rest | OOS pl% |
|---|---|---|---|
| live 1.5×ATR + 0.6R trail | +1 / −38 | +6 / −132 | 95 |
| stop 2.0×ATR | +5 / −16 | +21 / −92 | 93 |
| trail from 1.0R | +225 / −25 | +35 / −142 | 96 |
| 1R target, no trail | +37 / −88 | +20 / −112 | 91 |
| 1.5R target, no trail | +74 / −72 | −6 / −115 | 82 |
| 2.0×ATR + trail 1.0R | +178 / +10 | +13 / −129 | 94 |

  The cell is ≈ break-even under every geometry and ₹100–150/trade better than the rest under every
  geometry — a consistent *relative* effect, but not a positive expectancy. (The replay's +₹1/trade
  IS versus the journal's +₹258/trade IS is the June cluster: the replay does not reproduce those
  eight trades' realised sizes/fills; R67 §1 notes the bar-path replay is coarse on early trades.)
- Direction: the IS effect is short-driven (SHORT & against: 25 trades, 0.64, +₹8.6k; LONG: 13,
  0.54, +₹1.2k), the OOS count is long-driven (17 LONG / 6 SHORT). Neither side alone has a testable
  OOS count.
- Size: 61 of 345 trades, **~19 per month**.

## 4. R60's retrace-limit entry, replayed on this sample

Limit at the engine's fill price ∓ X% in the trade's favour, valid 60 min, filled if touched; then
either the live stop+trail or R60's own exit (2% disaster stop, hold to EOD):

| retrace | fills | exit | IS n / P(1R) / net (₹/t) / WR / payoff | OOS n / P(1R) / net (₹/t) / WR / payoff |
|---|---|---|---|---|
| 0.25% | 197 (57%) | live | 140 / 0.46 / −10,825 (−77) / 61 / 0.43 | 57 / **0.39** / −8,443 (−148) / 46 / 0.41 |
| 0.25% | | R60 2% + EOD | 140 / 0.46 / −483 (−3) / 49 / 1.00 | 57 / 0.39 / −2,251 (−39) / 26 / 1.67 |
| **0.50%** | 112 (32%) | live | 81 / **0.40** / −11,218 (−138) / 57 / 0.39 | 31 / **0.32** / −3,986 (−129) / 42 / 0.62 |
| 0.50% | | R60 2% + EOD | 81 / 0.40 / +1,355 (+17) / 51 / 1.11 | 31 / 0.32 / −767 (−25) / 39 / 1.04 |
| 0.75% | 65 (19%) | live | 49 / 0.47 / −10,408 (−212) / 53 / 0.33 | 16 / 0.38 / −1,857 (−116) / 56 / 0.38 |
| 0.75% | | R60 2% + EOD | 49 / 0.47 / +1,212 (+25) / 49 / 1.24 | 16 / 0.38 / −229 (−14) / 56 / 0.62 |

(chase P(1R) for comparison: 0.47 / 0.47.) At 0.5% the retrace fills are **adversely selected** —
P(1R from the fill) 0.40 IS / 0.32 OOS, longs 0.33 IS, shorts 0.11 OOS — because on this anchor the
entry is already a 5m-candle breakout confirmation: the ones that give back 0.5% within the hour are
the failing ones. With R60's wide stop and EOD hold the numbers are ≈ zero (+₹17 / −₹25 per fill).
**Caveat on fidelity:** R60 anchored the limit on the *scanner hit* (earlier than the engine's
candle-close trigger) and traded the scanner's own list over Jun–Jul; this is the same idea on the
engine's anchor and the engine's trades. On that anchor the idea does not transfer. **REJECT** for
the engine as built.

## 5. Verdict and what it means

1. **Counter-NIFTY is the one real entry-side signal in the data.** It moves the metric that matters
   (P(1R before stop) 0.47 → 0.61, both halves, every month, joint placebo 0.5%) and turns −₹130/trade
   into ≈ ₹0 under every exit. It does not make money: 61 trades, ~19/month, OOS +₹139. **Verdict:
   PROMISING as a label / gate, INSUFFICIENT as a strategy.** Pre-registered rule (unchanged from
   R65, now with the right metric): journal `counter_nifty` on every entry; at **≥ 60 new labelled
   trades** gate on it only if P(1R before stop) ≥ 0.55 on **both** sides and net ≥ 0 on the new
   trades alone. Note again that the LLM veto prompt currently instructs the model to *skip* these.
2. **Retrace-limit entry: REJECT on this anchor** (adverse selection; ≈ 0 with R60's exit).
3. **No other entry-time feature separates 1R-reachers** — the best in-sample AUC deviation (0.108)
   is inside the shuffle null (p95 0.113); the out-of-sample maximum is price level. Every
   bar-derived "not extended" idea flips between halves.
4. **Therefore: the signal source is the thing to change.** P(1R before stop) is 0.467 long / 0.472
   short with median MFE = median MAE (R67); nothing observable at the engine's entry instant tells
   a 1R-reacher from a stop-out except the tape's direction — a *context* filter, not a property of
   the setup. The engine faithfully executes breakouts whose forward excursion is symmetric. Options
   that change the signal itself: (a) a different Chartink screener / in-house scan rule whose hits
   have measurably asymmetric excursion — measure P(1R before stop) on the hits *before* wiring an
   engine to them (R56 did this for the in-house scanner and got 42.8% WR / payoff 0.34); (b) use
   the screener as a watch-list and enter on a defined setup rather than the candle-close chase
   (R57/R60's conclusion — but not the 0.5% retrace on this anchor); (c) restrict the engine to the
   counter-NIFTY context and accept it as a measurement while (a)/(b) are built.

No engine parameter or code was changed by this round. Harness: scratchpad `r68_entries.py` on the
R67 engine, frame and net matrix.
