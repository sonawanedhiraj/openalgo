# R82 pre-registration: sizing under ₹1L buying power, all-in sizing, execution (#786)

Written and committed **before** any Phase 2/4/5 test was run. Phase 1 (buying-power measurement) and Phase 3
(cost arithmetic) are measurements, not tests, and were run first; their results are in the research doc.

Nothing here changes engine code or parameters. Every rule below is implemented only in the research harness
(`backtest/r82_sizing/`), which drives the unchanged `SimplifiedStockEngine` (mode=disabled).

## Fixed facts the design rests on

- **Buying power is ₹1,00,000:** engine config ₹20,000 × 5. Zerodha grants up to 5× on equity MIS (20% margin); some stocks get less. The harness uses 5× for every name, which can only flatter the results.
- **Charges:** Zerodha MIS — brokerage min(₹20, 0.03%) per order, STT 0.025% on the sell leg, exchange 0.00307% (#783), SEBI, stamp duty 0.003% on the buy leg, GST.
- **Slippage assumption:** 2.4 bps per side on market orders.

## Candidates (Phase 5)

1. **BASE — the live-rules baseline.** Real in-house scanner signals feed the unchanged engine (arming, 5m breakout entry, ATR stop, trail, global lock, 15:20 EOD).
   - TRAIN 2024-01-01..2024-12-31 and VALIDATE 2025-01-01..2025-06-19 (R80 signals).
   - HOLDOUT 2025-06-20..2026-07-06 (R56 signals). R56 already reported this window under the old sizing, so it is **not virgin**. The new sizing and execution have never been run on it; this is disclosed.
2. **E3S — R81's E3 (large-gap fade), short side only.** The spec is otherwise unchanged; the PIT universe is from NSE month-end lists.
   - TRAIN 2019–2022 and VALIDATE 2023.
   - IN-SAMPLE-3 2024-01-01..2025-06-19: R81's holdout, already opened.
   - **SEALED HOLDOUT 2025-06-20..2026-11-13** (forward window, never seen by any round).

## Phase 2 — sizing rules (all with a buying-power ledger except S0)

The ledger: `BL = ₹1,00,000 − Σ(entry notional of positions still open)`, evaluated at the moment of entry.

| Id | Rule |
|---|---|
| S0 | Legacy (all prior rounds): `q = min(₹1L // P, ₹500 // R)`. No ledger. |
| S0L | Legacy with ledger: `q = min(BL // P, ₹500 // R)`. Skip if q < 1. |
| A500 / A750 / A1000 | All-in rule with budget B ∈ {500, 750, 1000}. |
| APCT | All-in rule with B = 2.5% × running equity. Running equity = ₹20,000 + cumulative realized net within the period. When equity ≤ 0, trading stops (ruin), and the ruin date is reported. |

**All-in rule.** Inputs: P = entry reference price; R = max(1.5 × engine ATR, ₹1) per share (the engine's own stop distance).

- **Per-share reserve:** `R + s + g`.
  - `s = 2 × 2.4 bps × P` (round-trip slippage).
  - `g = k_gap × R`, where **k_gap = the mean of (fill beyond stop)/R over the candidate's TRAIN stopped trades under S0/X0**. It is estimated once on TRAIN, frozen, and printed in the results.
- **Quantity:** `q_B` = the largest integer q with `q·(R + s + g) + charges(qP buy, qP sell) ≤ B`. Charges come from the exact schedule above.
- **Buying-power cap:** `q = min(q_B, BL // P)`.
- **Skip** if `q < 1` or `q < ceil(0.5 × q_B)`. The "minimum viable quantity" is half the risk-budgeted size: below it, buying power, not risk, would be setting the trade.
- **Neighbour `_nomin`:** the same rule without the half-size floor. It is reported, not selectable.
- **Engine coupling:** `max_risk_per_trade` is set to B in the harness config, so the engine's rupee-denominated trail start (0.6 × B) and global profit lock scale with the budget. For APCT the engine uses ₹500.
- **Feasibility:**
  - A budget is **unreachable** if, on TRAIN, the median ratio (`q·(R + s + g) + charges`) / B is below 0.9. That means buying power, not the budget, sets the size.
  - Unreachable budgets are reported as such, with their numbers labelled infeasible, and are not selectable.

## Phase 4 — execution variants

| Id | Entry | Entry slip | Exit slip |
|---|---|---|---|
| X0 | Market at signal-bar close (current assumption) | 2.4 bps | 2.4 bps |
| X1 | Market at the open of the first 1m bar after the signal | 2.4 bps | 2.4 bps |
| XL5 | Limit at the signal reference price, valid 5 min; filled only on a trade-through (long: a later 1m low < L; short: high > L) | 0 | 2.4 bps |
| XL2 / XL10 | As XL5 with a 2 / 10 min wait (plateau neighbours, not selectable) | 0 | 2.4 bps |
| XLi5 | Limit 5 bps inside the reference (long L = P(1 − 0.0005)), 5 min, trade-through | 0 | 2.4 bps |
| XS | X0, but excluding symbols in the worst quintile of TRAIN-estimated spread | 2.4 bps | 2.4 bps |

Further execution rules:

- **Limit fills:** an unfilled limit is a **missed trade**. For every miss, the harness also records what X0 would have made on it, so the winners missed are reported.
  - Under a limit, the stop is fill ∓ R.
  - For BASE the engine's pending entry is cleared, and the symbol stays armed exactly as in live after an unfilled order.
- **XS spread estimate:** the Abdi–Ranaldo (2017) estimator on 1m bars from 09:30 to 15:15, averaged per symbol over TRAIN.
  - No real equity MIS fills exist in this install. The sandbox fills at LTP; open15 trades only options. So **realised equity slippage cannot be measured**: the 2.4 bps is an assumption, and AR is a bar-based proxy.
  - AR is also reported by 30-minute time-of-day bucket and by liquidity (ADV) quintile.

## Selection (TRAIN + VALIDATE only) and the holdout

- **Grid:** {S0L, A500, A750, A1000, APCT} × {X0, X1, XL5, XLi5, XS}. Unreachable budgets are excluded.
- **Choice:** the config with the highest VALIDATE net ₹/trade, among those whose TRAIN net ₹/trade ≥ TRAIN net ₹/trade of S0L/X0.
  - Ties go to the simpler rule.
  - The choice is frozen, its id is written to `data/frozen.json` and committed, and only then is the holdout run.
- **E3S:** "VALIDATE" for selection is 2023 alone. IN-SAMPLE-3 is reported but not used to choose.
- **Gates for a RESULT** (the same as R81):
  - Positive net in every period, and on the holdout |t| ≥ 2 (≥ 2.58 = confirmed).
  - Half-years: E3S needs ≥ 7/10 positive over 2019–2023; BASE needs all 3 positive over 2024H1–2025H1.
  - **Plateau:** the selected config's neighbours (XL2/XL10 for a limit choice, budget ±1 step, `_nomin`; for E3S also the R81 gap neighbours g0.75/g1.25 short-only) are all positive on VALIDATE.
  - **Placebo:** the R67 timing + selection placebo at p < 0.05. It is computed only for configs that pass the sign gates, because a negative config needs no placebo to be rejected.
- **Revised bar:** for each config, `bar_bps = Σcharges/Σnotional + entry slip + exit slip`, at that config's own notionals. Gross bps are reported against it.
- **Ledger:** every config × period is recorded in `data/variant_ledger_r82.csv`, failures included.
- **One-period results are noise.**
