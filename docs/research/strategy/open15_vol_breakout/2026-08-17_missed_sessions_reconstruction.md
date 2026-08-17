# Missed sessions 2026-08-12 and 2026-08-17 — 1m-bar reconstruction

**Date:** 2026-08-17 · **Strategy:** `open15_vol_breakout` · **Status:** measurement, not a result

Two sessions produced no trades for reasons unrelated to the signal. This
reconstructs what each would have done, at the finest resolution the surviving
data supports, and states plainly how far that is from what the live strategy
would actually have booked.

Harness: [`backtest/open15_missed_days/`](../../../../backtest/open15_missed_days).

## 1. Why each day was missed

| Date | Day-log evidence | Cause |
| --- | --- | --- |
| 2026-08-12 | armed 09:10:01 (universe 192, prev-closes 211), then `first_candles source=ticks covered=0`, then `no_ticks_received` at 09:30 | The ZMQ feed delivered **zero** ticks for the whole window. The day armed correctly and watched nothing. `tick_capture_flushed records=0`. |
| 2026-08-17 | single event `skipped_late_boot armed_at 09:17:09` | OpenAlgo booted after 09:15. Per SPEC §5 the day is skipped loudly — the first candle cannot be reconstructed after the fact by the live service. |

Both failures are **infrastructure**, not strategy. Neither day has a captured
tick log (`tick_logs/open15/` jumps 2026-08-07 → 2026-08-13), so the tick stream
the strategy is built on is gone.

## 2. What the reconstruction can and cannot resolve

The entry gate is `cumvol_within_minute(t) ≥ vol_mult × baseline` evaluated **at a
tick**, and the whole point of this deployment (SPEC §2/§4) is measuring what
fraction of the level→close burst a mid-minute entry captures. With only 1-minute
bars from the broker's historical API:

- **Selection, watch list, volume gate — fully reproducible.** The harness drives
  the real `Open15Core` (not a reimplementation) with one synthetic tick per
  minute carrying that minute's close and cumulative volume. Gates, `top_n`,
  `trade_side`, shadowing, the rolling re-rank and the OI filter all execute as
  shipped.
- **Entry PRICE is not.** A one-tick-per-minute feed can only trigger at the
  minute **close** — Round 58's "honest close-entry" variant. The live tick entry
  fires somewhere *inside* that minute, at a better price. Entering at the level
  instead would be the look-ahead artifact R58 already rejected.

So every P&L below is reported as a **band**: the close-entry figure (late end)
and an "early-entry" sensitivity priced at the trigger minute's option **open**
(early end). The true fill sits between them. **The band is wide enough to flip
the sign on both days** — which is the same conclusion SPEC §2 reaches from
first principles: *1-minute bars cannot resolve that fraction, only live ticks can.*

### Day config used

Taken from each session's own `armed` event where one exists, else the
`open15_config` row in force that morning.

