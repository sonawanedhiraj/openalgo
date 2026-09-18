# R64 — open15_vol_breakout: hold the winners beyond 09:30 with a profit trail? (2026-09-18)

Issue #732. Operator question: *check all the winner trades and analyse whether it is
worth keeping them beyond 09:30 and trailing profit instead of exiting them.*

**Verdict: REJECT.** The profit is the 09:16–09:30 burst. 18 of the 23 winners trade
below their exit price within 5 minutes of the flatten (median: the very next
minute), so a breakeven floor stops every one of them out at 09:31 and any wider
trail hands back part of the burst. Only 2 of 23 (HINDZINC 08-26 real, HAL 08-06
modelled) kept running, and nothing observable at 09:30 separates them from the
other 21. Longs decay hard after 09:30 (−₹80k held to 15:10); the 6 short winners
did keep going (+₹67k) but 3 of the 6 are modelled and the 2026-09-10 review
already found shorts bounce 09:30→11:00 on the stock basis across ALL short
signals. Same conclusion as R59 Addendum 2 (July, 10 winners): re-examine only
with a multi-month sample, and only for shorts.

## Data

- **Journal:** all 52 real fills (`open15_trades.fill='real'`) 2026-08-06 → 09-17, read
  from the live DB in `mode=ro`. 23 rows were in profit at their actual exit fill:
  19 `eod_0930` flattens and the 4 in-window `profit_trail` exits (MCX/BIOCON 09-09
  at 09:21, BANKINDIA/PATANJALI 09-17 at 09:26). The hold-beyond path starts at
  each row's OWN exit fill and time, so the trail rows are measured from 09:21/09:26.
- **Post-exit marks — two cohorts, reported separately:**
  - **real (n=16):** broker 1m bars of the traded Sep contract (`29SEP26`), via the
    same `open15_option_shadow.fetch_1m_bars` seam the option shadow uses.
  - **modelled (n=7):** the Aug contracts (`25AUG26`) have expired and Kite no
    longer serves them. Priced from the stock's 1m path with Black-Scholes
    (`backtest/options_open15/bs.py`), IV solved from the ACTUAL exit fill at the
    09:30 stock close and held constant, r=6.5%. Modelled marks ignore spread and
    IV changes — treat them as an upper bound on what a hold could have captured.
- **Rules:** trails evaluated on minute closes with the peak seeded at the exit
  price; the breakeven-floor variants were re-run against minute LOWS (fill at the
  stop). Hard latest exit 15:10 (MIS square-off). Charges are unchanged by the hold
  (same round trip), so every number below is a gross delta vs the actual exit.

## Per-trade: what the winners did after they were sold

ΔP&L vs the actual exit, ₹ (positive = holding would have been better).

