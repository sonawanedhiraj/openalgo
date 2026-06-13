<!-- migrated from outputs/2026-06-13_one_crore_12mo_plan.md on 2026-06-13 | summary: Path to â‚¹1 Crore Profit in 12 Months â€” An Honest Plan -->

# Path to â‚¹1 Crore Profit in 12 Months â€” An Honest Plan

**Prepared for:** Dheeraj
**Date:** 2026-06-13
**Nature:** Planning + analysis only. No code committed, no trades placed, DB read-only.

> **Read this first.** This is not a pitch. It is the math, the evidence we
> actually have, and the honest gap between them. The single most important fact
> below is that **everything we have validated is either a backtest or a sandbox
> (simulated-money) result.** We have **zero real-money track record**. That one
> fact governs everything else.

---

## 1. Where we actually stand today (the ground truth)

| Signal | Reality |
|---|---|
| **Sandbox "capital"** | â‚¹1,00,00,000 â€” but this is the *simulated* book in `sandbox.db`. It is **not** real deployed capital. |
| **Live-ish track record** | The simplified engine ran in **sandbox mode**, 2026-06-01 â†’ 06-12. **57 journal rows, net +â‚¹2,735.** Of those, **28 rows were pytest-pollution cleanup (â‚¹0 P&L)**. The real ~29 trades: 4 EOD square-offs (+â‚¹7,859) minus 23 stop-losses (âˆ’â‚¹4,869). |
| **Real money at risk** | None evidenced anywhere in the repo. |
| **Best "validated" strategies** | All **backtest or paper** â€” see Â§3. None has placed a real order. |

**What +â‚¹2,735 over 2 weeks on â‚¹1 Cr means:** ~0.027% for the fortnight. Even
charitably annualized that is a fraction of a percent â€” and the sample is far too
small, too noisy, and **not real money** to call it edge. Treat it as *evidence the
plumbing works*, **not** *evidence of a money-making edge*.

**Load-bearing assumption flagged:** every return figure downstream assumes live
performance will *resemble* backtest/sandbox. History says live almost always
comes in **worse** (slippage, fills, IV floor, regime change, operator error).
Haircut everything.

---

## 2. The target math â€” this is the uncomfortable part

â‚¹1 Cr **profit** in 12 months. Required annual return = â‚¹1,00,00,000 Ã· Starting Capital.

| Starting capital S | Return needed for â‚¹1 Cr profit | Verdict |
|---|---:|---|
| â‚¹5 L | **2000%** | Fantasy. Nobody does this repeatably. |
| â‚¹10 L | **1000%** | Fantasy. Lottery-ticket leverage; ruin-probability â‰ˆ 1. |
| â‚¹25 L | **400%** | Effectively impossible without blowing up. |
| â‚¹50 L | **200%** | Extreme. Top-decile-CTA-in-a-great-year territory, sustained. Very unlikely. |
| â‚¹1 Cr | **100%** | Aspirational ceiling. Doublings happen â€” *repeatably and on purpose*, almost never. |
| â‚¹2 Cr | **50%** | Aggressive but in the realm of a great systematic year (with real drawdown risk). |
| â‚¹5 Cr | **20%** | **Plausible.** Within reach of our backtested blend if it holds live. |
| â‚¹10 Cr | **10%** | Comfortable. Below NIFTY's long-run; a low-vol blend clears it. |

### Reference benchmarks (so the numbers have a yardstick)

- **NIFTY 50 long-run:** ~12â€“15% CAGR (price + dividends).
- **Best discretionary funds:** ~25â€“35% in good multi-year stretches.
- **Top systematic CTAs:** 40â€“60% in *good* years â€” paired with 20â€“40% drawdowns and losing years.
- **Sharpe:** 1.5+ is genuinely good. **2.0+ is excellent and rare in live trading** â€” most backtest 2.0+ Sharpes decay below 1.0 live.

