# R62 — simplified_engine: out-of-sample check of R61 + full improvement search on the sandbox journal

**Date:** 2026-09-07 · **Issue:** #706 · **Verdict: REJECT — no variant of this strategy
is positive out-of-sample. R61's two recommended fixes fail on the 82 trades taken
after R61's window. The signal has no intraday edge; nothing tested rescues it.**

## Data

313 closed sandbox trades (`trade_journal`, `strategy_name='trending_equity_intraday'`),
59 trading days, 2026-06-01 → 2026-09-04, replayed against **broker 1m bars fetched
live from Zerodha** for all 371 symbol-days (0 fetch failures; `historify.duckdb` was
OS-locked by the running app). Excluded: 32 `phantom_cleanup*` tombstones, 4 rows from
2026-09-02 with no exit (app died 12:27 — bug #707), today's open row.

The journal now carries `charges_inr` (bug #579 fixed); my charge model reproduces it
to ₹1/trade (corr 0.9999). Entry fills are the journal's actual fills; entry slippage
vs `ltp_at_signal` is **₹1.6/trade** (negligible), so only the exit is counterfactual.

**Split:** IS = R61's window (≤ 2026-08-07, 231 trades, 41 days). OOS = everything after
(2026-08-08 → 09-04, 82 trades, 18 days). R61's recommendations were never shipped, so the
OOS trades are the live rule unchanged — a clean hold-out.

## Finding 1 — the live rule, net of charges

| | n | days | gross | charges | **net** | green days |
|---|---|---|---|---|---|---|
| IS | 231 | 41 | +₹8,740 | ₹17,608 | **−₹8,868** | 11/41 |
| OOS | 82 | 18 | −₹4,550 | ₹6,513 | **−₹11,063** | 6/18 |
| **All** | 313 | 59 | +₹4,189 | ₹24,120 | **−₹19,931** | 17/59 |

Charges are ₹77/trade, 576% of gross. 250/313 exits are `stop_loss`; **59 stops fire
within 10 minutes of entry for −₹10,644** — the ATR stop (median 0.73% of price, ₹1/share
floor on low-priced names) sits inside the breakout candle's own noise.

## Finding 2 — R61's fixes fail out-of-sample

| variant | IS net | OOS net |
|---|---|---|
| live rule (actual) | −₹8,868 | −₹11,063 |
| hold-to-EOD (R61 fix a) | **+₹11,385** | **−₹15,285** |
| skip 11:00–13:00, live exit (R61 fix b) | −₹2,081 | −₹6,475 |
| skip 11:00–13:00 + hold-to-EOD (R61 combined) | **+₹14,152** | **−₹9,925** |

Hold-to-EOD is *worse* than the live rule OOS; the midday cut removes trades but the
remainder still loses. R61 was an in-sample fit dressed as arithmetic: the "cost line"
argument only works if the non-midday edge is stable, and it was not (+0.17%/trade IS →
−0.20%/trade OOS).

## Finding 3 — the exit simulator: nothing is positive OOS

1m replay from each actual fill, stop checked on bar extremes (conservative), trail
updated on closes, EOD at 15:19. The replica of the live rule reproduces the sign and
rough magnitude (IS −₹2,977 vs actual −₹8,868; OOS −₹9,932 vs −₹11,063 — the residual is
tick-level stop hits inside 1m bars). Net of charges:

| variant | IS | OOS | OOS LONG | OOS SHORT |
|---|---|---|---|---|
| live replica | −2,977 | −9,932 | −1,537 | −8,395 |
| hold to EOD | +12,465 | −15,285 | −1,043 | −14,243 |
| fixed stop 1.0R, no trail | +7,447 | −13,891 | −3,630 | −10,261 |
| fixed stop 2R, no trail | +7,306 | −17,267 | −3,100 | −14,166 |
| fixed stop 3R, no trail | +12,539 | −18,083 | −2,277 | −15,807 |
| stop 1.0% / 1.5% / 2.0% of price | +3,350 / −168 / +11,276 | −16,355 / −18,251 / −17,823 | | |
| stop 2R + ATR trail (1×ATR after 1R) | +7,116 | −11,396 | −4 | −11,392 |
| stop 2R + ATR trail (2×ATR after 2R) | +12,699 | −12,950 | −1,093 | −11,856 |
| stop 2R + breakeven after 1R | +9,355 | −15,151 | | |
| stop 2R + 60-min time stop | +2,956 | −12,718 | | |
| live trail start 1.5R / trail 1.0 ATR / stop 2R | +8,915 / −6,506 / −2,021 | −13,275 / −11,701 / −11,373 | | |
| R60 retrace-limit entry (hit −0.5 ATR, 60 min) + stop 2R | −10,236 (167 fills) | −11,426 (57) | −638 | −10,788 |
| R60 retrace-limit entry (hit −1.0 ATR) + stop 2R | −3,929 (108) | −8,528 (39) | +2,202 | −10,730 |
| retrace −1.0 ATR + hold to EOD | −3,707 | −6,003 | +3,507 | −9,510 |

Every exit rule that looks good in IS is deeply negative in OOS. Retrace-limit entries
(the R60 "never chase" mechanic) reduce fills and losses but never turn positive. **The
IS/OOS reversal on every cell is the signature of a signal with no edge, not of a
mis-tuned exit.**

## Finding 4 — side, regime, timing, gates: all fail split-half

A-priori cells (not searched), live exit unless stated, IS → OOS net:

| cell | IS | OOS | both + ? |
|---|---|---|---|
| LONG only | −10,436 | −1,559 | no |
| SHORT only | +1,568 | −9,504 | no |
| skip 09:xx entries | −7,908 | −8,805 | no |
| first trade of day | +2,966 | −137 | no |
| first 3 trades / day | −3,129 | −5,990 | no |
| no prior loss today (stop after first loss) | +5,171 | −3,379 | no |
| against NIFTY's intraday sign at entry | −757 | −76 | no |
| with NIFTY's intraday sign at entry | −8,112 | −10,987 | no |
| with the 20-DMA trend | +4,280 | −7,121 | no |
| with the 5-DMA trend | −650 | −8,419 | no |
| relative move vs NIFTY > 1% in signal direction | −3,292 | −8,210 | no |
| notional cap-bound (₹1L) / risk-bound | −5,330 / −3,538 | −7,886 / −3,177 | no |
| ₹1/share risk-floor trades (qty 500) | −456 | −1,543 | no |
| LLM veto `take` (shadow) | −11,483 | −9,717 | no (the 9 IS `skip`s were +₹5,995) |

**The SHORT reversal is not beta.** IS SHORT +₹1,568 live / +₹19,487 hold-to-EOD;
OOS SHORT −₹9,504 / −₹14,243 — during a NIFTY drift *down* (Aug 17 → Sep 4 weekly −0.46%,
−0.31%, −1.15%). The sell screener's "top losers" bounced while the index sank: OOS
SHORT alpha vs NIFTY is **−0.17%/trade** (IS +0.31%). LONG alpha is +0.09% in both halves
— real but below the 0.088% cost line, exactly R61's unit-economics wall. Hedging the
stock leg with a NIFTY-ETF leg doubles the charges and nets −₹3.9k IS / −₹15.2k OOS.

Hindsight confirms what the trade is: aligned with NIFTY's **rest-of-day** direction the
book is +₹30k at EOD, against it −₹34k. It is a market-direction bet placed without a
market-direction signal.

## Finding 5 — overnight hold: the only both-halves-positive cell, and it is fragile

Reconfirms R56 (BUY signals carry overnight drift; SELL is anti-predictive overnight) —
LONG fill → T+1, net of CNC delivery charges (0.1% STT each side, DP ₹15.34):

| exit | IS (116) | OOS (41) | win% OOS | median net OOS |
|---|---|---|---|---|
| T+1 **opening print** | +₹7,164 | **+₹7,262** | 53.7 | +₹84 |
| T+1 09:16 close | +₹6,500 | **−₹3,709** | 34.1 | −₹338 |
| T+1 09:20 close | +₹11,816 | −₹620 | 39.0 | −₹195 |
| T+1 09:30 close | +₹7,688 | −₹6,866 | | |
| T+1 15:10 close | −₹9,158 | −₹7,442 | | |

The overnight gap is real (close → T+1 open +0.31% IS / +0.38% OOS, 61% / 71% up) but on
the OOS names it is **sold in the first minute** (open → 09:16 = −0.35%). Capturing it
needs a pre-open (09:00–09:07) sell that fills at the equilibrium print; **7 trades with
gaps > 2% (4.5% of the sample) carry 111% of the total** (₹16,058 of ₹14,426), and 5
bps/leg of slippage turns OOS negative. Placebo: 9.6% of random same-size subsets of all
313 signals match or beat the LONG total. **Not deployable; a hypothesis for a CNC
measurement at most** — and it is already the `sector_follow_cap5_vol` family's thesis
("the overnight hold IS the edge"), on a worse signal.

## Finding 6 — housekeeping the replay surfaced

- **Config snapshot was stale.** `config_snapshot.json` (2026-08-08) said trail 0.8 /
  rr-trail-start 1.5R / profit lock off / sl_confirm 0; the engine runs 0.5 / 0.6R / on /
  3.0s (`.env` + dataclass defaults). Re-synced in this commit. The 0.6R trail start
  (₹300 of profit) with a 0.5×ATR floor is why winners are ~₹120–₹260 scalps.
- **Four unpriced rows on 2026-09-02** (app died 12:27; no square-off order ever reached
  the sandbox; positions vanished from `sandbox_positions` without a fill). Priced at the
  15:19 close they are gross −₹3,499, so the published −₹280 for that day is really
  ≈ −₹4,100. Bug #707.
- Log retention keeps ~2 weeks of text logs; pytest runs write `[SIMPLIFIED-ENTRY]`
  RELIANCE/INFY lines into the live log (2026-08-25 19:38) — filter by symbol before
  scraping. Entry-filter rejections across 12 days: 30,722 `low_volume`, 467
  `small_range_vs_atr`; the 3-loss daily circuit breaker blocked 9 entries on 08-27.
- `strategies/simplified_engine/LEARNINGS.md` carries ~1,700 uncommitted lines of daily
  session notes (July 24 → Aug 31) in the working tree; they are not part of this commit.

## Recommendation

1. **Do not tune this strategy further.** R56 (1,177 replayed trades, 0/14 green months),
   R61 and now R62 (313 real sandbox trades, IS/OOS) agree: the Chartink breakout → 5m
   chase carries no intraday edge; every improvement found in one window dies in the
   next. Pause it, or keep it running only as an explicitly labelled measurement with the
   ~₹77/trade charge bill acknowledged (≈ −₹340/day at the current rate).
2. **Retire R61's hold-to-EOD / skip-midday recommendation** (issue #578 should not ship
   those; it stays useful only for the gross-vs-net labelling fix #579, which landed).
3. If anything is pursued, it is the **LONG-only overnight hold sold in the pre-open
   session**, as a sandbox-only CNC measurement with the gap-concentration and slippage
   caveats above, and it should be run inside the existing `sector_follow_cap5_vol` frame
   rather than as a new engine mode.
4. Process rule (added to the registry): **any claim on this strategy is reported IS/OOS by
   date, net of charges, with a placebo** — R61's "arithmetic, not curve-fit" framing did
   not survive one month of new trades.

Harness: scratchpad scripts (`build.py`, `sim.py`, `regime.py`, `rs_slip.py`, `t1_1m.py`),
broker 1m bars fetched via `services.history_service.get_history` using
`backtest.run_backtest.resolve_broker_auth`.
