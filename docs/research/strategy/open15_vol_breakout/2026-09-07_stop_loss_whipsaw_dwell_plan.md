# open15 per-trade stop: "hit and immediate reversal" — research + plan

**Date:** 2026-09-07 · **Strategy:** `open15_vol_breakout` (live, real money since 2026-07-24)
· **Tracking issue:** #708 · **Rule under study:** the per-trade stop loss (#696), counterfactual + scorecard (#704)
· **Operator hypothesis:** *"if the price goes below the SL and keeps trading below it
for some seconds, only then place the exit order"* — i.e. add a dwell/confirmation
before the stop fires.

## 0. TL;DR

1. **The observation is real.** In 2 of the 3 live stops so far the option premium
   traded back above the stop level within the same minute the stop fired
   (ANGELONE 09-04: fired at −₹3,000, the minute closed at −₹1,750; MCX 09-07:
   fired at −₹2,655, the minute closed at −₹2,227).
2. **But all 3 stops were RIGHT** by the #704 counterfactual — holding to 09:30
   would have lost more in every case (saved ₹747 / ₹2,228 / ₹1,322 net). The
   reversal was a bounce inside the noise, not a turn.
3. **A 25-row bar-level replay (2026-08-26 → 09-07) says every delayed-confirmation
   variant loses to the immediate touch stop**, because a later fire fills at a worse
   price and the "rescued" whipsaws are few (3 of 15 touches ended positive) and small
   next to the losers the stop cuts (−₹12.5k, −₹9.8k, −₹7.0k …).
4. **The deeper cause of "hit and reverse" is the threshold, not the trigger
   mechanics**: ₹2,500 on a ₹60k slot is ~4.9% of premium, and the median 1-minute
   premium range in the hold window is ~4.6%. The stop sits *inside* the one-minute
   noise band, so 15 of 25 rows touch it. On wide-spread contracts the bid–ask bounce
   alone is 20–95% of the stop distance.