### The single honest conclusion

> **â‚¹1 Cr profit in 12 months is a capital problem, not a strategy problem.**
> At a *defensible* expected return of ~15â€“25% (our best blend, haircut for live),
> the â‚¹1 Cr profit target requires roughly **â‚¹4â€“7 Cr of deployed capital.**
> Below ~â‚¹2 Cr starting capital, hitting â‚¹1 Cr in 12 months requires returns that
> are statistically indistinguishable from gambling, and the most likely outcome
> of *trying* to force them (max leverage on a small base) is **ruin, not â‚¹1 Cr.**

---

## 3. Strategy inventory â€” what we could actually deploy

Source: `strategies/STRATEGY_REGISTRY.md`. **All figures are backtest/paper unless stated.**

| Strategy | Status | Sharpe | CAGR | MaxDD | Capital style | Honest caveat |
|---|---|---:|---:|---:|---|---|
| **Sector Rotation ETF (26d risk-parity)** | Scaffold, live seed 2026-06-15 (manual) | 1.17 | 14.8% | âˆ’16.9% | Positional, monthly, long-only, **low capital-intensity, scalable** | Real ETF NAVs, 4-yr window â€” the **most trustworthy** number we have. Not a fast-selloff hedge. |
| **V_BLD5_B 5-sleeve blend (R41)** | Deploy *candidate*, paper from 2026-07-01 | 2.42 | 19.6% | âˆ’2.2% | Portfolio-of-sleeves | 23-mo OOS only; low-corr partly a zero-padding artifact; some sleeves tiny-N. **Sharpe 2.42 will not survive live intact.** |
| **Sector Follow CAP5_VOL (R40)** | Scaffold, `deployable:false` | 2.37 | (2026-YTD +12.9%) | âˆ’8.76% | Intradayâ†’T+1, **â‚¹2.5 L slots, scales modestly** | Wired but places no orders; 1m index feed history is short. |
| **Simplified Engine** | **Live in sandbox** | n/a | n/a | n/a | Intraday breakout | The standalone intraday breakout edge was **structurally unprofitable** in Rounds 2â€“6. Operated as a gated, monitored system, **not proven alpha.** |

**What 28 rejected rounds taught us (don't re-learn the hard way):**
- Intraday single-name F&O has **no exploitable directional edge net of costs** (R1â€“R15).
- Option *buying* on synthetic pricing is **systematically optimistic**; real premiums are 0.4â€“1.1 Sharpe worse (R36-real).
- Premium *selling* edge is real per-trade but **dies on frequency** (R37) â€” fires ~4Ã— in 23 months.
- **The only thing that clears the bar is portfolio-layer blending of uncorrelated lumpy sleeves** (R34â†’R41). Monthly consistency is *manufactured at the portfolio layer*, not found in a better signal.

**Translation:** our genuine, repeatable edge is **diversified positional sector
rotation + sleeve-blending at ~15â€“20% CAGR**, not a high-octane intraday money
printer. Plan around that.

---

## 4. Sample allocation framework (pick a capital tier, then deploy)

Drafted for a **â‚¹2 Cr tier** (the smallest tier where â‚¹1 Cr in 12 months is even
*discussable* â€” at 50% return â€” though base case is far lower). Scale proportionally.

| Sleeve | % | Capital | Expected return (haircut) | Confidence |
|---|---:|---:|---|---|
| **Core** â€” Sector Rotation ETF (risk-parity) | 55% | â‚¹1.10 Cr | 12â€“15% | **High** (real-NAV backtest) |
| **Alpha** â€” V_BLD5_B sleeves / Sector Follow | 25% | â‚¹0.50 Cr | 15â€“22% *if it holds; assume 10â€“12% live* | **Lowâ€“Med** (paper only) |
| **Tactical** â€” opportunistic (NO option *buying*; selling only if frequency fixed) | 10% | â‚¹0.20 Cr | 0â€“10% | **Low** |
| **Cash / margin buffer** | 10% | â‚¹0.20 Cr | ~6â€“7% (liquid) | â€” |

**Blended expected return (base, haircut):** ~**12â€“16%** â†’ on â‚¹2 Cr that is
**â‚¹24â€“32 L profit**, *not* â‚¹1 Cr. To get the blend to â‚¹1 Cr you need ~â‚¹5â€“7 Cr at
this return profile. **This is the central tension the plan must not paper over.**

---

## 5. Risk envelope (non-negotiable)

- **Max portfolio drawdown to stay committed:** 15â€“20%. The ETF core alone has a
  âˆ’16.9% historical DD â€” budget for it psychologically *before* it happens.
- **Daily loss limit:** 1.5% of deployed capital. **Monthly:** 6%. Hit it â†’ flatten, stop, review.
- **Position sizing:** â‰¤1% capital at risk per intraday trade; â‰¤8% capital per
  ETF position. **Never** full-Kelly â€” fractional (Â¼-Kelly) at most; Kelly assumes
  you *know* your edge, and we don't (it's backtest).
