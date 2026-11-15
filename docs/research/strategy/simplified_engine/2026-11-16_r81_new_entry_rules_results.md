# R81 results — five new ENTRY rules through the unchanged simplified stock engine

- **Issue:** #785.
- **Pre-registration:** `backtest/r81_entry/PREREGISTRATION.md` (1e8c2ce23); specs frozen in 8e5aacb1e before the holdout was opened.
- **Code:** `backtest/r81_entry/`.
- **Results:** `backtest/r81_entry/data/result_E*.json|md`; ledger `data/variant_ledger.csv` (75 rows: 25 variants × 3 periods).
- **Approval:** Dheeraj approved all five candidates. No spec changed after testing began.

## The answer

**No candidate clears the bar on the sealed holdout.**
- E1, E2, E4 and E5 lose money in every period, with t-stats from −1.75 to −15.
- E3 (large-gap fade) is the only positive on the holdout:
  - +₹13.3 a trade, t = 0.34, 95% CI −₹61 to +₹91.
  - It lost in train (−₹28) and validate (−₹42), was positive in 3 of 10 half-years, its plateau failed, and it failed the holdout placebo.
  - That is one positive period, which the protocol calls **noise**.
- **Bottom line:** replacing the ChartInk breakout with any of these five entry generators does not produce an edge that survives this engine's costs.

## Summary (net ₹ per trade after charges and 2.4 bps slippage per side; t clustered by day)

| Candidate | Train net ₹ (t) | Validate net ₹ (t) | **Holdout** net ₹ (t, 95% CI) | Holdout gross bps vs 14 | Half-years + (of 10) | Plateau | Placebo timing / selection pctile (val; hold) | Verdict |
|---|---|---|---|---|---|---|---|---|
| E2 sector-relative strength 09:45 | -75.2 (-4.23) | -82.4 (-2.44) | **-121.4** (-4.9, [-172.3, -73.9]) | -4.61 → BELOW | 0/10 | fail | 32.9/87.9; 55.3/11.2 | NOT A RESULT |
| E5 opening-drive efficiency (1/day) | -47.9 (-2.61) | -116.7 (-3.81) | **-51.9** (-1.75, [-111.1, 7.9]) | 6.51 → BELOW | 1/10 | fail | 1.8/64.8; 69.7/93.8 | NOT A RESULT |
| E1 VWAP trend-day pullback | -119.4 (-10.62) | -113.0 (-6.21) | **-130.1** (-7.23, [-166.5, -95.4]) | -3.43 → BELOW | 0/10 | fail | 93.1/87.6; 81.0/12.4 | NOT A RESULT |
| E4 first-hour compression → expansion | -140.8 (-15.02) | -136.9 (-7.64) | **-117.7** (-5.95, [-154.9, -74.8]) | 0.76 → BELOW | 0/10 | fail | 5.5/86.8; 24.4/48.7 | NOT A RESULT |
| E3 large-gap fade | -27.7 (-1.03) | -42.1 (-0.91) | **13.3** (0.34, [-61.0, 91.3]) | 17.56 → CLEARS | 3/10 | fail | 96.3/99.3; 89.6/93.9 | NOT A RESULT |

**The 14 bps bar against each candidate's actual size.** Entries made at or near the open carry a wide 5m ATR, so the engine sizes them smaller: ₹52k–₹94k of notional, against the ₹86k the bar assumed. Charges were therefore 8.5–10.3 bps a trade, and with slippage the real bar was **14–15 bps** for every candidate.

| Candidate | Holdout gross per trade | Against the bar |
|---|---|---|
| E3 | 17.6 bps | Clears it on gross, leaving 2.5 bps net |
| E5 | 6.5 bps | Below |
| E4 | 0.8 bps | Below |
| E1 | −3.4 bps | Below |
| E2 | −4.6 bps | Below |

## Per candidate: long and short separately

### E2 sector-relative strength 09:45

