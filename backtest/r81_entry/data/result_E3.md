# R81 E3 — result (code 9b6f9eecf3)

| Period | Side | Trades | /day | Gross ₹ | Gross bps | Net ₹ | Net bps | Net (0-slip) ₹ | Win % | Payoff | t | 95% CI ₹ | Max DD ₹ | >₹500 loss % | vs 14 bps |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| train | all | 375 | 0.38 | 50.5 | 9.69 | -27.7 | -5.31 | -2.7 | 63.5 | 0.5 | -1.03 | [-81.1, 26.6] | 12782.0 | 35.2 | BELOW |
| train | long | 186 | 0.19 | 31.9 | 6.02 | -47.5 | -8.96 | -22.1 | 60.8 | 0.51 | -1.2 | [-125.6, 31.6] | 11361.0 | 37.1 | BELOW |
| train | short | 189 | 0.19 | 68.8 | 13.44 | -8.2 | -1.59 | 16.4 | 66.1 | 0.49 | -0.23 | [-77.4, 63.4] | 7754.0 | 33.3 | BELOW |
| validate | all | 154 | 0.63 | 51.5 | 8.07 | -42.1 | -6.59 | -11.5 | 61.0 | 0.51 | -0.91 | [-125.8, 46.3] | 10888.0 | 34.4 | BELOW |
| validate | long | 76 | 0.31 | 16.4 | 2.66 | -74.7 | -12.13 | -45.1 | 52.6 | 0.64 | -1.0 | [-215.7, 81.5] | 8069.0 | 39.5 | BELOW |
| validate | short | 78 | 0.32 | 85.6 | 12.99 | -10.3 | -1.56 | 21.4 | 69.2 | 0.42 | -0.21 | [-102.9, 96.9] | 5012.0 | 29.5 | BELOW |

- **Half-years (train)**, trades:net ₹/trade: 2019H1 41:-114.3, 2019H2 35:-26.7, 2020H1 62:-33.7, 2020H2 43:54.6, 2021H1 42:-26.7, 2021H2 53:85.0, 2022H1 51:-158.2, 2022H2 48:-7.1
- **Neighbours (train)**, net ₹/trade: {'E3_c10': -44.0, 'E3_c20': -31.3, 'E3_g0.75': -30.7, 'E3_g1.25': -31.0}
- **Half-years (validate)**, trades:net ₹/trade: 2023H1 66:-140.3, 2023H2 88:31.6
- **Placebo (validate)**, stop-or-15:20 lookup: all: n=154, actual ₹0.4, timing p=0.037 (pctile 96.3), selection p=0.007 (pctile 99.3); long: n=76, actual ₹-20.6, timing p=0.209 (pctile 79.1), selection p=0.069 (pctile 93.1); short: n=78, actual ₹20.9, timing p=0.04 (pctile 96.0), selection p=0.026 (pctile 97.4)
- **Neighbours (validate)**, net ₹/trade: {'E3_c10': -48.4, 'E3_c20': -66.2, 'E3_g0.75': -62.7, 'E3_g1.25': -117.2}
- **Gates:** {"half_years_positive": "3/10", "half_years_ok": false, "train_pos": "False", "validate_pos": "False", "plateau": {"E3_g0.75": -62.7, "E3_g1.25": -117.2, "E3_c10": -48.4, "E3_c20": -66.2}, "plateau_ok": false, "placebo_validate_ok": true}