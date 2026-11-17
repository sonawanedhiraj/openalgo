# R85: premium selling / defined-risk options on F&O stocks (#790)

**Commits:**
- Phase 1 + pre-registration: `68e8decd9`, before any P&L
- freeze: `552e38566`, before the holdout

**Harness:** `backtest/r85_options/`

## Answer

**Nothing passes.**

O4, the expiry-week short strangle, is the strongest result in the project so far:
- positive in TRAIN, VALIDATE and the sealed holdout
- 9/10 half-years positive (2019–23)
- neighbours positive
- placebo p = 0.00 (30 draws)

It fails the holdout t gate: t = 1.11 against the 2.0 required. It also needs ₹44–72L of capital to earn about 1.5–2.4% a year on it.

O5, the earnings strangle, is negative on the holdout. O1, O2 and O3 fail TRAIN.

## Data (Phase 1)

- **Source:** NSE daily F&O bhavcopies, 2019-01-01 → 2026-11-13. Legacy format before 2024-07-08, UDiFF after.
- **Coverage:** 1,941 of 1,952 sessions. The 11 gaps are weekend special sessions plus 2021-03-30. 76.4M stock-option rows.
- **Untraded rows:** 58–85% of rows a year are untraded deep strikes. These are never entered.
- **Lot size:** UDiFF lot, or a legacy estimate from futures traded value (matches within 2% on 97.2% of 121k checks).
- **Spot:** the unadjusted front-month futures close.
- **Units fix:** UDiFF volume is in contracts. This was fixed before any strategy ran.
- **Liquidity:** names with median front-month option volume ≥ 2,000 contracts/day:

| Year | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|
| Names | 52 | 72 | 133 | 153 | 170 | 225 | 256 | 229 |

- **Liquidity filter:** every short leg needs ≥ 10 contracts traded and OI ≥ 50 lots on the entry day. The universe is the top 20 or 40 names by trailing option volume.
- **Limits:** daily closes only, so every entry, exit and stop is at a close. There is no bid/ask, so spread is modelled. Selling after the earnings crush cannot be timed with closes.
- **Earnings dates:** 76,615 NSE results broadcast timestamps. O5's holdout events stop at 2025-06, because later filings are not tagged "Quarterly" in the NSE list. It was not re-run, by instruction.

## Results

Net per trade (₹, 1 lot each, after charges) at central spread; t is clustered by entry date.

| Id | Structure | TRAIN 19–22 | VALIDATE 23–24H1 | HOLDOUT 24H2–26 | Verdict |
|---|---|---|---|---|---|
| O4 | Expiry-week short strangle (E-5 → E-1), z 1.0, top 20 | +1,624 (t 2.20) | +1,315 (t 2.14) | **+451 (t 1.11)** | fails t ≥ 2 on holdout |
| O1 | Monthly short strangle, z 1.0, target 50% / stop 2× / E-5 | −1,508 (−0.70) | +966 (0.61) | — | fails TRAIN |
| O2 | Monthly iron condor, wings z 1.75 | −1,273 (−1.35) | +128 (0.17) | — | fails TRAIN |
| O3 | Momentum credit spreads (bull put on top 8, bear call on bottom 8) | −506 (−1.00) | −121 (−0.24) | — | fails TRAIN and VAL |
| O5 | Earnings short strangle, 1-session hold | +300 (0.67) | +145 (0.27) | **−530 (−0.50)** | fails holdout; plateau failed (iron-condor version −797) |

### O4 in full

**Holdout:**
- 557 trades, 80.6% winners, average credit ₹5,275, costs ₹314 a trade.
- Daily Sharpe 1.14. Max drawdown −1.6% of peak margin.
- Worst day −₹79k (2026-03-23). Worst month −₹97k (2026-11). 1st-percentile trade −₹26.6k; worst trade −₹36k.
- Half-years: 4 of 5 positive (2026H2 −₹248).
- Neighbours on holdout: z 0.75 +₹622 (t 1.20); z 1.25 +₹361 (t 1.09).
- Placebo: random 4-session windows within the cycle average −₹472. p = 0.00 over 30 draws, fewer than the pre-registered 100. The 100-draw run was not completed.

**Annual net ₹1.06L a year** against peak margin of:
- ₹72.4L under the pre-registered delivery-ramp model, a return of 1.46%/yr
- ₹43.7L under broker practice, a return of 2.43%/yr

That is below the risk-free rate. Median margin per trade is ₹1.46L.

**Stress episodes:**
- COVID (2020-02-20 → 04-30): +₹3.36L
- 2022-05/06: +₹75k
- 2025 tariff month: −₹27k
- 2026-03: +₹84k

### Spread scenarios

Half-spread per leg: 0 / central max(1.5%, ₹0.05) / pessimistic max(3%, ₹0.10). Net per trade (₹, t):

| Id | Period | Zero | Central | Pessimistic |
|---|---|---|---|---|
| O4 | holdout | +658 (1.66) | +451 (1.11) | +244 (0.59) |
| O5 | holdout | −96 | −530 | −964 |
| O1 | train / val | −968 / +1,402 | −1,508 / +966 | −2,049 / +530 |
| O2 | train / val | −334 / +807 | −1,273 / +128 | −2,211 / −551 |
| O3 | train / val | +107 / +274 | −506 / −121 | −1,120 / −515 |

O4 does not reach t ≥ 2 on the holdout even at zero spread.

### Margin vs ₹20k

Fit to Kite `/margins/basket`, 2026-11-16 (`data/kite_margin_calibration.json`).

| Structure | Margin per lot (median) | Peak capital, top-20 book | At ₹20k |
|---|---|---|---|
| Naked strangle (O1, O5) | ₹1.2–1.5L | ₹9–38L | not viable |
| Expiry-week strangle (O4) | ₹1.46L + delivery ramp | ₹44–109L | not viable |
| Iron condor (O2) | ₹49–67k | ₹11–20L | not viable |
| Credit spread (O3) | ₹50–67k | ₹12–14L | not viable |

The cheapest single defined-risk spread on one stock costs about ₹33k at Kite today. **No structure fits ₹20k.**

### Physical settlement

- **O1, O2, O3 and O5** always exit by the close of E-5, the session before NSE's E-4 delivery-margin ramp. They never hold into expiry.
- **O4** trades inside the ramp. It adds delivery margin on ITM shorts at E-4..E-1 and always exits at E-1, never holding to expiry.
- **Ramp disclosure.** The pre-registered ramp (10/25/45/70% of contract value) overstates broker practice: 10/25/45% of VaR + ELM at E-4..E-2, and Zerodha 25% of contract value at E-1. Capital is reported under both models. P&L and gates are unaffected.

## Protocol

- **Pre-registration:** the data facts, cost and margin calibration (Kite calculators) and candidates were committed before any P&L. The engine was smoke-tested on 2019-02..04 for mechanics only.
- **Freeze:** the holdout list (O4, O5) was committed before the holdout opened.
- **Placebo:** incomplete. O4 has 30 of 100 draws; O5 was not run, because its holdout is negative.
- **Outputs:**
  - `data/summary_*.csv` (ledger, all periods and scenarios)
  - `data/frozen.json`
  - `data/placebo_O4_holdout.json`
  - `data/phase1_*`
