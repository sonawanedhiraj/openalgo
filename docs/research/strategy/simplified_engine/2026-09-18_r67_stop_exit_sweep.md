# R67 — simplified_engine: can a different stop / exit rule improve NET P&L? (full sample, IS/OOS, placebo-guarded)

**Date:** 2026-09-18 · **Operator question:** "Can we try different stops to improve profit?" —
optimising NET P&L, not win rate (the win-rate question was closed by R65).

**Direct answer: no exit rule tested turns this signal profitable, and none gets the payoff ratio
above 1.0 at a hit rate that pays.** Across **106 configurations** over 8 exit dimensions on the
full 345-trade sample, **not one is net-positive out-of-sample** (best OOS −₹3,545) and **not one is
net-positive over the whole sample** (best −₹2,745, and that one is +₹8.7k IS / −₹11.4k OOS). The
reason is in the entry, not the exit: at every fixed target T the probability of reaching T before
the stop is **below the break-even probability 1/(1+T)** — 47% vs 50% at 1R, 34% vs 40% at 1.5R,
25% vs 33% at 2R, 10% vs 25% at 3R. Median day-MFE from entry is 1.18R but median MAE is 1.00R:
half the trades visit the stop, and the favourable excursion is not large enough to pay for them
under ANY exit geometry. The only exits that push payoff above 1.0 (no trail, 3R target) do it by
dropping the win rate to 33–37%, and lose more. **This redirects the work to entries.**

One modest, plateau-shaped, both-halves improvement exists — **widening the stop to 1.75–2.5×ATR**
(OOS −₹7.7k to −₹8.4k vs −₹11.9k base; confirmed on the tick-validated subset) — but it is an
improvement from −₹11.9k to −₹7.7k, i.e. losing less, and its OOS gain sits at the 78th percentile
of what a *random* config gains (81 of 106 configs beat the base OOS because the live 1.5×/0.6R
trail is close to the worst geometry OOS). Not a reason to deploy; recorded as the direction if the
entry is ever fixed.

## 1. Setup

- **Sample:** the 345 closed sandbox trades of R65 (`trade_journal`, `trending_equity_intraday`,
  `chartink`, 2026-06-01 → 09-18, `phantom_cleanup` and 10 open/unpriced rows excluded). Split
  fixed before any sweep: **IS = placed ≤ 08-08 (231 trades, 41 days), OOS = 08-09 → 09-18 (114
  trades, 27 days)**. Actual journal: net **−₹24,187**, WR 53.3%, payoff (mean net win ÷ mean net
  loss) **0.60** (₹288 vs −₹479); IS 0.71 / OOS 0.38.