| Period | Side | Trades | /day | Gross ₹ / bps | Net ₹ / bps | Net 0-slip ₹ | Win % | Payoff | t | 95% CI ₹ | Max DD ₹ | Loss > ₹500 % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| train | all | 800 | 0.81 | 12.9 / 2.16 | -75.2 / -12.57 | -46.5 | 59.1 | 0.47 | -4.23 | [-110.4, -39.8] | 61741 | 40.0 |
| train | long | 438 | 0.44 | -7.6 / -1.23 | -97.9 / -15.94 | -68.4 | 56.2 | 0.48 | -3.93 | [-145.8, -48.0] | 45422 | 43.2 |
| train | short | 362 | 0.37 | 37.7 / 6.5 | -47.8 / -8.24 | -20.0 | 62.7 | 0.46 | -1.83 | [-96.6, 5.8] | 18435 | 36.2 |
| validate | all | 248 | 1.01 | 21.6 / 2.96 | -82.4 / -11.3 | -47.4 | 58.5 | 0.46 | -2.44 | [-148.1, -13.6] | 21716 | 39.5 |
| validate | long | 122 | 0.5 | 0.9 / 0.12 | -105.0 / -14.08 | -69.2 | 57.4 | 0.44 | -2.27 | [-198.7, -10.3] | 13662 | 42.6 |
| validate | short | 126 | 0.51 | 41.6 / 5.85 | -60.4 / -8.49 | -26.3 | 59.5 | 0.5 | -1.39 | [-141.8, 25.9] | 9685 | 36.5 |
| holdout | all | 378 | 1.04 | -29.1 / -4.61 | -121.4 / -19.26 | -91.1 | 55.3 | 0.42 | -4.9 | [-172.3, -73.9] | 46130 | 42.6 |
| holdout | long | 193 | 0.53 | -82.1 / -12.97 | -174.8 / -27.61 | -144.4 | 49.7 | 0.39 | -5.31 | [-240.3, -112.6] | 34076 | 47.7 |
| holdout | short | 185 | 0.51 | 26.3 / 4.19 | -65.6 / -10.47 | -35.5 | 61.1 | 0.45 | -1.85 | [-137.8, 7.2] | 12558 | 37.3 |

Half-years (trades : net ₹/trade): 2019H1 75:-94.8, 2019H2 85:-111.0, 2020H1 89:-54.0, 2020H2 86:-76.5, 2021H1 101:-16.7, 2021H2 128:-41.1, 2022H1 125:-102.3, 2022H2 111:-112.9, 2023H1 116:-154.3, 2023H2 132:-19.1, 2024H1 131:-161.4, 2024H2 116:-90.9, 2025H1 131:-108.4

Neighbours, validate net ₹/trade: {'E2_s0.20': -94.0, 'E2_s0.40': -78.1, 'E2_rs0.35': -113.1, 'E2_rs0.65': -41.2}. Holdout neighbours: {'E2_rs0.35': -102.8, 'E2_rs0.65': -116.7, 'E2_s0.20': -114.8, 'E2_s0.40': -101.5}

Placebo percentiles (timing / selection), all · long · short: validate 32.9/87.9 · 22.2/73.2 · 66.6/83.8; holdout 55.3/11.2 · 3.4/5.1 · 98.3/51.0

**Verdict:** NOT A RESULT

### E5 opening-drive efficiency (1/day)

