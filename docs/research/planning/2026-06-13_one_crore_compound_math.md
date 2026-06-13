<!-- migrated from outputs/2026-06-13_one_crore_compound_math.md on 2026-06-13 | summary: â‚¹1 Crore Profit â€” The Compound Math (Honest Version) -->

# â‚¹1 Crore Profit â€” The Compound Math (Honest Version)

**Prepared for:** Dheeraj Â· **Date:** 2026-06-13
**Nature:** Numbers-first analysis. No code committed, no trades placed, DB read-only.
Script that generates every table here: [`outputs/_one_crore_math.py`](_one_crore_math.py) â€” edit the axes and re-run.

> **What the prior plan got wrong.** The earlier draft
> ([`2026-06-13_one_crore_12mo_plan.md`](2026-06-13_one_crore_12mo_plan.md)) framed the
> target as a *linear* "â‚¹8.33 L profit/month" milestone. That is wrong: â‚¹1 Cr is the
> output of a **compounding** process, not a monthly salary. A linear milestone tells
> you nothing about how *time* and *re-investment* change the answer â€” which is the
> entire point. This version drops the pep-talk and the capital-tier hand-waving and
> just does the arithmetic.

---

## 1. The math

For starting capital **S**, annual return **r**, time **t** (years), with profit
re-invested every year:

```
Total(t) = S Ã— (1+r)^t
Profit(t) = S Ã— (1+r)^t âˆ’ S          â† this is what you keep
Time to â‚¹1 Cr profit:  t = ln(â‚¹1,00,00,000 / S + 1) / ln(1+r)
```

**Worked example â€” â‚¹25 L start, 40% CAGR:**

```
Total(t) = 25,00,000 Ã— 1.40^t
Want Profit = â‚¹1 Cr  â†’  1.40^t = (1,00,00,000 / 25,00,000) + 1 = 5
t = ln(5) / ln(1.40) = 1.609 / 0.3365 = 4.78 years
```

So â‚¹25 L compounding at 40%/yr crosses â‚¹1 Cr **profit** in **~4.8 years** â€” total
account â‚¹1.25 Cr. Not 12 months. And 40% sustained is already aggressive (see Â§4).

---

## 2. Time to â‚¹1 Cr profit â€” the grid

Annual compounding, no fresh capital added. **Cell = years to â‚¹1 Cr profit.**

| Capital \ CAGR | 15% | 20% | 30% | 40% | 50% | 70% | 100% |
|---|---:|---:|---:|---:|---:|---:|---:|
| **â‚¹5 L**   | 21.8 | 16.7 | 11.6 | 9.0 | 7.5 | 5.7 | 4.4 |
| **â‚¹10 L**  | 17.2 | 13.2 | 9.1 | 7.1 | 5.9 | 4.5 | 3.5 |
| **â‚¹15 L**  | 14.6 | 11.2 | 7.8 | 6.1 | 5.0 | 3.8 | 2.9 |
| **â‚¹25 L**  | 11.5 | 8.8 | 6.1 | 4.8 | 4.0 | 3.0 | 2.3 |
| **â‚¹50 L**  | 7.9 | 6.0 | 4.2 | 3.3 | 2.7 | 2.1 | 1.6 |
| **â‚¹75 L**  | 6.1 | 4.6 | 3.2 | 2.5 | 2.1 | 1.6 | 1.2 |
| **â‚¹1 Cr**  | 5.0 | 3.8 | 2.6 | 2.1 | 1.7 | 1.3 | 1.0 |
| **â‚¹2 Cr**  | 2.9 | 2.2 | 1.5 | 1.2 | 1.0 | 0.8 | 0.6 |
| **â‚¹5 Cr**  | 1.3 | 1.0 | 0.7 | 0.5 | 0.4 | 0.3 | 0.3 |

**How to read it.** Find your starting capital row, your honest CAGR column, read the
years. The "12 months" cells (â‰¤1.0y) only appear at **â‚¹1 Cr+ start AND 100% CAGR**, or
**â‚¹2 Cr+ start AND 50%+ CAGR**, or **â‚¹5 Cr start AND ~15%**. Everything in the
upper-left â€” small capital, sane returns â€” is **5 to 22 years.** That is the honest
shape of the problem.

