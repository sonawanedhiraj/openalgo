# R54 — Intraday stop-loss on the leveraged futures_follow_cap50 sleeve · 2026-07-13

**Question (operator):** the 2026-07-07 war-news day lost −₹34k in the sandbox
pilot and wiped out the pilot's gains. Would a **stop loss** have made the sleeve
profitable? Phase-1 rejected stops, but only on the *unleveraged* ₹2.5L equity
book — never on this ~7×-leveraged futures wrapper, where the tail math differs.

**Verdict: REJECT.** Tested directly on the leveraged sleeve across 2024–2026,
**every** stop distance reduces CAGR (−1.6 to −3.6pp) and Sharpe (−0.17 to
−0.36) versus no-stop, and the ones that fire make the **worst day bigger**
(−₹45k to −₹52k vs −₹34k), not smaller. A stop would have helped the 07-07 day
in isolation (~250–310 pts saved) but costs far more across the sample. The
Phase-1 equity finding **holds with leverage**. Do not add a hard stop.

Issue: [#397](https://github.com/sonawanedhiraj/openalgo/issues/397) ·
Harness: `outputs/2026-07-13_futures_stop_loss/run_stops.py`

---

## Method — stop overlay on the canonical NIFTY-only CAP50 trade set

The stop changes **only the exit** — CAP50 sizing/selection is decided at 15:20
entry *before* any stop can fire, so the set of trades taken is identical across
variants. So the overlay takes the canonical NIFTY-only CAP50 trade set (built
byte-identically via `sm_core` — the same harness behind the 14.44%/1.27 result)
and re-prices each trade under a menu of stops, using the **intraday 1m
low-path of NIFTY** over each overnight hold window.

Stop model (LONG index future, entry `E` = NIFTY index c1520(D), level
`L = E·(1−s)` or `E − k·ATR14`):

| Phase | Window | Trigger | Fill |
|---|---|---|---|
| 1 | D 15:21–15:29 (post-entry) | any 1m low ≤ L | ~L (touch, −slip) |
| 2 | D+1 09:15 open | open ≤ L | **at the open** (gap-through, worse than L) |
| 3 | D+1 09:15–15:25 | any 1m low ≤ L | ~L (touch, −slip) |
| — | else | — | normal 15:25 exit c1525(D+1) |

Window extended to **2026-07-10** (canonical was 2026-06-12) so the 07-07 war day
is in-sample. ATR is Wilder-14, **lookahead-free** (entry-day stop uses the prior
day's ATR). READ-ONLY on `historify.duckdb`.

**Fidelity control — baseline (no-stop) reproduces the canonical result:**

| Metric | Canonical NIFTY-only CAP50 | Baseline (this harness) |
|---|---|---|
| CAGR | 14.44% | **14.84%** |
| Sharpe | 1.27 | **1.31** |
| MaxDD | −8.0% | −8.01% |
| Worst day | −₹34,396 | **−₹34,396** (exact) |
| Trades | 149 | 153 |

Worst day matches exactly; CAGR/Sharpe within noise (slightly higher — the
extended window adds ~1 mostly-positive month). The overlay is trustworthy.

## Results (₹10L, NIFTY-only CAP50, 2024-01-01 → 2026-07-10, 153 trades)

| Variant | Win% | Net ₹ | CAGR% | Sharpe | MaxDD% | Worst day ₹ | Stops fired |
|---|---:|---:|---:|---:|---:|---:|---:|
| **BASELINE no-stop** | 52.9 | 417,458 | **14.84** | **1.31** | **−8.01** | **−34,396** | 0 |
| PCT 0.75% | 50.3 | 308,177 | 11.24 | 0.95 | −8.46 | −45,438 | 26 |
| PCT 1.00% | 51.6 | 334,273 | 12.12 | 1.01 | −9.89 | −45,438 | 15 |
| PCT 1.50% | 51.6 | 369,520 | 13.28 | 1.14 | −8.01 | −52,021 | 3 |
| PCT 2.00% | 51.6 | 347,552 | 12.56 | 1.06 | −8.01 | −69,027 | 2 |
| PCT 2.50% | 52.9 | 417,458 | 14.84 | 1.31 | −8.01 | −34,396 | 0 (never fires) |
| PCT 1.50% slip5 | 51.6 | 368,395 | 13.24 | 1.13 | −8.01 | −52,771 | 3 |
| ATR 1.0× | 51.6 | 339,955 | 12.31 | 1.03 | −8.29 | −67,846 | 9 |
| ATR 1.5× / 2.0× / 2.5× | 52.9 | 417,458 | 14.84 | 1.31 | −8.01 | −34,396 | 0 (never fires) |

**Every stop that fires loses money vs no-stop. Every stop that doesn't fire is
just the baseline.** There is no distance that wins.

## Two counter-intuitive findings

**1. Stops make the WORST day BIGGER, not smaller.** Baseline worst −₹34,396;
every firing stop produces a worst day of −₹45k to −₹69k. Two mechanisms:
(a) a stop can't catch an overnight gap — it fills at the gapped-open, sometimes
below the level; (b) on this signal family intraday dips frequently **recover**
into the 15:25 close, so stopping out at the intraday low locks in a price
*worse* than holding, and the whipsaw-outs cluster into new bad days. The stop
doesn't remove the tail — it relocates and deepens it.

**2. The war day in isolation is the exception that proves the rule.** Re-pricing
the actual 2026-07-07 → 07-08 position (index entry 24,381.65 → normal exit
23,888.10, **−493.6 pts / −2.02%**; 07-08 opened −0.39% then ground down to a
23,805 low at **14:51** before bouncing to 23,888 by 15:25):

| Stop | Level | Fill | Loss (pts) | Saved vs no-stop |
|---|---:|---:|---:|---:|
| none | — | 23,888.1 | −493.6 | — |
| 0.75% | 24,198.8 | 24,198.8 | −182.9 | **+310.7** |
| 1.00% | 24,137.8 | 24,137.8 | −243.8 | +249.7 |
| 1.50% | 24,015.9 | 24,015.9 | −365.7 | +127.8 |
| ATR 1.0× | 24,138.6 | 24,138.6 | −243.1 | +250.5 |

So on the war day a tight stop **would** have saved ~250–310 pts (≈₹16–20k on a
75-lot). The operator's intuition is correct *for that day*. But the same stop
that saves ₹18k here bleeds far more across the other 152 trades — the 07-07 loss
was 75% an intraday grind (stoppable), which is exactly why it *looks* like a stop
would fix everything, but the fuller sample shows the intraday grind is the
exception and intraday-dip-then-recover is the rule.

> Harness selection note: in the 1m-historify harness INFY was mid-hold on 07-07
> (entered 07-06), so the war day is not a *separate* base trade — its outcome is
> the direct re-pricing above. Live sandbox entered it fresh (broker-quote path
> vs historify sequencing divergence). Immaterial to the verdict: both the
> aggregate grid and the direct war-day sim agree.

## Conclusion & recommendation

The stop question is now answered **for the leveraged sleeve directly**, not
extrapolated from equity: a hard intraday stop is net-negative at every distance,
degrades Sharpe, and worsens the tail. **Do not add a stop.** This closes the
Phase-1 → futures generalisation gap.

The evidence-backed mitigations for the −₹34k tail remain unchanged:

1. **Size down** — the honest lever for a leveraged-beta sleeve; halving lots
   halves the ₹ tail (and the return). The −8% MaxDD is already in the Sharpe.
2. **Fix the 3% kill switch's T+1 blind spot** — it read ₹0 while the book lost
   ₹34k overnight, because it only counts same-day entries, not T+1 exit losses.
3. **OTM-put overnight hedge (separate round)** — the *only* structure that caps
   the ~19% overnight-gap portion a stop can't touch; needs its own backtest
   (option-buying overlays historically fight the IV floor — R33/R36).

Registry cross-cutting finding: *hard intraday stops are net-negative on the
sector_follow overnight signal family — confirmed both unleveraged (equity,
Phase-1) and ~7×-leveraged (NIFTY futures, R54); the edge is the full overnight
hold, and stops relocate/deepen the tail rather than capping it.*
