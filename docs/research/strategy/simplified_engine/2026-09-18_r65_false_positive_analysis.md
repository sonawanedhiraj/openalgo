# R65 — simplified_engine: what separates winners from losers? (false-positive analysis, IS/OOS)

**Date:** 2026-09-18 · **Issue:** #733 · **Question (operator):** how can the false-positive
trades be avoided / how can the win rate be raised — profitability later.

**Verdict: no persisted feature separates winners from losers robustly. The engine's win
rate is ~53% with a 95% CI of 48–59%, and every a-priori filter the LLM prompt and the
operator's intuition point at (lunch hour, power hour, low confidence, Nth trade,
correlated book, repeat-offender symbols, VIX, regime-trend) is either flat or flips sign
between the two halves. Three things did come out: (1) a BUG — the same-day stop-out
block, the 3-candle cooldown and the 6-trades/day cap are in-memory and die on every
intraday restart, and the 45 trades that slipped through ran 42% (fixing it is free);
(2) the one both-halves-positive cell is trades taken AGAINST NIFTY's intraday direction
(63% / 70% win rate, placebo 0.7%) — which is the exact opposite of what the LLM veto is
instructed to do, and the vetoed cohort confirms it; (3) the LLM veto's confidence
number has zero predictive power, the veto itself shows no measurable value (the
unreviewed `review_failed` control group did as well or better), and failing CLOSED on an
LLM error would have LOWERED the win rate.**

Filed as R65: R63/R64 were already assigned to open15 rounds in the registry when this
analysis was commissioned as "R63".

## Data