Your example (â‚¹5 Cr @ 20%) sits at **1.0 year** â€” which is exactly why it felt easy
to say. The catch is the â‚¹5 Cr, which you've said you don't have.

---

## 3. Compounding-period framing â€” what you'd need per day/week

Same annual CAGR, expressed as the equivalent *per-period* return. The final number is
identical; this just shows what the trader actually has to produce.

| CAGR | per month | per week | per trading-day |
|---|---:|---:|---:|
| 20% | 1.53% | 0.35% | 0.073% |
| 40% | 2.84% | 0.65% | 0.135% |
| 50% | 3.44% | 0.78% | 0.162% |
| 100% | 5.95% | 1.34% | 0.278% |

**Why this matters:** "double my money this year" (100%) sounds like one big bet. It is
actually **+0.28% every single trading day, compounded, for 250 days, with no losing
streak big enough to break the chain.** That is the part nobody prices in. A 15-name
intraday book that nets +0.28%/day *after costs* every day does not exist in the wild.

---

## 4. Realistic CAGR ceiling for THIS system

Pulled from `strategies/STRATEGY_REGISTRY.md` and the backtest reports. **Every number
below is backtest or sandbox. There is zero real-money track record.**

| Strategy | Sharpe | CAGR | MaxDD | Trust level | Honest read |
|---|---:|---:|---:|---|---|
| **Sector Rotation ETF (R26, 26d risk-parity)** | 1.17 | **14.8%** | âˆ’16.9% | **Highest** â€” real ETF NAVs, 4-yr, 47 rebalances | The only number I'd defend in court. Long-only, low-babysitting, scalable. |
| **V_BLD5_B 5-sleeve blend (R41)** | 2.42 | **19.6%** | âˆ’2.2% | Low â€” 23-mo OOS, paper only | Sharpe 2.42 and âˆ’2.2% MaxDD are *too good*; partly a windowing artifact. **Will decay to ~12â€“15% live.** |
| **Sector Follow CAP5_VOL (R40)** | 2.37 | (+12.9% 2026-YTD) | âˆ’8.76% | Low â€” short 1m-feed history, scaffold | Wired but places no orders. Promising, unproven. |
| **Simplified Engine (live in sandbox)** | n/a | **~0%** | n/a | Real plumbing, no edge | 2 weeks sandbox = +â‚¹2,735 on â‚¹1 Cr â‰ˆ 0.03%/fortnight. Intraday breakout was structurally unprofitable in R2â€“R6. Not alpha. |

**28 rejected rounds say the same thing:** single-name intraday F&O has no edge net of
costs; option *buying* on synthetic pricing is optimistic by ~38%; premium *selling*
edge is real but fires ~4Ã— in 23 months. **The only thing that clears the bar is
portfolio-layer blending of uncorrelated positional sleeves â€” at ~15â€“20% CAGR.**

> **Realistic ceiling, haircut for live: ~15% high-confidence, ~18â€“20% optimistic.**
> Use **15%** for planning and treat anything above 20% as unproven hope.

---

## 5. Leverage â€” gross vs net (the honest both-sides)

Indian F&O margin gives ~4â€“5Ã— intraday leverage. The seductive arithmetic:

```
Strategy return 40%  Ã—  4Ã— leverage  =  160% GROSS
```

Now subtract reality:
- **Turnover/STT/slippage** scale ~linearly with leverage: ~10â€“20% drag at 4Ã—.
- **Cost of carry / interest** on the borrowed margin: a few %.
- **Net good year â‰ˆ 80â€“100%.** Looks incredible.

**The other side, which is the whole story:**
- **Max drawdown also multiplies 4Ã—.** The ETF core's âˆ’16.9% DD becomes **âˆ’68% levered.**
- A âˆ’25% strategy move at 4Ã— = **âˆ’100% = account wiped.** Ruin probability climbs toward
  certainty over enough months.
- Compounding drawdowns are asymmetric: down 50% needs +100% just to get back.
- Leverage amplifies the *unproven* part of the edge fastest. We don't have a live
  edge to lever yet â€” levering a backtest is levering an assumption.

