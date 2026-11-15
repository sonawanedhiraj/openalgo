# R81 E5 — result (code 9b6f9eecf3)

| Period | Side | Trades | /day | Gross ₹ | Gross bps | Net ₹ | Net bps | Net (0-slip) ₹ | Win % | Payoff | t | 95% CI ₹ | Max DD ₹ | >₹500 loss % | vs 14 bps |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| train | all | 980 | 0.99 | 42.6 | 6.88 | -47.9 | -7.74 | -18.2 | 58.9 | 0.56 | -2.61 | [-82.8, -12.0] | 50189.0 | 39.9 | BELOW |
| train | long | 576 | 0.58 | 38.4 | 6.1 | -53.7 | -8.53 | -23.5 | 58.9 | 0.54 | -2.25 | [-100.0, -6.6] | 37483.0 | 40.3 | BELOW |
| train | short | 404 | 0.41 | 48.5 | 8.04 | -39.5 | -6.55 | -10.6 | 58.9 | 0.58 | -1.37 | [-97.0, 19.8] | 21315.0 | 39.4 | BELOW |
| validate | all | 245 | 1.0 | -14.1 | -1.95 | -116.7 | -16.12 | -82.0 | 55.1 | 0.45 | -3.81 | [-175.5, -54.2] | 32020.0 | 42.9 | BELOW |
| validate | long | 144 | 0.59 | -38.1 | -5.1 | -143.7 | -19.24 | -107.8 | 54.2 | 0.4 | -3.68 | [-218.5, -61.8] | 22807.0 | 44.4 | BELOW |
| validate | short | 101 | 0.41 | 20.1 | 2.91 | -78.4 | -11.33 | -45.2 | 56.4 | 0.53 | -1.59 | [-174.7, 17.5] | 9347.0 | 40.6 | BELOW |
| holdout | all | 364 | 1.0 | 42.3 | 6.51 | -51.9 | -7.97 | -20.6 | 59.1 | 0.54 | -1.75 | [-111.1, 7.9] | 20038.0 | 37.6 | BELOW |
| holdout | long | 190 | 0.52 | 4.9 | 0.72 | -92.0 | -13.66 | -59.7 | 55.3 | 0.52 | -2.32 | [-164.8, -12.2] | 19088.0 | 42.1 | BELOW |
| holdout | short | 174 | 0.48 | 83.2 | 13.3 | -8.0 | -1.28 | 22.0 | 63.2 | 0.56 | -0.18 | [-89.0, 82.6] | 8369.0 | 32.8 | BELOW |

- **Half-years (train)**, trades:net ₹/trade: 2019H1 121:-32.6, 2019H2 122:-61.3, 2020H1 120:-41.3, 2020H2 127:-81.5, 2021H1 121:-33.8, 2021H2 123:13.0, 2022H1 122:-63.9, 2022H2 124:-79.9
- **Neighbours (train)**, net ₹/trade: {'E5_er0.30': -43.5, 'E5_er0.50': -48.0, 'E5_m0.35': -48.4, 'E5_m0.65': -43.9}
- **Half-years (validate)**, trades:net ₹/trade: 2023H1 122:-152.1, 2023H2 123:-81.7
- **Placebo (validate)**, stop-or-15:20 lookup: all: n=245, actual ₹-108.9, timing p=0.982 (pctile 1.8), selection p=0.352 (pctile 64.8); long: n=144, actual ₹-118.4, timing p=0.979 (pctile 2.1), selection p=0.345 (pctile 65.5); short: n=101, actual ₹-95.4, timing p=0.876 (pctile 12.4), selection p=0.416 (pctile 58.4)
- **Neighbours (validate)**, net ₹/trade: {'E5_er0.30': -115.7, 'E5_er0.50': -158.8, 'E5_m0.35': -116.7, 'E5_m0.65': -118.1}
- **Half-years (holdout)**, trades:net ₹/trade: 2024H1 123:-51.9, 2024H2 125:-73.9, 2025H1 116:-28.0
- **Placebo (holdout)**, stop-or-15:20 lookup: all: n=362, actual ₹-29.5, timing p=0.303 (pctile 69.7), selection p=0.062 (pctile 93.8); long: n=188, actual ₹-85.5, timing p=0.653 (pctile 34.7), selection p=0.329 (pctile 67.1); short: n=174, actual ₹31.1, timing p=0.146 (pctile 85.4), selection p=0.052 (pctile 94.8)
- **Neighbours (holdout)**, net ₹/trade: {'E5_er0.30': -54.9, 'E5_er0.50': -57.9, 'E5_m0.35': -51.9, 'E5_m0.65': -55.8}
- **Gates:** {"half_years_positive": "1/10", "half_years_ok": false, "train_pos": "False", "validate_pos": "False", "plateau": {"E5_er0.30": -115.7, "E5_er0.50": -158.8, "E5_m0.35": -116.7, "E5_m0.65": -118.1}, "plateau_ok": false, "placebo_validate_ok": false, "holdout_pos": "False", "holdout_t": -1.75, "placebo_holdout_ok": false, "verdict": "NOT A RESULT"}