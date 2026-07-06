# R42 — Inverse (SHORT NIFTY futures) of futures_follow_cap50 · 2026-07-05

**Question (operator):** the live sleeve buys NIFTY futures when the market is
trending up into the close. Does the *opposite* work — SELL NIFTY futures when
the market is in a downtrend?

**Verdict: REJECT.** The short mirror is dead money with the E4 catastrophe
filter (CAGR +1.05%, Sharpe 0.15, MaxDD −11.3%) and actively destructive
without it (CAGR −7.8%, Sharpe −0.24, MaxDD −25.1%). The overnight edge this
strategy family monetises is **long-only**: sell-signal stocks *bounce*
overnight instead of continuing down.

Issue: [#336](https://github.com/sonawanedhiraj/openalgo/issues/336) ·
Harness + raw results: `outputs/2026-07-05_inverse_short_futures/`

---

## Method — public-data proxy with a fidelity control

`historify.duckdb` was exclusively locked by the live app (Saturday; broker
token expired, VSS unavailable, historify HTTP API session-gated), so the exact
`sm_core` 1m harness could not run. The round instead ran on **Yahoo daily
bars** (30 universe stocks + 8 sector indices + NIFTY, 2023-08..2026-07-03):

| Original harness | This proxy |
|---|---|
| stock/sector return = prev EOD close → 15:20 print | close → close (10-min shift) |
| vol_ratio = cumulative volume to 15:20 / 20d avg | full-day volume / 20d avg |
| futures P&L = index c1520 → next-day c1525 | index close → next-day close |

Everything else reproduces the 2026-06-14 study byte-for-byte in structure:
same sector map (`strategies/sector_follow_cap5_vol/sector_map.json`), same
gates (C1 sector ±1.5%, stock ±0.5%, vol>1×), same E4 sector-5d-vol>p80
catastrophe filter, same 3% kill switch, same top-5 vol-ratio selection, same
1-lot-per-signal greedy sizing under the 50% overnight-margin cap on ₹10L, same
lot revision (50→75 @2024-11-20), same decomposed Zerodha index-futures charges.

**Proxy fidelity — the long control on identical proxy data vs the canonical
study (2024-01..2026-06-12):**

| Metric | Original (1m harness) | Proxy long control | Δ |
|---|---|---|---|
| CAGR | 14.44% | 15.48% | +1.0pp |
| Sharpe | 1.27 | **1.27** | exact |
| MaxDD | −8.0% | −6.05% | +2pp |
| Trades | 149 | 153 | +4 |
| Win% | 52.3 | 51.0 | −1.3pp |

Sharpe matches exactly and every other metric is within noise — the proxy is
faithful enough for a directional verdict on the short side.

## Results (₹10L, CAP50, 2024-01-01 → 2026-07-03)

| Variant | Signals | Trades | Win% | Net ₹ | CAGR% | Sharpe | MaxDD% | Worst day ₹ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| LONG control (to 06-12) | 240 | 153 | 51.0 | +421,780 | **15.48** | **1.27** | −6.05 | −45,639 |
| LONG control (full) | 247 | 156 | 51.3 | +424,684 | 15.19 | 1.33 | −6.05 | −45,639 |
| **SHORT mirror +E4** | 169 | 121 | 43.0 | +26,607 | **1.05** | 0.15 | −11.31 | −69,284 |
| SHORT mirror +E4 (to 06-12) | 160 | 116 | 44.0 | +62,505 | 2.51 | 0.25 | −11.31 | −69,284 |
| **SHORT mirror no-E4** | 326 | 201 | 42.8 | −184,348 | **−7.82** | −0.24 | −25.13 | −111,401 |
| SHORT gate 1.0 +E4 | 376 | 221 | 45.7 | +39,803 | 1.57 | 0.18 | −18.38 | −84,636 |
| SHORT gate 1.0 no-E4 | 560 | 304 | 44.1 | −173,425 | −7.33 | −0.18 | −27.56 | −111,401 |
| LONG gate 1.0 +E4 | 404 | 251 | 51.8 | +415,334 | 14.89 | 1.02 | −10.62 | −91,278 |

No short variant comes anywhere near the 12% deployability bar; the best one
(gate 1.0 +E4, +1.57%) underperforms a savings account while carrying a −18%
drawdown and a −₹84k worst day.

## Why the short side fails — the diagnostics

Next-24h behaviour conditioned on the signal (close→next close):

| | LONG signals | SHORT signals (+E4) | SHORT signals (no-E4) |
|---|---:|---:|---:|
| n | 240 | 169 | 326 |
| Signal stock continues in signal direction | **59.2%** | 45.6% | 43.3% |
| Signal stock mean next-day return | **+0.51%** | **+0.38%** (bounces!) | +0.30% (bounces!) |
| NIFTY moves favourably | 52.9% | 48.5% | 46.6% |
| NIFTY mean next-day return | +0.17% | −0.06% | **+0.05%** (wrong sign) |

Three stacked failures:

1. **The stock-level edge is asymmetric.** After an up-signal day the stock
   keeps drifting up (+0.51%, 59% continuation). After a down-signal day the
   stock *reverses* (+0.38% mean, only 45.6% continue down). Panic closes in
   Indian large caps mean-revert overnight. There is no down-drift to lever.
2. **The index tailwind flips to a headwind.** The long sleeve harvests
   +0.17%/day of NIFTY drift on bullish days; on bearish signal days NIFTY is
   down only 48.5% of next sessions (−0.06% mean with E4, *positive* +0.05%
   without) — shorts fight the market's structural upward drift.
3. **The tails get worse, not better.** Worst day −₹69k to −₹111k vs the long
   side's −₹46k: the biggest overnight gaps after crash days are violent
   *bounces*, exactly what a short cohort is maximally exposed to.

Notably, E4 — designed as a *long* catastrophe filter — is the only thing
keeping the short book above water (+1.05% vs −7.8%): the deepest-vol regimes
it skips are where post-panic rip-your-face-off rallies live. Skipping them
saves the short ₹210k. That a strategy's best filter is "don't trade when your
signal is strongest" is itself the tell that there is no edge underneath.

This closes the loop with the two prior findings on the same signal family:
MIS T+0 leverage fails (the edge is the overnight hold, not the intraday move)
and option buying fails (theta eats the delta-1 overnight edge). Now the short
mirror fails too. **The only thing that works is being long the overnight
drift** — which is precisely why futures_follow_cap50 is honestly labelled
leveraged beta, not alpha.

## Caveats

- Daily-proxy signals, not 15:20 prints (calibrated by the control; see above).
- Short-side margin/charges symmetric to long in the model; real short-side
  SPAN is comparable. No securities-borrow needed for index futures, so the
  instrument itself is fine — the *signal* is what fails.
- Yahoo adjusted closes; NIFTYPVTBANK via `NIFTY_PVT_BANK.NS`, PSUBANK via
  `^CNXPSUBANK` etc. — see `outputs/2026-07-05_inverse_short_futures/meta.json`.
- Window includes 2026-06-13..07-03 out-of-sample extension beyond the original
  study; conclusions unchanged with or without it.

## What WOULD a bear-regime sleeve need (research direction, not a backlog commitment)

The failure is overnight mean-reversion of panic closes, so a workable short
sleeve would have to be a *different* trade shape, e.g. short on **multi-day**
downtrend confirmation held for days-weeks (trend-following, not next-day
echo), or long index PUT structures where the convexity pays for the
whipsaw — but R33/R36 already showed index-option buying struggles with the IV
floor. Neither is a parameter tweak on this signal set; both would be new
rounds. Registry cross-cutting finding added: *the sector_follow overnight
edge is long-only*.
