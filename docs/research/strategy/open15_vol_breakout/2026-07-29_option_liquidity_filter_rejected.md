# open15 option liquidity — two filter designs REJECTED, instrumentation shipped

**Date:** 2026-07-29 · **Issue:** [#488](https://github.com/sonawanedhiraj/openalgo/issues/488) · **Status:** both filters rejected; raw metrics now recorded

## Trigger

2026-07-28, option mode, two entries (both errored, so this is what-if analysis —
see `outputs/2026-07-28_open15_whatif_pnl.md`). POWERINDIA 31000 PE held 9 lots
into a 09:30 exit minute that traded **1 lot**. MPHASIS 2418 CE held 17 lots into
a 25-lot minute and would have cleared +₹84,132.

Question asked: what filter would have avoided the POWERINDIA position?
Answer after measurement: **none of the ones proposed**, and the framing was wrong.

## Correction 1 — there are no sector index options

Cross-referencing the Kite NFO dump against the NSE cash equity list:

```
NFO option underlyings: 215
  INDEX: 5  → NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50
  STOCK: 210 → includes POWERINDIA
```

POWERINDIA is **Hitachi Energy India Ltd**, a ~₹31,000/share stock with
`lot_size=25` — not an index. NSE lists no sector-index options at all, so
"exclude sector indices" was never an applicable filter.

## Correction 2 — strike count does not measure liquidity

Same-day-expiry ATM CE, volume over 09:15–09:30 on 2026-07-28 (the actual window
and contract type the strategy trades):

| Underlying | strikes | filter @40 | window lots | med lots/min |
|---|---:|:---:|---:|---:|
| TCS | 97 | pass | 4,924 | 350 |
| MARUTI | 47 | pass | 2,553 | 171 |
| BAJAJ-AUTO | 40 | pass | 505 | 30 |
| MPHASIS | 87 | pass | 359 | 17 |
| **POWERINDIA** | **29** | **SKIP** | **302** | **17** |
| RELIANCE | 45 | pass | 172 | 5 |
| IDEA | 10 | SKIP | 95 | 6 |
| ADANIENT | 44 | pass | 94 | 5 |
| SBIN | 38 | SKIP | 33 | 1 |
| TATASTEEL | 34 | SKIP | 6 | 1 |

POWERINDIA was **4th most liquid of ten** that morning. A threshold of 40 skips it
plus SBIN and TATASTEEL, while passing RELIANCE (half its flow) and ADANIENT (a
third). Strike count tracks strike spacing and how far the underlying drifted
during the contract's life — not participation.

## Correction 3 — trigger-time flow gates rank the two trades backwards

Exact state when each trigger fired (Zerodha 1m bars, `oi=1`):

| At trigger | POWERINDIA PE (bad exit) | MPHASIS CE (good exit) |
|---|---:|---:|
| cumulative volume | 148 lots | 101 lots |
| trailing rate | 55.0 lots/min | 14.8 lots/min |
| open interest | 492 lots | 180 lots |
| participation (pos ÷ rate) | 16% | 115% |
| **exit-minute depth** | **1 lot vs 9 held** | **25 lots vs 17 held** |

Every gate form is infeasible — not mis-tuned, *inverted*:

| Gate | block POWERINDIA | pass MPHASIS | feasible |
|---|---|---|---|
| rate floor | R > 55.0 | R ≤ 14.8 | no |
| participation cap | p < 16% | p ≥ 115% | no |
| OI floor | F > 492 | F ≤ 180 | no |
| cum-volume floor | C > 148 | C ≤ 101 | no |

## Suspected mechanism

open15 enters ATM and profits when price moves **away** from the strike. On
expiry day flow migrates to whatever is currently ATM, so a winning trade drifts
into an abandoned strike. Trailing flow measures where liquidity *was*.

```
POWERINDIA PE  OI: 508 → 492(trigger) → ... → 381   (−25%, monotonic unwind)
               vol: peaks 134 lots/min BEFORE entry, decays to 1 by exit
MPHASIS CE     OI: 180 → 180(trigger) → 192 → 143   (flat, then rising)
               vol: 117 at trigger, 127 the NEXT minute — still ramping
```

MPHASIS's 2418 strike had **zero volume before 09:20** — its trailing average was
low precisely because it was newly relevant. So OI *trend* may carry signal that
OI *level* does not, but that is a threshold fitted to n=2 and 3% over two
minutes is inside the noise band. Not deployed.

## Regulatory context (no usable threshold)

SEBI/NSE publish no per-strike OI floor. Closest official numbers: PSF retention
≥ ₹500 Cr avg daily notional OI *across all contracts* of a stock; LES illiquidity
proxy = mean impact cost ≥ 2% on a ₹1 lakh order; NSE disables zero-OI off-scheme
strikes. F&O entry gates (Aug 2024) are cash-market: MQSOS ≥ ₹75 L, MWPL ≥ ₹1,500 Cr,
ADDV ≥ ₹35 Cr. None translate to a per-strike contract count.

## Shipped instead

4 nullable columns on `open15_trades` — `opt_entry_volume`, `opt_entry_oi`,
`opt_exit_volume`, `opt_exit_oi` — captured at trigger and at exit, exposed on
`GET /open15_vol_breakout/api/trades`. No gate, no threshold, no UI control.
NULL means *not captured*, never *zero liquidity*.

Revisit once ~20 option trades have accumulated: regress realised exit slippage
against entry OI, entry volume, and OI trend. Set a threshold then, on evidence.

## Broader caveat

Median per-minute depth in this window is 1–17 lots for most stock options while a
₹45k slot buys 9–18 lots. Thin exits are the **norm** here, not a POWERINDIA
anomaly. If that proves to cost real money, the lever is execution (staggered or
limit exits) or instrument choice — not per-symbol selection.
