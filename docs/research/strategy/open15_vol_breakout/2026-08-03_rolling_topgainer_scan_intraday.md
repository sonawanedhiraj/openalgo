# Rolling top-gainer scan vs. the fixed 09:16 snapshot — 2026-08-03

**Status:** exploratory. Not a Round. Not a promote/reject.
**Harness:** `backtest/open15_rolling/`
**Scope (operator, 2026-08-03):** rolling re-ranking may only produce entries up
to **09:39**, matching the original strategy's opening-window intent. §6 is the
in-scope result; §2–3 are an out-of-scope full-day exploration kept for context.

> **Read §6 first.** §3's full-day P&L table measures a window the operator has
> since ruled out of scope.

## 1. The question

`open15_vol_breakout` ranks the universe by opening gap **once**, at 09:16, and
watches those top-3 for the rest of its window. The operator's hypothesis: the
names that actually run are often *not* the ones that gapped, so a leaderboard
recomputed every 30 s during the session would watch the right symbols.

## 2. Selection finding — the hypothesis is confirmed (this is the solid result)

Today's live pick set was `DIVISLAB (+4.02%)`, `BAJFINANCE (+3.08%)`,
`INDIGO (+2.49%)` — zero entries fired (`no_entry` for all three; INDIGO broke
its level but only reached 1.27× volume against a 1.5× gate).

Re-ranking every 30 s across 09:35–11:53 gives a completely different picture:

| Symbol | 09:15 gap rank | 09:15 gap | Day high vs prev close | % of 30 s snapshots in top-3 |
|---|---|---|---|---|
| ABCAPITAL | **#22** | +1.41% | **+6.5%** | 99.6% |
| JUBLFOOD | **#130** | +0.39% | **+6.5%** | 57.6% |
| GODFRYPHLP | **#106** | +0.53% | **+6.4%** | 49.1% |
| PAYTM | **#134** | +0.37% | **+5.1%** | 43.9% |
| DIVISLAB | #1 *(picked)* | +4.02% | +5.5% | 45.4% |
| BAJFINANCE | #2 *(picked)* | +3.08% | — | **0%** |
| INDIGO | #3 *(picked)* | +2.49% | — | **0%** |

Eight distinct symbols passed through the rolling top-3; the set differed from
its 09:35 composition in 99.6% of snapshots. **Two of the three live picks never
appeared in the intraday top-3 at all.** The gap ranking put the day's four
biggest movers at #22, #106, #130 and #134.

## 3. P&L finding — the selection fix does NOT convert into an edge

Identical entry gate (within-minute cumulative volume ≥ `vol_x` × trailing-20-min
mean minute volume, AND price beyond the level), identical costs
(`mis_round_trip_charges`, ₹112,500 notional/slot, max 3 concurrent, long-only,
one entry per symbol). The only variable is the selection regime.

`LIVE` = watch only the three 09:16 picks. `FIXED` = rank once at 09:35, never
re-rank. `30s`/`300s` = re-rank the whole universe on that cadence.

Level = prior day high (excluding the triggering tick), `vol_x` = 1.5:

| Regime | 15 m hold | 30 m hold | 60 m hold |
|---|---|---|---|
| LIVE (3 gap picks) | −0.385%/trd · −₹1,014 · n=2 | −0.094% · −₹385 · n=2 | **+0.399% · +₹712** · n=2 |
| FIXED @09:35 | −0.641% · −₹807 · n=1 | −0.064% · −₹159 · n=1 | −0.569% · −₹726 · n=1 |
| **30 s rolling** | **+0.197% · +₹677** · n=5 | +0.083% · +₹37 · n=5 | +0.082% · +₹25 · n=4 |
| 300 s rolling | −0.177% · −₹1,116 · n=4 | +0.137% · +₹196 · n=3 | +0.198% · +₹398 · n=3 |

Rolling wins at the 15 m hold, ties at 30 m, and **loses to the fixed live picks
at 60 m**. With n = 1–5 trades on one partial day this ordering is noise.

### Why it does not convert: the gate makes you chase

Every rolling entry (30 s / dayhigh / 15 m / 1.5×):