| Period | Side | Trades | /day | Gross ₹ / bps | Net ₹ / bps | Net 0-slip ₹ | Win % | Payoff | t | 95% CI ₹ | Max DD ₹ | Loss > ₹500 % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| train | all | 980 | 0.99 | 42.6 / 6.88 | -47.9 / -7.74 | -18.2 | 58.9 | 0.56 | -2.61 | [-82.8, -12.0] | 50189 | 39.9 |
| train | long | 576 | 0.58 | 38.4 / 6.1 | -53.7 / -8.53 | -23.5 | 58.9 | 0.54 | -2.25 | [-100.0, -6.6] | 37483 | 40.3 |
| train | short | 404 | 0.41 | 48.5 / 8.04 | -39.5 / -6.55 | -10.6 | 58.9 | 0.58 | -1.37 | [-97.0, 19.8] | 21315 | 39.4 |
| validate | all | 245 | 1.0 | -14.1 / -1.95 | -116.7 / -16.12 | -82.0 | 55.1 | 0.45 | -3.81 | [-175.5, -54.2] | 32020 | 42.9 |
| validate | long | 144 | 0.59 | -38.1 / -5.1 | -143.7 / -19.24 | -107.8 | 54.2 | 0.4 | -3.68 | [-218.5, -61.8] | 22807 | 44.4 |
| validate | short | 101 | 0.41 | 20.1 / 2.91 | -78.4 / -11.33 | -45.2 | 56.4 | 0.53 | -1.59 | [-174.7, 17.5] | 9347 | 40.6 |
| holdout | all | 364 | 1.0 | 42.3 / 6.51 | -51.9 / -7.97 | -20.6 | 59.1 | 0.54 | -1.75 | [-111.1, 7.9] | 20038 | 37.6 |
| holdout | long | 190 | 0.52 | 4.9 / 0.72 | -92.0 / -13.66 | -59.7 | 55.3 | 0.52 | -2.32 | [-164.8, -12.2] | 19088 | 42.1 |
| holdout | short | 174 | 0.48 | 83.2 / 13.3 | -8.0 / -1.28 | 22.0 | 63.2 | 0.56 | -0.18 | [-89.0, 82.6] | 8369 | 32.8 |

Half-years (trades : net ₹/trade): 2019H1 121:-32.6, 2019H2 122:-61.3, 2020H1 120:-41.3, 2020H2 127:-81.5, 2021H1 121:-33.8, 2021H2 123:13.0, 2022H1 122:-63.9, 2022H2 124:-79.9, 2023H1 122:-152.1, 2023H2 123:-81.7, 2024H1 123:-51.9, 2024H2 125:-73.9, 2025H1 116:-28.0

Neighbours, validate net ₹/trade: {'E5_er0.30': -115.7, 'E5_er0.50': -158.8, 'E5_m0.35': -116.7, 'E5_m0.65': -118.1}. Holdout neighbours: {'E5_er0.30': -54.9, 'E5_er0.50': -57.9, 'E5_m0.35': -51.9, 'E5_m0.65': -55.8}

Placebo percentiles (timing / selection), all · long · short: validate 1.8/64.8 · 2.1/65.5 · 12.4/58.4; holdout 69.7/93.8 · 34.7/67.1 · 85.4/94.8

**Verdict:** NOT A RESULT

### E1 VWAP trend-day pullback

| Period | Side | Trades | /day | Gross ₹ / bps | Net ₹ / bps | Net 0-slip ₹ | Win % | Payoff | t | 95% CI ₹ | Max DD ₹ | Loss > ₹500 % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| train | all | 1971 | 1.99 | -17.7 / -2.48 | -119.4 / -16.7 | -85.1 | 53.2 | 0.47 | -10.62 | [-141.1, -97.0] | 238556 | 40.2 |
| train | long | 1328 | 1.34 | -30.2 / -4.15 | -133.8 / -18.38 | -98.9 | 52.3 | 0.45 | -9.67 | [-159.7, -106.9] | 181617 | 41.4 |
| train | short | 643 | 0.65 | 8.1 / 1.18 | -89.8 / -13.03 | -56.7 | 55.1 | 0.51 | -4.57 | [-124.5, -52.0] | 58276 | 37.8 |
| validate | all | 620 | 2.53 | -0.9 / -0.11 | -113.0 / -14.02 | -74.3 | 55.2 | 0.44 | -6.21 | [-147.4, -77.0] | 70087 | 36.8 |
| validate | long | 425 | 1.73 | 8.9 / 1.1 | -104.6 / -12.84 | -65.5 | 56.5 | 0.44 | -4.36 | [-153.5, -57.7] | 44959 | 37.6 |
| validate | short | 195 | 0.8 | -22.2 / -2.82 | -131.4 / -16.68 | -93.6 | 52.3 | 0.44 | -4.38 | [-192.3, -75.4] | 26040 | 34.9 |
| holdout | all | 873 | 2.4 | -25.3 / -3.43 | -130.1 / -17.63 | -94.7 | 52.2 | 0.46 | -7.23 | [-166.5, -95.4] | 113994 | 40.8 |
| holdout | long | 558 | 1.53 | -28.6 / -3.78 | -135.9 / -17.95 | -99.5 | 53.6 | 0.41 | -6.57 | [-179.6, -95.2] | 75991 | 39.4 |
| holdout | short | 315 | 0.87 | -19.4 / -2.75 | -119.9 / -17.03 | -86.1 | 49.8 | 0.56 | -3.68 | [-182.3, -50.9] | 40783 | 43.2 |

