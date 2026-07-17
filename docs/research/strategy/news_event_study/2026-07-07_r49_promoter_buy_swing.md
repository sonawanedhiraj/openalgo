# R49 — Promoter-Buy Smallcap Swing (liquidity/cost-realistic)

**Date:** 2026-07-07 · **Issue:** #385 · **Extends:** R46 (PROMISING) · **Prior arc:** R43–R47
**Verdict: REJECT for deployment. The R46 edge is real but lives in the untradeable
liquidity tier — it does not survive size-aware smallcap costs. R46 stays informational, not promotable.**

## Question

R46 found promoter open-market buys drift +0.66%/5d (t=4.0, half-consistent),
but used a flat 0.25%/leg slippage calibrated for F&O large caps while 97.5% of
events are smallcaps (median day0 turnover ₹2Cr, p10 ₹9.5L). Operator preference:
smallcap **swing**, not intraday. Does the edge survive when (a) restricted to
tradable-liquidity names and (b) charged size-aware slippage?

## Method

Swing-only (intraday grids already REJECTED in R43/R44): T+1-open entry, CNC
delivery, holds of 5 and 10 trading days (R46's two half-consistent horizons)
plus a 5%-stop/h10 managed variant. 934 R46 events with valid 20-day history.
Per event: **ADV20** = median (close×volume) over 20 prior trading days →
liquidity tier THIN (<₹1Cr) / TRADABLE (₹1–5Cr) / LIQUID (≥₹5Cr). **Size-aware
slippage** `slip% = max(0.25%, 0.08·√(order_value/ADV20))` per leg (square-root
market-impact, K fit to ₹1Cr→~0.4% / ₹25L→~1.5% anchors). Conditioning cells
(pre-declared): cluster (≥2 buys/10d), first-buy-in-6mo, big-relative-to-ADV.
Split-half A(<2026-01-01)/B. Script: `analyze_promoter_swing.py`.

## The finding: the edge is anti-correlated with tradability

**Drift AR (NIFTY-adj) by tier — the R46 signal decomposed:**

| tier | h | half A (t) | half B (t) | pooled (t) |
|---|---|---|---|---|
| THIN | 5 | +1.23 (3.3) | +0.85 (2.1) | +1.04 (3.8) |
| THIN | 10 | +1.31 (2.1) | +1.45 (2.9) | +1.38 (3.4) |
| TRADABLE | 5 | −0.07 (−0.2) | +0.30 (0.6) | +0.10 (0.3) |
| TRADABLE | 10 | −0.87 (−1.6) | +1.76 (2.2) | +0.37 (0.8) |
| LIQUID | 5 | +0.39 (1.1) | +1.18 (3.3) | +0.76 (3.0) |
| LIQUID | 10 | +0.55 (1.1) | +3.19 (5.6) | +1.79 (4.7) |

R46's headline (half-consistent AND both-half-significant) is a **THIN-tier
phenomenon**. TRADABLE has no signal (t≈0 to −1.6). LIQUID is significant only
in half B — the smallcap-regime-blowup signature R45/R46 already flagged.

**Swing net returns (all-condition, size-aware costs):** every TRADABLE/LIQUID
cell sign-flips between halves or is negative in both (TRADABLE h5 −0.94%
pooled, both halves negative). THIN nets −1.6% to −3.2%/trade in **both**
halves — the real AR is there, but median ~1%/leg + p90 ~2.4%/leg slippage
eats it whole.

**Slippage applied (₹50k order):** THIN median 0.99% / p90 2.40% per leg;
TRADABLE 0.37% / 0.52%; LIQUID 0.25% (floor). The cost is highest exactly
where the alpha is.

**Size-matched control (TRADABLE vs R45 neutral):** spread flips sign at h5
(A +0.53pp / B −0.24pp) — no robust relative edge in the tradable tier even
before costs.

**Only nominal survivor:** LIQUID×FIRST_BUY×h10 (n=83, A +0.55% / B +5.86%) —
same-sign positive both halves, but half A barely clears and half B is 10×
larger: the regime-artifact signature, not trusted. n too small regardless.

## Conclusion

The promoter-buy drift is **real but structurally untradeable**: it is a
compensation for illiquidity, not a free lunch. Concentrated in names too thin
to trade ₹50k without ~1%+ impact; wherever liquidity is sufficient, the signal
is gone or regime-dependent. This downgrades R46 from a promotion candidate to
a closed line for standalone deployment.

Combined R43–R49: no framing of the NSE news/insider feed — chase, delayed
pattern, multi-day drift, promoter direction, or liquidity-filtered swing —
produces a deployable long edge for this book. **The news pipeline is closed;
informational-alert use only.**

## What (narrowly) remains

1. Q4-2026 OOS re-run (#372) still worth doing as a clean confirmation, but the
   bar is now "does it survive liquidity filtering," which R49 says it won't.
2. If ever revisited: promoter-buy as a *discretionary watch* on a name already
   surfaced by a liquid, validated setup — never as the liquidity-defining leg.

## Caveats

Square-root impact K is fit, not measured from real fills (no smallcap paper
trades yet); the direction of the bias (thin names cost even more than modeled)
only strengthens the REJECT. Survivorship: 129 unpriced symbols excluded upstream.