- **Live parameters verified** (`.env` = `config_snapshot.json` = code defaults; `PARAMETER_LOG`
  has no later change): `atr_period 14`, `atr_sl_mult 1.5`, `min_risk_per_share ₹1`,
  `trail_atr_mult 0.5`, `rr_trail_start_r 0.6`, lock ramp 0.10→0.95 over 0–3R,
  `sl_confirm_seconds 3.0`, `max_risk ₹500`, capital ₹20k × 5, `eod_exit 15:20` — but the
  journal shows every EOD flatten at **15:14** (the watchdog cap, #? 2026-06-10), so the replay
  uses 15:14.
- **Replay engine** (scratchpad `r67_engine.py`): replays each journaled entry (actual fill time
  and price) to EOD under an exit config, mirroring `simplified_stock_engine_core.py` — trail
  applied on every price before the stop check, stop fires on the first print beyond it, then the
  service's 3 s confirmation (`_confirm_and_place_exit`), EOD at 15:14. **Path:** the install's own
  tick logs where they exist (**08-19 → 09-18, 83 of the 114 OOS trades**, repeated identical
  prints dropped), else a 4-point intrabar path per 1m bar (O→L→H→C on up bars, O→H→L→C on down
  bars; stop fills at the level when the path crosses it, at the print when it gaps). Quantity is
  **re-sized per config** exactly as the engine would (`min(₹1L ÷ price, ₹500 ÷ risk)`; the rule
  reproduces the journal quantity exactly on 74.5% and within 10% on 96.8% of trades). Charges on
  every counterfactual: brokerage min(₹20, 0.03%)/leg, STT 0.025% of sell notional, NSE txn
  0.00297%, SEBI, stamp 0.003% buy, 18% GST (matches the journal's `charges_inr` within ~₹1).
- **Trade count is 345 for every config** — entries are fixed; no config trades more.

### Validation — the replay reproduces the live exits (done BEFORE sweeping)

Under the live params with the journal's own quantity:

| subset | n | exit reason match | net-sign agreement | exit price error (median / p90) | exit time error (median) | net actual → replay | corr |
|---|---|---|---|---|---|---|---|
| **tick-path (08-19 →)** | 83 | **97.6%** | **97.6%** | 0.022% / 0.080% | 0.0 min | −11,310 → −8,592 | 0.86 |
| bar-path | 262 | 86.3% | 93.1% | 0.036% / 0.356% | 0.5 min | −12,877 → −10,463 | 0.36 |
| IS | 231 | 87.4% | 93.1% | 0.036% / 0.361% | 0.4 min | −8,868 → −6,684 | 0.34 |
| OOS | 114 | 92.1% | 96.5% | 0.024% / 0.113% | 0.1 min | −15,319 → −12,372 | 0.87 |
| all | 345 | 89.0% | 94.2% | 0.032% / 0.241% | 0.3 min | −24,187 → −19,056 | 0.41 |

On ticks the replay is essentially exact. On the 4-point bar path it is right on 93% of signs but
~₹2k optimistic on IS, with a handful of large single-trade misses where the coarse path missed a
trail exit before a later move (HINDPETRO 06-12, LICI 06-19) — that is what drags the IS
correlation down. **Treat the sweep as relative (same bias applied to every config), with the
OOS half as the trustworthy one; every claim below is cross-checked on the 83 tick trades.**

**Live-code finding while building the replay (not a tunable — flagged for a separate fix):**
`_confirm_and_place_exit` clears the pending stop when `price > pos.stop_loss`. For a LONG that is
the intended "price bounced back above the stop" check; for a **SHORT** (whose stop fires on
`price >= stop`) it is inverted — the short's exit is only placed at a 3 s check where price has
come *back to or below* the stop level, and re-fires each tick until it does. The journal confirms
the effect: short stop-losers exit *better* than the ATR level 63% of the time (median −0.016% of
price; realised −0.91R vs longs' −1.09R), with no runaway case in 176 shorts (the 15:14 watchdog
backstops). Modelled faithfully for validation (`confirm_mode='live'`); the sweep also runs the
intended check (§2.8).

## 2. Sweep results (net ₹, charges included, qty re-sized per config)

Base = live rules under the replay: **IS −7,301 · OOS −11,868 · ALL −19,169** (WR 56.3 / 56.1,
payoff 0.67 / 0.40, maxDD −22,416, 291 stops / 54 EOD). The replay base is ~₹5k kinder than the
journal's −24,187 (validation bias); compare configs to it, not to the journal.

### 2.1 `atr_sl_mult` (stop width) — the one plateau

| mult | IS net | OOS net | ALL | IS / OOS WR | IS / OOS payoff | maxDD | stops → EOD | tick-83 net |
|---|---|---|---|---|---|---|---|---|
| 1.0 | −9,919 | −6,812 | −16,731 | 50.7 / 55.3 | 0.79 / 0.51 | −18,697 | 300 / 45 | |
| 1.25 | −8,978 | −9,808 | −18,786 | 54.1 / 57.0 | 0.70 / 0.41 | −19,589 | 294 / 51 | |
| **1.5 (live)** | −7,301 | −11,868 | −19,169 | 56.3 / 56.1 | 0.67 / 0.40 | −22,416 | 291 / 54 | −8,100 |
| **1.75** | −3,813 | **−7,743** | −11,557 | 57.6 / 59.6 | 0.68 / 0.43 | −16,247 | 284 / 61 | −4,308 |
| **2.0** | −2,854 | **−7,890** | −10,744 | 58.9 / 59.6 | 0.66 / 0.43 | −16,884 | 278 / 67 | −4,139 |
| **2.5** | −370 | −8,376 | −8,746 | 56.7 / 57.0 | 0.76 / 0.45 | −18,219 | 254 / 91 | −4,678 |
| 3.0 | +2,641 | −10,836 | −8,196 | 55.8 / 50.9 | 0.85 / 0.50 | −17,145 | 217 / 128 | |

IS improves monotonically with width (the trail keeps the winners; fewer noise stops), OOS is a
shallow plateau at **1.75–2.5** (+₹3.5–4.1k over base, +₹3.4–4.0k on the tick-validated 83) and
falls back at 3.0 (more EOD holds of losers). This is the R65 "2×ATR" result with a tick-grade
OOS: real, small, and still a loss. Payoff stays 0.43–0.45 OOS.

### 2.2 `rr_trail_start_r` (when the trail arms) — IS/OOS FLIP

| start | IS net | OOS net | IS / OOS WR | IS / OOS payoff |
|---|---|---|---|---|
| 0.4 | −11,120 | −12,594 | 52.0 / 53.5 | 0.69 / 0.36 |
| **0.6 (live)** | −7,301 | −11,868 | 56.3 / 56.1 | 0.67 / 0.40 |
| 0.8 | −4,290 | −12,843 | 51.5 / 50.0 | 0.87 / 0.51 |
| 1.0 | +3,639 | −12,135 | 49.4 / 47.4 | 1.09 / 0.63 |
| 1.5 | +3,090 | −16,528 | 43.3 / 41.2 | 1.37 / 0.67 |
| never | +4,233 | −16,129 | 38.1 / 36.0 | 1.73 / 0.92 |

Arming the trail later (or never) is worth **+₹11.5k IS and −₹4.3k OOS**. Payoff climbs above 1.0
only in-sample and only by giving back the win rate. This is the textbook overfit dimension — see
the placebo in §3, where the IS-best configs are all "wide stop + late trail" and every one loses
more OOS than the base.

### 2.3 `trail_atr_mult` (trail floor) — nothing

0.3: IS −5,990 / OOS −12,069 · **0.5 live** · 0.75: −7,606 / −12,914 · 1.0: −10,272 / −12,050 ·
1.5: −2,195 / −13,768. Flat OOS within ±₹2k; no plateau.

### 2.4 Fixed R-multiple targets — the direct attack on payoff

| target | with live trail: IS / OOS | no trail: IS / OOS | no-trail WR | no-trail payoff | P(target) vs break-even |
|---|---|---|---|---|---|
| 1R | −18,605 / −9,048 | −15,625 / −9,738 | 51.0 | 0.67 | **0.47 < 0.50** |
| 1.5R | −17,240 / −9,469 | −11,156 / −10,571 | 43.8 | 0.98 | **0.34 < 0.40** |
| 2R | −13,940 / −9,439 | −6,019 / −10,398 | 40.9 | 1.20 | **0.25 < 0.33** |
| 3R | −13,306 / −10,786 | −5,589 / −13,110 | 38.6 | 1.30 | **0.10 < 0.25** |
| none (live) | −7,301 / −11,868 | | 56.3 | 0.67 | |

A target caps exactly the trades the trail was riding: every target is worse IS (−₹6k to −₹11k)
and about +₹2k better OOS — a flip, and never positive. The last column is the whole story: at
no level does the signal reach the target before the stop often enough to break even.

### 2.5 Breakeven stop after +0.5R / +1R

+0.5R: IS −9,666 / OOS −11,441 (WR 47 / 44 — it converts trail winners into scratches);
+1R: −7,073 / −12,358 (≈ base, because the live trail already has the stop above entry by 1R).
Nothing.

### 2.6 Time stop (exit after N min if < +0.5R)

15 m: −8,713 / −9,297 (201 time exits, WR 38) · 30 m: −9,011 / −8,353 (126) · 45 m: −7,407 /
−8,870 (85) · 60 m: −8,229 / −8,853 (58). +₹2.5–3.5k OOS, −₹1–2k IS — a flip; the time exits cut
losers a little sooner OOS and cut winners-in-the-making IS.

### 2.7 Asymmetric by direction

Per-side selection from all 106 configs (IS-best → OOS):

| side | base IS / OOS | IS-best config | its IS → OOS | OOS rank of that pick | OOS-best (hindsight) |
|---|---|---|---|---|---|
| LONG | −17,035 / −2,044 | sl 3.0 + trail 1.0R | −8,063 → **−2,015** | 59 / 106 | sl 1.0 + 1.5R target, +2,936 (its IS −18,667) |
| SHORT | +9,734 / −9,825 | sl 1.0 + no trail | +22,601 → **−7,799** | 44 / 106 | sl 1.0 + trail 1.0R, −4,711 (its IS +19,684) |

Letting each side choose its own geometry gains **₹29 OOS on longs and ₹2k on shorts**, and each
side's IS pick ranks mid-pack OOS. The long/short divergence R65 found is a *regime* divergence
(the sell screener's names bounced from August), not a stop-geometry one; no exit setting
recovers the short side's −₹9.8k OOS.

### 2.8 `sl_confirm_seconds`

Only the 83 tick-path trades can distinguish these (bars cannot model 3 s):

| confirm | OOS net | Δ vs live 3 s | note |
|---|---|---|---|
| 0 s | −10,597 | **+1,271** | fill at the first print beyond the stop |
| 3 s live (short check inverted) | −11,868 | — | |
| 3 s as intended | −11,371 | +497 | shorts lose the favourable re-touch fill, longs unchanged |
| 10 s | −11,099 | +770 | |
| 30 s | −11,467 | +401 | |

Confirmation does not help: with a tick feed the first print beyond the stop was the best exit.
The effect is small (≈ ₹15/trade) and the tick sample is 83 trades — not a change worth making
on its own, but there is no evidence for the 3 s either.

## 3. Overfitting guard

- **Configs tested: 106** (35 one-at-a-time + 71 two-dimensional). With 345 trades and 8
  dimensions, the best-of-106 IS number is meaningless on its own — the table below is why.
- **IS-best → OOS.** The IS-best config (`sl 2.0 + trail_start 1.5R`, IS +₹10,488) does **−₹3,752
  worse** than the base OOS. Every one of the ten IS-best configs is "wide stop + late/no trail";
  eight of the ten are OOS-worse than base, the other two are +₹442 and −₹119.
- **Placebo — random date splits.** 500 random 41-day / 27-day partitions of the 68 trading days;
  in each, pick the IS-best of the 106 configs and measure its OOS gain over base. Placebo gain:
  mean +₹900, median +₹1,061, p10 −₹4,958, p90 +₹6,678; the IS-best pick is OOS-positive in 59% of
  splits. **The real split's IS-best lands at the 15th percentile** — selecting on IS does worse
  than a coin flip here.
- **Random config.** Mean OOS gain of an arbitrary config over base is +₹1,860 (81/106 configs beat
  the base OOS; max +₹8,323, and that config is −₹10k worse IS). The base geometry is close to
  the OOS-worst, which is what makes "anything else looks better OOS" — it is not evidence for any
  specific alternative.
- **Plateau test.** `atr_sl_mult` 1.75 / 2.0 / 2.5 are each both-halves-better with neighbours
  that agree (§2.1) — the only dimension with a plateau. Its OOS gain (+₹3,978 at 2.0) is the 78th
  percentile of the random-config distribution. Every other both-halves-better cell (39 of 106)
  is a wide-stop combination whose OOS gain comes from the stop width alone.
- **Bootstrap on the ALL-best** (`sl 2.0 + trail 1.0R`, +₹16.4k over base): 95% CI [+₹437,
  +₹34,614] — but +₹16k of it is IS and the OOS gain is +₹442. A tight CI on an in-sample number.
- **Bias direction.** The bar-path replay is ~₹2k optimistic IS; the OOS numbers ride mostly on
  ticks. Any config's true edge is at least as bad as shown.

## 4. Is there ANY exit rule that gets payoff > 1.0 at an acceptable hit rate?

**No.** Only three of 106 configs reach payoff ≥ 1.0 OOS — `no trail` (1.30 at sl 1.0), `3R
target no trail` (1.03) and `sl 1.25 no trail` (1.06) — and their OOS win rates are **33–37%**,
which is below the ~43–50% those payoffs need; their OOS nets are −₹10.2k / −₹13.1k / −₹13.4k.
Win rate × payoff never clears 1 in either half for any config. The ceiling is set by the
excursion the entry produces:

| | ≥ 0.5R | ≥ 1R | ≥ 1.5R | ≥ 2R | ≥ 3R | median MFE | median MAE |
|---|---|---|---|---|---|---|---|
| IS (231) | 77% | 58% | 43% | 32% | 15% | 1.22R | 0.97R |
| OOS (114) | 78% | 60% | 40% | 27% | 10% | 1.10R | 1.13R |
| LONG / SHORT (≥1R, ≥2R) | | 61% / 57% | | 27% / 33% | | 1.18R / 1.16R | |

(rest-of-day MFE and MAE from entry, in units of the live 1.5×ATR stop.) The favourable and the
adverse excursions are the same size. 59% of trades *touch* +1R at some point, but only 47% reach
it before touching the stop — and that is the number an exit rule has to work with. A 2R target
needs 33% and gets 25%; 3R needs 25% and gets 10%. **The entry signal does not produce moves
large enough, whatever the exit.** Everything in §2 is re-slicing the same excursion: widen the
stop and you keep more of the 1R touches but hold more losers to the close; trail later and you
keep the runners in one regime and give them all back in the next.

## 5. What this means

1. **Stop/exit tuning is exhausted for this signal.** The single defensible change — stop at
   1.75–2×ATR — takes the OOS loss from −₹11.9k to ~−₹7.8k and the whole-sample loss from −₹19.2k
   to ~−₹11k. It does not make the strategy positive and should not be sold as a fix. If the
   engine keeps running as a measurement, 2.0 is the better geometry; if the operator wants a
   profitable book, this is the wrong place to look.
2. **Redirect to entries.** The payoff ceiling (§4) is an entry property. R65's one both-halves
   cell (entries > 0.25% *against* NIFTY's sign, 63% / 70%, placebo 0.7%) and the excluded-side
   measurement are where a real change in P(1R before stop) could come from; R60's "never chase"
   retrace-limit entry is the other documented lever. Both change *which* moves are entered, not
   how they are exited.
3. **Two code findings, no code changed here:** (a) the short-side stop confirmation is inverted
   (§1) — benign in practice but a latent runaway risk with no stop until 15:14; (b) R65's
   restart-rehydration hole for the same-day block / cooldown / cap still stands.

No engine parameter or code was changed. Harness: scratchpad `r67_ticks.py` (tick extraction from
`tick_logs/`, 82 symbol-days), `r67_engine.py` (replay), `r67_validate.py`, `r67_sweep.py`; the
R65 trade frame and broker 1m bar cache were reused; `db/openalgo.db` was read via the R66
sqlite-backup byte copy.