Half-years (trades : net ₹/trade): 2019H1 201:-133.8, 2019H2 177:-140.2, 2020H1 212:-68.0, 2020H2 252:-144.3, 2021H1 273:-143.8, 2021H2 299:-88.7, 2022H1 276:-119.6, 2022H2 281:-121.6, 2023H1 291:-121.2, 2023H2 329:-105.9, 2024H1 310:-99.0, 2024H2 288:-142.1, 2025H1 275:-152.6

Neighbours, validate net ₹/trade: {'E1_t0.75': -145.4, 'E1_t1.25': -118.5, 'E1_s0.7': -85.5, 'E1_s0.9': -112.6}. Holdout neighbours: {'E1_s0.7': -136.1, 'E1_s0.9': -130.1, 'E1_t0.75': -123.2, 'E1_t1.25': -111.8}

Placebo percentiles (timing / selection), all · long · short: validate 93.1/87.6 · 99.0/95.5 · 20.3/28.1; holdout 81.0/12.4 · 66.5/8.5 · 82.0/39.9

**Verdict:** NOT A RESULT

### E4 first-hour compression → expansion

| Period | Side | Trades | /day | Gross ₹ / bps | Net ₹ / bps | Net 0-slip ₹ | Win % | Payoff | t | 95% CI ₹ | Max DD ₹ | Loss > ₹500 % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| train | all | 2545 | 2.57 | -19.7 / -2.2 | -140.8 / -15.73 | -97.8 | 53.0 | 0.4 | -15.02 | [-159.3, -122.3] | 358503 | 35.4 |
| train | long | 1457 | 1.47 | -41.4 / -4.62 | -162.5 / -18.15 | -119.5 | 50.5 | 0.4 | -12.81 | [-186.1, -138.6] | 236981 | 38.5 |
| train | short | 1088 | 1.1 | 9.3 / 1.05 | -111.7 / -12.49 | -68.8 | 56.2 | 0.41 | -7.72 | [-140.9, -82.3] | 123191 | 31.2 |
| validate | all | 688 | 2.81 | -11.4 / -1.21 | -136.9 / -14.52 | -91.6 | 49.7 | 0.45 | -7.64 | [-170.1, -99.9] | 94397 | 22.5 |
| validate | long | 436 | 1.78 | -14.5 / -1.53 | -140.3 / -14.83 | -94.9 | 50.0 | 0.43 | -6.02 | [-185.5, -92.6] | 61418 | 23.2 |
| validate | short | 252 | 1.03 | -6.1 / -0.65 | -130.9 / -13.98 | -85.9 | 49.2 | 0.48 | -4.22 | [-185.4, -69.9] | 32978 | 21.4 |
| holdout | all | 893 | 2.45 | 7.1 / 0.76 | -117.7 / -12.61 | -72.9 | 53.4 | 0.46 | -5.95 | [-154.9, -74.8] | 109363 | 32.0 |
| holdout | long | 517 | 1.42 | -13.9 / -1.5 | -138.4 / -14.9 | -93.8 | 53.6 | 0.38 | -6.58 | [-180.2, -94.7] | 71970 | 32.1 |
| holdout | short | 376 | 1.03 | 36.0 / 3.83 | -89.3 / -9.5 | -44.2 | 53.2 | 0.57 | -2.41 | [-153.8, -11.7] | 42096 | 31.9 |