| date | sym | side | src | exit | actual | →10:00 | →11:00 | →13:00 | →15:00 | MFE | MAE | peak at | 1st close < exit |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 08-06 | HAL | L | model | 09:30 | 5,910 | +7,282 | +5,829 | +10,493 | +12,251 | +12,251 | 0 | 15:00 | never |
| 08-13 | ASHOKLEY | L | model | 09:30 | 2,000 | +4,877 | −3,640 | −17,258 | −19,027 | +7,219 | −23,149 | 09:54 | +2 min |
| 08-18 | DIXON | L | model | 09:30 | 4,650 | −5,361 | −9,640 | −14,554 | −10,257 | +1,666 | −18,405 | 09:33 | +2 min |
| 08-19 | CGPOWER | S | model | 09:30 | 12,920 | +5,515 | +7,455 | −1,665 | +13,445 | +18,716 | −6,224 | 11:24 | +13 min |
| 08-20 | PFC | S | model | 09:30 | 18,564 | +7,963 | +12,446 | +14,914 | +35,197 | +44,653 | −5,960 | 15:09 | +1 min |
| 08-21 | BRITANNIA | S | model | 09:30 | 38,299 | −13,721 | +9,248 | +5,783 | +8,575 | +19,265 | −19,734 | 14:14 | +1 min |
| 08-21 | PREMIERENE | L | model | 09:30 | 1,788 | −10,407 | −13,995 | −16,760 | −27,037 | +367 | −27,037 | 09:31 | +2 min |
| 08-26 | HINDZINC | L | real | 09:30 | 11,686 | +7,179 | +7,791 | +16,121 | +19,306 | +23,594 | −416 | 14:43 | never |
| 08-27 | LICHSGFIN | L | real | 09:30 | 5,700 | −15,450 | −16,200 | −1,650 | −1,650 | +13,650 | −22,350 | 13:15 | +1 min |
| 08-27 | TVSMOTOR | S | real | 09:30 | 6,710 | +761 | −3,859 | −9,765 | −9,739 | +1,286 | −12,495 | 10:08 | +3 min |
| 08-27 | CGPOWER | L | real | 09:30 | 552 | +170 | −1,360 | −1,785 | +2,125 | +4,505 | −3,698 | 09:36 | +1 min |
| 08-28 | WIPRO | L | real | 09:30 | 2,160 | +2,280 | +2,520 | +7,080 | +2,520 | +9,960 | −2,520 | 13:11 | +1 min |
| 08-28 | TCS | L | real | 09:30 | 7,088 | +6,480 | +2,362 | +7,088 | +4,995 | +12,015 | −1,924 | 13:11 | +1 min |
| 08-28 | INFY | L | real | 09:30 | 4,048 | +2,608 | +928 | +3,488 | −3,632 | +5,888 | −4,992 | 10:12 | +119 min |
| 09-01 | DIVISLAB | S | real | 09:30 | 2,940 | −3,096 | −1,461 | +1,464 | +1,914 | +9,654 | −4,986 | 14:19 | +2 min |
| 09-03 | SBICARD | L | real | 09:30 | 5,496 | +2,472 | −2,328 | −9,528 | −3,528 | +4,152 | −10,008 | 09:52 | +2 min |
| 09-03 | BANDHANBNK | L | real | 09:30 | 864 | +6,552 | 0 | −4,464 | −5,760 | +10,872 | −6,480 | 09:50 | +5 min |
| 09-07 | IDEA | L | real | 09:30 | 15,725 | −4,289 | −3,574 | −7,148 | +2,859 | +5,003 | −9,292 | 13:50 | +1 min |
| 09-08 | NATIONALUM | L | real | 09:30 | 4,200 | +187 | −563 | −10,312 | −15,563 | +4,687 | −16,125 | 10:12 | +1 min |
| 09-09 | MCX | L | real | 09:21 | 1,967 | −8,582 | −7,254 | −9,999 | −10,134 | +576 | −13,532 | 09:25 | +1 min |
| 09-09 | BIOCON | L | real | 09:21 | 3,100 | −12,000 | −8,000 | −11,500 | −16,000 | +2,500 | −18,000 | 09:21 | +1 min |
| 09-17 | BANKINDIA | L | real | 09:26 | 416 | +4,992 | −7,072 | −7,904 | −16,224 | +14,768 | −18,928 | 09:40 | +58 min |
| 09-17 | PATANJALI | S | real | 09:26 | 2,558 | +6,246 | +8,879 | +5,869 | +15,276 | +16,404 | −150 | 14:56 | +2 min |

Reversion speed (all 23): median 1 minute to the first LOW below the exit price, 2
minutes to the first CLOSE below it; 18 within 5 min, 19 within 15 min; median
15-minute low −4.4% of premium; median 15:10 mark −1.6% of premium. MFE sum
+₹243.7k against MAE sum −₹246.4k: the post-09:30 path is symmetric noise with wide
dispersion, not a continuation.

## Aggregates (ΔP&L vs actual, ₹; "better" = number of trades improved)

**Hold to a fixed time**

| cohort | n | actual | →09:45 | →10:00 | →10:30 | →11:00 | →12:00 | →13:00 | →14:00 | →15:00 | →15:10 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| all winners | 23 | +159,340 | −3,221 (14 better) | −7,342 (15) | −16,171 (12) | −21,486 (9) | −41,218 (8) | −51,991 (9) | −51,403 (8) | −20,086 (11) | −12,483 (11) |
| real bars only | 16 | +75,209 | +18,653 (11) | −3,489 (11) | −22,223 (9) | −29,189 (5) | −41,443 (5) | −32,945 (6) | −53,170 (5) | −33,234 (7) | −33,288 (7) |
| modelled (Aug) | 7 | +84,130 | −21,873 (3) | −3,853 (4) | +6,052 (3) | +7,703 (4) | +225 (3) | −19,046 (3) | +1,767 (3) | +13,148 (4) | +20,805 (4) |
| long | 17 | +77,349 | −3,805 (11) | −11,010 (11) | −41,141 (9) | −54,194 (5) | −69,782 (4) | −68,593 (5) | −93,856 (5) | −84,755 (6) | −79,832 (6) |
| short | 6 | +81,991 | +584 (3) | +3,668 (4) | +24,970 (3) | +32,708 (4) | +28,564 (4) | +16,601 (4) | +42,453 (3) | +64,668 (5) | +67,349 (5) |

