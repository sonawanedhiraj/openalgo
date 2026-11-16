# R82: sizing under ₹1L buying power, all-in sizing, slippage (#786)

**Date:** 2026-11-16
**Pre-registration:** [`backtest/r82_sizing/PREREGISTRATION.md`](../../../../backtest/r82_sizing/PREREGISTRATION.md), commit `196b2a3a0`
**Selection frozen:** `00ce17f6f` (before any holdout run)
**Harness:** `backtest/r82_sizing/`
**Engine:** the unchanged `SimplifiedStockEngine` in `mode=disabled`. Sizing and execution live only in the harness: `calculate_qty` is replaced on the engine instance, and fills are passed to `confirm_entry`. Parity with R80: quantities identical on 145/145 trades; exits identical on 140/145, with the other 5 differing only in the 15:20 EOD print, by ≤ ₹0.50.

## The plain answer

1. **The buying-power breach is real.**
   - 85% of sandbox days had two or more positions open at once.
   - The median day's peak open notional was ₹2.3L, the worst ₹8.8L, against ₹1L of buying power.
   - 53% of the 511 sandbox trades could not have been funded.
   - The sandbox never noticed, because it checks margin against its shared ₹1 Cr book. Filed as bug [#787](https://github.com/sonawanedhiraj/openalgo/issues/787).
2. **The breach does not change any verdict.** Re-run with only the trades a ₹1L account could fund, every book in R56/R78/R80/R81 is still negative per trade. One number does change: **R81's only positive holdout (E3, +₹13/trade) came entirely from unfundable trades.** Fundable-only, it is −₹15.9.
3. **Sizing cannot rescue the baseline.** The all-in rule does what it should: losses beyond ₹500 fall from 31.6% of trades to 0.2%. But net bps are unchanged (−12 to −14 bps), because the gross edge is 0–2 bps and sizing does not touch gross.
   - **₹1,000 is unreachable:** buying power caps the size, so the median trade uses only 75% of the budget.
   - **The percent-of-capital rule ruins the account:** ₹20,000 → ₹241 in 2024, → ₹964 in 2025 H1, → ₹383 over 2025-06..2026-07.
4. **Slippage cannot be engineered away here.** A limit order at the trigger removes the 2.4 bps entry cost. But the orders it misses are the winners (+₹46 to +₹197 a trade under X0), and the ones it fills are worse than average. Net change ≈ 0.
   - Entering at the next bar's open, or excluding wide-spread names, does nothing either.
   - The bar-based spread proxy says the per-side cost is about 1.9 bps, so 2.4 was mildly conservative. Nothing measurable is left to win.
5. **Does anything clear the revised bar on the sealed holdout? No.**
   - **BASE:** every config is negative, with t from −3.0 to −5.6.
   - **E3 short, under the frozen config:** +₹174/trade on the forward window, with gross 32 bps against a 13.4 bps bar. But t = 1.86, below the pre-registered 2.0.
   - E3S had also already failed three gates before the holdout was opened: it was negative on 2024–25 (−₹54), positive in only 5/10 half-years, and its g1.25 neighbour was negative on VALIDATE.
   - **Not a result.** The forward window's last two quarters are negative (−₹109, −₹168 a trade).

## Phase 1 — buying power

**Leverage.** Engine config: `account_capital` ₹20,000 × `account_leverage` 5 = ₹1,00,000. Zerodha grants **up to** 5× (20% margin) on equity MIS; the multiplier is per stock, and some names get less ([Zerodha support](https://support.zerodha.com/category/trading-and-markets/margins/margin-leverage-and-product-and-order-types/articles/how-much-margins-leverage-does-zerodha-provide)). Everything here assumes 5× for every name, which flatters the results.

**How each layer handles buying power:**
- `calculate_qty` (`services/simplified_stock_engine_core.py:728`) sizes every entry against the full ₹1L and never deducts open positions.
- The live pre-check (`_check_live_funds`) only checks that available cash ≥ ₹20k.
- The sandbox's margin check runs against its ₹1 Cr book.
- So in live, the broker's RMS would be the first thing to refuse an over-limit order.

**Concurrent positions** (`concurrency.py`, `fundable.py`). "Unfundable" means the trade could not be funded at entry, replayed first-come against a ₹1L ledger.

| Book | Trades | Days with ≥2 open | Peak open notional p50 / p90 / max | Unfundable | Net/trade, all → fundable only |
|---|---|---|---|---|---|
| Sandbox journal 2026-06..11 | 511 | 85% | ₹2.32L / ₹4.11L / ₹8.81L | 53% | −₹57.7 → −₹25.6 |
| R80 replay 2024–25H1 | 1,655 | 83% | ₹1.99L / ₹3.19L / ₹4.93L | 43% | −₹75.1 → −₹69.1 |
| R81 E1 (all periods) | 3,464 | 59% | ₹1.22L / ₹2.11L / ₹2.97L | 27–39% | negative either way |
| R81 E4 | 4,126 | 88% | ₹2.00L / ₹2.98L / ₹3.00L | 48–53% | negative either way |
| R81 E2 / E3 | 1,426 / 755 | 23% / 33% | ~₹0.7L p50 | 11–19% | negative either way, except below |
| R81 E5 (1 trade a day) | 1,589 | 0% | ₹0.63L p50 | 0% | unchanged |

**R81 E3 holdout:** +₹13.3 on all trades. The 193 fundable trades made −₹15.9; the 33 unfundable ones made +₹184.

**What it does to earlier results.**
- Every rupee **total** quoted for these books (for example R56's −₹2.5L) assumed capital that did not exist. Per-trade **signs** survive.
- In the sandbox journal the unfundable trades were the worse ones: −₹86 against −₹26. That is a ₹1L account loses less, not more.
- Nine sandbox days show more than 6 journal rows against `max_trades_per_day=6`; most carry `recovered_from_sandbox` rows. Noted in #787, not investigated.

## Phase 2 — sizing

All-in rule, as pre-registered:
- The per-share reserve is R + round-trip slippage + `k_gap`·R, and exact charges are added on top.
- Quantity is capped at the buying power left after open positions and pending orders.
- The trade is skipped if the funded quantity is below half the budgeted quantity.

`k_gap`, frozen on TRAIN, is **0.0034R for BASE and 0.005R for E3S**. 1m bars almost never gap through a stop in these names.

BASE TRAIN (2024), market entry (X0):

| Rule | Trades | Mean notional | Median budget used | Skipped: no buying power / below min viable | Losses beyond ₹500 | Net ₹/trade | Net bps |
|---|---|---|---|---|---|---|---|
| S0 legacy (no ledger) | 1,138 | ₹81.3k | — | 0 / 0 | 31.6% | −114.7 | −14.1 |
| S0L legacy + ledger | 1,017 | ₹59.5k | — | 33% / — | 20.9% | −74.2 | −12.5 |
| A500 | 877 | ₹62.7k | 0.99 | 15% / 35% | **0.2%** | −85.4 | −13.6 |
| A750 | 712 | ₹86.0k | 0.93 | 26% / 42% | 35.7% (0% beyond ₹750) | −102.6 | −11.9 |
| A1000 | 616 | ₹94.8k | **0.75 — unreachable** | 36% / 37% | 43% (0% beyond ₹1,000) | −122.6 | −12.9 |
| APCT (2.5% of running equity) | 1,027 | ₹17.1k | 0.97 | 2% / 6% | 0.4% | −19.2 | −11.2 |

- **A1000:** the ₹1L cap binds on most trades, so a ₹1,000 budget mostly means "as large as buying power allows". It is reported but was not selectable.
- **APCT:** its small per-trade rupee loss comes from the account shrinking, not from a better rule. Equity went ₹20,000 → ₹241 in TRAIN, so per-trade rupees are not comparable across rules here; net bps is the fair column.
- **E3S:** every budget is reachable, because E3 trades at 09:30 when ATR is wide and quantities are small.

## Phase 3 — the arithmetic ceiling

Zerodha MIS round trip; the bar adds 2 × 2.4 bps of slippage (`cost_curve.py`).

| Notional | Charges | Charges bps | Bar bps | Bar ₹ |
|---|---|---|---|---|
| ₹10k–₹66.7k | 0.03% brokerage (below the ₹20 cap) | 10.63 | **15.43 (flat)** | 15–103 |
| ₹86k | ₹77.7 | 9.04 | 13.84 | 119 |
| ₹1,00,000 | ₹82.7 | 8.27 | **13.07** | 131 |
| ₹2.86L | ₹148.7 | 5.20 | 10.00 | 286 |
| ₹10L | ₹402 | 4.02 | 8.82 | 882 |
| unbounded | — | 3.55 | **8.35** | — |

- Your read is right, with one correction. The ₹47.2 is only a fixed cost above ₹66,667 per order. Below that, brokerage is 0.03% per order, so sizing down does not raise the bar in bps: it stays flat at 15.4.
- The bar's floor is 3.55 bps of charges + 4.8 bps of slippage = 8.35 bps. Within ₹1L of buying power, **the best achievable bar is 13.07 bps**, reached only with one position at a time at the full ₹1L.
- Reaching 10 bps would need ₹2.86L per trade (₹57k of capital at 5×) with no overlap.
- The realised bar at each config's actual notionals was **13.3–15.4 bps** with market orders and **10.9–12.9 bps** with limits.

## Phase 4 — slippage

**Spread proxy** (Abdi–Ranaldo on 1m bars, TRAIN). No real equity fills exist in this install, so this is a proxy.

| Universe | Median full effective spread | By time of day | By liquidity quintile, low → high |
|---|---|---|---|
| BASE (2024) | 3.88 bps | 4.21 (09:30) → 3.67 (12:00) → 4.20 (15:00) | 4.44 → 3.33 |
| E3S (2019–22) | 3.94 bps | | |

So a market order costs about 1.9 bps per side: 2.4 was mildly conservative. Choosing names or times can move this by about 0.5 bps per side, no more.

**Execution variants.** BASE TRAIN / VALIDATE net ₹ per trade, A500 sizing:

| Exec | Fill rate | Net TRAIN | Net VALIDATE | Bar bps | X0 net of the trades it missed | X0 net of the trades it filled |
|---|---|---|---|---|---|---|
| X0 market at signal close | 100% | −85.4 | −69.9 | 14.6 | — | — |
| X1 next 1m open | 100% | −88.2 | −76.0 | 14.6 | — | −86.1 |
| XL5 limit at trigger, 5 min | 91.6% | −84.4 | −65.5 | 12.2 | **+46.1** | −94.0 |
| XLi5 limit 5 bps inside | 77.9% | −78.3 | −71.9 | 12.2 | **+53.6** | −112.8 |
| XL2 / XL10 | 88% / 94% | −95.3 / −79.9 | −63.0 / −64.6 | 12.1 | +66 / +120 | −104 / −97 |
| XS drop the worst-spread quintile | 100% | −84.4 | −97.5 | 14.4 | — | — |

- The limit order saves the 2.4 bps it was meant to. But the signals it fails to fill are the breakouts that ran away without coming back to the level: under X0, those made +₹46 to +₹197 a trade, depending on sizing and wait time.
- Gross falls by about 2–4 bps, which cancels the saving.
- E3S shows the same pattern: its missed trades made +₹209 to +₹417 under X0.
- **The best achievable slippage improvement, net of the trades the limit misses, is about zero** for both candidates. The overlooked half of the bar is overlooked because it is already close to the market's own spread.

## Phase 5 — re-run under the new sizing and best execution

**Selection** (frozen at `00ce17f6f`): the highest VALIDATE net ₹/trade among selectable configs whose TRAIN net ≥ that of S0L/X0.

**BASE → `APCT.X1`.** This pick is an artefact. The pre-registered metric is ₹/trade, and APCT's per-trade rupees shrink with the account. In bps it is no better than the rest.

| Config | TRAIN 2024 | VALIDATE 2025H1 | HOLDOUT 2025-06-20..2026-07-06 (n, t, 95% CI) | Gross vs bar (holdout) |
|---|---|---|---|---|
| APCT.X1 (selected) | −19.0 | −37.7 | **−18.1** (1,081, −3.05, −29 to −6); equity ₹20k → ₹383 | 6.2 vs 15.2 bps |
| A500.X1 | −88.2 | −76.0 | −68.4 (880, −4.77) | 4.2 vs 14.4 |
| S0L.X0 | −74.2 | −82.6 | −53.5 (1,041, −3.92) | 5.3 vs 14.1 |
| S0.X0 (legacy) | −114.7 | −116.7 | −77.7 (1,177, −5.60) | 4.5 vs 13.7 |

- Long and short sides are both negative in every period on the holdout. Example, APCT.X1: long −₹20.5 (t −2.75), short −₹15.2 (t −1.57).
- **The baseline holdout is not virgin.** R56 reported this window under the old sizing; the new sizing and execution had never been run on it.

**E3S (E3 gap fade, short only) → `A1000.XS`**

| Config | TRAIN 19–22 | VAL 2023 | 2024–25H1 (opened in R81) | **SEALED forward 2025-06-20..2026-11-13** (n, t, 95% CI) | Gross vs bar |
|---|---|---|---|---|---|
| A1000.XS (selected) | +17.6 | +12.4 | **−54.4** | **+173.6** (87, **1.86**, +2 to +363) | 32.4 vs 13.4 bps |
| A1000.X0 | +33.9 | −47.8 | −5.7 | +158.5 (90, 1.74) | 30.7 vs 13.4 |
| A750.XS | −23.1 | +12.2 | −26.0 | +99.7 (93, 1.42) | 27.2 vs 14.2 |
| A1000_nomin.X0 | +28.1 | −38.9 | +3.0 | +123.4 (103, 1.53) | 28.3 vs 13.4 |
| S0L.X0 | −5.3 | −11.8 | +11.8 | +18.5 (113, 0.41) | 17.9 vs 14.8 |
| S0.X0 | −8.2 | −11.3 | +16.2 | +34.8 (115, 0.73) | 20.1 vs 14.7 |

**Pre-registered gates for A1000.XS:**

| Gate | Outcome |
|---|---|
| Positive in every period | FAIL (−54.4 on 2024–25H1) |
| Half-years 2019–2023 | FAIL (5/10 positive, needs 7; every H1 negative and every H2 positive, a pattern with no mechanism) |
| Plateau on VALIDATE | FAIL (A750.XS +12.2 and g0.75 +20.9 pass; g1.25 −70.3 fails) |
| Holdout \|t\| ≥ 2 | FAIL (t = 1.86) |
| Placebo | not run (the config fails the sign gates; pre-registered) |

**Verdict: NOT A RESULT.**

What the forward window does say, which is worth recording:
- The short side of E3 made money in 2025 H2 – 2026 H1 at every size.
- Larger, all-in-sized positions clear the bar on gross, because they amortise the fixed brokerage.
- 81% of the forward profit comes from the top 5 of 87 trades.
- The last two quarters are negative: 2026Q3 −₹109/trade, 2026Q4 −₹168/trade.

That is one good period after a negative one, which is exactly what the protocol calls noise.

## Recommendations

- **Fix #787 regardless of strategy viability.** A live engine that ignores open positions will hit broker RMS rejections mid-day; that is the #626 failure shape in equity. The fix is specified in Phase 2: an all-in rule with a buying-power ledger and a minimum-viable skip.
- **Adopt the all-in budget rule (A500) for any future live use.** It bounds the loss per trade to the budget (0.2% of trades beyond ₹500, against 31.6%) at no cost in bps.
- **Do not raise the budget.** Under ₹1L of buying power, ₹1,000 is unreachable and ₹750 barely reachable, and neither improves bps.
- **Do not pursue limit-order entry or spread filtering** for this signal class. The adverse selection measured here cancels the saving.
- **Recognise that ₹20k of capital cannot carry this engine.** The bar cannot go below 13.07 bps inside ₹1L, and the signals tested in R56–R82 earn 0–7 bps gross, except E3 short in one window.
- **The only open thread:** E3 short, at ≥ ₹90k notional, measured forward (paper or sandbox with the ₹1L ledger enforced). Pre-register a decision rule before looking at the data, for example "after 60 new trades, keep only if the net CI excludes 0". It is not deployable on this evidence.

## Protocol compliance and disclosures

- **Pre-registration** committed before any Phase 2/4/5 run (`196b2a3a0`). Selection frozen by commit before any holdout run (`00ce17f6f`). Holdout configs run: the selected one, its pre-registered neighbours, and the S0/S0L references.
- **Ledger:** `backtest/r82_sizing/data/variant_ledger_r82.csv`, every config × period, failures included.
- **BASE holdout:** previously seen under the old sizing (R56). The E3S holdout is a genuinely unseen forward window, fetched for this round.
  - **Universe:** NSE month-end F&O lists for 2025-06..2026-10, all 17 present, 255 names; HFCL and PEL unresolved.
  - **Data:** Kite 1m 2025-04-01..2026-11-13 with 0 fetch failures, stored in `data/bars_fwd.duckdb` (local, not committed).
- **Resolutions not spelled out in the pre-registration:**
  - A limit filled inside a 1m bar takes any stop touch in that same bar at the stop price. This is conservative.
  - Pending limit orders reserve buying power.
  - BASE's engine signal stays pending while a limit is working, which blocks re-entry, as in live.
  - APCT runs with the engine's rupee-denominated trail start at ₹500.
  - APCT's rupee figures are not comparable across rules, and the bps column is the fair comparison. This was not anticipated when the ₹/trade selection metric was pre-registered, and the metric was not changed after seeing it.
- **Leverage** was assumed at 5× for every name. Real per-stock MIS multipliers can be lower, so every buying-power figure here is a best case.
