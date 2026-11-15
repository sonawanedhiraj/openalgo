# R81 E4 — result (code 9b6f9eecf3)

| Period | Side | Trades | /day | Gross ₹ | Gross bps | Net ₹ | Net bps | Net (0-slip) ₹ | Win % | Payoff | t | 95% CI ₹ | Max DD ₹ | >₹500 loss % | vs 14 bps |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| train | all | 2545 | 2.57 | -19.7 | -2.2 | -140.8 | -15.73 | -97.8 | 53.0 | 0.4 | -15.02 | [-159.3, -122.3] | 358503.0 | 35.4 | BELOW |
| train | long | 1457 | 1.47 | -41.4 | -4.62 | -162.5 | -18.15 | -119.5 | 50.5 | 0.4 | -12.81 | [-186.1, -138.6] | 236981.0 | 38.5 | BELOW |
| train | short | 1088 | 1.1 | 9.3 | 1.05 | -111.7 | -12.49 | -68.8 | 56.2 | 0.41 | -7.72 | [-140.9, -82.3] | 123191.0 | 31.2 | BELOW |
| validate | all | 688 | 2.81 | -11.4 | -1.21 | -136.9 | -14.52 | -91.6 | 49.7 | 0.45 | -7.64 | [-170.1, -99.9] | 94397.0 | 22.5 | BELOW |
| validate | long | 436 | 1.78 | -14.5 | -1.53 | -140.3 | -14.83 | -94.9 | 50.0 | 0.43 | -6.02 | [-185.5, -92.6] | 61418.0 | 23.2 | BELOW |
| validate | short | 252 | 1.03 | -6.1 | -0.65 | -130.9 | -13.98 | -85.9 | 49.2 | 0.48 | -4.22 | [-185.4, -69.9] | 32978.0 | 21.4 | BELOW |
| holdout | all | 893 | 2.45 | 7.1 | 0.76 | -117.7 | -12.61 | -72.9 | 53.4 | 0.46 | -5.95 | [-154.9, -74.8] | 109363.0 | 32.0 | BELOW |
| holdout | long | 517 | 1.42 | -13.9 | -1.5 | -138.4 | -14.9 | -93.8 | 53.6 | 0.38 | -6.58 | [-180.2, -94.7] | 71970.0 | 32.1 | BELOW |
| holdout | short | 376 | 1.03 | 36.0 | 3.83 | -89.3 | -9.5 | -44.2 | 53.2 | 0.57 | -2.41 | [-153.8, -11.7] | 42096.0 | 31.9 | BELOW |

- **Half-years (train)**, trades:net ₹/trade: 2019H1 341:-120.9, 2019H2 336:-152.9, 2020H1 257:-135.9, 2020H2 347:-133.5, 2021H1 315:-159.7, 2021H2 344:-162.9, 2022H1 273:-154.9, 2022H2 332:-107.7
- **Neighbours (train)**, net ₹/trade: {'E4_c0.5': -130.0, 'E4_c0.7': -133.5, 'E4_v1.25': -138.4, 'E4_v1.75': -138.0}
- **Half-years (validate)**, trades:net ₹/trade: 2023H1 343:-155.3, 2023H2 345:-118.5
- **Placebo (validate)**, stop-or-15:20 lookup: all: n=688, actual ₹-83.2, timing p=0.945 (pctile 5.5), selection p=0.132 (pctile 86.8); long: n=436, actual ₹-71.3, timing p=0.758 (pctile 24.2), selection p=0.045 (pctile 95.5); short: n=252, actual ₹-103.8, timing p=0.935 (pctile 6.5), selection p=0.731 (pctile 26.9)
- **Neighbours (validate)**, net ₹/trade: {'E4_c0.5': -115.6, 'E4_c0.7': -127.1, 'E4_v1.25': -129.6, 'E4_v1.75': -125.6}
- **Half-years (holdout)**, trades:net ₹/trade: 2024H1 226:-131.3, 2024H2 346:-137.7, 2025H1 321:-86.6
- **Placebo (holdout)**, stop-or-15:20 lookup: all: n=893, actual ₹-111.3, timing p=0.756 (pctile 24.4), selection p=0.513 (pctile 48.7); long: n=517, actual ₹-120.0, timing p=0.646 (pctile 35.4), selection p=0.478 (pctile 52.2); short: n=376, actual ₹-99.3, timing p=0.702 (pctile 29.8), selection p=0.49 (pctile 51.0)
- **Neighbours (holdout)**, net ₹/trade: {'E4_c0.5': -135.9, 'E4_c0.7': -109.7, 'E4_v1.25': -125.9, 'E4_v1.75': -116.6}
- **Gates:** {"half_years_positive": "0/10", "half_years_ok": false, "train_pos": "False", "validate_pos": "False", "plateau": {"E4_c0.5": -115.6, "E4_c0.7": -127.1, "E4_v1.25": -129.6, "E4_v1.75": -125.6}, "plateau_ok": false, "placebo_validate_ok": false, "holdout_pos": "False", "holdout_t": -5.95, "placebo_holdout_ok": false, "verdict": "NOT A RESULT"}