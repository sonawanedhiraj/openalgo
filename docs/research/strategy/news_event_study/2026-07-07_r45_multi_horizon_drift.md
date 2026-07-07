# R45 — News-Event Multi-Horizon Drift (PEAD-style)

**Date:** 2026-07-07 · **Issue:** #368 · **Prior:** R43 (chase REJECT), R44 (delayed entries REJECT)
**Verdict: REJECT absolute deployment. The apparent multi-day drift is a benchmark/regime artifact; the one durable cross-sectional fact (down-reactions keep underperforming) is not implementable long-only.**

## Question

Literature (PEAD in India: abnormal returns over ~64-day holds conditioned on
earnings surprise; announcement-drift studies over days–weeks) says R43/R44
tested the wrong horizon. Does the day-0 tape reaction predict market-adjusted
drift over T+1..T+20 trading days?

## Data & method

19,062 event groups (12-month announcement tape incl. a 3,888-event SAST
supplement missing from R43's filter), forward daily bars for 1,953 symbols +
NIFTY (515k bars). Per event: day-0 reaction r0 = close vs prev close;
market-adjusted abnormal return (AR) to T+1/2/3/5/10/15/20 closes (NIFTY
subtracted); slices by reaction bucket, F&O membership, price bucket,
category, volume confirmation. **Discipline:** every cell reported on two
independent halves (A: Jul–Dec 2025, B: Jan–Jul 2026); 3 pre-declared sims
with real CNC charges; no post-hoc cell shopping. Script:
`backtest/news_event_study/analyze_drift.py`.

## Headline: the pooled "drift" is real in-sample and fake out-of-sample

Pooled, the tables look like textbook PEAD (>+5% bucket +1.6% AR by T+20,
t=4.6; ≤−5% bucket −0.6%..−1.1%, t up to −5.5). **The half-split kills it:**
in half A *every* bucket drifts negative (even >+5%: −1.6% @T+20); in half B
*every* bucket drifts positive (even −5..−2%: +1.9% @T+20). The event
universe is smallcap-tilted while the benchmark is NIFTY — the "abnormal
return" is mostly the smallcap-vs-largecap cycle (H2-2025 smallcap weakness,
H1-2026 recovery), not news information. Any absolute long strategy built on
these tables inherits that regime bet.

## What survives the half test (and what it's worth)

- **Cross-sectional ordering survives:** relative to the same-half neutral
  bucket, up-reactions outperform and down-reactions underperform in BOTH
  halves. But the long side's consistent spread is thin (+0.1..+1.0pp per 20d
  — under round-trip costs), while the robust side is **≤−5% reactions
  underperforming peers by −1.3..−1.6pp per 20d in both halves**. That is a
  short/avoid signal on smallcaps — not implementable long-only, not
  shortable multi-day in cash equities, and the F&O subset (shortable via
  futures) does NOT show it (F&O ≤−5% is positive in half B).
- **Loud-vs-quiet (suggestive only):** volume-confirmed up-moves drift
  *worse* than quiet up-moves pooled (+2..+5% with vol≥2×avg20: negative to
  T+10) — the third round in a row where volume confirmation is
  anti-selective. The quiet-strength cell itself fails halves (−1.3% A /
  +3.8% B) — regime, not edge.
- **SAST (takeover-reg filings):** the only category with positive AR@10 in
  both halves (+1.2% / +2.5%), and the pre-declared H3 sim is the only
  net-positive sim pooled (+1.65%/trade) — **but** AR@20 flips sign (−1.2% A /
  +4.4% B) and the H3 sim loses −3.8%/trade in half A. Direction
  (promoter buy vs sell) is NOT in the feed summaries (pure Reg-29/31
  boilerplate; direction lives in the PDF attachments), so the
  literature-backed promoter-buying signal remains **untested, not refuted**.

## Pre-declared sims (₹50k/pos, CNC charges, T+1-open entry)

| Sim | n | Net/trade | Half A | Half B |
|---|---|---|---|---|
| H1 results r0≥+3% + vol, hold 10d | 1,086 | −1.99% | −2.27% | −1.78% |
| H2 order-wins r0≥+2%, hold 10d | 341 | −0.06% | −1.87% | +1.43% |
| H3 SAST hold 20d | 2,155 | +1.65% | −3.77% | +3.22% |

None passes both halves. H1 (the PEAD-on-results hypothesis) rejects
decisively — Indian results-day winners in this sample do NOT continue.

## Combined R43–R45 conclusion

Across 0–20 day horizons, chase / delayed-pattern / drift framings all fail
the deployability bar for a long-only cash-equity trader. News moves stocks —
but the direction it moves them *after* day 0 is dominated by segment regime,
and the residual cross-sectional signal is on the short side in
non-shortable names. **Stop testing long entries keyed off the announcement
feed.**

## What could still change the verdict (concrete, literature-backed)

1. **Promoter-direction parsing (the real H3):** LLM/regex over SAST +
   insider-trading PDF attachments to recover buy-vs-sell + size; test
   promoter *market purchases* ≥ some % of equity, hold 20–60d. Positive
   both-halves base rate at T+10 makes this the best-qualified follow-up.
2. **True SUE:** parse results PDFs for actual EPS vs prior — the PEAD
   literature conditions on the *earnings* surprise, not the price reaction;
   our r0-proxy may be the weak link.
3. **Fresh OOS re-run** of this exact script in Q4-2026 — every regime
   conclusion here rests on one smallcap down-leg and one up-leg.

## Caveats

- NIFTY is an imperfect benchmark for a smallcap-tilted universe (this is
  the round's core finding, not an oversight — a smallcap index benchmark
  would isolate true idiosyncratic drift; NIFTY daily was the available
  clean series).
- Survivorship: today's master contract resolves symbols; year-old delistings
  drop out.
- SAST supplement derives eff_date from a 15:25 IST cutoff heuristic.