Half-years (trades : net ₹/trade): 2019H1 341:-120.9, 2019H2 336:-152.9, 2020H1 257:-135.9, 2020H2 347:-133.5, 2021H1 315:-159.7, 2021H2 344:-162.9, 2022H1 273:-154.9, 2022H2 332:-107.7, 2023H1 343:-155.3, 2023H2 345:-118.5, 2024H1 226:-131.3, 2024H2 346:-137.7, 2025H1 321:-86.6

Neighbours, validate net ₹/trade: {'E4_c0.5': -115.6, 'E4_c0.7': -127.1, 'E4_v1.25': -129.6, 'E4_v1.75': -125.6}. Holdout neighbours: {'E4_c0.5': -135.9, 'E4_c0.7': -109.7, 'E4_v1.25': -125.9, 'E4_v1.75': -116.6}

Placebo percentiles (timing / selection), all · long · short: validate 5.5/86.8 · 24.2/95.5 · 6.5/26.9; holdout 24.4/48.7 · 35.4/52.2 · 29.8/51.0

**Verdict:** NOT A RESULT

### E3 large-gap fade

| Period | Side | Trades | /day | Gross ₹ / bps | Net ₹ / bps | Net 0-slip ₹ | Win % | Payoff | t | 95% CI ₹ | Max DD ₹ | Loss > ₹500 % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| train | all | 375 | 0.38 | 50.5 / 9.69 | -27.7 / -5.31 | -2.7 | 63.5 | 0.5 | -1.03 | [-81.1, 26.6] | 12782 | 35.2 |
| train | long | 186 | 0.19 | 31.9 / 6.02 | -47.5 / -8.96 | -22.1 | 60.8 | 0.51 | -1.2 | [-125.6, 31.6] | 11361 | 37.1 |
| train | short | 189 | 0.19 | 68.8 / 13.44 | -8.2 / -1.59 | 16.4 | 66.1 | 0.49 | -0.23 | [-77.4, 63.4] | 7754 | 33.3 |
| validate | all | 154 | 0.63 | 51.5 / 8.07 | -42.1 / -6.59 | -11.5 | 61.0 | 0.51 | -0.91 | [-125.8, 46.3] | 10888 | 34.4 |
| validate | long | 76 | 0.31 | 16.4 / 2.66 | -74.7 / -12.13 | -45.1 | 52.6 | 0.64 | -1.0 | [-215.7, 81.5] | 8069 | 39.5 |
| validate | short | 78 | 0.32 | 85.6 / 12.99 | -10.3 / -1.56 | 21.4 | 69.2 | 0.42 | -0.21 | [-102.9, 96.9] | 5012 | 29.5 |
| holdout | all | 226 | 0.62 | 93.6 / 17.56 | 13.3 / 2.49 | 38.9 | 62.8 | 0.63 | 0.34 | [-61.0, 91.3] | 5401 | 34.5 |
| holdout | long | 115 | 0.32 | 88.5 / 17.09 | 10.5 / 2.02 | 35.3 | 58.3 | 0.75 | 0.18 | [-91.7, 122.3] | 5364 | 36.5 |
| holdout | short | 111 | 0.3 | 98.8 / 18.02 | 16.2 / 2.95 | 42.5 | 67.6 | 0.52 | 0.31 | [-79.3, 121.5] | 4703 | 32.4 |

Half-years (trades : net ₹/trade): 2019H1 41:-114.3, 2019H2 35:-26.7, 2020H1 62:-33.7, 2020H2 43:54.6, 2021H1 42:-26.7, 2021H2 53:85.0, 2022H1 51:-158.2, 2022H2 48:-7.1, 2023H1 66:-140.3, 2023H2 88:31.6, 2024H1 78:8.2, 2024H2 61:-5.0, 2025H1 87:30.6