- **Concentration caps:** â‰¤25% per sector, â‰¤10% per single name, â‰¤30% in the
  unproven "Alpha" sleeve until it has **live** track record.
- **Leverage:** the one lever that can hit the target on small capital is also the
  one that converts a bad month into ruin. **Default to none** on the core. Sandbox
  shows 5Ã— MIS / 10Ã— futures available â€” availability is not advice.

---

## 6. Monthly milestones (linear vs compounded)

For the **â‚¹1 Cr / 12-mo** headline target (capital-agnostic framing):

| Model | Cadence | Note |
|---|---|---|
| **Linear** | â‚¹8.33 L profit/month | Easy to track; ignores compounding. |
| **Compounded to 100%/yr** (â‚¹1Cr base) | **+5.95%/month** | Tough early, easier late. |
| **Compounded to 50%/yr** (â‚¹2Cr base) | **+3.44%/month** | A *great* systematic month, every month. |
| **Compounded to 20%/yr** (â‚¹5Cr base) | **+1.53%/month** | **Realistic** for the blend. |

A +1.5%/month *consistent* result is already top-tier. +5.95%/month *every* month
for a year has essentially no real-world precedent at this scale.

---

## 7. Three scenarios

| Scenario | Assumptions | â‚¹2 Cr outcome | â‚¹5 Cr outcome |
|---|---|---:|---:|
| **Bull** | Blend hits ~80% of backtest Sharpe, NIFTY grinds up, no major DD | +35â€“45% â†’ â‚¹70â€“90 L | +35â€“45% â†’ â‚¹1.75â€“2.25 Cr âœ… |
| **Base** | Live degrades to ~12â€“16%, one âˆ’10% DD month survived | +12â€“16% â†’ â‚¹24â€“32 L | +12â€“16% â†’ â‚¹60â€“80 L |
| **Bear** | DD in month 1â€“2, recovery needed, Alpha sleeve disappoints | âˆ’5% to +5% â†’ âˆ’â‚¹10 L to +â‚¹10 L | same % â†’ âˆ’â‚¹25 L to +â‚¹25 L |

**Only the â‚¹5 Cr / Bull cell reaches â‚¹1 Cr.** That is the honest shape of it.

---

## 8. Honest constraints (the things that bite)

- **Costs & taxes:** STT, turnover, GST, SEBI lot sizes, SPAN margin (5â€“15Ã— max
  loss on option selling) â€” all already modelled in the registry and all *reduce*
  realistic return.
- **SEBI static-IP mandate (Apr 1, 2026):** all transactional orders need broker-side
  static-IP whitelisting â€” an operational dependency, not just config.
- **Daily token expiry (~3 AM IST):** every trading day needs a Zerodha re-login.
  A missed login = a missed day. (Documented repeatedly in session learnings.)