| Symbol | Entry | Already up at entry | vol× | Return over 15 m | Net ₹ |
|---|---|---|---|---|---|
| ABCAPITAL | 09:40:21 | **+5.17%** | 3.47 | −0.106% | −206 |
| GODFRYPHLP | 09:56:53 | **+4.73%** | 1.56 | +0.295% | +243 |
| PAYTM | 10:07:33 | **+4.72%** | 2.30 | −0.135% | −236 |
| DIVISLAB | 10:28:34 | **+4.72%** | 1.65 | −0.119% | −216 |
| JUBLFOOD | 10:38:20 | **+5.35%** | 2.21 | **+1.050%** | **+1,091** |

The rolling scan does find the right names — but a "new day high on a volume
surge" in a name already +5% is a chase, and it buys the tail of the move.
**4 of 5 trades are negative; the entire positive result is one JUBLFOOD trade.**
This is the same shape as the R60/R60b finding
(`[[r60-scanner-mild-band-afternoon-symmetric]]`): on real tape, chasing fails and
the edge sits in a *retrace-limit* entry, not a breakout entry.

## 4. Limitations (load-bearing)

1. **No data before 09:35.** `tick_logs/` is written by the simplified engine,
   which arms only after the Chartink webhook. `open15`'s own capture
   (`tick_logs/open15/`) covers 09:15–09:30 but persists **selected symbols
   only** (3 symbols today — `_capture_tick` docstring). So the window where the
   hypothesis matters most is blind: ABCAPITAL went from +1.41% at the open to
   +4.32% by 09:35 entirely inside it.
2. **`to_end` rows in `sweep_out.json` are not an exit rule** — the data ends at
   11:53 with the market open, so they are a favourable MTM snapshot. Ignore them.
3. **One partial session, n ≤ 5.** No statistical claim is available.
4. The live config trades `atm_option`, not stock. All numbers here are the
   **underlying** equity leg.
5. Sub-second artefact: a re-rank scheduled at *t* is evaluated on the first tick
   at or after *t*, so the symbol carrying that tick is priced ≤ 1 s late. Every
   other input (price, cumulative volume, level) is strictly as-of *t*.

## 6. IN-SCOPE RESULT — additive watch list, TRUE window (entries ≤ 09:29, flat 09:30)

The strategy's real window is `no_entry_after 09:29` / `exit_time 09:30`
(`open15_config`). **The existing tick logs cannot test the proposal in that
window.** Only 4 days survive, each with ~6 min of decision time.

