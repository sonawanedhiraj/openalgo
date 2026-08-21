# Round 59 — open15_vol_breakout July 2026: stock vs ATM-option backtest (issue #453)

**Date:** 2026-07-25 · **Window:** 2026-07-01 → 2026-07-24 (18 trading days) ·
**Verdict:** both legs POSITIVE this month on the honest entry convention; the
option leg beat the stock leg ~4.4× in rupees on the same signals, but one trade
(NATIONALUM 07-23) is 68% of the option P&L. Fills the strategies-dashboard
Backtest column (`parity_target` in `config_snapshot.json`).

## What was run (production-faithful)

- **Rules/config = the deployed defaults** from
  `services.open15_breakout_service.resolve_day_config(None, 0)`:
  vol_mult 1.5, top-3 gainers long / top-3 losers short, **max_trades 3/day**
  (first-come across both sides), notional ₹150k/trade (₹30k slot × 5×), entry
  window 09:16–09:29, hard exit 09:30 open, MIS.
- **Charges = production models**: `mis_round_trip_charges` (stock),
  `option_round_trip_charges` (options). Option contract selection =
  production `pick_contract` (nearest strike to trigger price, nearest alive
  expiry → all July trades resolve to the 28-JUL monthly).
- **Data**: selection layer + equity 1m from the **stored historify DB**
  (exported through the running app's `/historify/api/data` with the logged-in
  session — nothing re-downloaded); option 1m premiums = **real broker
  (Zerodha) bars**, cache-first (`backtest/options_open15/data/july_fetch_cache.json`),
  only missing contracts fetched.
- **Entry convention (the R58 look-ahead discipline)**: 1m bars cannot see the
  mid-bar tick fill production gets, so the PRIMARY entry is the **open of the
  minute after the trigger minute** (the production option-shadow convention,
  real-time legal). Trigger-close and level entries are reported as
  sensitivity/reference only.
- Harness: `backtest/options_open15/july_full_run.py` (untracked-local per
  R56/57 precedent); trade dump `r59_trades.json` in the session scratchpad.

## Daily top-3 gainers / losers (the strategy's selection)

| date | long picks (gap%) | short picks (gap%) |
|---|---|---|
| 2026-07-01 | PAGEIND (+2.07), TITAN (+1.48), SHREECEM (+1.35) | KPITTECH (-10.00), MANAPPURAM (-1.60), TATAELXSI (-1.56) |
| 2026-07-02 | TVSMOTOR (+2.63), INFY (+2.14), VMM (+1.83) | BAJFINANCE (-1.99), OIL (-1.14), GVT&D (-0.93) |
| 2026-07-03 | HCLTECH (+4.54), NATIONALUM (+2.33), MUTHOOTFIN (+2.27) | POWERINDIA (-5.00), CGPOWER (-5.00), GVT&D (-5.00) |
| 2026-07-06 | GODREJCP (+1.68), OBEROIRLTY (+1.35), ASTRAL (+1.23) | KOTAKBANK (-1.76), VBL (-1.12), POLICYBZR (-0.95) |
| 2026-07-07 | HDFCBANK (+1.58), TITAN (+1.46), VBL (+1.04) | TRENT (-7.89), KALYANKJIL (-5.35), COCHINSHIP (-3.66) |
| 2026-07-08 | KALYANKJIL (+1.48), NAUKRI (+1.08), ONGC (+1.08) | HINDPETRO (-2.70), INDIGO (-2.38), BPCL (-2.23) |
| 2026-07-09 | DIXON (+1.69), PHOENIXLTD (+1.61), KALYANKJIL (+1.56) | BAJFINANCE (-1.03), DRREDDY (-0.67), LODHA (-0.66) |
| 2026-07-10 | DIXON (+2.95), TCS (+2.72), INFY (+2.30) | DRREDDY (-1.94), AUROPHARMA (-0.71), TORNTPHARM (-0.66) |
| 2026-07-13 | OIL (+0.82), ONGC (+0.55), LTM (+0.50) | BAJAJHLDNG (-4.73), DMART (-3.21), INDIGO (-2.58) |
| 2026-07-14 | BIOCON (+3.99), ONGC (+1.43), OIL (+1.12) | BANDHANBNK (-2.42), INDIGO (-2.11), GODREJCP (-1.97) |
| 2026-07-15 | HYUNDAI (+1.80), DIXON (+1.61), MANAPPURAM (+1.49) | TATAELXSI (-4.44), TECHM (-1.29), INFY (-1.27) |
| 2026-07-16 | DIXON (+3.30), HDFCLIFE (+2.86), HINDALCO (+1.74) | ICICIGI (-6.32), AUBANK (-1.48), KALYANKJIL (-0.57) |
| 2026-07-17 | JIOFIN (+5.03), 360ONE (+2.50), BHEL (+2.21) | WIPRO (-2.36), TORNTPHARM (-1.45), UNITDSPR (-1.35) |
| 2026-07-20 | PNB (+2.35), JSWSTEEL (+1.25), GRASIM (+0.81) | AXISBANK (-4.03), HDFCBANK (-3.61), FEDERALBNK (-2.15) |
| 2026-07-21 | ULTRACEMCO (+1.40), SONACOMS (+1.18), HINDZINC (+0.82) | HDFCBANK (-1.09), AXISBANK (-0.88), PAYTM (-0.78) |
| 2026-07-22 | BAJAJ-AUTO (+1.99), TVSMOTOR (+1.55), NYKAA (+1.31) | BANDHANBNK (-10.00), INDIGO (-1.50), DRREDDY (-1.24) |
| 2026-07-23 | OFSS (+4.13), ETERNAL (+1.92), NATIONALUM (+1.05) | DRREDDY (-6.92), INDUSINDBK (-3.06), BPCL (-2.74) |
| 2026-07-24 | SRF (+1.91), MPHASIS (+0.65), MOTILALOFS (+0.45) | CGPOWER (-2.69), TITAN (-1.97), INFY (-1.85) |