Neighbours, validate net ₹/trade: {'E3_g0.75': -62.7, 'E3_g1.25': -117.2, 'E3_c10': -48.4, 'E3_c20': -66.2}. Holdout neighbours: {'E3_c10': -64.3, 'E3_c20': -41.2, 'E3_g0.75': 5.8, 'E3_g1.25': -25.6}

Placebo percentiles (timing / selection), all · long · short: validate 96.3/99.3 · 79.1/93.1 · 96.0/97.4; holdout 89.6/93.9 · 70.6/87.1 · 92.1/86.6

**Verdict:** NOT A RESULT


## What the five results say

1. **All five sit at a coin flip.**
   - Under the engine's own exit, P(+1R before the stop) is 43–53% for every candidate in every period.
   - Payoff (average net win ÷ average net loss) is 0.39–0.75.
   - Win rates of 49–68% therefore still lose money: the trail takes winners early, every loser pays the full stop plus about ₹53–80 of charges, and the slippage stress then decides the sign.
   - This is the pattern R76/R79/R80 found on the ChartInk entry, now reproduced by five unrelated entry generators. The constraint is not the ChartInk signal alone.
2. **The low-frequency candidates did not escape the arithmetic.** E2 trades about 1 a day and E5 exactly 1 a day, so the cost per trade is unchanged at ~10 bps, while their gross was 2–7 bps (E2) and −2 to +7 bps (E5).
   - E5's validate timing placebo sits at the 1.8th percentile: entering after an efficient opening drive did *worse* than entering the same name at a random time that day. Efficient opening drives mean-revert.
3. **E3 is the only candidate carrying information, and it is not enough.**
   - Its selection beat random selection on validate (99th percentile) and on the holdout (94th).
   - Its gross is the highest of the five: 8–10 bps in train/validate and 17.6 bps in the holdout. The short side ran 13–18 bps gross in every period.
   - Net, it is −₹28, −₹42 and +₹13: a single positive period, a plateau where 3 of 4 neighbours are negative, and a holdout placebo at p = 0.10 / 0.06.
   - **It is recorded, not promoted.** If anyone revisits it, the pre-registered question is the short side alone on new data from 2025-06-20 onward, not a re-tune of these thresholds.
4. **The ₹500 cap breach is universal.** 22–48% of trades in every candidate lost more than ₹500 after charges. Any full stop-out does, because sizing is on price risk only. This is the sizing bug flagged in Phase 1, and it is independent of the entry.

## Universe and data (what was done and what is biased)

**Point-in-time membership.**
- All 78 NSE month-end F&O bhavcopies, 2018-12 → 2025-05, were downloaded (old and UDiFF formats). None were missing.
- Trading month M uses the FUTSTK list from the last trading day of month M−1.

**Residual survivorship.**
- Union of 325 symbols: 300 resolved to Kite tokens. Renames were mapped by token continuity: CADILAHC→ZYDUSLIFE, MOTHERSUMI→MOTHERSON, LTI/LTIM→LTM, ZOMATO→ETERNAL and 13 more.
- **25 could not be used:**
  - Delisted or merged: HDFC, MINDTREE, IDFC, PEL, DHFL, RCOM, JETAIRWAYS, RELCAPITAL, RELINFRA, EQUITAS, ALBK, ORIENTBANK, SYNDIBANK, BHARATFIN, TATAMTRDVR, HEXAWARE, TV18BRDCST, IDFCBANK.
  - No token in today's master: GSPL, HFCL, INFIBEAM, JPASSOCIAT, SREINFRA.
  - Excluded because the rename was not a continuation of the same company: KPIT (became Birlasoft), UJJIVAN (merged into a different listed entity).
- That is **2.79% of point-in-time symbol-months**, concentrated in 2019–2023, and over-weighted toward failed companies. It biases the train period toward survivors: it flatters longs and makes shorts look harder. Every candidate failed anyway, so the bias cannot have hidden a result.

**Data.**
- Kite 1m bars for each symbol's membership months plus 35 days of warm-up, 2018-12 → 2023-12, plus R80's 2023-12-18 → 2025-06-20 bars: 90.6 million new rows, zero failed requests.
- Kite daily bars for ATR, the previous close and ADV.
- Prices are Kite's split-adjusted series. The ₹100–5,000 price band is applied to adjusted prices, a small distortion before a split.