Rules are now live-exact on both previously-deviating axes: the level is the
**true 09:15 candle high** (`first_candles_by_date.json`, fetched per symbol per
date from the app's 1m history) and the volume baseline is the **expanding** mean
over completed minutes since 09:16.

| Date | Capture start | Usable decision window | LIVE | ADDITIVE | REPLACE |
|---|---|---|---|---|---|
| 2026-07-14 | 09:20 | 09:23–09:29 | 0 trd | 1 · −₹149 | 1 · −₹149 |
| 2026-07-17 | 09:20 | 09:23–09:29 | 0 | 0 | 0 |
| 2026-07-28 | 09:20 | 09:23–09:29 | 1 · +₹687 | 2 · +₹1,111 | 1 · +₹687 |
| 2026-07-30 | 09:20 | 09:23–09:29 | 0 | 1 · −₹113 | 0 |
| **total** | | | **1 trd · +0.690%/trd · +₹687** | **4 trd · +0.267% · +₹849** | 2 trd · +0.317% · +₹538 |

Incremental (ADDITIVE − LIVE): **3 trades, +₹162, avg +0.126% gross, 1 win of 3.**
Nothing is displaced — slots never bind at this trade volume.

| | Entry | Up at entry | vol× | Return | Net |
|---|---|---|---|---|---|
| MCX (07-14) | 09:25:54 | +2.40% | 1.50 | −0.056% | −₹149 |
| NAUKRI (07-28) | 09:24:52 | +1.80% | 1.51 | +0.457% | +₹424 |
| TECHM (07-30) | 09:24:45 | +1.34% | 1.51 | −0.024% | −₹113 |

Directionally encouraging: inside the real window the incremental entries fire at
**+1.3% to +2.4%** already-up — early in the move, not the +4.7–5.4% chase the
full-day variant produced (§3). But n = 3.

**The level fix changed the answer materially**, which is why it mattered: under
the earlier running-high substitute, 07-17 produced a +₹1,881 LIVE trade (360ONE)
that does not exist against the true 09:15 level, and 07-28's LODHA seed trade did
not appear. Any conclusion drawn from the pre-fix run was an artefact of the
substituted level.

**Nothing can be concluded.** One trade versus three, across four days.

The reason is structural, not analytical: the real entry window is 09:16–09:29
(**14 min**) and the tick logs observe at best **09:23–09:29 (6 min, <45%)** —
missing the opening stretch, which is the busiest. 15 of 19 days are unusable
outright. See §4.1: this is a capture problem.

## 6a. ATM-option leg via Black-Scholes (`bs_option_pnl.py`)

The live config is `instrument = atm_option`; §6 measures the equity leg. July
contracts have expired out of the master contract, so real premiums are not
fetchable — BS is the fallback, reusing `backtest/options_open15/bs.py`,
IV = RV60 × 1.10, r = 0.065, live sizing (`lots = floor(22500 / (premium × lot))`)
and `open15_option_shadow.option_round_trip_charges`.

**Calibration against the 4 real premiums in `open15_trades`:**

| Date | Symbol | DTE | Real | BS | BS/real |
|---|---|---|---|---|---|
| 07-22 | BAJAJ-AUTO | 6.3 d | ₹152.0 | ₹150.70 | 0.99 |
| 07-28 | POWERINDIA | 0.3 d | ₹193.9 | ₹208.36 | 1.07 |
| 07-23 | OIL | 5.3 d | ₹5.8 | ₹9.80 | **1.69** |
| 07-28 | MPHASIS | 0.3 d | ₹9.2 | ₹5.51 | **0.60** |

Per-trade error runs **0.60× to 1.69×**. MPHASIS shows the expiry-day IV spike
(implied 0.49 vs RV60×1.10 = 0.34) — on 0DTE the BS premium is too low, so the
fit-to-capital lot count is too high and P&L is overstated.

**Result — and its instability:**

| IV assumption | ADDITIVE (4 trd) | Incremental (3 trd) |
|---|---|---|
| stock leg (§6) | +₹849 | +₹162 |
| BS, no expiry bump | **+₹35,885** | +₹15,797 |
| BS, expiry-day IV ×1.44 | **+₹21,711** | +₹9,189 |

| | K | DTE | lots | prem in→out | stock net | option net |
|---|---|---|---|---|---|---|
| MCX 07-14 | 2850 | 14.3 d | 1 | 90.85→90.00 | −₹149 | −₹420 |
| NAUKRI 07-28 | 1220 | 0.25 d | 7 | 5.38→7.95 | +₹424 | +₹9,609 |
| LODHA 07-28 (seed) | 1260 | 0.25 d | 4 | 8.47→13.60 | +₹687 | +₹12,522 |
| TECHM 07-30 | 1664 | — | — | — | −₹113 | **unaffordable** |

**Not usable as a P&L estimate.** Reasons, in order of severity:

1. **Two 0DTE trades on one day are the entire result.** Drop 2026-07-28 and the
   option leg is MCX −₹420 plus a skip — a loss.
2. **One defensible IV assumption moves the answer 40%** (₹35.9k → ₹21.7k).
3. **BS per-trade error is 0.60–1.69×** on the only real data available.
4. **Spread is unmodelled.** NAUKRI at ₹5.38 with a ₹0.05 tick: a ₹0.20 spread on
   7 lots × 550 is ~₹1,540 round-trip, ~16% of that trade's modelled profit.
5. **Fill realism.** 7 lots of a ₹5 0DTE option at one tick is optimistic.

**One robust, pricing-independent finding:** TECHM was **unaffordable**
(₹71 premium × 600 lot = ₹42,600 > the ₹22,500 slot), so the option instrument
*silently drops signals the stock version takes*. Direction is not reliably
preserved either — the live OIL trade was **−₹18 on stock, +₹716 on the option**.
Equity-leg results therefore cannot be used as a proxy for what this strategy
would actually book.

## 6b. Out-of-scope sensitivity — entries ≤ 09:39 (a wider window than live)

**Mechanism (operator, corrected 2026-08-03):** the watch list *starts* as the
09:16 gap picks and **grows** — every 30 s the current top-3 is APPENDED.
Nothing is ever removed. It is a strict superset of the live watch list, so it
can only add entries; the only way it removes one is **slot competition**
(3 concurrent slots, first-come-first-served).

`REPLACE` (watch list = current top-3, symbols dropped when they fall out) is
kept as a contrast — it is NOT the proposal.

**Seed correctness (load-bearing).** The seed is reconstructed as
`daily_open / prev_close − 1`, top-3 — verified identical to the 09:15 1m bar
open on 4 spot-checks, and prev-closes matched the live logs 24/24. It
deliberately does NOT reproduce the logged historical picks: `first_candle_source`
was `None` (tick-built) on every day before 2026-08-03, i.e. the #502 bug the
SPEC documents, so the logged picks are a defect's output. The reconstruction is
what the strategy does *today*.

7 usable days (2026-07-09 … 07-31); 12 days skipped and reported (capture too
late to leave decision time before 09:39).

| Flatten | LIVE (seed only) | **ADDITIVE (proposal)** | REPLACE |
|---|---|---|---|
| 09:45 | 9 trd · +0.313%/trd · +₹2,391 | **14 trd · +0.270% · +₹3,040** | 10 trd · +0.269% · +₹2,135 |
| 09:40 | 9 trd · +0.114% · +₹388 | **14 trd · +0.107% · +₹489** | 10 trd · −0.003% · −₹903 |

ADDITIVE beats LIVE on total rupees at both exits, and **dilutes per-trade**
(+0.313 → +0.270; +0.114 → +0.107). It also fixes REPLACE's 09:40 sign flip —
accumulating is strictly better than churning, as expected.

### Decomposition — where the difference actually comes from

Diffing the trade lists: **7 incremental trades gained, 2 LIVE trades displaced**
by slot competition.

| Flatten | Incremental (7) | Displaced LIVE (2) | Net effect |
|---|---|---|---|
| 09:45 | +0.146%/trd · **+₹538** | would have lost ₹109 → +₹109 | +₹647 |
| 09:40 | +0.037%/trd · **−₹316** | would have lost ₹416 → +₹416 | +₹100 |

Two things this exposes:

1. **Roughly half the gain is luck, not edge.** At 09:40 the incremental trades
   *lose* ₹316; ADDITIVE is only ahead because it happened to crowd out two
   losing LIVE trades. That is slot-ordering noise.
2. **One trade carries everything.** NAUKRI (2026-07-28, +1.17%, +₹1,228) is the
   only meaningful incremental winner:

   | | all 7 incremental | excluding NAUKRI |
   |---|---|---|
   | 09:45 | +₹538 | **−₹690** (1 win / 6) |
   | 09:40 | −₹316 | **−₹1,098** (1 win / 6) |

The watch list grew to 4–14 symbols by 09:39 (median 8).

### Verdict

**Not supported — and not refuted.** The additive mechanism does what it is
supposed to do (adds ~78% more entries, never loses one except to slot
contention) but the added entries carry no demonstrable edge: 1 win in 6 once the
single dominant trade is removed, and the sign of the whole result flips on a
5-minute change in exit time. n = 7 incremental trades over 7 days cannot decide
this either way. The blocking issue is sample size, which is a **data-capture**
problem (§4.1), not an analysis one.

## 5. What to do next

1. **Widen tick capture to the full session and full universe** so the 09:15–09:35
   window and all 211 symbols are replayable. This is the blocking prerequisite —
   without it no multi-day version of this test can see the interesting window.
2. **Replay the 16 existing full-session tick logs** (2026-07-09 → 07-31, each
   ~250 MB, starting 09:20–09:55). That is the real multi-day answer and needs no
   new data: `sweep.py <ticks> <prev_closes>`.
3. **Test retrace-limit entry, not breakout entry**, on the rolling-selected
   names — per R60 that is where the edge was on real tape.