Gap sanity-checked against broker daily bars — the exact −5.00%/−10.00% opens
(07-01 KPITTECH, 07-03 power names, 07-22 BANDHANBNK) are real circuit-band
opens, not corporate-action artifacts.

## Executed trades — stock leg vs option leg (same signals)

15 signals fired the 1.5× volume-breakout in 09:16–09:29 (max-3/day cap applied
in trigger order). Option leg = production option-mode: BUY the ATM 28-JUL CE/PE,
`lots = floor(₹30k / (premium × lot))`; 2 signals were **unaffordable** (1 lot
premium > ₹30k), exactly as production would journal them.

| date | sym | side | gap% | trig | qty | entry | exit | stock net | option contract | lots | prem in->out | option net |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-07-01 | MANAPPURAM | S | -1.60 | 09:27 | 469 | 319.75 | 319.25 | +134 | -- unaffordable | | | -- |
| 2026-07-01 | SHREECEM | L | +1.35 | 09:27 | 5 | 25820.00 | 25790.00 | -243 | SHREECEM28JUL2625750CE | 1 | 782.5->755.0 | -907 |
| 2026-07-02 | VMM | L | +1.83 | 09:16 | 1251 | 119.84 | 119.85 | -88 | VMM28JUL26120CE | 1 | 4.0->4.1 | +20 |
| 2026-07-02 | GVT&D | S | -0.93 | 09:20 | 31 | 4796.90 | 4752.10 | +1,289 | -- unaffordable | | | -- |
| 2026-07-06 | POLICYBZR | S | -0.95 | 09:19 | 94 | 1587.20 | 1582.80 | +314 | POLICYBZR28JUL261580PE | 1 | 51.3->54.9 | +1,029 |
| 2026-07-07 | TITAN | L | +1.46 | 09:25 | 32 | 4591.00 | 4596.30 | +71 | TITAN28JUL264600CE | 1 | 95.2->99.0 | +483 |
| 2026-07-08 | BPCL | S | -2.23 | 09:24 | 497 | 301.60 | 302.80 | -696 | BPCL28JUL26300PE | 1 | 7.8->7.2 | -1,462 |
| 2026-07-09 | DRREDDY | S | -0.67 | 09:21 | 113 | 1321.40 | 1327.70 | -812 | DRREDDY28JUL261320PE | 1 | 39.5->37.5 | -1,480 |
| 2026-07-09 | KALYANKJIL | L | +1.56 | 09:26 | 371 | 403.95 | 407.10 | +1,068 | KALYANKJIL28JUL26405CE | 1 | 17.9->18.9 | +1,080 |
| 2026-07-10 | AUROPHARMA | S | -0.71 | 09:18 | 96 | 1556.70 | 1557.70 | -196 | AUROPHARMA28JUL261560PE | 1 | 38.6->38.9 | -100 |
| 2026-07-13 | ONGC | L | +0.55 | 09:24 | 604 | 248.20 | 248.52 | +93 | ONGC28JUL26247.5CE | 2 | 5.3->5.6 | +1,264 |
| 2026-07-15 | HYUNDAI | L | +1.80 | 09:19 | 74 | 2023.00 | 2030.60 | +462 | HYUNDAI28JUL262020CE | 1 | 60.5->67.5 | +1,720 |
| 2026-07-20 | HDFCBANK | S | -3.61 | 09:28 | 192 | 778.40 | 778.80 | -177 | HDFCBANK28JUL26780PE | 3 | 13.2->13.2 | -472 |
| 2026-07-22 | BAJAJ-AUTO | L | +1.99 | 09:20 | 14 | 10697.50 | 10716.50 | +166 | BAJAJ-AUTO28JUL2610700CE | 2 | 152.0->170.0 | +2,436 |
| 2026-07-23 | NATIONALUM | L | +1.05 | 09:21 | 433 | 346.30 | 349.25 | +1,177 | NATIONALUM28JUL26345CE | 2 | 6.2->8.3 | +7,583 |

