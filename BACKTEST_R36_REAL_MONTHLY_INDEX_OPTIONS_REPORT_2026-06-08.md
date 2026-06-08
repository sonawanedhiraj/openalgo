# R36 REAL — Monthly Directional Index Option Buying (REAL bhavcopy premiums)

**Date:** 2026-06-08 · **Verdict: REJECT across all 6 variants — and now a clean,
defensible REJECT (no synthetic-data caveat).**

R36 v1 rejected all 6 variants but with the binding caveat that **every premium was
synthetic Black-Scholes** — `fo_bhavcopy_eod` held equity F&O only. This round
backfills **real NIFTY/BANKNIFTY monthly index-option EOD** from the NSE bhavcopy
archive and re-runs the identical strategy. Full detail, per-variant files, and the
backfill loader: [`outputs/r36_monthly_index_options_REAL_2026-06-08/`](outputs/r36_monthly_index_options_REAL_2026-06-08/REPORT.md).

## Data (Stage 1)
New DuckDB table **`index_options_eod`** (Option B — parallel to `fo_bhavcopy_eod`,
which stays equity-only): **1,637,499 rows**, NIFTY + BANKNIFTY **monthly** expiries,
**2022-01-03 → 2026-06-04**, 0 parse failures. Monthly = max-expiry-in-month over the
global expiry set (drops weeklies, auto-handles Thu→Wed shifts). Canonical premium =
`close` (UDiFF `SttlmPric==ClsPric`; legacy `SETTLE_PR` is the contaminated underlying
→ corrected to `CLOSE`). Liquid-contract IVs verified 12–16%.

## Synthetic vs REAL (OOS 2024-01 → 2025-11 @25bps) — 0 BS fallbacks, 100% real
| V | N s→r | Sharpe s→r | Payoff s→r | Green% s→r | Ret% s→r |
|---|---|---|---|---|---|
| A | 7→6 | −0.88→−1.57 | 0.89→0.46 | 8.7→4.3 | −9.3→−13.6 |
| B | 12→8 | **+0.52→−0.32** | 2.30→1.12 | 21.7→13.0 | +11.5→**−4.8** |
| C | 22→20 | −0.47→−1.57 | 1.91→1.29 | 26.1→17.4 | −13.1→−31.3 |
| D | 7→4 | −0.91→−1.48 | 2.19→0.0 | 4.3→0.0 | −10.3→−10.8 |
| E | 14→8 | **+0.48→−0.34** | 2.89→1.08 | 21.7→13.0 | +10.3→**−4.6** |
| F | 12→8 | **+0.59→−0.21** | 5.47→2.22 | 13.0→8.7 | +16.2→**−4.2** |

**Real is uniformly worse.** The three marginally-positive synthetic variants
(B/E/F) all flip negative. Best real OOS Sharpe −0.21; best green% 17.4. The 4-year
window narrows the gap (B/E/F real +20–33% on the high-vol tail) but green months
stay 19–30%, far below the 45–55% gate.

## Mechanism (same contract, v1 vs real)
NIFTY 2024-02-27 22100CE: v1 synthetic entry **₹370.85** (IV=RV×1.10=9.57%); real
bhavcopy entry **₹513.30** (market IV ≈15.5%). Synthetic **underpriced by +38%** on a
calm day — real index IV floors above realized vol. Buyers pay it, then premium-stop
fires. **Synthetic BS was systematically optimistic for index-option buying.**
Hand-validated B-row-1 real P&L = **−₹17,285.08** (engine matches to the rupee).

## Verdict & finding
**REJECT — all 6.** Dominated by V_BLD_B (Sharpe 1.41, 70% green) and
Sector-Rotation-ETF. **Not a 5th sleeve.** Finding to flag: synthetic BS pricing
overstated index-option-buying edge by 0.4–1.1 Sharpe on OOS — **R8's PROMISING
verdict (BS-priced) deserves an optimistic-pricing caveat** and should be re-checked
against real premiums now that `index_options_eod` exists.

_(Not committed — left on the working tree per the operator's request.)_