**Verdict on leverage:** it is the only lever that hits â‚¹1 Cr fast on small capital, and
it is also the one most likely to deliver **ruin instead of â‚¹1 Cr.** Default to none on
the core until there is a real-money track record. Availability â‰  advice.

---

## 6. Monthly-addition acceleration

If you add â‚¹A/month from salary/savings, time-to-target drops. Profit here is **honest**:
final value minus *all* cash you put in (additions are principal, not profit).

| Start | CAGR | +â‚¹0/mo | +â‚¹25 k/mo | +â‚¹50 k/mo | +â‚¹1 L/mo |
|---|---:|---:|---:|---:|---:|
| **â‚¹25 L** | 40% | 4.8y | 4.4y | 4.1y | 3.6y |
| **â‚¹10 L** | 60% | 5.1y | 4.4y | 4.0y | 3.4y |
| **â‚¹50 L** | 30% | 4.2y | 4.0y | 3.8y | 3.5y |

**The lesson:** additions matter *most when the base is small*. â‚¹10 L @ 60% goes from
5.1y â†’ 3.4y (âˆ’1.7y) by adding â‚¹1 L/month â€” because early on, your contributions dwarf
your returns. Once the base is large (â‚¹50 L), additions barely move the needle (âˆ’0.7y)
â€” compounding on the existing base dominates. **When you're small, save aggressively
INTO the account; when you're big, let it compound.**

---

## 7. Verdict â€” your situation

The repo shows **no real deployed capital** anywhere â€” the â‚¹1 Cr in `sandbox.db` is
virtual, and the only "live-ish" record is 2 weeks of sandbox trading that netted
~â‚¹2,735 (0.03%). So the honest planning inputs are: **a credible ~15% engine
(unproven live), and whatever real capital you actually fund the account with.**

**At a responsible 15% CAGR:**
- â‚¹25 L â†’ â‚¹1 Cr profit in **11.5 years**
- â‚¹50 L â†’ **7.9 years**
- â‚¹1 Cr â†’ **5.0 years**
- â‚¹5 Cr â†’ **1.3 years** â† the only place 12-ish months is real

**Is â‚¹1 Cr in 12 months reachable at a responsible risk level? No.** Not from any
starting capital below ~â‚¹5 Cr. To force it from a small base you need 100%+ returns or
4Ã—+ leverage, and the most probable outcome of *trying* that is a blown account, not
â‚¹1 Cr. This is a **capital problem, not a strategy problem** â€” the strategy tops out
near 15â€“20%, and no amount of cleverness changes the exponent.

**What the math says to actually do:** fund the account with your real capital, run the
ETF core at ~15%, **reinvest everything**, and add savings monthly while the base is
small. â‚¹1 Cr profit is a **3â€“8 year goal** depending on your starting capital and how
much you add â€” not a 12-month sprint.

---

## 8. Compression levers â€” what actually shaves years off

Ranked by honesty/safety, best first:

1. **More starting capital** (safest, biggest lever). â‚¹25 Lâ†’â‚¹1 Cr at 15% cuts 11.5yâ†’5.0y.
   Raising capital beats chasing return â€” and doesn't add ruin risk.
2. **Monthly additions while small** (Â§6). â‚¹1 L/mo on a â‚¹10 L base cuts ~1.7 years.
3. **Accept a smaller target.** â‚¹50 L profit instead of â‚¹1 Cr roughly *halves* the time
   at any cell. â‚¹25 L @ 15% hits â‚¹50 L profit in ~6.5y vs 11.5y for â‚¹1 Cr.
4. **Higher CAGR** (real but capped + risky). 15%â†’20% on â‚¹25 L cuts 11.5yâ†’8.8y. But
   every step above ~15% is unproven and buys drawdown. Don't plan above 20%.
5. **Leverage** (fastest, most dangerous â€” Â§5). Only after a real-money track record,
   and never on the core. Treat as the last lever, not the first.

The compounding takeaway in one line: **time and the size of the base do the heavy
lifting; return rate and leverage do the dangerous lifting.** Pull the safe levers first.

---

*All performance figures are backtest or sandbox. No real-money results exist yet.
Nothing here is a return guarantee. Tweak the axes in `outputs/_one_crore_math.py` and
re-run to test your own numbers.*
