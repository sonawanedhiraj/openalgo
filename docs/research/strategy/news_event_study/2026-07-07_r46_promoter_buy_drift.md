# R46 — Promoter Open-Market Buying Drift (structured PIT data)

**Date:** 2026-07-07 · **Issue:** #371 · **Prior:** R43–R45 (news arc, all REJECT)
**Verdict: PROMISING, not deployable.** First half-consistent positive signal of the news arc — but short-horizon and too thin to clear costs long-only; the literature's medium-horizon version is regime-dominated on this window.

## Source pivot (why not PDFs)

The SAST-PDF pilot disqualified itself: 48% of PDFs non-text (OCR required), 71%
of extractable ones pledge filings, only 7% actual buys. Superseded by NSE's
structured insider API (`/api/corporates-pit` — returns acquirer, person
category, transaction type, mode, quantity, value as JSON; note it legitimately
returns empty for windows with no filings, which initially looked like a
broken endpoint). Harvest: `harvest_insider_pit.py`, 11,061 transactions,
2025-07-01..2026-07-07, 0 failed days. The R45 "SAST drift" signal is hereby
reinterpreted: that bucket was ~71% pledges, not promoter buying.

## Signal population

11,061 filings → 5,493 promoter/promoter-group → 3,740 buys → 2,870 open-market
purchases → 1,901 priced (129 SME/BSE-only/delisted symbols unfetchable via
Zerodha NSE — excluded, a survivorship caveat) → **1,167 deduped (symbol, day)
events**. Analysis: `analyze_promoter_buys.py` → `results_r46.duckdb`.

## Results (NIFTY-adjusted AR; split halves A <2026-01-01 ≤ B)

- **h=5: the one absolute cell that passes.** +0.54% (A, t=2.5) / +0.79% (B,
  t=3.2), pooled +0.66% (t=4.0, n=941). Same-sign both halves, significant.
- **h≥20: the R45 regime artifact, exactly.** A −0.9..−5.3%, B +4.8..+12.7% —
  sign flips everywhere; the 20/40-day sims profit only in half B (S2:
  −5.5%/trade in A, +9.2% in B). The medium-horizon PEAD-style hold is a
  smallcap-cycle bet, REJECT.
- **Size-matched control (the R45 discipline): PASSES.** Promoter-buy AR minus
  the same-universe neutral bucket is positive in BOTH halves at h=10
  (+1.24pp / +1.04pp) and h=20 (+0.97pp / +2.63pp) — promoter buys genuinely
  outperform their own universe's baseline, ~1pp/10d. The effect is real;
  the absolute P&L is hostage to the segment regime.
- Bigger buys are not better: the ≥₹1Cr bucket is *weaker* than ≥₹25L at
  every horizon (t@5 = 0.9) — signal value is in the disclosure, not its size.

## Why "not deployable" despite being real

Long-only capture of +0.5–0.8% over 5 days costs ~0.63% round-trip
(CNC + slippage) → ~0–0.2%/trade net. The consistent relative signal
(vs universe) needs the universe short leg to harvest — not implementable in
non-shortable smallcaps. Classification: **PROMISING** — a validated input,
not a strategy.

## Paths that could make it actionable (future, pre-declared)

1. Dedicated short-horizon design: h=5 with MIS-free CNC entry only on
   events also matching an existing validated setup (promoter-buy as a
   *filter/booster* on e.g. sector_follow candidates, not standalone).
2. Q4-2026 fresh-OOS re-run (#372) — if h=5 holds on a third independent
   window, revisit.
3. Cluster events (multiple promoter buys same symbol within 10d) — untested.

## Caveats

Survivorship (129 unpriced symbols, likely the smallest/most distressed);
12-month window = one smallcap down-leg + one up-leg; event dedup sums
same-day filings; h=40/60 truncated for recent events.
