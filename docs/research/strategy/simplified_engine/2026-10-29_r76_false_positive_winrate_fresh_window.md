# R76: simplified_engine false positives and win rate, scored on a fresh window

Issue #775. Operator question (2026-10-29): *"how can the false positives be removed
from simplified stock engine and improved win rate. show numbers."*

Harness: `backtest/simplified_engine_r76/fp.py`. It is read-only on `db/openalgo.db`.

## Data and method

- **Trades:** 474 closed sandbox `trade_journal` rows, 2026-06-01 → 10-27.
  - **Excluded:** 32 `phantom_cleanup_*` rows (test pollution).
  - **P&L:** net = `pnl − charges_inr`, using the journal's modelled Zerodha MIS charges.
- **Three date windows:**
  - **W1** ≤ 08-08 (n=231): R61/R62 in-sample.
  - **W2** 08-09 → 09-18 (n=114): R65's out-of-sample window.
  - **W3** 09-19 → 10-27 (n=129): **fresh**. No earlier round has seen these trades.
- **Survival rule:** a filter survives only if its net P&L is positive in all three windows.
- **Counter-NIFTY placebo:** the cell's mean net P&L is compared with 5,000 random
  same-size draws from the W3 trades that carry a NIFTY reading.

## 1. Win rate is not the problem. The payoff is.

| Window | n | Win rate (net) | Avg win | Avg loss | Payoff | Break-even win rate | Net |
|---|---|---|---|---|---|---|---|
| W1 | 231 | 53.7% | ₹336 | ₹472 | 0.71 | 58.4% | −₹8,868 |
| W2 | 114 | 52.6% | ₹188 | ₹493 | 0.38 | 72.4% | −₹15,319 |
| **W3** | 129 | **63.6%** | ₹224 | ₹504 | 0.44 | **69.2%** | −₹5,303 |

- **W3 had the best win rate of the run (63.6%) and still lost money.**
- **Gross payoff:** 0.89 / 0.61 / 0.66 by window.
- **Charges** are about ₹76 per trade. That pulls net payoff down to 0.71 / 0.38 / 0.44.
- **All 474 trades:** gross +₹7,074, charges ₹36,564, **net −₹29,490**.

## 2. Filters: none is positive in all three windows

Each cell is n, win rate and net P&L.

| Cohort | W1 | W2 | W3 (fresh) |
|---|---|---|---|
| Before 11:00 | 49 · 63.3% · −₹626 | 28 · 50.0% · −₹5,444 | 24 · 79.2% · **+₹2,765** |
| 11:00–13:00 | 106 · 56.6% · −₹6,787 | 48 · 50.0% · −₹8,714 | 75 · 66.7% · −₹3,306 |
| After 13:00 | 76 · 43.4% · −₹1,455 | 38 · 57.9% · −₹1,161 | 30 · 43.3% · −₹4,761 |
| First trade of day | 41 · 56.1% · **+₹2,966** | 26 · 57.7% · −₹3,138 | 25 · 68.0% · +₹300 |
| No loss yet today | 93 · 57.0% · **+₹5,171** | 55 · 58.2% · −₹6,533 | 69 · 71.0% · +₹325 |
| After a loss today | 138 · 51.4% · −₹14,039 | 59 · 47.5% · −₹8,786 | 60 · 55.0% · −₹5,628 |
| Trade #4+ | 116 · 50.9% · −₹5,739 | 47 · 53.2% · −₹5,607 | 54 · 59.3% · −₹4,059 |
| 3rd+ same direction | 121 · 48.8% · −₹11,453 | 42 · 52.4% · −₹6,851 | 56 · 64.3% · −₹1,857 |
| LONG | 116 · 52.6% · −₹10,436 | 53 · 66.0% · −₹2,660 | 47 · 57.4% · −₹6,214 |
| SHORT | 115 · 54.8% · +₹1,568 | 61 · 41.0% · **−₹12,659** | 82 · 67.1% · +₹911 |
| VIX < 12 | — | 103 · 52.4% · −₹14,281 | 20 · 75.0% · +₹1,328 |
| Same-symbol re-entry | 13 · 46.2% · −₹3,167 | 3 · 33.3% · −₹401 | 0 |

- **Every cohort that is positive somewhere flips sign in at least one other window.**
- **Same-symbol re-entries:** none in W3. The R65 guard-rehydrate bug no longer shows in the
  journal.
- **The chase filter is untestable:** almost every entry is within 0.1% of `ltp_at_signal`.

## 3. R65's pre-registered counter-NIFTY label fails

R65 set a rule for this label: at ≥ 60 new labelled trades, gate on it only if both sides
and both months clear the baseline. The journal's NIFTY reading
(`nifty_pct_at_entry`) starts 08-11, so W1 has no data.

| Cell | W2 | W3 (fresh) |
|---|---|---|
| Counter-NIFTY >0.25% | 22 · 68.2% · −₹358 | 23 · 65.2% · **−₹1,915** |
| — LONG | 17 · 64.7% · −₹1,063 | 7 · 42.9% · −₹1,766 |
| — SHORT | 5 · 80.0% · +₹705 | 16 · 75.0% · −₹149 |
| With NIFTY >0.25% | 55 · 43.6% · −₹11,194 | 71 · 57.7% · −₹3,006 |
| Flat NIFTY | 37 · 56.8% · −₹3,767 | 35 · 74.3% · −₹381 |

- **The win rate held, but net P&L is negative in both windows.**
- **Placebo:** W3 placebo p = **0.69**, i.e. random same-size picks do as well.
- **LONG side:** reversed to 42.9%.
- **By month:** Aug −₹285, Sep −₹191, Oct −₹1,798.
- **Verdict: REJECT.** The label stays observational only. The veto prompt's regime-conflict
  rule needs no change on this evidence.

## 4. Cost sensitivity: Kotak Neo API, ₹0 brokerage

- **The change:** removing brokerage + GST drops about ₹43 per trade (₹21.8k over the
  run). W3's break-even win rate falls from about 69% to about 64%, against 63.6% observed.
- **Result:** near break-even, but still no edge.
- **Sandbox optimism:** these are sandbox fills at LTP, so live would be worse.
- **Stops fire on the full move:** 397 of the 474 exits are `stop_loss` (trailing included).
  The losers run to the full stop while winners are banked early by the 0.5 trail and
  0.6R lock.

## Verdict

**REJECT: no filter removes "false positives" profitably.** This is the sixth round to agree
the signal has no intraday edge (R56, R57, R61, R62, R65, R76).

- **The binding constraint is payoff:** the average loss is about 2.2× the average win.
  Selection does not fix it.
- **Exit rules don't fix it either.** R62 tested 23 of them out-of-sample and all failed.
- **The only lead is a different entry mechanic:** R60b's retrace-limit entry (hit ∓0.5%,
  valid for 60 min). It belongs in a sandbox measurement, not in a filter on this entry.
- **No config change.**