345 closed sandbox trades from `trade_journal` (`strategy_name='trending_equity_intraday'`,
`signal_source='chartink'`, `mode='sandbox'`), 2026-06-01 → 2026-09-18, excluding 32
`phantom_cleanup*` tombstones and 10 open/unpriced rows (4 `abandoned_eod_watchdog:
broker_flat` from 07-28..30, 4 unpriced from the 09-02 app death — bug #707 — and 2 from
09-07). The brief's baseline of 342 is the same set before 3 trades closed on 09-18.
"Win" = `pnl − charges_inr > 0` (net of the journal's own modelled MIS charges, #579).

Every trade is joined to its `signal_decision` row (379/387 linked), which carries the
LLM decision, confidence, reasoning and the **`context_snapshot`** taken at review time
(`nifty_pct`, `india_vix`, `regime_snapshot`, `positions_count`, `trades_today`,
`pnl_today`). The journal's own `nifty_pct_at_entry` / `regime_snapshot` columns only
exist from 2026-08-11, so all regime work below uses the review-time snapshot (100%
coverage in both halves; on the overlap it matches the journal column to 0.07%).

Broker 1m bars were fetched read-only for all 345 taken symbol-days and 84 vetoed
symbol-days (0 failures) for the stop-geometry and counterfactual work. The live-rule
replica (Wilder ATR14 on 5m candles, 1.5×ATR stop with the ₹1 floor, 500/risk sizing
capped at ₹1L notional, 0.6R trail start with the 0.5×ATR floor, stop on 1m extremes,
EOD 15:19) reproduces the journal's quantity exactly on 96% of risk-bound trades and the
sign of the net outcome on 89% (IS) / 96% (OOS) of trades.

**Split (fixed before any cell was looked at):** IS = placed ≤ 2026-08-08 (n=231, 41 days);
OOS = 2026-08-09 → 09-18 (n=114, 27 days). Every table below is IS | OOS. Buckets under
~30 trades are marked ⚠ and must not be read as evidence.

## Baseline

| | n | win% (net) | 95% CI | net ₹ |
|---|---|---|---|---|
| All | 345 | **53.3** | 48–59 | −24,187 |
| IS | 231 | 53.7 | | −8,868 |
| OOS | 114 | 52.6 | | −15,319 |
| LONG | 169 | 56.8 | | −13,096 |
| SHORT | 176 | 50.0 | | −11,091 |
| IS LONG / SHORT | 116 / 115 | 52.6 / 54.8 | 44–61 / 46–64 | −10,436 / +1,568 |
| OOS LONG / SHORT | 53 / 61 | **66.0 / 41.0** | 53–77 / 30–54 | −2,660 / −12,659 |

The largest single effect in the data is the **LONG/SHORT flip between halves** (SHORT
54.8% → 41.0%) — the R62 finding that the sell screener's top losers bounced while NIFTY
drifted down. No entry filter below repairs it; it is the regime, not a false-positive
class.

## 1. Time of day — the "lunch-hour / power-hour" dampeners are not borne out

Entry hour × half (win%, n):

| hour | IS win% (n) | OOS win% (n) | IS net | OOS net |
|---|---|---|---|---|
| 09 | 50.0 (8) ⚠ | 40.0 (5) ⚠ | −960 | −2,258 |
| 10 | 65.9 (41) | 52.2 (23) ⚠ | +333 | −3,186 |
| 11 | **67.2** (61) | **48.0** (25) ⚠ | −4,839 | −5,852 |
| 12 | **42.2** (45) | **52.2** (23) ⚠ | −1,947 | −2,862 |
| 13 | 51.6 (31) | 64.7 (17) ⚠ | +159 | −672 |
| 14 | **33.3** (33) | **52.6** (19) ⚠ | −1,126 | −492 |
| 15 | 50.0 (12) ⚠ | 50.0 (2) ⚠ | −488 | +4 |

The regime label the LLM sees tells the same story: `lunch` 54.9% (82) → 54.3% (35) —
exactly baseline in both halves; `power_hour` **35.5% (31) → 61.5% (13 ⚠)** — the worst
IS bucket is the best OOS bucket; `mid_morning` 64.1% (64) → 47.2% (36). Every hour that
looks bad in one half looks fine in the other.

| filter | IS Δwin% | OOS Δwin% | dropped IS / OOS | verdict |
|---|---|---|---|---|
| skip 11:00–12:59 (R61's midday cut) | −2.5 | +1.9 | 106 / 48 | noise |
| skip ≥ 14:30 (power hour) | +3.1 | −1.1 | 32 / 13 | noise |
| skip ≥ 14:00 | +3.8 | +0.1 | 45 / 21 | noise |
| skip 09:xx | +0.1 | +0.6 | 8 / 5 | nothing there |
| only 10:00–11:29 | **+10.9** | **−4.1** | 166 / 79 | noise (classic IS fit) |

The LLM reasoning text agrees with the data, not with its own rubric: trades whose
reasoning mentions "lunch" ran 54.5% vs 52.7% for the rest; "power-hour"/"runway"
mentions carry no signal.

## 2. Exits and stop geometry — the stops are not "on noise"

275/345 exits are `stop_loss`, but **152 of them are profitable trail-stops**; the
losing stop-outs are 123 (36% of all trades). The stop sits at median **0.51% (IS) /
0.43% (OOS)** of price; 10.7% of trades are on the ₹1/share floor.

| exit reason | n | win% | mean net | median net |
|---|---|---|---|---|
| stop_loss (incl. trail) | 275 | 55 | −110 | +73 |
| eod_watchdog | 32 | 47 | +99 | −55 |
| recovered_from_sandbox | 29 | 45 | −97 | −66 |
| sandbox_eod_squareoff | 7 ⚠ | 57 | +861 | +2 |

MAE/MFE on 1m bars during the hold, in R (stop distance):

| | n | median MAE | median MFE (hold) | median MFE (rest of day) |
|---|---|---|---|---|
| IS winners | 124 | 0.33 | 1.01 | 2.04 |
| IS losers | 107 | 1.03 | **0.28** | 0.52 |
| OOS winners | 60 | 0.28 | 1.11 | 1.74 |
| OOS losers | 54 | 1.16 | **0.27** | 0.58 |

Winners rarely visit the stop (90th-percentile MAE 0.84R); losers never get going
(median MFE 0.28R, only 23% ever reach +0.5R). Of the 123 losing stop-outs, **35% touch
+1R later in the day, 63% get back to entry, and holding all of them to EOD would have
been gross −₹36,618** (33% finish positive). The stop is doing its job; the entries it is
protecting are the false positives.

Stop-width sensitivity (replica, net of charges, same trail):

| ATR multiple | IS net (win%) | OOS net (win%) |
|---|---|---|
| 1.5 (live) | +1,198 (55.0) | −12,636 (53.5) |
| 2.0 | +10,048 (57.1) | −11,002 (56.1) |
| 2.5 | +7,555 (55.8) | −11,771 (52.6) |
| 3.0 | +9,501 (56.3) | −10,792 (50.9) |

Widening adds ₹9k IS and ~₹1.5k OOS; the OOS win rate is flat-to-down. Not a lever.
"Stops within 10 minutes" (65 trades, net −₹10.9k) look like noise-stops but flip too:
IS LONG 35% (23 ⚠) → OOS LONG 75% (16 ⚠).

## 3. Regime × direction — the only both-halves-positive cell is COUNTER-NIFTY

NIFTY % at review × direction (win%, n; net ₹):

| NIFTY bucket | IS LONG | IS SHORT | OOS LONG | OOS SHORT |
|---|---|---|---|---|
| < −0.75 | 100 (3) ⚠ | 47.6 (21) ⚠ −6.5k | — | 66.7 (3) ⚠ |
| −0.75..−0.25 | 60.0 (10) ⚠ | **63.4 (41)** +2.8k | 64.7 (17) ⚠ | **31.7 (41)** −11.6k |
| −0.25..0.25 | 44.1 (34) −6.1k | 42.9 (28) ⚠ −3.3k | 60.9 (23) ⚠ | 45.5 (11) ⚠ |
| 0.25..0.75 | 54.3 (35) | **66.7 (21)** ⚠ +9.5k | **76.9 (13)** ⚠ | **83.3 (6)** ⚠ |
| > 0.75 | 52.9 (34) | 25.0 (4) ⚠ | — | — |

Read down the SHORT columns: shorts placed while NIFTY is *up* win 67% / 83%; shorts
placed while NIFTY is *down* win 63% IS but 32% OOS (the bounce). Read the LONG columns:
longs on a down tape win 60–65%, longs on a flat tape 44–61%.

| filter (keep) | IS win% (n) | OOS win% (n) | IS net | OOS net | placebo* |
|---|---|---|---|---|---|
| baseline | 53.7 (231) | 52.6 (114) | −8,868 | −15,319 | |
| against NIFTY's sign | 57.9 (57) | 56.4 (39) | +7,686 | −3,578 | 10.2% |
| **strongly against NIFTY (>0.25% opposite)** | **63.2 (38)** | **69.6 (23) ⚠** | **+9,793** | **−216** | **0.7%** |
| with NIFTY's sign | 52.3 (174) | 50.7 (75) | −16,554 | −11,741 | |
| strongly with NIFTY (>0.25%) | 55.7 (131) | **43.9 (57)** | −9,301 | −11,326 | |
| flat tape (\|NIFTY\| ≤ 0.25%) | 44.4 (62) | 55.9 (34) | −9,360 | −3,777 | |

\* share of 5,000 random same-size subsets whose win rate meets or beats the observed
value in BOTH halves simultaneously.

The counter-NIFTY cell is the one thing in this study with a consistent sign, a
mechanism (a stock moving against the tape is an idiosyncratic move, not beta — the
relative-strength reading of R60's "never chase"), a per-trade net of **+₹157 vs −₹119**
for everything else, and independent confirmation from the **vetoed cohort** (§5: vetoed
candidates that were against NIFTY's sign ran 57% / 52% in the replica; aligned ones
40% / 41%). Its weaknesses are equally real: the OOS bucket is 23 trades; by direction
the OOS LONG side reverses (against 61% n=28 vs aligned 72% n=25); by month it wins in
Jul (63 vs 52) and Aug (65 vs 53) and loses in Jun (43 vs 48, n=14) and Sep (38 vs 55,
n=13). **Verdict: PROMISING, not robust** — and note that the LLM veto prompt instructs
the model to skip precisely these trades ("skip when the broader market regime conflicts
with the signal's stated Direction").

Everything else in the regime block is flat or flips:

| cell | IS | OOS | note |
|---|---|---|---|
| VIX > 12.5 at review | 52.1% (140) | 66.7% (6) ⚠ | VIX range 10.9–14.9 all summer — no stress regime in sample |
| regime `trend` aligned with direction | **33.3% (30)** | **65.0% (20) ⚠** | flips; the EMA trend label is a slow daily signal |
| skip trend-against | −2.4 pp | +1.3 pp | nothing |
| `breadth` | always `mixed` | always `mixed` | **the breadth universe is unconfigured** — the LLM is told "mixed breadth" on every trade |
| `volatility` | — | always `medium` | same |
| sector-leader concentration | — | <1: 73.9% (23) ⚠, 1–2: 48.5% (68) | OOS only; untested IS |

**Sector alignment** could only be tested with a hand-built symbol→index map (the
platform persists no sector for a stock; the LLM infers it from its own knowledge). The
snapshot keeps just the 5 largest-|move| sector indices, so the stock's sector return is
unknown for ~50% of trades. Where it is known, "sector against the trade" occurred **6
times** in 345 trades (the veto already removes them) and "SHORT in a top-3 leading
sector" 8 times — no testable signal. Stock in a leading sector, LONG: 55% / 69% vs 51% /
63% not-leading — same direction both halves but +4/+6 pp on n=49/29, inside noise.

## 4. LLM confidence — zero predictive power (slightly inverted in-sample)

276 taken trades carry a `take` with a confidence float (mean 0.618, IQR 0.60–0.66;
146 of 276 sit in 0.60–0.64 — it is a rubric constant, not a forecast).

| confidence | n | win% | net | mean net |
|---|---|---|---|---|
| < 0.60 | 40 | **60.0** | −3,717 | −93 |
| 0.60–0.64 | 146 | 51.4 | −10,447 | −72 |
| 0.65–0.69 | 36 | 58.3 | −2,272 | −63 |
| ≥ 0.70 | 46 | 52.2 | −6,661 | **−145** |

Point-biserial r(confidence, win) = **0.03 (p=0.63)**; Spearman r(confidence, net) =
−0.004. By quartile within each half: IS q1 (conf 0.49) 52% → q4 (0.73) **49%** and the
worst net (−₹8.6k); OOS q1 58% → q4 57%. Mean confidence on winners 0.622, on losers
0.614. **The number the model attaches to its decision does not know anything the
decision itself doesn't.** A confidence ≥ 0.65 gate would keep 65 IS / 17 OOS trades at
53.8% / 58.8% — dropping 79% of trades for +0.2 / +6.2 pp on a 17-trade OOS bucket.
Reject as a gate.

## 5. The `review_failed` control group — and what the veto is worth

`review_failed` = the reviewer raised and the engine failed OPEN (`(True, decision_id)`
with `decision='review_failed'`). **60 closed trades** (not ~107 — the 107
`signal_decision` rows include re-fires of the same candidate on later candles; 60 of
them became journaled, closed trades) across 18 days from 06-19 to 09-09. The
September cluster (13 trades, 09-02..09-09) was **not** a logged-out CLI: the log carries
the reviewer's stderr verbatim — `claude review exited 1: You've hit your session limit ·
resets 2pm (Asia/Kolkata)` — i.e. the Claude subscription's usage cap, hit around midday,
resetting at 14:00. June/July failures pre-date the #534 logged-out-CLI fixes and their
stderr was not captured.

| cohort | n | win% | mean net | net |
|---|---|---|---|---|
| LLM `take` (reviewed, approved) | 276 | 52.9 | −90 | −24,880 |
| `review_failed` (never reviewed) | 60 | **58.3** | −88 | −5,302 |
| same-days-only control: IS take / review_failed | 55 / 25 ⚠ | 47.3 / **64.0** | | |
| same-days-only control: OOS take / review_failed | 9 ⚠ / 6 ⚠ | 33.3 / 66.7 | | |

Bootstrap 95% CI on (review_failed − take) win rate: **−8 to +19 pp**; Fisher p = 0.48.
The unreviewed trades did at least as well as the reviewed ones. The same-day control
(days on which both cohorts traded) leans the same way in both halves but on 25 and 6
trades.

**The vetoed cohort, replayed.** Since 2026-07-01 the veto has been *active* for this
engine (`strategy_llm_config` = `veto`). 84 distinct (symbol, day, direction) candidates
were skipped; replayed through the live-rule replica from the 1m close at the veto
timestamp:

| cohort (replica, net of charges) | n | win% | net | per trade |
|---|---|---|---|---|
| vetoed | 84 | **47.6** | −6,466 | −77 |
| taken `take` ≥ 07-01, same replica | 244 | 53.7 | −14,586 | −60 |
| vetoed, IS / OOS | 36 / 48 | 50.0 / 45.8 | −1,753 / −4,713 | |
| vetoed & against NIFTY's sign | 42 | **54.8** | +66 | +2 |
| vetoed & with NIFTY's sign | 42 | 40.5 | −6,531 | −155 |

Fisher p = 0.38 on 47.6% vs 53.7%. The veto removes 26% of candidates at a win rate
6 pp below the taken set — a difference the sample cannot distinguish from zero — and
the half of the removed trades it is *instructed* to remove (regime-conflict) is the half
that would have run at baseline. Whatever value the veto has is in the *aligned* skips
(trade-budget, "already 5 of 6", sector-specific reasons), not in the regime rule.

**Fail-closed assessment.** Treating `review_failed` as `skip`:

| | IS | OOS |
|---|---|---|
| trades dropped | 44 (19%) | 16 (14%) |
| win% of the dropped trades | **61.4** | 50.0 |
| win% base → fail-closed | 53.7 → **51.9** (−1.8) | 52.6 → 53.1 (+0.5) |
| net avoided | −₹3,381 | −₹1,921 |

Failing closed drops 60 trades (17% of the sample) and **lowers** the win rate in-sample
while leaving it flat out-of-sample; the ₹5.3k it avoids is the same −₹88/trade that the
approved trades lose. On the observed data there is no case for it. (Fail-open remains
the right *engineering* default for a different reason — a dead reviewer must never
silently switch the strategy off — but that is an availability argument, not an edge one.)

## 6. Symbols — no repeat offenders that survive walk-forward

135 distinct symbols; 32 traded ≥ 4 times (155 trades). The names the brief flagged all
have ≥ 50% win rates (VOLTAS 4/6, MANAPPURAM 3/5, COALINDIA n=3, VBL n=3); the worst
cumulative names (PERSISTENT −₹2.6k, TECHM −₹2.5k, 360ONE 2/8, KPITTECH 1/5) are
IS-only clusters from the June/July IT-short days. Walk-forward rules that can be
implemented without hindsight:

| rule | IS Δwin% (dropped) | OOS Δwin% (dropped) | verdict |
|---|---|---|---|
| skip symbol after 2 net losses (decay 1 per win) | +0.2 (10) | +1.4 (14 ⚠) | noise |
| cap 3 trades per symbol lifetime | +0.1 (17) | −1.2 (42) | noise |

## 7. Nth trade / book state — no decay, no correlated-book effect

Win% by trade-of-the-day (journal order): IS 57.5 / 52.6 / 58.3 / 67.6 / 40.0 / 47.2
(n=40..53); OOS 57.7 / 52.2 / 44.4 / 50.0 / 53.8 / 55.6 (n=13..26). No monotone decay in
either half.

| filter (keep) | IS Δwin% (dropped) | OOS Δwin% (dropped) | verdict |
|---|---|---|---|
| first 3 trades of the day | +2.5 (117) | −0.4 (47) | noise |
| first 2 trades | +1.4 (153) | +2.5 (65) | noise, drops 60% |
| skip when `pnl_today` < 0 at review | −0.9 (87) | −1.3 (38) | no |
| skip when `pnl_today` < −1,000 | −1.4 (34) | −0.2 (32) | no |
| skip when ≥ 2 positions already open | −0.5 (73) | **+5.9** (32) | flips in sign of the dropped set (54.8% → 37.5%) |
| skip 3rd+ concurrent same-direction position | +0.9 (57) | **+5.4** (14 ⚠) | 14-trade OOS bucket at 14% — untrustworthy |
| skip after 2 closed losses today | +1.6 (43) | −1.1 (15 ⚠) | noise |

The "third correlated short on a losing day" intuition: 9 reasoning texts contain
"third" (3/9 won), 14 contain "correlated" (6/14) — too few to score.

## 8. Same-day re-entry after a stop-out — the block is DEFEATED BY RESTARTS (bug)

`same_day_stopout_block` defaults on (`RISK_SAME_DAY_STOPOUT_BLOCK` unset → `True`,
`services/simplified_stock_engine_service.py:110`) and shipped 2026-05-29
(`7a9b8be66`), yet the journal holds **16 re-entries after a same-day stop on the same
symbol** (14 symbol-days with 2–3 entries: COCHINSHIP 3× on 07-07, OBEROIRLTY 3× on
08-17, COALINDIA 2× on 09-09 …). They ran **43.8%, net −₹3,568**.

Root cause, traced on 2026-09-09: COALINDIA stopped at 11:42:26 (the cooldown log fires
at 11:48, so `_same_day_blocked_until` was set); at **11:52:16 the app restarted**
(`[SIMPLIFIED-REHYDRATE] Restored 1 position(s) from trade_journal`); at 12:08:56 the
veto approved COALINDIA again and it re-entered with **no `[SIMPLIFIED-SAME-DAY-BLOCK]`
line** — while HDFCLIFE and ADANIENT, stopped *after* the restart, were blocked
correctly (459 block lines). `rehydrate_positions_from_journal` restores open positions
only; `_same_day_blocked_until`, `_sl_cooldown` **and `trades_today`** are rebuilt
empty. The same hole explains the **38 trades placed beyond the 6/day cap** on 9 days
(13 trades on 06-29, 07-06 and 07-07; 11 on 08-17; 8 on 09-09) — they ran 44.7%.

| guard-bypass cohort | n | win% | net |
|---|---|---|---|
| re-entry after same-day stop | 16 | 43.8 | −3,568 |
| trade #7+ of the day | 38 | 44.7 | −537 |
| union | 45 (36 IS / 9 OOS) | **42.2** | −4,170 |
| with the guards enforced: IS / OOS | 195 / 105 | **55.9 / 53.3** (+2.2 / +0.7 pp) | −6,393 / −13,624 |

This is a correctness bug, not a filter: the operator already decided these trades
should not exist. Per `docs/SYSTEM_MAP.md` the fno-scan-cycle task can restart OpenAlgo
mid-session, so intraday restarts are routine, and the retained logs show two more
(09-07 12:34, 09-09 11:52). Fix shape (not done here — analysis only): rehydrate the
day's `stop_loss` exits and entry count from `trade_journal` alongside the open
positions, so the block/cooldown/cap survive a restart (the #624 "rebuild state, never
re-decide" rule).

## Ranked shortlist

| # | rule | IS Δwin% (n kept) | OOS Δwin% (n kept) | dropped | verdict |
|---|---|---|---|---|---|
| 1 | **Persist the same-day stop block, cooldown and daily cap across restarts** (rehydrate from the journal) | +2.2 (195) | +0.7 (105) | 45 (13%) | **ROBUST** — it is a bug fix; the trades are ones the operator already excluded, and they ran 42% |
| 2 | **Remove the "skip when regime conflicts with direction" instruction from the veto prompt** (keep the budget/sector skips) | vetoed-against ran 54.8% vs vetoed-aligned 40.5% in replica (n=42/42) | | adds ~17 candidates/month | **PROMISING** — the rule vetoes the best-performing cell in the taken set; confirmed on the vetoed cohort, but n=42 |
| 3 | Counter-NIFTY gate: enter only when NIFTY is > 0.25% *against* the trade direction at signal time | +9.5 (38) | +17.0 (23 ⚠) | 82% of trades | **PROMISING / untrustworthy sample** — placebo 0.7% and the vetoed cohort agrees, but OOS n=23, the LONG side reverses OOS and 2 of 4 months reverse. Journal it as a **label** (`counter_nifty=true`) and pre-register: at ≥ 60 new labelled trades, gate only if both sides and both months clear baseline |
| 4 | Fail CLOSED on LLM error | −1.8 (187) | +0.5 (98) | 60 (17%) | **REJECT** — lowers WR IS, flat OOS; the dropped trades ran 58% |
| 5 | Confidence ≥ 0.65 gate | +0.2 (65) | +6.2 (17 ⚠) | 79% | **REJECT** — r = 0.03; top quartile is the worst IS bucket |
| 6 | Skip lunch / skip power hour / skip midday | −2.5..+3.1 | −1.1..+1.9 | 13–106 | **REJECT** — every hour flips between halves |
| 7 | Widen the stop to 2×ATR | +2.1 | +2.6 | 0 | **REJECT for WR** — OOS net −₹11.0k; 3× is 50.9% OOS |
| 8 | Skip 3rd+ concurrent same-direction / ≥ 2 open positions | +0.9 / −0.5 | +5.4 / +5.9 | 14–73 | **NOISE** — the OOS buckets are 14 and 32 trades and the IS effect is absent |
| 9 | First-N-trades, stop-after-loss, symbol blacklists, VIX, trend label | ±2 | ±2 | | **NOISE** |

**What raises the win rate, honestly:** #1 (mechanical, +1–2 pp), and possibly #2/#3 if
the counter-tape effect is real — that needs ~2 more months of labelled trades to know.
Nothing in the persisted feature set separates a false positive from a true one at
signal time; the 53% win rate with its 48–59% CI is the signal's own property (R56/R62).
The LONG-vs-SHORT flip between halves is bigger than every filter here combined.

## Instrumentation gaps that limited this study (recommendations, no code changed)

1. **Persist the stop and ATR on the journal row** (`stop_loss`, `risk_per_share`,
   `atr_at_entry`) — recomputed here from broker bars; the live values are only in the
   text log.
2. **Persist the stock's sector** (symbol → index) — the LLM infers it; nothing in the DB
   can test sector alignment.
3. **Configure the breadth universe** (`_classify_breadth` returns `mixed` on an empty
   universe) — the LLM has been told "mixed breadth" on 345/345 trades; `volatility` is
   likewise constant.
4. **Capture the reviewer's stderr on every `review_failed`** (only post-#534 rows have
   it) so a session-limit hit vs a logout vs a timeout is distinguishable in the DB.
5. **Journal a `restart_rehydrated` marker** on positions restored after a restart —
   the guard-bypass cohort had to be inferred from trade order.

Harness: scratchpad scripts (`build.py`, `analyze.py`, `sim.py`, `regime2.py`) on a
byte-copy of `db/openalgo.db`; broker 1m bars via `services.history_service.
get_history_with_auth` using `backtest.run_backtest.resolve_broker_auth` (read-only).
No engine parameter, config or code was changed.