No signals on 07-03, 07-14, 07-16, 07-17, 07-21, 07-24 (level+volume never
coincided within the window).

## Results

**Stock leg** (₹150k notional/trade, production MIS charges):

| entry convention | n | win% | net ₹ | avg ₹/trade |
|---|---|---|---|---|
| **next-minute open (PRIMARY, production-legal)** | 15 | 60% | **+2,564** | +171 |
| trigger-close (R58 honest sensitivity) | 15 | 60% | +2,113 | +141 |
| level (REJECTED look-ahead, reference) | 15 | 67% | +7,950 | +530 |

**Option leg** (production option-mode, real broker premiums): n=13 (2
unaffordable), win 62%, **net +₹11,195**, avg +₹861/trade.

**Risk stats on the ₹90k margin base** (3 slots × ₹30k), 18 daily P&L points:

| | net ₹ | month ret% | ann. ret% | Sharpe (ann.) | maxDD ₹ | maxDD % |
|---|---|---|---|---|---|---|
| STOCK (next-open) | +2,564 | +2.85 | 48.2 | 5.02 | −696 | −0.77 |
| STOCK (trig-close) | +2,113 | +2.35 | 38.4 | 4.19 | −897 | −1.00 |
| OPTIONS (ATM buy) | +11,195 | +12.44 | 416 | 5.02 | −1,962 | −2.18 |

## Honest caveats (read before believing)

1. **One-month window.** Annualized CAGR/Sharpe from 18 days are indicative
   only. R58's full-history verdict stands: the bar-honest edge over
   2024-01→2026-07 was **−0.16%/trade** — July 2026 being green does not
   overturn that; it is one favourable month.
2. **Concentration.** NATIONALUM 07-23 alone is +₹7,583 of the option total
   (+₹11,195) and +₹1,177 of the stock total. Ex-that-trade: options +₹3,612
   (12 trades), stock +₹1,387.
3. **Selection parity vs live is approximate.** The live engine builds the
   09:15 candle from ticks; the backtest uses stored 1m bars. On the two
   audited live days the pick overlap was 4/6 (07-22) and 3/6 (07-23) — the
   live day traded OIL where the backtest traded NATIONALUM (the backtest's
   biggest winner came from a pick the live engine did NOT select). Border-rank
   picks are convention-sensitive.
4. **Executed-trade parity where both fired is tight**: BAJAJ-AUTO 07-22 —
   live qty 14, trigger 10699.5, P&L +₹259 gross vs backtest qty 14, entry
   10697.5, +₹166 net.
