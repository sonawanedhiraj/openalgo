# open15 option liquidity — what NSE publishes, what it does not, and what we measure instead

**Date:** 2026-08-09 · **Issue:** [#583](https://github.com/sonawanedhiraj/openalgo/issues/583) ·
**Status:** design settled by measurement; Phase 1 (score only) implemented
**Predecessor:** [`2026-07-29_option_liquidity_filter_rejected.md`](2026-07-29_option_liquidity_filter_rejected.md) (#488)

## Why

`open15_vol_breakout` is live on real money and buys the ATM CE/PE with **MARKET**
orders. Contract choice is two lines — nearest non-expired expiry, then nearest strike
(`services/open15_option_shadow.py:77-79`) — and no liquidity input reaches any
decision anywhere in the strategy.

The universe is very uneven in *option* liquidity. From the NSE F&O bhavcopy for
2026-08-07, ATM call-side premium turnover across the 208 stock-option underlyings
runs from **₹0.39 Cr/day (BAJAJHLDNG) to ₹679 Cr (SBIN)** — and BAJAJHLDNG had **6 of
its 12 nearest ATM strikes trade zero contracts all day**.

The damage mechanism is specific, not theoretical. **NSE/FAOP/40075** (2019-01-29): an
option with premium ≤ ₹50 trades within **±₹20 absolute** of the reference price, above
₹50 within **±40%**, and an order that would trade outside that range *"shall be
cancelled by the Exchange"* (full or partial). A MARKET order into a thin book risks
being cancelled or filled far from the mid.

---

## 1. There is no NSE-published illiquid-options list

**NSE/FAOP/60058** (2023-12-29, "periodic disablement of illiquid strikes") is the
closest thing, and it does not do this job. Its criteria, verbatim:

- contracts *"eligible as per the strike scheme will continue to be available for
  trading, **irrespective of the open interest**"*;
- contracts not eligible per the scheme **and with zero OI** are disabled.

So every near-ATM strike we would ever pick stays listed however dead it is. "The
contract exists" carries no liquidity information.

---

## 2. Three published classifications, all tested, all rejected

Each was measured rather than reasoned about, because the task explicitly called for
no assumptions.

### 2a. Group I / II / III — excludes 0 of 208

NSE Clearing's categorisation: traded ≥80% of days over six months **and** mean impact
cost ≤1% ⇒ Group I; >1% ⇒ Group II; the rest ⇒ Group III. Impact cost is recomputed on
the 15th monthly from four daily **cash** order-book snapshots, ₹1 lakh order, measured
from the mid.

Measured over **170 NSE sessions** (2025-12-01 → 2026-08-07) on `historify` daily bars
for all 208 F&O underlyings: the **lowest cash trading frequency in the entire universe
is 360ONE at 99.4%**; every other name is 100.0%. Group III needs <80%. **Zero names
qualify — the filter would never fire.**

Group II is nearly as unreachable: F&O entry requires MQSOS ≥ ₹75 lakh, i.e. a ₹75 lakh
order moves price only a quarter sigma, so a ₹1 lakh order is far under 1%.

### 2b. NIFTY 100 membership — fails in both directions

Constituents pulled from `nsearchives.nseindia.com/content/indices/`.

- Only **97 of 208** F&O names are in NIFTY 100 — using it would cut the universe by
  **53%** to address illiquidity affecting ~20%.
- **45 of the 111 non-members are in the top half of CE liquidity**, including
  **HEROMOTOCO (p99, ₹284 Cr/day** — the 2nd most active option book of all 208, and
  currently in NIFTY Midcap 150, not the 100), PGEL (₹133 Cr), BSE (₹128 Cr),
  KALYANKJIL (₹125 Cr).
- **7 members are in the bottom 20%**, including **BAJAJHLDNG at p0** — the single
  worst option book in the universe — plus SHREECEM, UNITDSPR, SBILIFE, JINDALSTEL,
  TATACONSUM, MAXHEALTH.
- Signal exists but is weak: median CE percentile 59 for members vs 42 for
  non-members.

NIFTY 500 contains all 208, so it excludes nothing.

### 2c. Root cause — option liquidity is a different quantity

Spearman rank correlation between **cash turnover and ATM option premium turnover**
across the 208 names: **ρ = 0.617**. Related, not interchangeable.

**LICI is the clean case:** ₹352 Cr/day in the cash market — unambiguously Group I, a
NIFTY 100 member, invisible to every published filter — with the **3rd-thinnest option
book of 208** (₹1.07 Cr of ATM premium/day). BANDHANBNK is the same shape (₹325 Cr cash,
7th-thinnest options), as are LODHA, NAUKRI and TORNTPHARM.

The inverse also holds: BAJAJHLDNG, the worst option book, is *also* thin in cash
(₹59.7 Cr/day). So a cash-based filter catches some of the right names — just not the
ones that actually bite, which are the large liquid stocks nobody thinks to check.

**What we do keep from the Group framework is its methodology**: impact cost for a
fixed order value, measured from **order-book snapshots** rather than trade prints, as a
% from the **mid of best bid/offer**, over a rolling window with a periodic review. That
is exactly the shape of the planned entry-time gate, computed on the option book for our
slot size instead of ₹1 lakh. NSE's newly-listed rule — provisional category, real
computation at the next review — is the precedent for how new F&O listings are handled.

---

## 3. Data source: the broker API, not an NSE feed

`get_multiquotes` returns per option contract `ltp`, **`bid`**, **`ask`**, `volume`,
`oi`, OHLC (`broker/zerodha/api/data.py:399-413`), batching **500 instruments per call**
with a 1.0 s delay, with no cap on `symbols` in `MultiQuotesSchema`.

- A full sweep is 208 × 2 sides × 6 strikes ≈ **2,500 instruments → ~5 calls / ~6 s**.
- It is already the house pattern — open15 itself calls it twice
  (`open15_breakout_service.py:390`, `:488`).
- **It provides bid/ask, which the UDiFF bhavcopy structurally cannot.** The strategy
  crosses the spread twice, so spread is the cost.

Two existing tools deliberately not used: `option_chain_service.get_option_chain()`
(2 REST calls per underlying ⇒ ~400 calls / ~135 s, and it reads `bid_qty`/`ask_qty`
which the Zerodha mapper never emits, so they silently return 0); and modifying the
shared quote mapper (Kite's `/quote` already carries all 5 depth levels and
`data.py:403-404` discards them — worth capturing, but that mapper is shared by every
broker, so it belongs in its own issue).

### Units — verified, not assumed

| Source | Volume | Open interest | Turnover field |
|---|---|---|---|
| NSE UDiFF bhavcopy | **LOTS** (`TtlTradgVol`) | UNITS | `TtlTrfVal` is **notional**, not premium |
| Zerodha quote | **UNITS**, cumulative for the day | UNITS | — |

Verified on 2026-08-07: `OpnIntrst` is divisible by lot size for **27,577 of 27,577**
STO rows, while `TtlTradgVol` is for only 17 (coincidences). So bhavcopy premium
turnover needs the lot multiply and the broker path does not.

**Cross-source check (V1).** Every option row open15 has journaled with liquidity
captured (2026-08-04 → 08-07) was matched to the same contract-day in the bhavcopy —
**12 of 12 matched**:

- broker `opt_lot_size` == NSE `NewBrdLotQty` in 12/12;
- broker OI (09:30) vs NSE settled OI: same magnitude every time (−54% to +52%,
  expected since OI moves through the day);
- broker cumulative volume ÷ lot, as a share of NSE full-day lots: **3.8% – 58.9%,
  always a fraction, never exceeding.**

If broker volume were already in lots the share would be ~0.003%; if NSE's were in
units it would exceed 100%. Twelve of twelve land in a plausible first-15-minutes band.

> ⚠ **The contract lookup key must include expiry.** A first pass keyed on
> `(date, symbol, strike, type)` collided across the three contract months and reported
> `nse_oi = 0` for actively-traded HAL and KALYANKJIL. Same-strike-different-month is
> the trap; `test_band_takes_the_front_month_only` pins it.

---

## 4. Four measurement decisions, each settled by data

### 4a. Score each SIDE separately — mandatory

`trade_side` is `long_only`, so the strategy buys **CE**. On **20-day medians**
(2026-06-01 → 08-07, the seeded history), blending CE and PE misclassifies
**17 of 208** names:

```
thin on CALLS, rescued by puts :  BLUESTARCO GMRAIRPORT IRFC MAXHEALTH OBEROIRLTY
                                  OIL PNBHOUSING PRESTIGE
thin on PUTS, dragged by them  :  360ONE APLAPOLLO CROMPTON DALBHARAT GODFRYPHLP
                                  PIDILITIND RADICO SUPREMEIND UNOMINDA
```

**UNOMINDA** is the clean case: CE **p28** against PE **p10** — perfectly tradeable as
a long, and should never be entered short. The largest median divergences are ANGELONE
(CE 41.9 / PE 64.3), UNOMINDA (28.2 / 9.6) and CHOLAFIN (49.3 / 65.5).

For a future `trade_side = both`: **25 names fail both sides, 33 fail CE, 34 fail PE,
17 fail exactly one** (8 CE-only, 9 PE-only).

> ⚠ **Corrected 2026-08-09, and the correction is itself the §4d argument.** An
> earlier draft of this document cited *"FORTIS trades 10.8× more call premium than
> put"* and counted 14 misclassifications. Both came from a **single day** (2026-08-07),
> and that day was a FORTIS outlier: its CE turnover spiked to ₹42.71 Cr against a
> typical ₹1–3 Cr, scoring p89 on the day. On the 20-day median FORTIS is **CE p9.6 /
> PE p17.5** — thin on *both* sides, with its calls the *thinner* of the two, i.e. the
> opposite of the single-day reading. GODREJCP has the same shape (p75 on the day,
> median p6.9). Every figure in this section is now median-based. This is exactly the
> instability §4d quantifies, caught in our own documentation.

### 4b. Premium turnover, not trade count

An earlier draft ranked on `min(trades_pctile, turnover_pctile)` and put **MANAPPURAM
in the bottom quintile at p14**. That is wrong: MANAPPURAM has the **second-largest
trade-count-vs-turnover rank disagreement in the entire universe** (p14 on trades, p40
on turnover) because its average ATM ticket is **₹44,191** — larger than SBIN's
₹19,918. It turns over ₹9.65 Cr of ATM premium a day, ~2,144× a ₹45k slot.

**Few, large tickets is block flow, not illiquidity.** The same error hit OFSS,
LAURUSLABS, IDEA, BOSCHLTD and POWERINDIA. Rupees are what a fill scales against;
trade count is a diagnostic, and the broker feed does not expose it at all.

### 4c. Band = 6 strikes per side

The picked strike is within **0.81% of the trigger price in all 16 live journaled rows**
(mean 0.39%), so the band must be tight — but not a single strike, because the strike is
ATM to the *post-gap* trigger (observed gaps 0.14%–2.55% ⇒ 1–2 strike steps).

Exclusion sets at p20 by band width, 2026-08-07:

| Band | Names differing from the 6/side set |
|---|---:|
| 1 strike/side | **20** — unstable |
| 2/side | 8 |
| 3/side | 8 |
| 6/side (chosen) | — |
| 10/side | **0** |
| full chain | 4 |

The ranking sits on a stable plateau from ~3/side outward; 6/side is safely inside it.

### 4d. A 20-day median is required, not smoothing

**A single-day score is not stable enough to gate on.** Scoring 2026-08-04..07
independently, the p20 exclusion set churns badly: consecutive-day **Jaccard 0.42–0.62**,
only **15 names excluded on all four days** against **72 on at least one**. Individual
names swing violently — LICI p93 → p33 → p2 → p2, FORTIS p10 → p11 → p30 → p89,
COCHINSHIP p21 → p9 → p89 → p75. Median day-to-day stdev of a symbol's percentile:
**10.4 points**.

Replaying the fix over `fo_bhavcopy_eod` for 2026-02-01 → 2026-05-29 (1,106,455 CE rows,
53 scored days, 34 with a full window, universe ~211):

| Scoring | Mean consecutive-day Jaccard | Worst day | Names entering/leaving per day |
|---|---:|---:|---:|
| Single day | 0.48 | 0.21 | **29.9** |
| **20-day median** | **0.91** | **0.82** | **3.4** |

A **9× reduction in churn**. Median-set size 32–40 (mean 36), consistent with the 42
seen on 2026-08-07. 13 names are excluded on all 34 median-days.

**Acceptance criterion for any future change to the scorer:** consecutive-day Jaccard
**≥ 0.85** and **≤ 6** names entering/leaving per day. A build that scores single-day, or
shortens the window, fails this by construction.

---

## 5. The score as implemented (Phase 1)

`services/option_liquidity_service.py`, persisted to `option_liquidity_daily` keyed
`(as_of_date, symbol, side)`.

| Metric | Role |
|---|---|
| `atm_premium_turnover` = Σ `volume × ltp` over the side's band | **Primary.** No lot multiply — the quote's volume is already in units |
| `atm_zero_vol_strikes` ≥ half the band | **Hard tell**, forces the score to exactly 0.0 |
| `atm_spread_pct` (median, of the **mid**) | Secondary. Broker-only |
| `atm_volume_lots`, `atm_oi_lots`, `atm_trades`, `avg_ticket_inr` | Diagnostics. The last two are NULL on the broker path |

`daily_pctile` is the **mid-rank** percentile `100 × (i + 0.5) / n` within side, within
that day's universe. Mid-rank is deliberate: it is bounded to (0, 100) exclusive, which
reserves an exact **0.0 for the dead-band floor alone** so "disqualified" stays
distinguishable from "ranked last". `option_liquidity_pctile` is the 20-day median of
that, **NULL below 10 sessions of history** — a newly listed name reports "cannot rank",
never "illiquid".

**No broker session ⇒ no row.** The score is a median, so a missed day barely moves it,
whereas a half-swept day would corrupt a percentile that is a rank *within that day's
universe*.

### Universe reconciliation — already finding real drift

`SCANNER_SYMBOLS` is hand-maintained and does not track NSE. Verified 2026-08-09:

```
SCANNER_SYMBOLS (NSE-resolved): 211
option underlyings in master contract: 213
in SCANNER_SYMBOLS but NO option contracts (3): EXIDEIND, NUVAMA, SAMMAANCAP
has options but NOT watched (5): the 5 indices (deliberately dropped)
```

Those three can still win a top-N slot today and only fail *after* a trigger fires. The
job logs the diff both ways and **never edits `SCANNER_SYMBOLS`** — the scanner,
sector_follow and the aggregator all read it.

---

## 6. Relationship to #488, and what is still unproven

#488 measured two option-liquidity filters and rejected both as **inverted, not
mis-tuned**, on n=2. This is a different experiment: #488 tested **per-strike,
same-day-expiry, trigger-time flow**, while this scores the **underlying**,
structurally, on a 20-day median. Its own table shows how noisy the per-strike measure
is — it ranks SBIN 8th of ten on 2026-07-28, while SBIN is the single most active option
book in the universe on the bhavcopy.

**Honest limits, to be settled in Phase 3:**

- **The bhavcopy figures are full-day; open15 trades 09:15–09:30.** That a full-day rank
  predicts first-15-minute depth is a *hypothesis*. #488's broader caveat stands: median
  per-minute depth in that window is 1–17 lots for most stock options while a ₹45k slot
  buys 9–18 lots, so thin exits may be the norm rather than a per-symbol property.
- **This score would NOT have caught POWERINDIA**, the #488 bad exit — it sits at **p80**.
  A per-name aggregate is the wrong instrument for a same-day-expiry strike abandoned
  intraday; that case needs the live book at the trigger, which is the planned Gate 2.
- **Expiry-roll bias is unmeasured.** Near-month vs all-expiry ranking already differs by
  8 of 42 names on a single day, and in the last sessions before expiry premium collapses
  while lot volume rises, mechanically depressing a *premium*-turnover score.
- **No placebo has been run yet.** Before any exclusion is credited with improving
  results, it must beat a random exclusion of the same size, and survive a split-half —
  the tests that killed R61's size lever and the R45 news-drift result.