The only positive fixed-time cell on real bars (→09:45, +₹18.7k) is 11 of 16 better
by small amounts and is dominated by three trades (HINDZINC, BANDHANBNK, TCS); on
the modelled cohort the same cell is −₹21.9k (BRITANNIA gives back ₹13.7k by 09:45).
It does not survive the split.

**Trailing stops from the running peak (peak seeded at the exit price, closes)**

| variant | all 23 | better / worse | real 16 | long 17 | short 6 |
|---|---|---|---|---|---|
| 10% of peak | −17,654 | 8 / 15 | −13,956 | −20,202 | +2,548 |
| 15% of peak | −60,517 | 8 / 15 | −46,913 | −46,060 | −14,458 |
| 20% of peak | −52,013 | 9 / 14 | −18,101 | −42,118 | −9,894 |
| 30% of peak | −11,369 | 11 / 12 | −40,983 | −78,719 | +67,349 |
| ₹2,000 give-back | +12,771 | 10 / 13 | +14,860 | +9,925 | +2,846 |
| ₹3,000 give-back | −6,042 | 9 / 14 | +3,377 | −5,083 | −959 |
| ₹5,000 give-back | −21,832 | 9 / 14 | −8,339 | −13,155 | −8,677 |

The one positive row (₹2,000 give-back, +₹12.8k on 23 trades, 10 better / 13 worse)
is a ₹555/trade mean with sign-flipping components — HINDZINC +₹6.1k and PFC
(modelled) carry it, and a ₹1,000 shift either way turns it negative. Not a rule.

**Trails with a breakeven floor at the exit price** — on closes the 20%-of-peak floored
trail reads +₹12,137, but that is **2 better / 21 worse**: HINDZINC +₹20.7k and
everything else −₹100…−₹4,500 (the first minute's close below the floor). Re-run
against minute LOWS, **22 of 23 winners stop out at the first minute after the
exit** (the floor sits AT the price — R59's 0%-offset finding, again); the only
survivor is BANKINDIA, which had exited at 09:26 on the in-window trail and whose
"hold" is inside the entry window. Net: +₹0 to +₹15k depending on the variant,
all of it BANKINDIA.

**Holding the losers too** (18 `eod_0930` rows in loss at 09:30, −₹67.9k actual): →10:00
+₹23.5k, →11:00 −₹38.1k, →15:10 −₹18.1k; every trail variant negative. A "hold
everything" rule is not a rescue either.

## What this means

1. **The exit time is right, on both sides, on the option instrument.** The
   2026-09-10 review found the same on the stock basis (09:26 no better; 09:30→11:00
   −0.15% for shorts). This is the third independent look (R59 July stock legs, the
   09-10 stock-basis review, R64 option marks) and all three say the burst does not
   continue.
2. **A breakeven stop after 09:30 is a 09:31 exit.** The 09:30 print is a local
   high for the winners often enough that a floor at that price is hit immediately.
   Any offset wide enough to breathe (10–30% of premium) gives back more than it
   lets run.
3. **The two runners are not identifiable at 09:30.** HINDZINC (+₹23.6k MFE) and HAL
   (+₹12.3k modelled) had no distinguishing entry feature — both were early
   (09:17/09:21) A-grade-shaped triggers, but so were LICHSGFIN, DIXON and IDEA,
   which gave back ₹22k, ₹18k and ₹9k. The R63 rating separates winners from losers
   at the trigger; it does not predict continuation past 09:30.
4. **Shorts are the only cohort worth a future re-look** (+₹67k held to 15:10, 5 of
   6 better) — but n=6, half modelled, and the all-shorts stock-basis result on
   09-10 was negative. Pre-register before touching it: re-test on ≥15 REAL short
   winners with real option bars, held with a 30%-of-peak trail, and require the
   held cohort to beat the flat exit in BOTH halves of the sample.

**Not deployed. No config change.** The in-window profit lock/trail (#696/#716) and
the exit time are unchanged.

## Reproducibility

Scratchpad harness (not committed): `fetch_paths.py` (52 rows → option + stock 1m
bars via `services.open15_option_shadow.fetch_1m_bars`, read-only on the journal),
`analyze_hold.py` (fixed-time + trail variants on closes), `short_ext.py`
(breakeven floors on minute lows, short holds), `reversion.py` (time-to-first-dip).
Option bars for the Sep contracts must be fetched before 2026-09-29 expiry to
re-run the real cohort.