5. **Mid-bar fill unknown.** Production fills between the level and the bar
   close; the level convention (+₹7,950) brackets the upside. The deployed
   measurement (journal `trigger_price` vs `level`) remains the only way to
   resolve the real captured fraction — 2 live samples so far (+0.42pp,
   +0.62pp drift vs level), consistent with R58's +0.54% mean burst.
6. **Option-leg costs are modelled, not quoted**: production
   `option_round_trip_charges` includes statutory charges + flat brokerage but
   **no bid-ask spread haircut** (the live option-mode would pay the real
   spread on MARKET orders; R58's harness used 2%/side as the conservative
   bracket — that haircut would cost roughly ₹300–700/trade on these premiums).

## Decision

- `parity_target` on the dashboard is filled with the **stock** instrument
  (the deployed default), next-open convention, with the option variant
  recorded alongside.
- The stock-vs-option question stays open in production via the option shadow
  columns (issue #435) — July says option-buying amplified a green month ~4×
  on ~2× the drawdown; it would equally amplify a red one (R58 full-history
  options: −₹160k). No instrument flip recommended on one month of data.

## Addendum (same day): Rs1L per-slot capital scenario (operator request)

Same 15 signals, `margin_per_slot` raised 30k -> **Rs1,00,000** (x5 leverage ->
Rs5L notional/trade; capital base 3 slots = Rs3L). Selection and triggers are
capital-invariant; only sizing changes. Harness override: `BT_MARGIN_PER_SLOT=100000`.

| | n | win% | net Rs | month ret (Rs3L base) | maxDD | Sharpe (ann.) |
|---|---|---|---|---|---|---|
| STOCK (next-open) | 15 | 60% | +10,127 | +3.38% | -0.74% | 5.82 |
| STOCK (trig-close) | 15 | 60% | +8,644 | +2.88% | -0.96% | 5.04 |
| OPTIONS (ATM buy) | **15** | 67% | **+57,268** | +19.09% | -3.37% | 6.11 |

Notables vs the 30k run:
- **No unaffordable skips left** — MANAPPURAM 07-01 PE (+1,432) and GVT&D
  07-02 PE (+7,026) now trade; both won, lifting the option win-rate 62->67%.
- **Options P&L scales super-linearly** (+11,195 -> +57,268, ~5.1x on 3.33x
  capital): fit-to-capital lot granularity improves (e.g. HYUNDAI 1->6 lots,
  BAJAJ-AUTO 2->8), and the two recovered trades add +8.5k.
- Stock leg scales ~linearly (+2,564 -> +10,127 on 3.33x capital; slight
  gain from qty-rounding granularity on high-priced names like SHREECEM).
- Concentration caveat persists: NATIONALUM 07-23 is +30,473 of the option
  total — and it remains a pick the live tick-based selection did NOT make.
- parity_target on the dashboard deliberately stays on the **30k production
  config** — sandbox trades at 30k slots, and the Backtest column exists for
  parity against that book. If the operator raises the production
  `margin_per_slot` to 1L (UI-configurable on /open15_vol_breakout/logs),
  re-point parity_target at this scenario in the same change.

Per-trade detail at Rs1L/slot:

| date | sym | side | trig | qty | stock net | contract | lots | prem in->out | option net |
|---|---|---|---|---|---|---|---|---|---|
| 2026-07-01 | MANAPPURAM | S | 09:27 | 1563 | +558 | MANAPPURAM28JUL26320PE | 2 | 11.4->11.8 | +1,432 |
| 2026-07-01 | SHREECEM | L | 09:27 | 19 | -790 | SHREECEM28JUL2625750CE | 5 | 782.5->755.0 | -4,348 |
| 2026-07-02 | VMM | L | 09:16 | 4172 | -182 | VMM28JUL26120CE | 5 | 4.0->4.1 | +287 |
| 2026-07-02 | GVT&D | S | 09:20 | 104 | +4,436 | GVT&D28JUL264800PE | 3 | 261.7->283.0 | +7,026 |
| 2026-07-06 | POLICYBZR | S | 09:19 | 315 | +1,163 | POLICYBZR28JUL261580PE | 5 | 51.3->54.9 | +5,335 |
| 2026-07-07 | TITAN | L | 09:25 | 108 | +350 | TITAN28JUL264600CE | 6 | 95.2->99.0 | +3,137 |
| 2026-07-08 | BPCL | S | 09:24 | 1657 | -2,212 | BPCL28JUL26300PE | 6 | 7.8->7.2 | -8,538 |
| 2026-07-09 | DRREDDY | S | 09:21 | 378 | -2,605 | DRREDDY28JUL261320PE | 4 | 39.5->37.5 | -5,779 |
| 2026-07-09 | KALYANKJIL | L | 09:26 | 1237 | +3,672 | KALYANKJIL28JUL26405CE | 4 | 17.9->18.9 | +4,462 |
| 2026-07-10 | AUROPHARMA | S | 09:18 | 321 | -544 | AUROPHARMA28JUL261560PE | 4 | 38.6->38.9 | -258 |
| 2026-07-13 | ONGC | L | 09:24 | 2014 | +421 | ONGC28JUL26247.5CE | 8 | 5.3->5.6 | +5,199 |
| 2026-07-15 | HYUNDAI | L | 09:19 | 247 | +1,653 | HYUNDAI28JUL262020CE | 6 | 60.5->67.5 | +10,556 |
| 2026-07-20 | HDFCBANK | S | 09:28 | 642 | -480 | HDFCBANK28JUL26780PE | 11 | 13.2->13.2 | -1,604 |
| 2026-07-22 | BAJAJ-AUTO | L | 09:20 | 46 | +653 | BAJAJ-AUTO28JUL2610700CE | 8 | 152.0->170.0 | +9,887 |
| 2026-07-23 | NATIONALUM | L | 09:21 | 1443 | +4,032 | NATIONALUM28JUL26345CE | 8 | 6.2->8.3 | +30,473 |

## Addendum 2 (same day): hold-winners trail variant — REJECT on this sample

Operator request: at 09:30 do NOT square off winners; stop-loss at the 09:30
price (set 09:31 in backtest; ~09:30:30 live), trailed every 5 minutes
(ratchet to each 5-min mark's close, never loosened; stop checked per 1m bar,
gap-through fills at open; EOD flatten 15:10). Harness
`backtest/options_open15/july_trail_variant.py`, 1L/slot sizing, 10 winners held.

| stop offset below 09:30 price | stock-leg July total |
|---|---|
| 0% (as specified) | +5,346 |
| 0.25% | +3,238 |
| 0.50% | -2,570 |
| 1.00% | +6,042 |
| **base (exit all at 09:30)** | **+10,127** |

- At 0% offset the stop sits AT the current price -> every winner stops out
  09:31-09:32, several with gap-through slippage (MANAPPURAM +558 -> -770);
  the trail never engages.
- Wider offsets let trades breathe but the opening burst mean-reverts after
  09:30: 9 of 10 winners exit worse than the 09:30 price at 0.5% (TITAN the
  lone improver, +2,482). Even 1% underperforms base.
- Verdict: on this one-month sample, **the profit IS the 09:16-09:30 burst;
  holding past 09:30 with any stop tested gives part of it back** — consistent
  with R58 (edge confined to the opening window) and the R54 stop-loss
  learnings. Not deployed; re-examine only with a multi-month sample.

## Addendum 3 (same day): live-selection verification + the 07-23 prev-close race (issue #456)

Tick-log replay (`tick_logs/open15/`) of all three live days: 07-22 gaps
reproduce to 2 decimals from first-tick opens x settled prev closes —
**selection code verified, universe verified identical (211)**. 07-23's six
logged gaps are all off by 0.06-0.72pp because the 09:10 arm read historify-D
prev closes **while the daily-D resettle (#299) was still overwriting them**
(job ran 09:08:37-09:18:45 that morning; no post-close convergence rows exist
for 07-22 evening at all). The arm's `_load_prev_closes` takes `arg_max(close,
timestamp)` with no settled-ness check, so the provisional closes shifted the
gap ranking (ETERNAL/NATIONALUM out, ONGC/OIL in). Filed as **issue #456**
with fix directions (broker prev-close registry cross-check / job sequencing /
armed-event provenance logging). The R59 backtest side is unaffected — it used
settled closes; the 07-23 backtest-vs-live pick divergence is now fully
explained (data race, not code bug, not tick-vs-bar opens).
