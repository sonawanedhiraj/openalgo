# R81 E1 — result (code 9b6f9eecf3)

| Period | Side | Trades | /day | Gross ₹ | Gross bps | Net ₹ | Net bps | Net (0-slip) ₹ | Win % | Payoff | t | 95% CI ₹ | Max DD ₹ | >₹500 loss % | vs 14 bps |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| train | all | 1971 | 1.99 | -17.7 | -2.48 | -119.4 | -16.7 | -85.1 | 53.2 | 0.47 | -10.62 | [-141.1, -97.0] | 238556.0 | 40.2 | BELOW |
| train | long | 1328 | 1.34 | -30.2 | -4.15 | -133.8 | -18.38 | -98.9 | 52.3 | 0.45 | -9.67 | [-159.7, -106.9] | 181617.0 | 41.4 | BELOW |
| train | short | 643 | 0.65 | 8.1 | 1.18 | -89.8 | -13.03 | -56.7 | 55.1 | 0.51 | -4.57 | [-124.5, -52.0] | 58276.0 | 37.8 | BELOW |
| validate | all | 620 | 2.53 | -0.9 | -0.11 | -113.0 | -14.02 | -74.3 | 55.2 | 0.44 | -6.21 | [-147.4, -77.0] | 70087.0 | 36.8 | BELOW |
| validate | long | 425 | 1.73 | 8.9 | 1.1 | -104.6 | -12.84 | -65.5 | 56.5 | 0.44 | -4.36 | [-153.5, -57.7] | 44959.0 | 37.6 | BELOW |
| validate | short | 195 | 0.8 | -22.2 | -2.82 | -131.4 | -16.68 | -93.6 | 52.3 | 0.44 | -4.38 | [-192.3, -75.4] | 26040.0 | 34.9 | BELOW |
| holdout | all | 873 | 2.4 | -25.3 | -3.43 | -130.1 | -17.63 | -94.7 | 52.2 | 0.46 | -7.23 | [-166.5, -95.4] | 113994.0 | 40.8 | BELOW |
| holdout | long | 558 | 1.53 | -28.6 | -3.78 | -135.9 | -17.95 | -99.5 | 53.6 | 0.41 | -6.57 | [-179.6, -95.2] | 75991.0 | 39.4 | BELOW |
| holdout | short | 315 | 0.87 | -19.4 | -2.75 | -119.9 | -17.03 | -86.1 | 49.8 | 0.56 | -3.68 | [-182.3, -50.9] | 40783.0 | 43.2 | BELOW |

- **Half-years (train)**, trades:net ₹/trade: 2019H1 201:-133.8, 2019H2 177:-140.2, 2020H1 212:-68.0, 2020H2 252:-144.3, 2021H1 273:-143.8, 2021H2 299:-88.7, 2022H1 276:-119.6, 2022H2 281:-121.6
- **Neighbours (train)**, net ₹/trade: {'E1_s0.7': -116.6, 'E1_s0.9': -114.3, 'E1_t0.75': -127.4, 'E1_t1.25': -126.3}
- **Half-years (validate)**, trades:net ₹/trade: 2023H1 291:-121.2, 2023H2 329:-105.9
- **Placebo (validate)**, stop-or-15:20 lookup: all: n=620, actual ₹-80.9, timing p=0.069 (pctile 93.1), selection p=0.124 (pctile 87.6); long: n=425, actual ₹-53.8, timing p=0.01 (pctile 99.0), selection p=0.045 (pctile 95.5); short: n=195, actual ₹-140.0, timing p=0.797 (pctile 20.3), selection p=0.719 (pctile 28.1)
- **Neighbours (validate)**, net ₹/trade: {'E1_s0.7': -85.5, 'E1_s0.9': -112.6, 'E1_t0.75': -145.4, 'E1_t1.25': -118.5}
- **Half-years (holdout)**, trades:net ₹/trade: 2024H1 310:-99.0, 2024H2 288:-142.1, 2025H1 275:-152.6
- **Placebo (holdout)**, stop-or-15:20 lookup: all: n=870, actual ₹-115.5, timing p=0.19 (pctile 81.0), selection p=0.876 (pctile 12.4); long: n=555, actual ₹-140.0, timing p=0.335 (pctile 66.5), selection p=0.915 (pctile 8.5); short: n=315, actual ₹-72.3, timing p=0.18 (pctile 82.0), selection p=0.601 (pctile 39.9)
- **Neighbours (holdout)**, net ₹/trade: {'E1_s0.7': -136.1, 'E1_s0.9': -130.1, 'E1_t0.75': -123.2, 'E1_t1.25': -111.8}
- **Gates:** {"half_years_positive": "0/10", "half_years_ok": false, "train_pos": "False", "validate_pos": "False", "plateau": {"E1_t0.75": -145.4, "E1_t1.25": -118.5, "E1_s0.7": -85.5, "E1_s0.9": -112.6}, "plateau_ok": false, "placebo_validate_ok": false, "holdout_pos": "False", "holdout_t": -7.23, "placebo_holdout_ok": false, "verdict": "NOT A RESULT"}