5. **Plan: measure first, then decide by a pre-registered rule (the #704 pattern).**
   Phase 0 records the seconds-level mark path the rule actually sees and shadow-scores
   dwell / consecutive-poll / mid-price / noise-scaled-threshold variants against the
   live rule with **no behaviour change**. Phase 1 ships `stop_confirm_s` (default 0 =
   today) only if the shadow wins over ≥ 20 stop events. Nothing in this plan changes
   what the live stop does today.

## 1. How the stop works today (what "hit" means in code)

- `Open15BreakoutService._risk_loop` (`services/open15_breakout_service.py`) runs on
  the `open15-risk-monitor` daemon thread every `live_poll_interval_s` (**currently 2 s**
  in `open15_config`, clamped 2–60).
- Each tick calls `open15_pnl_curve.live_pnl()` → ONE batched `get_multiquotes` for every
  open (and #704 ghost) contract; `_batched_ltp` keeps only **`ltp`** — the quote's
  `bid`/`ask`/`depth` are discarded (the Zerodha mapper does return them).
- `_risk_tick`: for each open real row, `mtm = (ltp − entry_fill_price) × qty`; if
  `mtm ≤ −stop_loss_inr` (**₹2,500**) the row is exited **on that poll** via
  `_exit_open_row(symbol, pos, "stop_loss")` — MARKET SELL of the option, book-verified,
  fill-reconciled, child mirrors follow.
- So "hit" = **a single LTP print, sampled once every 2 s, at or beyond the level**.
  There is no dwell, no consecutive-poll requirement, no bid/ask awareness, and the
  per-poll path is **not persisted** (only the 1m bars are, after the fact).

## 2. The three live stops (all the seconds-level evidence that exists)

| Date | Row | Entry → fire | Fired at MTM | Fill slippage past the fired mark | Same-minute bounce | Held to 09:30 (cf, gross) | Verdict |
|---|---|---|---|---|---|---|---|
| 09-04 | ANGELONE 305CE ×5000 | 09:20:48 → 09:27:24 (6m36s) | −₹3,000 (jumped from > −2,500 in ONE 2 s poll) | ₹0 (fill 9.90 = LTP) | minute close −₹1,750 (+₹1,250 above the stop) then −₹3,750 by 09:29 | −₹3,750 | right, saved ₹747 |
| 09-07 | BAJAJ-AUTO 11700PE ×300 | 09:20:46 → 09:22:46 (2m00s) | −₹2,598 | **−₹672** (LTP 169.00 at fire, fill 166.76; spread was 1.7% of premium) | none — fell straight to −₹5,898 by 09:25 | −₹5,508 | right, saved ₹2,228 |
| 09-07 | MCX 3350CE ×450 | 09:22:56 → 09:23:19 (**23 s**) | −₹2,655 | −₹225 | minute close −₹2,227 (+₹428 above the stop) then −₹4,455 by 09:25 | −₹4,208 | right, saved ₹1,322 |

Sources: `open15_trades` rows 79/81/83, the day logs' `exit` events, today's log
(`per-trade stop loss hit` lines), and `uv run python -m services.open15_sl_counterfactual
--date … ` dry-runs (the rows' `cf_*` columns are still NULL — see §7).

Reading: a dwell of ~10–30 s would probably have **skipped** the MCX and ANGELONE first
breaches (both bounced) and fired later at a worse mark, or not at all and eaten
−₹4,208 / −₹3,750. On these three it would have cost money. n = 3 is not evidence
either way — which is exactly why Phase 0 exists.

## 3. Bar-level replay: touch vs confirmed stops (25 real option rows)

Scratch replay over every `fill='real'`, `instrument='option'` row from 2026-08-26
(first Sep-expiry day — earlier contracts have expired and their 1m bars are gone) to
2026-09-07, using the broker 1m bars `open15_option_shadow.fetch_1m_bars` fetches for
the intra-hold curve. MTM path from the entry minute to 09:30, stop = ₹2,500, gross
(charges identical across variants). The **entry minute's low is excluded** (it can
predate the entry itself — the first pass counted IDEA 09-07 as a touch on a low that
happened before the fill).

| Variant | Fires on | Fill assumption | Gross P&L (entry→09:30) | Fired |
|---|---|---|---|---|
| **hold** (no stop) | — | 09:30 close | **+₹6,162** | 0 / 25 |
| **A — touch** (≈ today's 2 s LTP rule) | first 1m LOW ≤ level | **at the level, zero slippage (optimistic)** | **+₹16,485** | 15 / 25 |
| A with ₹600 slippage/fire (≈ observed) | " | level − ₹600 | ≈ +₹7,500 | 15 |
| B — bar-close confirm | first 1m CLOSE ≤ level | that close | −₹1,739 | 11 |
| D — touch + next close still below (~60 s dwell) | low ≤ level AND next minute's close ≤ level | that close | −₹924 | 9 |
| C — two consecutive closes | two closes ≤ level | second close | +₹4,604 | 8 |

Per-row detail (the rows that decide it):

- **The stop's value is on the big losers**, and every confirmation delays them into
  worse fills: MAXHEALTH (hold −12,495 → A −2,500 / B −3,307 / C −5,407),
  HEROMOTOCO (−9,774 → −2,500 / −8,109 / −6,774), HDFCBANK (−6,988 → −2,500 / −2,600 /
  −4,550), ADANIPORTS (−5,700 → −2,500 / −5,700 / −5,700), BAJAJ-AUTO
  (−5,508 → −2,500 / −2,958 / −4,398), MCX (−4,208 → −2,500 / −2,993 / −4,455).
- **The whipsaw rescues are real but few**: 7 of 15 touches bounced back above the
  level within the same minute, but only 3 touched rows ended positive at 09:30 —
  TCS (+7,492), WIPRO (+2,880), CGPOWER (+680); POLYCAB and SUNPHARMA bounced and still
  finished below the level (−2,180 / −1,050 held vs −2,500 stopped: ≈ break-even).
- Even TCS, the best rescue, is also the best argument against confirmation *as
  implemented by bars*: every confirmed variant still stopped it (−3,173 / −2,700).

**Caveats, all load-bearing:** (a) 1m bars cannot resolve "some seconds" — D is the
closest proxy and is coarse; (b) A's fill at exactly the level is optimistic — the three
live fills slipped ₹0 / ₹672 / ₹225 past the fired mark, and the 2 s sampling itself
jumped ANGELONE ₹500 through the level; (c) n = 25 rows over 9 trading days, one regime;
(d) the replay uses today's ₹2,500 on rows that were traded before the stop existed.
None of these change the ordering: **no confirmation variant beats the touch stop on
this sample, and the margin is not close.**

## 4. Why it "hits and reverses": the threshold sits inside the noise

Measured on the same 25 rows (median): stop distance **4.9% of premium**; 1-minute
high−low range of the premium during the hold **4.6%**; entry bid–ask spread **0.9%**
(BAJAJ-AUTO 1.7%, UPL 3.5%, LTF 4.7%).

- A ₹2,500 stop on a ₹60k ATM-option slot is one typical minute's range. That is the
  textbook "whipsaw collector" — a stop inside the market's normal noise band — and it is
  why 60% of rows touch it. A dwell filters *wicks*; it cannot move the level out of the
  band.
- The rule reads **LTP**, which flips between bid and ask prints. On BAJAJ-AUTO the
  spread was ₹915 of MTM — 37% of the stop distance; on LTF/UPL-class books it is most
  of it. The bid/ask pair is already in the batched quote response and is thrown away.
- The 2 s poll adds sampling error on top: the crossing is observed *somewhere* past the
  level (ANGELONE: ₹500 past it), and the MARKET exit then crosses the spread again.

So there are **three separable levers**, and only one of them is "dwell":

| Lever | What it changes | Cost | How to test |
|---|---|---|---|
| **Dwell / consecutive polls** (`stop_confirm_s`, `stop_confirm_polls`) | ignores a breach that reverts within N s | every real breach fills N s later, at a worse mark | shadow on the recorded path |
| **Mark source** (mid = (bid+ask)/2 instead of LTP) | removes bid–ask bounce from the MTM | none at the broker (fields already returned); mid is slightly optimistic for a seller | shadow on the recorded path |
| **Noise-scaled threshold** (`stop = max(₹2,500, k × median 1m premium range × qty)`, frozen at entry) | moves the level out of the noise band per contract | bigger worst-case per trade; needs a hard cap | shadow, plus the 1m-bar replay above |

## 5. What NOT to do

- **Broker-side SL orders.** Zerodha does not allow SL-M on options, and an SL-L order
  triggers on the first LTP touch — precisely the whipsaw being avoided — with a limit
  that can then miss in a fast market. It would also bypass `_exit_open_row` (book
  verification #626, fill reconciliation #641, child-mirror fan-out #690). Verified
  against Zerodha's support articles 2026-09-07 (see Sources).
- **Changing the live rule on belief.** The strategy is real money; the #704 scorecard
  was built precisely so the stop is judged by a rule fixed before the sample fills.
  Adding a dwell mid-sample without labelling would also corrupt that scorecard
  (a "stop" under a 20 s dwell is a different event from one under 0 s).
- **Any broker call on the tick thread** (#626 rule) — all variants below evaluate on
  the risk-monitor thread or the summary job.

## 6. Plan

### Phase 0 — measure the seconds-level path and shadow every variant (no behaviour change)

1. **Keep bid/ask from the batched quote.** `_batched_ltp` → `_batched_marks` returning
   `{contract: {ltp, bid, ask}}`; `live_pnl` keeps computing MTM on LTP (unchanged) but
   the payload carries `bid`/`ask`/`mid` per trade. Zero additional broker calls.
2. **Persist the per-poll mark path.** The risk loop appends
   `[hh:mm:ss.f, ltp, bid, ask]` per open real row (and per #704 ghost row) to an
   in-memory series; at the row's exit (`_exit_open_row`, `flatten`) and again at the
   summary job it is written to a new `open15_trades.mark_path` JSON column (≈ 300 polls ×
   4 numbers per row per day). Restart-safe: whatever was captured is flushed; the 1m
   bars remain the backstop for gaps.
3. **Enrich the existing `exit` event for `reason=stop_loss`** (no new event name — the
   #615/#622 rule): `fired_mtm`, `fired_ltp`, `fired_bid`, `fired_ask`,
   `first_breach_ts`, `secs_below` (time from first breach to fire — today always ≈ 0),
   `polls_below`, `slippage_vs_fired` (fill − fired LTP, once reconciled).
4. **Shadow variants scored from the recorded path** — a pure function
   `open15_stop_variants.evaluate(row, mark_path, ghost_path, sched_exit)` returning,
   per variant, the simulated exit mark (LTP at the first poll satisfying the variant,
   minus the trailing average `slippage_vs_fired`) or the held-to-exit counterfactual
   when the variant never fires. Variant grid, fixed in code:
   dwell ∈ {0, 4, 10, 20, 30} s × mark ∈ {ltp, mid} × threshold ∈ {₹2,500 fixed,
   noise-scaled k=1.5 with a ₹5,000 hard cap}. Results persist as
   `open15_trades.cf_variants` JSON; the summary job and the 09:10 arm back-fill, exactly
   like #704's `backfill_missing`.
   *Why the ghost path makes this computable:* the live rule fires on the first breach,
   so "would a dwell have skipped it" is answered by whether the #704 ghost marks came
   back above the level inside the dwell, and "where would it have fired instead" by the
   next sustained breach on the same ghost series (which already rides the same 2 s
   batch until the scheduled exit).
5. **Scorecard extension** (`open15_sl_counterfactual.scorecard`): one column per
   variant — cumulative net vs the live rule, right-rate, worst event — rendered in the
   existing STOP-LOSS SCORECARD card on `/logs`, plus a "path" strip on the intra-hold
   chart for stopped rows (first breach → fire → bounce).
6. **Pre-registered decision rule** (code constants, like `DECISION_MIN_EVENTS`): adopt a
   variant only if, after **20 stop events**, its cumulative net beats the live rule by
   **≥ ₹500 per event** AND its worst single event is not worse than the live rule's
   worst by more than **one stop distance**. Anything else → keep the immediate stop.
7. Replay CLI: `uv run python -m services.open15_stop_variants --date YYYY-MM-DD
   [--apply]`, dry-run default (#704 shape).

Tests: path recorder (append/flush/restart), variant evaluator (breach→recover→skip;
breach→persist→fire at the later mark; mid vs LTP; noise-scaled threshold; never
fires after the scheduled exit), digest/row-builders unchanged by the enriched `exit`
fields, scorecard columns, CLI dry-run writes nothing.

### Phase 1 — `stop_confirm_s` (only if Phase 0 says so)

- UI knob on `/logs` next to `stop_loss_inr`: `stop_confirm_s` (0–60, **default 0 =
  today's behaviour**, arm-frozen like the other risk thresholds) and `stop_mark`
  (`ltp` | `mid`). Env seeds `OPEN15_STOP_CONFIRM_S` / `OPEN15_STOP_MARK` first-boot only.
- `_risk_tick`: per-symbol `breach_since` (monotonic); fire when
  `now − breach_since ≥ stop_confirm_s`; reset when the mark comes back above the level.
  Exit still goes through `_exit_open_row` — nothing else changes. The `exit` event's
  `secs_below` then records the actual dwell; `armed` records the effective config.
- The scorecard keys events by the config they fired under so pre- and post-change
  cohorts are never blended (the #643 label-don't-refuse rule).

### Phase 2 — tick-resolution marks (optional, after Phase 1)

The open15 ZMQ SUB already receives every frame the WS proxy publishes
(`SUBSCRIBE ""`), but `_handle_raw` drops anything outside the NSE stock universe. After a
fill, subscribe the held NFO contract on the proxy (the scanner-presubscribe nudge
machinery) and let `_handle_raw` update a per-contract last-mark; the risk thread reads
it. Dwell then resolves at tick rate instead of 2 s; the exit is still dispatched from
the risk thread (never from the tick callback, #626). Only worth doing if Phase 0 shows
the 2 s sampling error itself matters.

## 7. Side findings (not part of the plan)

- **The three stop rows' `cf_*` columns are still NULL** (`stop_counterfactual` events
  absent from both day logs): the app that ran today's 09:35 summary predated the #704
  merge (10:14) and was restarted at 12:33. Tomorrow's 09:10 arm back-fills them; to fill
  now: `uv run python -m services.open15_sl_counterfactual --date 2026-09-07 --apply`
  (and `--date 2026-09-04`).
- **`thread_registry` raises a false "DEAD" alert for `open15-risk-monitor` every trading
  day from ~09:30** (09-04 and 09-07 both): `_risk_loop` returns by design once
  `day_status != "armed"`, but the registry only knows `completed` for boot one-shots, so
  a loop that finished its day is reported as a died thread — every 30 min, all
  afternoon. Needs a `completed`/`idle` terminal state for window-scoped loops.

## Sources

- Zerodha support: SL-L as SL-M for options —
  https://support.zerodha.com/category/trading-and-markets/charts-and-orders/order/articles/how-to-use-sl-l-order-like-a-sl-m-order
- Kite Connect forum on SL trigger vs LTP —
  https://kite.trade/forum/discussion/8773/trigger-price-for-stoploss-buy-orders-should-be-higher-than-the-last-traded-price-1112-45
- Whipsaw / confirmation practice: close-based vs intrabar triggering, time filters,
  noise-band (ATR) stop sizing — https://arrowalgo.com/whipsaw-trading/ ,
  https://www.luxalgo.com/library/concept/volatility-stop/ ,
  https://www.dummies.com/article/business-careers-money/personal-finance/investing/general-investing/filter-out-whipsaws-from-your-trading-decisions-189994/
- Replay scripts (scratch, read-only): `stop_replay.py`, `stop_replay2.py` in the session
  scratchpad; inputs = `open15_trades` (ro) + `fetch_1m_bars` broker 1m bars.