| | 2026-08-12 | 2026-08-17 |
| --- | --- | --- |
| universe | 192 (liquidity gate **enforcing**, 19 dropped) | 211 (gate off since 08-14) |
| `trade_side` | `long_only` + shadow shorts | `both` (changed 09:01 that morning) |
| OI filter (#595) | not yet shipped | active, 500 lots |
| vol_mult / top_n / max_trades | 1.5 / 3 / 3 | 1.5 / 3 / 3 |
| slot capital | ₹60,000 | ₹60,000 |
| rolling watch list | on, 30 s, top-3 | on, 30 s, top-3 |
| instrument | `atm_option` | `atm_option` |

## 3. Harness validation against days that DID trade

Replayed 2026-08-13 and 2026-08-14, where a live tick feed and a journal exist.

**Selection reproduces live exactly.** On 08-14 the harness picks
`PAGEIND / WAAREEENER / CUMMINSIND` long and `TMPV / NATIONALUM / TRENT` short —
identical to the live `selection` event, gaps included, *including* the promotion
caused by the OI filter blocking MFSL, JUBLFOOD and CHOLAFIN.

That match depended on one non-obvious detail. A bar's `oi` is stamped at the
**end** of its minute, so the live filter's ~09:16:02 batched quote corresponds to
the **09:15** bar, not the 09:16 one. Measured against 08-14's own log:

| contract | live verdict | 09:15 bar | 09:16 bar |
| --- | --- | --- | --- |
| MFSL 1560CE | 418 lots | **418** | 791 |
| JUBLFOOD 505CE | 346 lots | **346** | 367 |
| CHOLAFIN 1900PE | 482 lots | **482** | 482 |

Reading the 09:16 bar puts MFSL at 791 lots — clear of the 500 floor — and the
whole day's selection diverges. MFSL's OI was ramping hard that morning
(339 lots at the 08-13 close → 1,228 by the 08-14 close), so only the
correctly-stamped bar reproduces the live decision.

**Triggers reproduce partially, as expected.**

| | live triggers | reproduced at the same minute | extra | missed |
| --- | --- | --- | --- | --- |
| 2026-08-13 | 8 | 5 | 1 (CONCOR 09:24) | 3 (UNOMINDA, BDL, DIVISLAB) |
| 2026-08-14 | 2 | 1 (CUMMINSIND 09:21) | 1 (MAXHEALTH vs live HINDZINC) | 1 |

The misses are the expected failure mode: a symbol that pokes past the level
mid-minute and closes back inside triggers live but not on bars. The extras are
the mirror case. Rolling-watch-list differences also follow from cadence — live
re-ranks twice a minute on live LTP, bars support once.

**P&L is same-sign and same order of magnitude, not exact.** CUMMINSIND 08-14:
live entry ₹116.50 → exit ₹106.95, net −₹4,000; replay ₹115.00 → ₹108.40,
net −₹3,085. Strike choice can also drift, because the ATM strike is picked off
the trigger price (ULTRACEMCO 08-13: live 11600PE, replay 11860PE).

**Verdict:** the harness is trustworthy for *what the strategy would have watched
and selected*, and indicative only for *what it would have earned*.

## 4. 2026-08-12 — reconstructed

Seed picks (`long_only`, so the three shorts are shadow-only):
MANAPPURAM L +2.76%, NATIONALUM L +2.71%, MCX L +2.42%,
PIIND S −1.08%, MAXHEALTH S −0.89%, IDFCFIRSTB S −0.78%. 10 rolling additions.

Six triggers, none from a seed long — **both real entries came from the rolling
watch list**:

| trigger | symbol | side | bucket | contract | entry | exit | lots | net (close-entry) | net (early-entry) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 09:27 | PNB | L | real | PNB25AUG26114CE | 4.39 | 4.35 | 1 | −₹679 | +₹2,454 |
| 09:29 | BOSCHLTD | L | real | BOSCHLTD25AUG2645500CE | 1570.25 | 1570.25 | 1 | −₹398 | +₹831 |
| 09:18 | LTF | S | shadow | LTF25AUG26315PE | 10.50 | 8.95 | 2 | −₹7,411 | −₹7,411 |
| 09:23 | ZYDUSLIFE | S | shadow | ZYDUSLIFE25AUG261190PE | 38.85 | 41.55 | 1 | +₹2,059 | +₹5,313 |
| 09:29 | MAXHEALTH | S | shadow | MAXHEALTH25AUG261030PE | 27.15 | 27.15 | 4 | −₹556 | +₹4,083 |
| 09:29 | PIIND | S | shadow_cap | PIIND25AUG262650PE | 99.05 | 99.05 | 3 | −₹511 | +₹3,232 |

**Real bucket: −₹1,077 (close-entry) / +₹3,285 (early-entry), 0 wins of 2.**
Shadow bucket: −₹5,908 / +₹1,985, 1 win of 3. The `max_trades` cap never bound;
`shadow_max_trades=3` dropped a 4th shadow trigger (PIIND), which is reported in
its own `shadow_cap` bucket rather than summed into the shadow total — live would
have journaled nothing for it.

⚠ The three 09:29 triggers are degenerate: a 09:29:59 synthetic trigger fills at
the 09:30 open, which is also the exit, so entry = exit by construction and the
row reduces to charges. Live, a 09:29 trigger would have held ~30–50 seconds.
This is the clearest single case where the bar convention destroys the answer.

## 5. 2026-08-17 — reconstructed

Seed picks (`trade_side=both`, so all six are tradeable):
AMBER L +1.85%, BDL L +1.09%, SUPREMEIND L +0.87%,
BSE S −1.65%, LAURUSLABS S −0.97%, PNBHOUSING S −0.91%. 6 rolling additions.

The OI filter reshaped the day. Blocked at the 500-lot floor:
**PATANJALI** (388 lots), **SONACOMS** (447) and **ALKEM** (384) long;
**COCHINSHIP** (463), **NMDC** (481) and **360ONE** (240) short; **OIL** (280)
long off the rolling list. NMDC and COCHINSHIP would otherwise have been seed
shorts, and OIL would have taken a real slot — so three of the day's six
funded/considered names come from filter promotions.

| trigger | symbol | side | bucket | contract | entry | exit | lots | net (close-entry) | net (early-entry) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 09:17 | AUROPHARMA | L | real | AUROPHARMA25AUG261640CE | 34.00 | 33.90 | 3 | −₹712 | +₹6,247 |
| 09:18 | BSE | S | real | BSE25AUG263300PE | 80.10 | 69.00 | 3 | −₹7,104 | −₹266 |
| 09:20 | HAL | L | real | HAL25AUG265100CE | 125.70 | 114.00 | 3 | −₹5,792 | +₹1,619 |
| 09:25 | MOTILALOFS | L | sim (cap) | MOTILALOFS25AUG26900CE | 27.50 | 27.15 | 2 | −₹968 | +₹1,601 |
| 09:26 | LTM | S | sim (cap) | LTM25AUG264700PE | 137.50 | 142.65 | 2 | +₹1,122 | +₹2,132 |
| 09:26 | PNBHOUSING | S | sim (cap) | PNBHOUSING25AUG261140PE | 22.30 | 22.50 | 4 | −₹47 | +₹1,780 |

**Real bucket: −₹13,608 (close-entry) / +₹7,600 (early-entry), 0 wins of 3.**
Sim bucket (capped by `max_trades=3`): +₹108 / +₹5,513.

The `max_trades` cap bound hard — 3 of 6 triggers were turned away. On the
close-entry convention the capped cohort roughly broke even against a −₹13.6k
funded cohort; on the early-entry convention both are positive and the funded
one leads. That is the `sim` bucket doing its job (SPEC §4a), and it says the
slot budget is worth watching on this day but not that it was clearly the
binding constraint.

⚠ The corrected OI source materially changed this section. Reading OI off the
09:16 bar instead of 09:15 (see §3) let **NMDC** through as a seed short, and its
₹1.67 ATM put — where one ₹0.05 tick is 3% of the premium — priced at +₹17,924,
which would have dominated the day. The live filter would have blocked it at
481 lots. That a single mis-stamped bar produced a five-figure phantom result is
the strongest argument here for treating any reconstruction as indicative only.

## 6. What to conclude

1. **Nothing about the signal.** Both days' real-bucket numbers span zero. Adding
   them to the R58 decision-rule dataset would inject two observations whose
   error bars exceed the ±0.4pp/trade threshold the rule is trying to resolve.
2. **The rolling watch list supplied every real entry on 08-12** and half of
   08-17's, which is consistent with the #529 measurement continuing to look
   worth running — but neither day is evidence for promoting it.
3. **The OI filter (#595) is doing visible work.** On 08-17 it blocked seven
   candidates, four of them clustered at 384–481 lots — just under the floor —
   and two of those (NMDC, COCHINSHIP) would have been seed shorts. Worth
   watching whether that cluster is typical: a floor that repeatedly bites at
   80–96% of its threshold is either well-placed or slightly too high.
4. **The real finding is operational.** Two sessions in six trading days were
   lost to infrastructure — a dead ZMQ feed (08-12) and a late boot (08-17).
   The 08-12 case is exactly the shape the tick-liveness watchdog exists to
   catch; that it armed, watched 192 symbols and received zero ticks without
   the day being salvaged is worth its own issue.

## 7. Reproducing

```bash
uv run python backtest/open15_missed_days/fetch_data.py 2026-08-12,2026-08-17
uv run python backtest/open15_missed_days/replay_day.py --date 2026-08-12
uv run python backtest/open15_missed_days/option_data.py --date 2026-08-12
uv run python backtest/open15_missed_days/replay_day.py --date 2026-08-12
uv run python backtest/open15_missed_days/price_options.py --date 2026-08-12
```

`option_data.py` needs the unfiltered replay first to learn the candidate pool,
so `replay_day.py` runs twice — the second pass consumes the OI verdicts. All
steps need the local app up with a live Zerodha session (they proxy the broker's
historical API). Control days: substitute `2026-08-13` / `2026-08-14`.

**These numbers are deliberately NOT written into `open15_trades`.** That journal
is the experiment (SPEC §4), and its four buckets exist so estimates can never be
summed with fills. A reconstruction whose band flips sign does not belong in any
of them; if it is ever journaled it needs its own `fill='replay'` bucket added to
`NON_REAL_FILLS`, behind an issue and a PR.