- **Single-operator bandwidth:** this whole project is one person splitting time
  between *fixing the system* and *running strategies*. The undercurrent of every
  recent session is bug-firefighting. **Capacity to babysit live capital is the
  real bottleneck**, not idea supply.
- **Single-codebase concentration:** all strategy IP + execution in one OpenAlgo
  instance = single point of failure. A boot bug (e.g. the Py3.14 eventlet boot-fail)
  can silently take the whole book offline.
- **Backtest â‰  live, restated:** the âˆ’2.2% MaxDD on V_BLD5_B is *suspiciously*
  good and partly a windowing artifact. Do not size against it.

---

## 9. Concrete next-30-days plan

**Goal of the next 30 days is NOT â‚¹1 Cr. It is to convert backtest confidence into
the first real-money evidence, safely and small.**

**Deploy (in priority order):**
1. **Sector Rotation ETF** â€” highest-trust, lowest-babysitting. Run the
   2026-06-15 operator-manual seed entry per its DEPLOYMENT_CHECKLIST. **Tiny real
   tranche (e.g. â‚¹2â€“5 L)** â€” first real orders, manual review each rebalance.
2. **V_BLD5_B / Sector Follow** â€” keep in **paper/sandbox** from 2026-07-01 as
   planned. **Do not** put real money on a paper-only Sharpe yet.
3. **Simplified Engine** â€” stays sandbox. No live flip until the EOD-watchdog and
   reconciliation issues are fully closed and a clean week is logged.

**Milestones:**
- **Week 1:** ETF seed entry placed + reconciled to the rupee. Daily login routine
  proven (no missed token). Zero unplanned restarts.
- **Week 2:** V_BLD5_B paper running, daily P&L logged vs backtest expectation.
- **Week 4 decision gates:**
  - **Scale up** the ETF tranche only if: real fills tracked backtest within ~0.5%/leg
    AND no operational misses AND DD < 8%.
  - **Hold / scale down** if: any unexplained P&L gap > 1%, a missed trading day,
    or DD > 12%.
  - **Keep Alpha sleeves on paper** until they show â‰¥30 live-equivalent trades.

---

## 10. Decision matrix

| If your real starting capital isâ€¦ | The honest call |
|---|---|
| **< â‚¹50 L** | â‚¹1 Cr in 12 mo is **not a plan, it's a gamble.** Reframe the goal: target **15â€“25% (â‚¹7â€“12 L profit)**, compound, and grow the base over *years*. Forcing it with leverage most likely ends in ruin. |
| **â‚¹50 L â€“ â‚¹2 Cr** | Deploy the ETF core + paper the blend. Target **â‚¹15â€“40 L profit** this year. â‚¹1 Cr is a 2â€“4 year goal at this base, reached by **compounding + adding capital**, not by a heroic single year. |
| **â‚¹2 Cr â€“ â‚¹5 Cr** | â‚¹1 Cr is the **Bull-case ceiling**, not the plan. Base case â‚¹30â€“80 L. Run the full allocation in Â§4, lean on the ETF core, prove the Alpha sleeve live before sizing it. |
| **â‰¥ â‚¹5 Cr** | â‚¹1 Cr (â‰¤20%) is **genuinely achievable** with the validated blend if it holds live. This is the tier where the project's real edge and the target line up. Focus shifts from *return* to *risk control + operational reliability*. |

---

### The one-sentence version

**We have a credible ~15â€“20% engine and no real-money track record; so â‚¹1 Cr in 12
months is achievable only if you bring â‚¹5 Cr+ and the blend holds live â€” at any
smaller base, the right move is to prove the edge with real money small, compound
relentlessly, and treat â‚¹1 Cr as a multi-year target rather than a 12-month sprint
you force with leverage.**

*All performance figures are backtest or sandbox. No real-money results exist yet.
Nothing here is a return guarantee.*
