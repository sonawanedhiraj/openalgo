<!-- migrated from outputs/2026-06-13_screener_performance_2026-06-12.md on 2026-06-13 | summary: Screener Performance â€” 2026-06-12 (Fri) -->

# Screener Performance â€” 2026-06-12 (Fri)

**Generated:** 2026-06-13 (analysis only, read-only on `db/openalgo.db`)
**Source:** in-process `scanner_comparison` table (job `scanner_comparison_eod`) +
`scan_cycle`, `signal_decision`, `trade_journal` cross-reference.

## Did the EOD comparison job fire?

**YES.** Both rows (BUY + SELL) written at **2026-06-12 15:45:00 IST**, `telegram_sent=1`.
The in-process job is now the system of record (no more manual runs as on 06-11).

## BUY side

| metric | value |
|---|---|
| in-house | **0** |
| chartink | **7** |
| intersection | 0 |
| Jaccard | 0.0 |
| ratio | 0.0 |

- **Tuning suggestion (job):** "too tight: 0/7 Chartink names matched â€” in-house gates missing names; loosen or check data coverage."
- **False negatives (Chartink-only), all 7:** ASHOKLEY, BANKBARODA, HINDPETRO, INDIGO, MOTILALOFS, NYKAA, PGEL
- **False positives (in-house-only):** none

## SELL side

| metric | value |
|---|---|
| in-house | **1** (MFSL) |
| chartink | **1** (ONGC) |
| intersection | 0 |
| Jaccard | 0.0 |
| ratio | 0.0 |

- **Tuning suggestion (job):** "structural mismatch: fully disjoint â€” most likely in-house tick starvation, not a threshold problem. Check WS subscription coverage."
- **False positive:** MFSL Â· **False negative:** ONGC

## scan_cycle (Chartink webhook activity)

40 cycles, all `cycle_kind='chartink'`: **16 ok, 18 empty, 6 aborted_preflight**.
- BUY hits posted in 13 cycles: MOTILALOFSÃ—13, PGELÃ—8, BANKBARODAÃ—5, ASHOKLEYÃ—3, HINDPETROÃ—2, INDIGOÃ—2, NYKAAÃ—2
- SELL hits posted in 3 cycles: ONGCÃ—3

## signal_decision (what the engine evaluated)

- **448 records**, all source `chartink_FnO_intraday_buy`.
- decision: **take=5, skip=443**. directions: BUY=314, SELL=134. actually_taken: 5.
- Top symbols evaluated: ASHOKLEY(162), NYKAA(148), ONGC(134) â€” repeated re-evaluation per tick.

## trade_journal (what executed â€” sandbox)

- **5 trades**, all `trending_equity_intraday` (the simplified engine), all LONG.
- exits: 4 stop_loss, 1 eod_watchdog (ASHOKLEY, unpriced). **Total P&L â‰ˆ âˆ’â‚¹638.75.**
- MOTILALOFS +148, HINDPETRO +171, BANKBARODA âˆ’416, PGEL âˆ’542, ASHOKLEY n/a.

## Verdict: **IN-HOUSE SCANNER TICK-STARVED â€” but the engine traded fine off Chartink**

The 0/7 BUY and disjoint 1/1 SELL are NOT a threshold/drift problem. The smoking
gun: **5 of the 7 Chartink BUY names were actually traded** (MOTILALOFS, BANKBARODA,
PGEL, ASHOKLEY, HINDPETRO in `trade_journal`), and 448 signal_decisions were
evaluated â€” the engine *acted on the Chartink webhook feed* correctly. Yet the
**in-house tick-driven scanner** (`scan_results source='inhouse'`) surfaced 0 BUY /
1 SELL because it only sees ticks the engine subscribed (the known
"in-house scanner starved / no self-subscribe" pattern). Disjoint = coverage gap,
not tuning gap. The job's own SELL verdict already flags this.

**Delta vs 06-11:** 06-11 was "parity by vacancy" (0/0 BUY â€” every cycle aborted
preflight, Chartink posted nothing). On 06-12 the Chartink feed was healthy
(7 BUY names, 16 ok cycles) and the engine traded; the in-house scanner remained
the lagging component. So the gap moved from *no-input-either-side* to
*Chartink-rich, in-house-starved* â€” confirming the starvation hypothesis.

**Tuning levers / coverage issues:**
1. **Not a threshold tune.** Loosening in-house gates won't help if ticks never arrive â€” fix WS subscription coverage so the in-house scanner sees the Chartink universe.
2. The 6 `aborted_preflight` cycles still leak ~15% of Chartink cycles â€” worth a look but did not block the 5 trades.
3. Trading side healthy: engine consumed Chartink signals, applied stops, EOD watchdog fired (ASHOKLEY caught). Net âˆ’â‚¹638 sandbox on a low-conviction day.
