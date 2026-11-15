# R81 E2 — result (code 9b6f9eecf3)

| Period | Side | Trades | /day | Gross ₹ | Gross bps | Net ₹ | Net bps | Net (0-slip) ₹ | Win % | Payoff | t | 95% CI ₹ | Max DD ₹ | >₹500 loss % | vs 14 bps |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| train | all | 800 | 0.81 | 12.9 | 2.16 | -75.2 | -12.57 | -46.5 | 59.1 | 0.47 | -4.23 | [-110.4, -39.8] | 61741.0 | 40.0 | BELOW |
| train | long | 438 | 0.44 | -7.6 | -1.23 | -97.9 | -15.94 | -68.4 | 56.2 | 0.48 | -3.93 | [-145.8, -48.0] | 45422.0 | 43.2 | BELOW |
| train | short | 362 | 0.37 | 37.7 | 6.5 | -47.8 | -8.24 | -20.0 | 62.7 | 0.46 | -1.83 | [-96.6, 5.8] | 18435.0 | 36.2 | BELOW |
| validate | all | 248 | 1.01 | 21.6 | 2.96 | -82.4 | -11.3 | -47.4 | 58.5 | 0.46 | -2.44 | [-148.1, -13.6] | 21716.0 | 39.5 | BELOW |
| validate | long | 122 | 0.5 | 0.9 | 0.12 | -105.0 | -14.08 | -69.2 | 57.4 | 0.44 | -2.27 | [-198.7, -10.3] | 13662.0 | 42.6 | BELOW |
| validate | short | 126 | 0.51 | 41.6 | 5.85 | -60.4 | -8.49 | -26.3 | 59.5 | 0.5 | -1.39 | [-141.8, 25.9] | 9685.0 | 36.5 | BELOW |

- **Half-years (train)**, trades:net ₹/trade: 2019H1 75:-94.8, 2019H2 85:-111.0, 2020H1 89:-54.0, 2020H2 86:-76.5, 2021H1 101:-16.7, 2021H2 128:-41.1, 2022H1 125:-102.3, 2022H2 111:-112.9
- **Neighbours (train)**, net ₹/trade: {'E2_rs0.35': -79.8, 'E2_rs0.65': -67.6, 'E2_s0.20': -63.8, 'E2_s0.40': -67.8}
- **Half-years (validate)**, trades:net ₹/trade: 2023H1 116:-154.3, 2023H2 132:-19.1
- **Placebo (validate)**, stop-or-15:20 lookup: all: n=248, actual ₹-75.7, timing p=0.671 (pctile 32.9), selection p=0.121 (pctile 87.9); long: n=122, actual ₹-85.4, timing p=0.778 (pctile 22.2), selection p=0.268 (pctile 73.2); short: n=126, actual ₹-66.4, timing p=0.334 (pctile 66.6), selection p=0.162 (pctile 83.8)
- **Neighbours (validate)**, net ₹/trade: {'E2_rs0.35': -113.1, 'E2_rs0.65': -41.2, 'E2_s0.20': -94.0, 'E2_s0.40': -78.1}
- **Gates:** {"half_years_positive": "0/10", "half_years_ok": false, "train_pos": "False", "validate_pos": "False", "plateau": {"E2_s0.20": -94.0, "E2_s0.40": -78.1, "E2_rs0.35": -113.1, "E2_rs0.65": -41.2}, "plateau_ok": false, "placebo_validate_ok": false}