**Sector indices (E2).**
- NIFTY OIL AND GAS, HEALTHCARE and CONSR DURBL have 1m bars only from 2022-08-29.
- Stocks without a mapped sector in the pre-registered map (`strategies/intraday_pullback_top2/sector_map.json`), or without sector 1m data, were excluded: **44% of eligible symbol-days in train, 37% in validate, 36% in holdout.**
- E2 therefore ran on a narrower universe than the others. Its result is still negative in every period.

## Spec ambiguities resolved during the run (each written down here, none chosen by result)

1. **Eligibility:** in the previous month-end F&O list; previous close ₹100–5,000; ADV20 ≥ ₹50 crore (D−1); daily ATR14 available. E2/E5 also need ≥ 28 of the 30 1m bars 09:15–09:44.
2. **E1:**
   - "Share of 5m closes on the trade side since 09:30" counts bars starting ≥ 09:30 up to and including the trigger bar.
   - The trigger window is bars *ending* 10:15–14:30.
   - A touch means low ≤ VWAP × 1.001 (long) or high ≥ VWAP × 0.999 (short).
   - 5m bars with a missing 1m bar are not eligible triggers.
   - Same-time ties within the 3-a-day cap go to the higher trend strength.
3. **E2** ranks by RS ÷ daily ATR%, because the threshold is ATR-scaled.
4. **E3:**
   - Gap = (09:15 open − previous close) ÷ daily ATR14 in rupees.
   - "First 15 minutes close against the gap" uses the 09:29 1m close against the 09:15 open.
   - The 10/20-minute neighbours use 09:24 and 09:34.
5. **E4:**
   - The first hour is the 1m bars 09:15–10:14.
   - The 20-day norms need 20 prior sessions.
   - The slot-volume norm is the same 5m slot over the prior 20 sessions.
   - One trigger per symbol-day (whichever side came first). The window is bars ending 10:20–14:00.
6. **E5** path efficiency is measured over [09:15 open, 1m closes 09:15–09:44].
7. **Engine replay:**
   - The engine ATR comes from its own `load_historical_candles` over the prior ~7 sessions of 5m bars up to the signal bar.
   - After entry, each completed 5m bar updates ATR via `on_new_candle`. The symbol is never armed, so it cannot re-enter.
   - Slippage is applied in the accounting only, so the engine's stop and trail decisions are identical with and without slippage. That makes zero-slippage a true upper bound from the same run.
8. **Placebo:**
   - Both placebos and the candidate's own comparison value are priced from one lookup: initial stop = 1.5 × continuous 5m Wilder ATR, else the 15:19 close, same sizing, slippage and charges. Like for like, and fast enough for 1,000 draws.
   - E2, E3 and E5 decide at a single instant, so their timing-placebo window was set to 09:30–14:30. E1 and E4 use their own trigger windows.
9. **Plateau** is judged on combined long + short VALIDATE net expectancy, as written.

## Protocol compliance

| Requirement | Done |
|---|---|
| Train 2019–22 / validate 2023 / sealed holdout 2024-01-01 → 2025-06-19 | Yes. Freeze commit 8e5aacb1e precedes every holdout computation. |
| Entries on completed bars at the bar close | Yes |
| Corrected charges (0.00307%), 2.4 bps slippage per side, zero-slip bound | Yes, both reported in every row |
| \|t\| ≥ 2 for a result, 2.58 to confirm | Day-clustered t; nothing qualified |
| Positive in every period and ≥ 7/10 half-years | No candidate met it (best: 3/10) |
| Plateau | Failed for all five |
| R67 placebo (timing + selection, 1,000 draws) | Run on validate and holdout |
| Ledger with every failure | 75 rows, with code hash, window, counts, gross, net, six charge components, slippage, t, CI, max DD, ₹500 breaches, R attainment, half-years |
