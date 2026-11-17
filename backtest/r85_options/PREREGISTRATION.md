# R85 pre-registration — premium selling / defined-risk options on F&O stocks (#790)

Committed AFTER Phase 1 (data validation; descriptive only) and BEFORE any strategy P&L was computed or viewed.
The engine was smoke-tested on 2019-02..04 for mechanics only (trade counts, exit reasons, margins — P&L not printed).

## Data (Phase 1)
- **Source:** NSE daily F&O bhavcopies 2019-01-01 → 2026-11-13. Legacy format before 2024-07-08, UDiFF after. Stock options and stock futures only.
- **Spot:** the front-month stock-futures close from the same bhavcopy. It is UNADJUSTED, so it matches the strikes. Basis is a known small bias.
- **Volatility:** 60-day realised vol of Kite split-adjusted daily returns.
- **Lot size:** UDiFF `NewBrdLotQty`. In the legacy era, futures traded value / (contracts × close), rounded to 25.
- **Earnings:** NSE financial-results broadcast timestamps, 2018-12 → 2026-11 (76,615 filings), quarterly only.
- **Daily data only.** Entries and exits happen at a session's close. There are no intraday stops: a stop is checked at the close and executed at the close.

## Execution / pricing (fixed)
- **Entry:** at the entry session's close.
  - Every leg must have traded that day (contracts ≥ 1, close > 0).
  - Every SHORT leg must also have **≥ 10 contracts traded and OI ≥ 50 lots**.
  - Strikes are the listed strikes nearest the target that TRADED that day. Wings must sit strictly beyond the short strike.
- **Marks and exits:** the close if the contract traded that day; otherwise the exchange settlement price, with a **doubled** spread penalty.
- **Spread** (half-spread per leg per transaction):
  - zero = 0
  - **central = max(1.5% of price, ₹0.05)**
  - pessimistic = max(3% of price, ₹0.10)

  Basis: live intraday entries for open15 show a 1.5% FULL spread of mid. The post-close ATM sweep shows 3.0% in the top OI quintile and a 5.2% median. **PASS is judged at central. A result that survives only at zero spread is a FAIL.**
- **Charges** (Kite `/charges/orders`, 2026-11-16, applied to all history): ₹20 brokerage per order; STT 0.15% of premium on sells; exchange 0.03553%; SEBI; stamp 0.003% on buys; GST 18% on brokerage + exchange + SEBI.
- **Physical settlement.** Every position is closed at the close of **E-5**, the session before NSE's E-4 → E-1 delivery-margin ramp. No position is ever held into expiry. O4 deliberately trades inside the ramp: it adds 10/25/45/70% of contract value as margin on ITM shorts at E-4/E-3/E-2/E-1, and exits at E-1.
- **Margin** (fit to Kite `/margins/basket` on 5 stocks, 2026-11-16, applied to all history):
  - Naked short put = S·lot·max(0.17 − 0.70·OTM + ITM, 0.08).
  - Naked short call = S·lot·max(0.18 − 0.55·OTM + ITM, 0.08).
  - Strangle = max(put, call) + 0.25·min(put, call).
  - Any credit spread or condor = 1.4 × wing width × lot.
  - Margin is re-marked daily with spot.
  - **Capital required = peak concurrent margin across open trades.** It is reported per strategy.

## Universe
At each entry, names are ranked by the prior-60-session median of front-expiry option contracts (all strikes); the top N are used. Being present in the bhavcopy is point-in-time F&O membership by construction.

## Periods (by entry date)
- TRAIN 2019-01 → 2022-12
- VALIDATE 2023-01 → 2024-06
- **SEALED HOLDOUT 2024-07 → 2026-11-13**

