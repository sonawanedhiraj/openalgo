# open15_vol_breakout — Round 63: what the winners have in common, and an A/B/C trade rating

**Date:** 2026-09-14 (Sun) · **Issue:** #725 · **Harness:** `backtest/open15_rating/` (read-only on the live DB, `mode=ro`)

**Data.** 48 real closed fills 2026-08-06 → 2026-09-11 (33 long / 15 short, all ATM options, net of
modelled charges — the #552 convention). Plus a 44-row *holdout* of every other triggered row with tick
coverage (shadow / sim / paper / rejected-unfillable / July stock-mode), scored on the **stock's move from
the trigger tick to 09:30** so the option pricing is not in the way. Tick features come from the per-second
universe capture in `tick_logs/open15/` (207 symbols/day, 09:14:58 → 09:30:05; 94 of 102 triggered rows
have coverage — the 8 without are pre-capture July rows). News comes from `market_intel(kind='news')`
(ET Markets + LiveMint RSS, ~220 items/day), matched on symbol / company-name keywords in the window
T-3 15:00 → T 09:35.

**Read this first.** n=48 real, 20 trading days, and trades on one day are correlated (08-28's three IT
longs all won on one piece of news; 09-11's two longs both lost in a 1% market crash). Every number below
is in-sample unless the holdout or the time split says otherwise. The strategy's own P&L is
**+₹66.5k on the first 24 fills and −₹26.7k on the last 24** — the September half is not the August half.

---

## 1. Winners vs losers — feature medians (real fills)

Everything below is knowable **at the trigger** unless marked *post*.

| Feature (median) | Long win (16) | Long lose (17) | Short win (5) | Short lose (10) |
|---|---:|---:|---:|---:|
| Gap % (side-signed for shorts: negative = gap-down) | +0.94 | +0.49 | −0.37 | −0.90 |
| 09:15 level vs prev close % (signed with side) | 1.73 | 1.15 | 1.23 | 1.19 |
| Minutes after 09:15 at trigger | 5.5 | 7.0 | 3.9 | 7.2 |
| Volume ratio at trigger (gate = 1.5×) | 1.53 | 1.60 | 1.51 | 1.64 |
| Tick intensity (ticks in trigger minute ÷ prior-minute avg) | 1.03 | 1.31 | 1.29 | 1.07 |
| Seconds since price first crossed the level | 115 | 283 | 129 | 210 |
| Fraction of pre-trigger ticks already beyond the level | 0.48 | 0.78 | 0.58 | 0.63 |
| How far beyond the level at trigger % | 0.28 | 0.83 | 0.57 | 0.36 |
| Move from 09:15 close to trigger % (signed) | 0.67 | 0.98 | 0.68 | 0.47 |
| Universe median return 09:15 → trigger % | −0.11 | −0.20 | −0.11 | −0.16 |
| 09:15 candle close position in its range (0 = low) | 0.71 | 0.80 | 0.09 | 0.18 |
| Option spread % of mid | 0.92 | 1.01 | 1.79 | 1.70 |
| News hits (T-3 → T 09:35) | 0 | 1 | 1 | 0 |
| *post* Stock move +60 s after trigger % | +0.14 | −0.05 | +0.09 | 0.00 |
| *post* Stock move +180 s % | +0.38 | −0.26 | +0.18 | −0.22 |
| *post* MFE / MAE to 09:30 % | +0.85 / −0.15 | +0.12 / −0.60 | +0.78 / −0.10 | +0.10 / −0.44 |

The picture is the same on both sides: **winners trigger early, on a clean crossing of the volume gate,
shortly after price first broke the level, in a market that is not falling apart**. Losers are late,
stale (price has sat beyond the level for minutes waiting for volume to catch up), blow through the gate,
or fire into a broad-weak tape. Post-entry, winners move at once — +0.14 % in the first 60 s — and never
retrace much (MAE −0.15 %); losers show no follow-through and bleed.

## 2. Binned win rates (real, net option P&L)

### Longs (n=33)

| Feature | Bin | n | WR | Net ₹ |
|---|---|---:|---:|---:|
| Trigger min after 09:15 | < 3 | 4 | 100 % | +23,507 |
| | 3–5 | 6 | 33 % | −5,616 |
| | 5–7 | 14 | 57 % | +10,247 |
| | 7–9 | 3 | 33 % | −6,958 |
| | ≥ 9 | 6 | 17 % | −15,462 |
| Volume ratio at trigger | < 1.55 | 18 | 61 % | +26,679 |
| | 1.55–1.70 | 10 | 20 % | −20,288 |
| | ≥ 1.70 | 5 | 60 % | −674 |
| Universe median 09:15→trigger | < −0.3 % | 8 | 12 % | −11,833 |
| | ≥ −0.3 % | 25 | 60 % | +17,550 |
| 09:15 high vs prev close | < 1.0 % | 8 | 0 % | −23,220 |
| | 1.0–2.0 % | 18 | 67 % | +29,249 |
| | ≥ 2.0 % | 7 | 57 % | −312 |
| Gap | < 0.5 % | 12 | 25 % | −9,835 |
| | 0.5–1.5 % | 18 | 67 % | +17,221 |
| | ≥ 1.5 % | 3 | 33 % | −1,668 |
| Seconds since first level cross | < 120 | 11 | 82 % | +38,923 |
| | ≥ 120 | 22 | 36 % | −33,206 |
| Fraction of pre-trigger ticks beyond level | < 0.7 | 17 | 65 % | +27,120 |
| | ≥ 0.7 | 16 | 31 % | −21,402 |
| Option spread | < 2 % | 25 | 56 % | +16,400 |
| | ≥ 2 % | 6 | 17 % | −16,017 |
| *post* +60 s move | > +0.1 % | 13 | 77 % | +40,920 |
| | 0 … +0.1 % | 4 | 50 % | −8,248 |
| | ≤ 0 | 16 | 25 % | −26,955 |

### Shorts (n=15 — treat as hypotheses)

| Feature | Bin | n | WR | Net ₹ |
|---|---|---:|---:|---:|
| Trigger min after 09:15 | ≤ 7 | 10 | 50 % | +50,710 |
| | > 7 | 5 | 0 % | −16,612 |
| Volume ratio at trigger | < 1.55 | 7 | 57 % | +58,658 |
| | ≥ 1.55 | 8 | 12 % | −24,561 |
| Universe median 09:15→trigger | < −0.3 % | 3 | 0 % | −11,916 |
| | ≥ −0.3 % | 12 | 42 % | +46,013 |
| Gap | > −0.5 % (mild) | 6 | 50 % | +39,893 |
| | ≤ −0.5 % | 9 | 22 % | −5,796 |
| Move 09:15 close → trigger | 0.6–1.0 % | 5 | 60 % | +54,930 |
| | other | 10 | 20 % | −20,834 |
| 09:15 close in lower third | yes | 13 | 38 % | +37,406 |
| | no | 2 | 0 % | −3,309 |
| *post* +60 s move | > +0.1 % | 3 | 67 % | +30,585 |
| | ≤ +0.1 % | 12 | 25 % | +3,512 |

Confirms the 2026-09-10 review (LEARNINGS): the top-3 seed gap-downers lose, mild gaps win; volume far
past the gate loses; late triggers lose. The 09:15-candle-closes-low rule is already true of 13 of 15
shorts, so it has no power inside the traded set.

## 3. What the ticks add that the journal cannot see

1. **Freshness of the break.** The journal knows *when* the trigger fired, not how long price had already
   been beyond the level. `secs_since_first_cross` and `frac_beyond_level_pre` are the two strongest
   in-sample long discriminators (82 % vs 36 %; 65 % vs 31 %). A trigger that fires within two minutes of
   the first break is volume confirming a move that is *happening*; one that fires five minutes after the
   break is volume arriving on a move that already *happened* — the same mechanism as the #721 volume
   ceiling, seen from the time axis. ⚠ This did **not** hold on the holdout (§5) — keep it as a hypothesis.
2. **Broad tape at the trigger.** The equal-weight universe median return from the 09:15 open to the
   trigger tick is a free NIFTY proxy. Longs fired into a universe already down > 0.3 % won 1 of 8;
   shorts 0 of 3. This one **does** hold on the holdout on both sides.
3. **60-second follow-through** (post-entry). Longs that are up > 0.1 % sixty seconds after the trigger
   win 77 % (+₹40.9k on 13); longs flat or down at 60 s win 25 % (−₹27k on 16). Shorts: 3 of 3 with
   > 0.1 % follow-through won (+₹30.6k). This is not a rating input (it is after the fill) but it is the
   single clearest signature of a winner, and it is what the per-trade stop is crudely approximating.
   A time-boxed confirmation exit ("flat or against after 60–120 s → exit") is a different rule from the
   #696 money stop and was not tested here; note the option round-trip spread (1–2 % of premium) taxes any
   early exit.
4. **Tick intensity does not help.** Winners' trigger minutes are not busier than losers' (1.03 vs 1.31
   on longs — if anything the reverse).

## 4. News — did any information explain the win?

The feed is mostly noise: 60 % of matches are ET's auto-generated "Share Price Live Updates" pages, which
exist for every large cap every day. Where the store was silent, the winners and the notable losers were
looked up online (Upstox / Business Standard / univest / 5paisa / company filings, searched 2026-09-14).
"Store" = found in `market_intel`; "web" = found online only; "none" = nothing company-specific found.

| Date | Trade | Catalyst | Source | Outcome |
|---|---|---|---|---|
| 08-06 | HAL L | none (Q1 results came 08-12) | web | **WIN +₹5.6k** — pure tape |
| 08-18 | DIXON L | new OEM subsidiary filing (after hours 08-11, stale) | web | WIN +₹4.2k |
| 08-18 | MOTHERSON L | record high 08-19; broker "easy gains no longer in sight" | web | LOSS −₹5.7k |
| 08-19 | CGPOWER S (−0.93 % gap) | **suspected cyberattack on IT systems, filed after hours 08-18; stock −4 %** | web | **WIN +₹12.3k** |
| 08-20 | PFC S | none — "pressure emerging after the open" | web | **WIN +₹17.9k** — pure tape |
| 08-21 | BRITANNIA S | none confirmed (−3.6 % day; Goldman sugar-cost note around then) | web | **WIN +₹37.6k** — pure tape |
| 08-26 | HINDZINC L (+2.09 % gap) | T-1 15:50: "shares fall 3 % on reports of fresh government OFS" — the gap-up is a rebound | store | **WIN +₹11.1k** |
| 08-27 | LICHSGFIN L | NCLT clarification + new MD & CEO; "rises for third straight session" | web | WIN +₹5.1k |
| 08-27 | TVSMOTOR S | 08:35: "among 6 stocks hitting 52-week highs" | store | WIN +₹6.1k (fade of the high) |
| 08-28 | WIPRO / TCS / INFY L | 08:45: "Wipro ADRs rise 5 % after expanded Google Cloud AI partnership" + IT ADRs up | store | **all three WIN** (+₹11.7k) — one theme, three tickets |
| 08-31 | HDFCBANK L (+1.27 % gap) | 09:15: CEO exit disclosure, "investors pile in" | store | LOSS −₹7.0k (faded) |
| 09-01 | HEROMOTOCO L (+2.94 % gap) | **August dispatches +2.6 % YoY but 3.7 % below the 590k consensus; exports −24 %** — scheduled monthly-sales day | web | LOSS −₹9.3k (faded; stock −7 % next day) |
| 09-03 | SBICARD L | T 08:30: sector-negative "revolver misfire" piece; ESG score | store/web | WIN +₹5.0k (against the news) |
| 09-04 | ANGELONE L (+3.18 % gap) | **August business update: 0.57 m gross client adds, record ₹74 bn funding book** — scheduled monthly data | web | LOSS −₹3.5k (faded, stopped) |
| 09-07 | IDEA L | **Vi rebrand + Shah Rukh Khan campaign (announced 09-01); 25-month high on 395 m shares "in a weak market"** | web | **WIN +₹15.1k** (a C by the market veto — see §5) |
| 09-08 | NATIONALUM L | **T-1: EGA technology partnership for 0.5 MTPA brownfield smelter expansion** | web | WIN +₹3.6k |
| 09-09 | BIOCON L | T-1: Pertuzumab supply deal; T 08:20: Active Pine block deal | store | WIN +₹2.6k |
| 09-10 | MCX S | 08:50: Bernstein "prefers MCX over BSE" + IAMAI MoU (positive, against the short) | store | LOSS −₹3.1k |
| 09-11 | ONGC L, TECHM L | market: "Sensex crashes 700 pts — Freaky Friday" | store | both LOSS |
| various | ASHOKLEY, PREMIERENE, CUMMINSIND, MANKIND, HINDALCO, MAXHEALTH (reg-30 filing, content unknown), LTF, DLF, DIVISLAB, POLYCAB, ADANIPORTS, LODHA, VBL, CANBK, UNIONBANK, UPL, BAJAJ-AUTO | none found | web | mixed |

Reading, with n ≈ 12 catalyst trades in 48:

- **(a) Scheduled-data gap-ups fade.** All three ≥ 1.5 % gap-up longs on a *scheduled* disclosure —
  HEROMOTOCO (monthly sales), ANGELONE (monthly business update), HDFCBANK (CEO succession) — faded. The
  gap had already priced the headline, and the second read (Hero's consensus miss) went the other way.
  Numerically this is the gap ≥ 1.5 % bin (1 of 3) and the "beyond the level > 0.3 %" bin (40 %).
- **(b) Unscheduled, company-specific news on a *modest* gap is where the news-backed winners are:**
  CGPOWER short on the cyberattack filing (−0.93 % gap), IDEA on the rebrand momentum, NATIONALUM on the
  EGA deal, HINDZINC as an OFS rebound, BIOCON on a block deal, the 08-28 IT trio on the Wipro ADR move.
- **(c) The biggest winners had no news at all.** BRITANNIA +₹37.6k, PFC +₹17.9k, HAL +₹5.6k were pure
  tape — early, clean-volume triggers with immediate follow-through (all three are grade A/B in §5).
  Information is not a prerequisite for a win on this signal.
- **(d)** The only news with a clean directional read is the market-wide one (09-11 crash), and the
  universe-median feature captures it better than text.

There is no basis for a news *score* in the rating. Two cheap, mechanical flags are worth carrying as
annotations for the re-cut at 40 new fills: **`scheduled_disclosure_day`** (monthly auto sales on the
1st, monthly broker/NBFC business updates, results dates from the master calendar) and
**`gap_ge_1p5`**, since their intersection is 0 of 3 so far. Free-text news stays a manual annotation on
`/logs` (the `news_context_service` already exists for the futures sleeve), never a scoring input. The
`market_intel` feed itself missed 5 of the 12 catalysts above (CGPOWER, IDEA, NATIONALUM, HEROMOTOCO,
ANGELONE) — its two RSS sources are not enough for per-stock coverage; an exchange-filings feed
(NSE corporate announcements) would have carried four of the five.

## 5. The rating — two versions, and only one survives the holdout

### v1 — seven checks (in-sample fit)

Long: early (≤ 09:22) · clean volume (< 1.55×) · fresh (< 120 s since first cross or < 70 % pre-ticks
beyond level) · market OK (universe median > −0.3 %) · spread < 2 % · level ≥ +1 % vs prev close · gap in
[0.5, 1.5] %. Short: the first five plus gap > −0.5 % and 09:15 close in lower third. A = ≥ 6/7,
C = ≤ 4/7 or a hard veto (long into a broad-weak tape; any trigger after 09:24).

| Cohort | A | B | C |
|---|---|---|---|
| Real, net P&L | n=14, **86 % WR, +₹65.8k** | n=17, 41 %, +₹9.6k | n=17, 12 %, −₹35.6k |
| Holdout, stock basis | n=5, **40 %**, med −0.07 % | n=18, 61 %, +0.16 % | n=21, 43 %, −0.07 % |

It fits the sample it was built on and **fails the holdout**: on the 44 other rows the A grade is no better
than C. Per-check on the holdout, `fresh` reverses (43 % pass vs 69 % fail on longs), `level_ext` and
`gap_band` have no power, `spread_ok` has none. Those four are the in-sample artifacts.

### v2 — the three checks that hold on both cohorts

| Check | Definition (all known at the trigger) | Real L pass/fail WR | Real S | Holdout L | Holdout S |
|---|---|---|---|---|---|
| `early` | trigger ≤ 7 min after 09:15 (≤ 09:22:00) | 62 % / 22 % | 60 % / 0 % | 60 % / 55 % | 56 % / 0 % |
| `market_ok` | equal-weight universe median return 09:15 open → trigger tick > −0.3 % | 64 % / 12 % | 50 % / 0 % | 59 % / 33 % | 40 % / 25 % |
| `clean_vol` | volume ratio at trigger < 1.55× (crossed the 1.5× gate, did not blow through it) | 67 % / 33 % | 57 % / 25 % | 60 % / 55 % | 43 % / 29 % |

**Grades:** **C** = `market_ok` false OR trigger later than 09:24; **A** = not C and `early` and
`clean_vol`; **B** = everything else.

| Cohort | A | B | C |
|---|---|---|---|
| Real, net P&L | n=17 (13 days), **76 % WR, +₹95.7k**, avg +₹5,629 | n=16, 38 %, −₹24.3k | n=15, **13 %, −₹31.6k** |
| Real longs | n=13, 69 %, +₹25.6k | n=9, 56 %, −₹1.6k | n=11, 18 %, −₹18.2k |
| Real shorts | n=4, 100 %, +₹70.1k | n=7, 14 %, −₹22.7k | n=4, 0 %, −₹13.4k |
| Holdout, stock basis | n=6, 67 %, med +0.22 % | n=18, 56 %, +0.08 % | n=20, **40 %, −0.07 %** (shorts 17 %) |
| Real, first 24 fills (08-06 → 09-01) | n=9, **100 %, +₹100.4k** | n=10, 40 %, −₹16.9k | n=5, 20 %, −₹16.9k |
| Real, last 24 fills (09-01 → 09-11) | n=8, 50 %, −₹4.7k | n=6, 33 %, −₹7.4k | n=10, **10 %, −₹14.7k** |

What is reliable and what is not:

- **C is the robust half.** It is 12–20 % WR and net negative in the real sample, both halves of the time
  split, and the holdout (40 % WR, negative median, shorts 17 %). "Late, or into a falling tape" loses
  everywhere we can look. Vetoing C on the 48 real fills removes 15 trades and −₹31.6k; on the September
  half it removes 10 of 24 trades and −₹14.7k of a −₹26.7k total.
- **A is real but its size is not.** 9 of 9 in August, 4 of 8 in September, 4 of 6 on the holdout. The
  first-half number is the kind that does not repeat; expect A to be a ~60–65 % grade, not 100 %.
- **B is "no information"** — 38–56 % everywhere, i.e. the strategy's unconditional rate.
- The `market_ok` threshold (−0.3 %) was chosen on 8 + 3 failing rows; record the raw value, not just the
  flag, so it can be re-cut.

## 6. Plan — stamp it, measure it, then let it gate

1. **Compute at the trigger, in-process** (`_enter`, before the shadow / cap / ceiling checks so every
   cohort gets a grade). All three inputs are already in hand: trigger time and `vol_ratio` are on the
   action; the universe median needs the per-symbol 09:15 open (the `first_candles` snapshot) and the last
   LTP per symbol (`_handle_raw` already sees every universe tick — keep a dict, ~207 floats). No broker call
   on the tick thread (the #626 rule).
2. **Persist** `open15_trades.rating` ∈ {A, B, C} plus the raw inputs `univ_med_ret_pct`,
   `secs_since_first_cross` (the v1 hypothesis, recorded for the re-cut) — and carry `rating` + inputs on
   the existing `entry` / `entry_skipped` / `entry_shadow` / `entry_rejected` events (no new event names,
   #615/#622). Render as a chip on `/logs` row builders (both the JS and its Python twin) and a
   per-grade line in the summary digest.
3. **Observational first — the rating gates nothing on day one.** Every fill and every sim/shadow row is
   graded, real money keeps flowing exactly as today.
4. **Pre-registered promotion rule (decide at 40 NEW real fills, ~late Oct 2026):** promote C to a veto
   (`entry_skipped · rating_c`, sim-priced at 1 lot like `vol_ratio_cap`, no `max_trades` slot consumed)
   **only if** on the new fills alone the C cohort is net negative AND its WR is ≥ 15 points below the
   A+B cohort. A and B never gate. If the rule fails, delete the grade rather than re-tune it — a rating
   that is re-cut every month is curve-fitting with a UI.
5. **Interaction with existing knobs.** `no_entry_after` 09:23 and the #721 ceiling at 1.7× each cover
   part of `early` / `clean_vol`; the rating does not replace them — it adds the market-tape veto (which
   nothing covers today) and packages the three into one per-trade label an operator can read at a glance.
6. **Not in scope:** the 60-s follow-through exit (a different rule family, would need its own replay
   against the #696 stop) and any news term.

## 7. Per-trade table (real fills, v2 grade)

Grade is the v2 three-check grade. "Univ median" is the equal-weight universe return from the 09:15 open
to the trigger tick. "60s follow" is the stock's move 60 s after the trigger, side-signed (post-entry).

| Date | Symbol | Side | Src | Grade | Net Rs | Stock to 09:30 % | Gap % | Level vs prev % | Trig (min after 09:15) | Vol ratio | Univ median % | Secs since first cross | 60s follow % | News hits |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2026-08-06 | MUTHOOTFIN | L | rolling | B | -288 | -0.02 | 1.42 | 1.47 | 5.5 | 1.62 | -0.14 | 28 | -0.43 | 2 |
| 2026-08-06 | HAL | L | rolling | A | +5,622 | 1.34 | 1.08 | 1.65 | 7.0 | 1.52 | -0.13 | 411 | 0.36 | 0 |
| 2026-08-13 | ASHOKLEY | L | rolling | C | +1,438 | 0.26 | 0.40 | 1.93 | 11.6 | 1.91 | -0.04 | 26 | -0.01 | 0 |
| 2026-08-14 | CUMMINSIND | L | seed | B | -4,446 | -0.44 | 0.62 | 1.25 | 7.0 | 1.67 | -0.10 | 350 | -0.16 | 0 |
| 2026-08-18 | DIXON | L | rolling | A | +4,170 | 0.33 | 0.53 | 1.82 | 2.8 | 1.54 | -0.10 | 10 | 0.18 | 0 |
| 2026-08-18 | MOTHERSON | L | rolling | B | -5,679 | -0.18 | 0.02 | 1.15 | 4.8 | 2.34 | -0.10 | 19 | -0.31 | 0 |
| 2026-08-19 | CGPOWER | S | rolling | A | +12,283 | 0.65 | -0.93 | 1.65 | 3.6 | 1.51 | -0.19 | 57 | 0.03 | 0 |
| 2026-08-19 | MANKIND | L | rolling | C | -5,830 | -0.22 | 0.00 | 0.53 | 9.3 | 1.65 | -0.21 | 497 | -0.02 | 0 |
| 2026-08-20 | PFC | S | rolling | A | +17,912 | 0.55 | 0.00 | 0.21 | 3.9 | 1.53 | -0.11 | 111 | 0.24 | 0 |
| 2026-08-20 | HINDALCO | S | rolling | B | -5,329 | -0.05 | 0.49 | 0.09 | 8.5 | 1.78 | -0.10 | 115 | 0.09 | 4 |
| 2026-08-21 | VEDL | L | rolling | B | -809 | 0.13 | 0.86 | 1.38 | 5.0 | 1.62 | -0.01 | 239 | -0.09 | 0 |
| 2026-08-21 | BRITANNIA | S | rolling | B | +37,553 | 1.13 | -0.37 | 1.23 | 5.7 | 1.51 | -0.11 | 283 | 0.11 | 4 |
| 2026-08-21 | PREMIERENE | L | rolling | B | +1,254 | -0.02 | 0.96 | 1.16 | 6.7 | 1.65 | -0.16 | 253 | 0.17 | 0 |
| 2026-08-26 | HINDZINC | L | seed | A | +11,125 | 1.10 | 2.09 | 2.79 | 2.5 | 1.52 | -0.07 | 30 | 0.44 | 2 |
| 2026-08-26 | MAXHEALTH | S | rolling | B | -13,117 | -1.04 | 0.18 | 0.30 | 3.6 | 2.62 | -0.09 | 175 | -0.34 | 1 |
| 2026-08-26 | LICHSGFIN | L | rolling | C | -1,986 | -0.09 | 0.00 | 0.13 | 11.6 | 3.95 | -0.00 | 471 | -0.22 | 0 |
| 2026-08-27 | LICHSGFIN | L | rolling | B | +5,130 | 0.36 | 0.72 | 1.01 | 5.5 | 2.07 | 0.01 | 246 | 0.15 | 0 |
| 2026-08-27 | TVSMOTOR | S | rolling | B | +6,149 | 0.75 | 0.11 | 0.47 | 5.5 | 1.62 | 0.01 | 233 | 0.12 | 1 |
| 2026-08-27 | CGPOWER | L | rolling | B | +231 | 0.27 | 0.73 | 1.29 | 7.7 | 1.55 | -0.02 | 379 | 0.04 | 0 |
| 2026-08-28 | WIPRO | L | seed | A | +1,652 | 0.23 | 1.05 | 1.64 | 2.8 | 1.53 | -0.24 | 45 | 0.14 | 5 |
| 2026-08-28 | TCS | L | seed | A | +6,561 | 0.92 | 1.05 | 1.96 | 2.9 | 1.53 | -0.24 | 64 | 0.20 | 3 |
| 2026-08-28 | INFY | L | seed | A | +3,493 | 0.43 | 1.15 | 1.86 | 3.8 | 1.53 | -0.28 | 91 | 0.09 | 7 |
| 2026-08-31 | LTF | S | rolling | C | -3,568 | -0.24 | -1.80 | 2.72 | 3.5 | 1.86 | -0.60 | 195 | -0.24 | 0 |
| 2026-08-31 | HDFCBANK | L | rolling | C | -6,995 | -0.53 | 1.27 | 1.79 | 4.7 | 1.53 | -0.61 | 234 | -0.24 | 4 |
| 2026-09-01 | DIVISLAB | S | seed | A | +2,387 | 0.11 | -2.28 | 3.07 | 3.6 | 1.50 | -0.23 | 4 | -0.03 | 1 |
| 2026-09-01 | HEROMOTOCO | L | seed | A | -9,291 | -0.80 | 2.94 | 3.81 | 5.5 | 1.51 | -0.20 | 126 | -0.21 | 2 |
| 2026-09-01 | POLYCAB | S | seed | B | -2,113 | -0.03 | -2.59 | 2.91 | 7.8 | 1.63 | -0.24 | 388 | 0.06 | 0 |
| 2026-09-02 | DLF | S | rolling | C | -4,571 | -0.44 | -1.36 | 2.43 | 7.8 | 1.52 | -0.57 | 300 | -0.13 | 0 |
| 2026-09-02 | SUNPHARMA | L | rolling | C | -2,646 | -0.10 | -0.98 | -0.20 | 12.7 | 1.67 | -0.33 | 502 | -0.04 | 5 |
| 2026-09-02 | IDEA | L | seed | C | -1,173 | 0.00 | 0.43 | 0.50 | 14.9 | 1.61 | -0.42 | 0 | 0.00 | 0 |
| 2026-09-03 | SBICARD | L | rolling | A | +4,989 | 0.73 | 0.92 | 1.63 | 3.8 | 1.51 | -0.15 | 158 | 0.38 | 1 |
| 2026-09-03 | BANDHANBNK | L | rolling | B | +422 | 0.21 | 1.22 | 2.20 | 5.5 | 1.72 | -0.13 | 251 | -0.01 | 0 |
| 2026-09-03 | ADANIPORTS | L | seed | C | -5,265 | -0.56 | 1.33 | 2.35 | 10.8 | 1.60 | -0.31 | 27 | -0.08 | 3 |
| 2026-09-04 | ANGELONE | L | seed | A | -3,501 | -0.57 | 3.18 | 3.81 | 5.8 | 1.50 | 0.02 | 300 | -0.34 | 0 |
| 2026-09-04 | UPL | S | seed | C | -1,471 | -0.03 | -0.73 | 0.73 | 12.2 | 1.61 | 0.03 | 25 | -0.05 | 3 |
| 2026-09-07 | BAJAJ-AUTO | S | seed | C | -3,777 | -0.31 | -0.91 | 1.65 | 5.8 | 1.51 | -0.31 | 74 | -0.12 | 0 |
| 2026-09-07 | IDEA | L | rolling | C | +15,073 | 1.83 | 0.47 | 1.47 | 6.8 | 1.51 | -0.38 | 22 | 0.00 | 0 |
| 2026-09-07 | MCX | L | rolling | C | -3,372 | -0.39 | 0.05 | 0.71 | 7.9 | 1.51 | -0.36 | 345 | -0.01 | 2 |
| 2026-09-08 | UNIONBANK | S | seed | B | -1,838 | 0.01 | -0.88 | 1.12 | 4.0 | 1.92 | -0.10 | 1 | 0.01 | 0 |
| 2026-09-08 | NATIONALUM | L | seed | A | +3,647 | 0.47 | 1.31 | 2.27 | 5.5 | 1.53 | -0.08 | 158 | 0.20 | 0 |
| 2026-09-08 | LODHA | S | rolling | B | -3,275 | -0.61 | -0.42 | 0.77 | 6.6 | 1.64 | -0.12 | 262 | -0.14 | 0 |
| 2026-09-09 | MCX | L | seed | A | +1,489 | 0.21 | 0.60 | 1.11 | 5.8 | 1.50 | -0.09 | 4 | 0.10 | 6 |
| 2026-09-09 | BIOCON | L | rolling | B | +2,553 | 0.11 | 0.43 | 2.13 | 5.9 | 1.57 | -0.09 | 30 | 0.20 | 3 |
| 2026-09-10 | VBL | L | rolling | A | -615 | -0.83 | 0.49 | 0.74 | 5.0 | 1.51 | -0.03 | 254 | -0.15 | 0 |
| 2026-09-10 | CANBK | L | rolling | A | -3,780 | 0.66 | -0.06 | 0.69 | 7.0 | 1.54 | -0.04 | 179 | 0.11 | 1 |
| 2026-09-10 | MCX | S | seed | B | -3,129 | -0.73 | -1.14 | 1.26 | 7.8 | 1.51 | -0.20 | 39 | -0.05 | 8 |
| 2026-09-11 | ONGC | L | seed | C | -3,636 | -0.15 | 1.11 | 1.34 | 5.8 | 1.57 | -0.40 | 234 | -0.12 | 3 |
| 2026-09-11 | TECHM | L | rolling | C | -3,818 | -0.32 | -0.43 | 0.73 | 7.5 | 1.55 | -0.44 | 261 | -0.23 | 4 |

Two rows worth a second look: IDEA 09-07 (a C by the market veto — universe −0.38 % — that made +₹15k)
and the three September A-losers (HEROMOTOCO 09-01, ANGELONE 09-04, VBL 09-10: two of them ≥ 2.9 % gaps
that had already priced their news, the third stopped at −₹615 with a held-to-exit counterfactual of
−₹8.2k). The rating is a base rate, not a verdict.

## Method notes

- Stock exit for the holdout and the "stock to 09:30" column is the last tick at or before 09:30:05; real
  rows exited at their actual fill (09:30 or a #696 stop/trail), so the option net and the stock move can
  disagree in sign (CANBK 09-10: stock +0.66 % by 09:30, option stopped at −₹3.8k at 09:2x).
- `prev_close` for the level/gap features is reconstructed as `first_tick / (1 + gap)`; the arm event
  carries the exact value if anyone wants to re-cut.
- News keyword matching is by symbol and company-name tokens with hand overrides for the ambiguous ones
  (IDEA, PFC, HAL, MCX, LTF, UPL, VBL, DLF); ET "Share Price Live Updates" pages inflate `n_news` for large
  caps and were not filtered — which is why `n_news` has no power.
- Nothing here was written to `open15_trades`; the harness is read-only (`mode=ro`).