## Candidates (σ = 60d realised vol, T = sessions to expiry / 252, strikes at S·exp(∓z·σ·√T))
| Id | Structure | Entry → exit | Neighbours (plateau, judged on VALIDATE) |
|---|---|---|---|
| O1 | Short strangle, z = 1.0, top 20 liquid names, 1 lot each | first session of each monthly cycle → profit target 50% of credit / stop at loss = 2× credit / E-5 | z 0.75, 1.25; target 40%, 60%; stop 1.5×, 3×; N 10, 40 |
| O2 | Iron condor: O1 shorts + wings at z = 1.75, top 20 | same; no stop (defined risk) | z 0.75, 1.25; wings 1.5, 2.0; target 40%, 60% |
| O3 | Directional credit spreads: from the top 40 liquid names, bull put spread on the 8 highest 6-1 momentum, bear call spread on the 8 lowest. Short z 1.0, long z 1.75 | first session of cycle → target 50% / E-5 | z 0.75, 1.25; k 5, 12 |
| O4 | Expiry-week short strangle, z = 1.0 (T = sessions to expiry), top 20 | E-5 close → target 50% / stop 2× / E-1, with the delivery-margin ramp | z 0.75, 1.25 |
| O5 | Earnings short strangle, z = 1.0, top-40 liquid names. Opened at the last close BEFORE the results are public; closed at the close of the first session after. Front month unless that breaches E-5, then the next month | 1-session hold | z 0.75, 1.25; iron-condor version (wings 1.75) |

The user's "sell after the announcement when IV collapses" variant **cannot be expressed** with daily closes: the crush happens at the open after the announcement, and the first price we can sell at is that day's close, after the crush. O5 is therefore the classic pre-announcement short, held across the event.

## Rationale (stated before testing)
- **O1, O2:** the variance risk premium. Option buyers (hedgers and lottery-seekers) persistently pay implied volatility above the volatility that is later realised.
- **O3:** the same premium, collected on the side momentum says is less likely to be tested.
- **O4:** theta accelerates near expiry, and retail buys cheap weekly lottery tickets.
- **O5:** implied volatility is bid up into the event and collapses after it.

## Gates (all at central spread; PASS needs ALL)
- **Holdout:** net ₹ per trade > 0 with **t ≥ 2**, clustered by entry date (≥ 2.58 = confirmed).
- **Annual net return on peak-margin capital > 0 in every period.**
- TRAIN and VALIDATE net per trade > 0.
- **≥ 7/10 half-years positive** in 2019–2023.
- **Plateau:** every neighbour's net per trade > 0 on VALIDATE.
- **Placebo p < 0.05**, 100 draws:
  - O1, O2: a random entry session inside the cycle, same exits.
  - O3: random names from the same top 40.
  - O4: a random 4-session window in the cycle.
  - O5: random non-event 1-session windows.
- **Tails reported for every candidate:** max drawdown on capital, worst day and worst month, 1st and 5th percentile trade, worst trade, and the 2020-03 / 2022-06 / 2024-06-04 / 2025-04 / 2026-03 episodes.
- Only candidates with TRAIN and VALIDATE both > 0 at central go to the holdout. That list is frozen in `data/frozen.json` by commit first.

## Phase 1 facts (validated before this commit; `data/phase1_*.csv`, `phase1_summary.json`)

**Coverage.** 1,941 of 1,952 NSE sessions, 2019-01 → 2026-11-13. The 11 gaps are weekend special sessions (Muhurat, Budget Saturday) plus 2021-03-30. There are 76.4M stock-option rows.

**Untraded rows.** 58–85% of rows a year are untraded, mostly deep strikes. Their "close" is stale, so they are never used for entries. Front-month strikes within ±15% of spot traded on a median 25–79% of strikes a day.

**Lot sizes.** In the legacy era the lot is recovered from futures traded value. It matches the UDiFF lot within 2% on 97.2% of 121k checks.

**UDiFF units bug, found and fixed.** UDiFF `TtlTradgVol` is in CONTRACTS, not shares (traded value = volume × lot × price, verified on RELIANCE 2025-09-10). The first parse divided by lot. It was fixed before any strategy ran, and volume is continuous across the 2024-07 format change (RELIANCE / SBIN / TATAPOWER monthly medians).

**Usable names** (median front-month contracts ≥ 2,000 a day):

| Year | Names |
|---|---|
| 2019 | 52 |
| 2020 | 72 |
| 2021 | 133 |
| 2022 | 153 |
| 2023 | 170 |
| 2024 | 225 |
| 2025 | 256 |
| 2026 | 229 |

A top-20 / top-40 liquidity universe is therefore always deep enough.

**What the data cannot support:**
- intraday entries, exits or stops
- bid/ask (spread is modelled, see the scenarios above)
- the "sell after the announcement" timing (see O5)
