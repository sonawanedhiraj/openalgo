# R66 — simplified_engine, September 2026: what if each equity trade had been 1 lot of the ATM option instead?

**Date:** 2026-09-18 · **Operator question:** "with the 55% win rate and trades only for the
month of September, what would the result have been if we bought 1 lot of options on the same
stock instead of trading the stock, with the same stop-loss and entry logic?"

**Data verdict up front: REAL option premiums were available and were used.** Every September
trade's ATM contract is the `29-SEP-26` monthly, which is still alive, so Zerodha's historical
API served real 1-minute option bars for all 42 contracts (0 fetch failures). No Black-Scholes
model was needed and none is used in the headline numbers. Two contracts turned out to be
illiquid on the tape (see §2): **KAYNES 3500PE is unpriceable** (no print during the hold) and
**BANDHANBNK 180CE is flagged** (₹1.17 premium, 75 lots traded all day). The aggregate below is
on the **41 priceable trades**; the 40-trade liquid-only cut is shown alongside.

**Result (41 trades, real premiums, 1 lot each, same entry/exit timestamps as the equity trade):**

| | trades | wins | win rate | gross ₹ | charges ₹ | spread cost ₹ | **net ₹** |
|---|---|---|---|---|---|---|---|
| **Equity, actual** (journal) | 41 | 20 | **48.8%** | −2,875 | 3,297 | (in fills) | **−6,172** |
| Options 1 lot — **A: no spread** (upper bound) | 41 | 22 | 53.7% | +2,211 | 3,519 | 0 | −1,308 |
| Options 1 lot — **B: 1.47% intraday spread (primary)** | 41 | 22 | **53.7%** | +2,211 | 3,519 | 9,810 | **−11,118** |
| Options 1 lot — B75: 2.18% spread | 41 | 19 | 46.3% | +2,211 | 3,519 | 14,549 | −15,857 |
| Options 1 lot — **C: per-name post-close spread** (lower bound) | 41 | 14 | 34.1% | +2,211 | 3,519 | 30,574 | −31,882 |

**Bottom line:** the option version did NOT rescue September. Before spread it is a wash
(+₹2.2k gross, −₹1.3k net, same ~50% win rate); after a realistic market-order spread it loses
**~₹11k — roughly 1.8× the equity loss — on 2.9× the capital per trade** (median premium
outlay ₹14.6k vs the ₹500-risk equity slot) and at **4.6× the per-trade volatility** (σ ₹1,968
vs ₹426). September is **n = 41, not a validated result**; September's actual equity win rate
was 48.8–50.0%, not 55% (R65's full-sample rate is 53.3%, CI 48–59).

## 1. Trade set and method

- **Trades:** `trade_journal`, `strategy_name='trending_equity_intraday'`,
  `signal_source='chartink'`, placed 2026-09-01 → 09-18 IST, closed, excluding `phantom_cleanup`
  rows. 48 rows → **42 closed** (6 unpriced: VOLTAS / GODREJPROP / MANAPPURAM / TIINDIA on 09-02
  from the app death, bug #707; MPHASIS / ICICIPRULI on 09-07). All 42 are `mode='sandbox'`.
  Equity actual: 21 wins / 42 (50.0%), gross −₹2,640, charges ₹3,374, net −₹6,014
  (15 LONG: 8 wins, net −₹1,711 · 27 SHORT: 13 wins, net −₹4,304). Exits: 33 `stop_loss`,
  6 `eod_watchdog`, 3 `sandbox_eod_squareoff`. Read from a sqlite-backup-API byte copy of
  `db/openalgo.db`.
- **Contract:** LONG → ATM CE, SHORT → ATM PE. ATM = strike nearest the equity ENTRY FILL
  price; expiry = nearest monthly as of the entry date via the production picker
  `open15_option_shadow.pick_contract` (which also skips Zerodha's expiry-week block window —
  no trade fell on 09-28/29, so **every trade uses `29-SEP-26`**, 11–28 calendar days to
  expiry). Lot sizes from the master contract (`symtoken`), all NSE-current: e.g. TORNTPHARM
  125, KAYNES 150, IREDA 4,525.
- **Entry / exit premium:** the CLOSE of the 1-minute option bar containing the equity
  `entry_fill_at` / `exited_at` timestamp — i.e. the last print in the minute the market order
  went out. The stop is on the UNDERLYING exactly as it fired live (the journal's own exit
  timestamp); the option is simply sold at whatever it printed at that moment. Sensitivity:
  filling both legs at the NEXT minute's open instead changes gross from +₹2,211 to +₹443 and
  net (B) to −₹12.9k — same conclusion.
- **Print staleness** (Kite pads option minute series with zero-volume bars carrying the last
  price, so a bar existing is not proof of a trade): 36/41 trades priced on a bar with volume in
  the entry minute and 38/41 in the exit minute; the rest are ≤ 7 min stale (ZYDUSLIFE 09-03
  entry 5 min, TORNTPHARM 09-15 entry 7 min, IREDA 09-01 exit 6 min, ABCAPITAL 1 min,
  HINDPETRO/UPL exit 2 min). BANDHANBNK 09-18 (48 min stale at entry) and KAYNES 09-10 (43 /
  226 min) are the two flagged in §2.
- **Charges — Zerodha's LIVE schedule** (zerodha.com/charges, read 2026-09-18; STT confirmed
  from the Budget-2026 change): ₹20 brokerage per executed order (₹40 round trip), NSE
  transaction charge 0.03553% of premium turnover, **STT 0.15% of the SELL-side premium**
  (raised 0.10% → 0.15% effective 2026-04-01), SEBI ₹10/crore, stamp 0.003% of the buy
  premium, 18% GST on brokerage + txn + SEBI. Total ₹3,519 on 41 trades = brokerage ₹1,640 +
  STT ₹1,003 + txn ₹474 + other ₹402 — ₹86/trade, vs ₹80/trade on the equity legs.
  **How option STT differs from equity intraday:** equity MIS STT is 0.025% of the sell-side
  *stock value* (₹951 on these 41 trades); option STT is 0.15% of the sell-side *premium* —
  six times the rate, on a base ~15× smaller, so in rupees it is similar (₹1,003) but as a
  share of what you are actually risking it is far heavier. ⚠ The in-repo
  `open15_option_shadow.option_round_trip_charges` still carries 0.0625% STT and a
  0.3503% txn rate (10× NSE's) — not used here; flagged for a separate fix.
- **Spread — the assumption that decides the answer.** Real bars are trade prints; a MARKET
  order crosses the book, so each leg pays ~half the bid-ask spread. Two measurements from this
  install's own data: (a) **intraday**: open15 captures bid/ask on every ATM stock-option
  decision (96 captures, 2026-08-20 → 09-18, 09:16–09:30): median spread **1.47% of mid**,
  p75 2.18%, p90 3.57% → scenarios **B** (primary) and B75; (b) **post-close**: the 15:45
  `option_liquidity_daily` sweep records `atm_spread_pct` per symbol/side/day: median for these
  41 names **3.88%** (31 same-day rows, 10 symbol-medians; KAYNES 42.7%) → scenario **C**.
  Post-close quotes are wider than intraday ones, so C is a lower bound and B the realistic
  case. Cost = ½·spread·(entry premium + exit premium)·lot. Scenario A (no spread) is only there
  to show where the money goes.

## 2. Per-trade table (scenario B; ₹, 1 lot; equity net from the journal)

`prem P&L/sh × lot = gross`; `net = gross − charges − spread(B)`. Bold = option and equity
disagree on the sign.

| date | symbol | side | entry→exit (IST) | underlying entry→exit | strike | lot | opt in | opt out | Δ/sh | gross | chg | spread | **opt net** | eq net |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 09-01 | LTF | S/PE | 14:44→15:11 | 308.20→309.30 | 310 | 2250 | 11.95 | 11.05 | −0.90 | −2,025 | 107 | 380 | −2,512 | −440 |
| 09-01 | IREDA | S/PE | 15:08→15:14 | 114.34→114.31 | 115 | 4525 | 6.38 | 6.28 | −0.10 | −452 | 115 | 421 | −988 | −46 |
| 09-02 | HINDPETRO | S/PE | 11:03→11:44 | 357.35→356.00 | 360 | 2025 | 9.85 | 10.75 | +0.90 | +1,823 | 98 | 307 | +1,418 | +293 |
| 09-02 | SWIGGY | S/PE | 11:18→11:20 | 266.10→267.85 | 265 | 1825 | 8.80 | 8.65 | −0.15 | −274 | 85 | 234 | −593 | −574 |
| 09-03 | ZYDUSLIFE | S/PE | 10:13→15:14 | 1148.00→1140.10 | 1150 | 900 | 23.00 | 29.40 | +6.40 | +5,760 | 107 | 347 | +5,306 | +604 |
| 09-03 | RBLBANK | L/CE | 10:48→11:22 | 406.15→407.65 | 405 | 3175 | 12.05 | 13.25 | +1.20 | +3,810 | 145 | 590 | +3,074 | +196 |
| 09-03 | GODREJCP | S/PE | 13:13→15:00 | 871.55→874.65 | 870 | 500 | 16.75 | 16.80 | +0.05 | +25 | 67 | 123 | −165 | −437 |
| 09-03 | SBICARD | L/CE | 13:34→13:48 | 658.70→656.85 | 660 | 800 | 19.80 | 19.05 | −0.75 | −600 | 84 | 228 | −912 | −363 |
| 09-03 | BSE | L/CE | 14:14→14:21 | 3308.00→3296.00 | 3300 | 200 | 127.50 | 120.00 | −7.50 | −1,500 | 105 | 364 | −1,969 | −443 |
| 09-04 | HAVELLS | S/PE | 09:45→09:57 | 1161.10→1168.40 | 1160 | 500 | 33.55 | 28.35 | −5.20 | −2,600 | 82 | 227 | −2,909 | −549 |
| 09-07 | MANAPPURAM | S/PE | 11:18→12:05 | 325.25→327.05 | 325 | 3000 | 9.30 | 8.00 | −1.30 | −3,900 | 106 | 381 | −4,387 | −588 |
| 09-07 | TRENT | S/PE | 13:05→13:09 | 2793.40→2783.20 | 2800 | 225 | 70.50 | 74.55 | +4.05 | +911 | 87 | 240 | +585 | +274 |
| 09-08 | 360ONE | S/PE | 11:20→13:01 | 1107.10→1112.10 | 1100 | 500 | 23.95 | 21.55 | −2.40 | −1,200 | 73 | 167 | −1,440 | −534 |
| 09-08 | ABCAPITAL | S/PE | 14:43→15:14 | 397.35→398.05 | 395 | 3100 | 6.70 | 6.55 | −0.15 | −465 | 96 | 302 | −862 | −259 |
| 09-08 | VOLTAS | S/PE | 14:53→15:14 | 1146.70→1144.10 | 1140 | 375 | 24.00 | 24.65 | +0.65 | +244 | 69 | 134 | +41 | +143 |
| 09-09 | VBL | S/PE | 11:34→15:20 | 399.80→406.00 | 400 | 1275 | 8.60 | 6.25 | −2.35 | −2,996 | 67 | 139 | −3,203 | −1,634 |
| 09-09 | COALINDIA | L/CE | 11:35→11:42 | 434.85→433.15 | 435 | 1350 | 8.00 | 7.10 | −0.90 | −1,215 | 70 | 150 | −1,435 | −474 |
| 09-09 | HDFCLIFE | S/PE | 11:58→12:26 | 511.60→510.10 | 510 | 1100 | 9.20 | 10.70 | +1.50 | +1,650 | 74 | 161 | +1,415 | +209 |
| 09-09 | COALINDIA | L/CE | 12:08→15:07 | 433.50→432.00 | 435 | 1350 | 7.20 | 5.90 | −1.30 | −1,755 | 67 | 130 | −1,952 | −428 |
| 09-09 | WIPRO | S/PE | 12:25→15:20 | 167.36→167.20 | 167.5 | 3000 | 4.53 | 4.88 | +0.35 | +1,050 | 81 | 207 | +761 | +2 |
| 09-09 | ADANIENT | L/CE | 12:28→12:38 | 3101.00→3107.70 | 3100 | 309 | 80.50 | 85.55 | +5.05 | +1,560 | 109 | 377 | +1,074 | +131 |
| 09-09 | PAYTM | L/CE | 12:53→13:15 | 1745.50→1750.00 | 1740 | 725 | 63.40 | 65.80 | +2.40 | +1,740 | 160 | 688 | +892 | +173 |
| 09-09 | COFORGE | S/PE | 12:58→13:35 | 1856.30→1865.40 | 1860 | 475 | 50.60 | 46.40 | −4.20 | −1,995 | 100 | 339 | −2,434 | −565 |
| 09-10 | KAYNES | S/PE | 10:35→13:38 | 3543.60→3533.40 | 3500* | 150 | 90.00 | 90.00 | 0.00 | 0 | 79 | 198 | **unpriceable** | +158 |
| 09-10 | VBL | L/CE | 12:40→13:05 | 411.65→409.60 | 410 | 1275 | 11.45 | 10.50 | −0.95 | −1,211 | 79 | 206 | −1,496 | −582 |
| 09-10 | PIIND | S/PE | 13:29→14:21 | 2356.00→2361.80 | 2350 | 175 | 49.00 | 48.20 | −0.80 | −140 | 67 | 125 | −332 | −327 |
| 09-10 | ZYDUSLIFE | S/PE | 13:34→15:15 | 1115.70→1118.40 | 1120 | 900 | 21.85 | 22.35 | +0.50 | +450 | 95 | 292 | **+63** | **−324** |
| 09-10 | BDL | S/PE | 14:28→15:20 | 1193.00→1190.40 | 1200 | 425 | 40.60 | 42.00 | +1.40 | +595 | 89 | 258 | +248 | +133 |
| 09-10 | HCLTECH | S/PE | 14:39→15:03 | 1204.10→1200.90 | 1200 | 400 | 25.25 | 26.75 | +1.50 | +600 | 72 | 153 | +375 | +182 |
| 09-15 | TORNTPHARM | S/PE | 13:23→15:14 | 4879.00→4855.00 | 4900 | 125 | 83.05 | 106.25 | +23.20 | +2,900 | 77 | 174 | +2,649 | +397 |
| 09-15 | LICI | S/PE | 14:35→15:09 | 392.45→391.45 | 390 | 1400 | 5.15 | 5.65 | +0.50 | +700 | 66 | 111 | +523 | +171 |
| 09-16 | PREMIERENE | S/PE | 10:38→10:54 | 893.70→899.90 | 900 | 650 | 26.15 | 20.90 | −5.25 | −3,412 | 81 | 225 | −3,718 | −516 |
| 09-16 | PATANJALI | L/CE | 10:54→11:01 | 353.45→355.60 | 355 | 1075 | 8.80 | 10.75 | +1.95 | +2,096 | 74 | 154 | +1,868 | +294 |
| 09-16 | COLPAL | L/CE | 11:23→11:30 | 1894.00→1884.00 | 1900 | 275 | 32.85 | 28.30 | −4.55 | −1,251 | 66 | 124 | −1,441 | −603 |
| 09-16 | UPL | S/PE | 11:59→12:33 | 560.65→559.00 | 560 | 1355 | 8.45 | 8.75 | +0.30 | +407 | 75 | 171 | +160 | +210 |
| 09-17 | PNBHOUSING | S/PE | 10:58→12:07 | 1107.50→1116.60 | 1100 | 650 | 20.45 | 15.10 | −5.35 | −3,477 | 72 | 170 | −3,719 | −515 |
| 09-17 | SRF | L/CE | 11:53→12:42 | 2542.20→2547.60 | 2550 | 200 | 42.45 | 46.40 | +3.95 | +790 | 69 | 131 | +591 | +127 |
| 09-18 | LODHA | L/CE | 12:19→12:59 | 1140.60→1144.20 | 1140 | 625 | 24.00 | 26.30 | +2.30 | +1,438 | 86 | 231 | +1,121 | +230 |
| 09-18 | SUPREMEIND | L/CE | 12:45→13:30 | 3462.60→3472.70 | 3450 | 175 | 67.15 | 69.00 | +1.85 | +324 | 76 | 175 | +73 | +200 |
| 09-18 | ZYDUSLIFE | L/CE | 14:18→14:21 | 1155.20→1157.40 | 1160 | 900 | 18.15 | 19.05 | +0.90 | +810 | 87 | 246 | +476 | +106 |
| 09-18 | TCS | S/PE | 14:38→15:07 | 2112.30→2105.70 | 2120 | 225 | 43.00 | 51.05 | +8.05 | +1,811 | 74 | 156 | +1,582 | +227 |
| 09-18 | BANDHANBNK | L/CE | 14:48→15:14 | 177.79→177.40 | 180 | 3600 | 1.17 | 1.50 | +0.33 | +1,188 | 59 | 71 | **+1,058 ⚠ illiquid** | **−274** |

\* KAYNES: the ATM 3550PE printed **nothing** all day; the nearest printed strike (3500PE)
traded 9 lots all day and had no print inside the 10:35→13:38 hold (last print 43 min before
entry, 226 min before exit) — there is no real price to sell at, and the post-close sweep
measured its spread at 42.7%. It is excluded from the aggregate rather than modelled: a
Black-Scholes number for a contract nobody was quoting would be fiction, and in practice a
₹13.5k lot in a 40%-wide book is a loss on entry. ⚠ BANDHANBNK 180CE: 75 lots traded all
day, only 4 traded minutes in the hold, entry print 48 min stale — the "+₹1,058" is a
₹1.17→₹1.50 print on an option that barely trades. It is kept in the 41 but is the reason
the 40-trade liquid cut is also shown (§3).

## 3. Aggregates and the split

| cut / scenario | n | wins | WR | gross | charges | spread | net | LONG (CE) n / WR / net | SHORT (PE) n / WR / net |
|---|---|---|---|---|---|---|---|---|---|
| Equity actual, 42 | 42 | 21 | 50.0% | −2,640 | 3,374 | — | −6,014 | 15 / 53% / −1,711 | 27 / 48% / −4,304 |
| Equity actual, 41 (excl. KAYNES) | 41 | 20 | 48.8% | −2,875 | 3,297 | — | −6,172 | 15 / 53% / −1,869 | 26 / 46% / −4,303 |
| Options A no spread, 41 | 41 | 22 | 53.7% | +2,211 | 3,519 | 0 | −1,308 | 15 / 60% / +4,887 | 26 / 50% / −6,195 |
| **Options B 1.47%, 41 (primary)** | 41 | 22 | **53.7%** | +2,211 | 3,519 | 9,810 | **−11,118** | 15 / 60% / **+1,022** | 26 / 50% / **−12,140** |
| Options B75 2.18%, 41 | 41 | 19 | 46.3% | +2,211 | 3,519 | 14,549 | −15,857 | 15 / 53% / −845 | 26 / 42% / −15,012 |
| Options C post-close, 41 | 41 | 14 | 34.1% | +2,211 | 3,519 | 30,574 | −31,882 | 15 / 33% / −5,266 | 26 / 35% / −26,615 |
| Options B, 40 liquid (also excl. BANDHANBNK) | 40 | 21 | 52.5% | +1,023 | 3,460 | 9,740 | −12,176 | 14 / 57% / −36 | 26 / 50% / −12,140 |

By exit type (B, 41): the 6 `eod_watchdog` runners made **+₹7,202** on options vs +₹564 on
equity (ZYDUSLIFE 09-03 +5.3k, TORNTPHARM +2.6k); the 32 `stop_loss` exits lost **−₹16,127**
vs −₹5,237; the 3 sandbox square-offs −₹2,194 vs −₹1,499. The option amplifies both tails —
the leverage is real, the edge is not.

**Equity winners vs option winners (the win rates are NOT the same thing).** Scenario B:
0 equity winners became option losers and 2 equity losers became option winners (ZYDUSLIFE
09-10 +₹63 on a −₹324 equity trade — a 0.24% adverse stock move that the put still gained on;
and the illiquid BANDHANBNK print). Under the wider scenario C, **6 equity winners flip to
option losers** (VOLTAS, BDL, UPL, SRF, SUPREMEIND, ZYDUSLIFE 09-18 — every one a +0.2–0.3%
equity move whose option gain of ₹240–₹810 is smaller than the round-trip spread) and none flip
the other way. Correlation of the equity move % with the option return % is 0.84: the option
tracks the stock, then subtracts a fixed toll. Theta is negligible at these holds (median 32
min, 11–28 days to expiry); the spread is the toll.

## 4. Capital and risk — 1 lot is not risk-sized

- **Premium outlay per lot:** min ₹4,212 (BANDHANBNK), median **₹14,599**, max **₹45,965**
  (RBLBANK 405CE, 3,175 × ₹12.05). 10 of 41 trades need more than the ₹20,000 the equity
  engine calls its capital. The delta-1 notional behind one lot is median **₹5.85 lakh** (max
  ₹12.9 lakh, BANDHANBNK) against the equity slot's ₹99k median — a lot is ~6× the size the
  engine actually trades.
- **Max concurrent outlay:** **₹1,04,275 at 12:58 IST on 09-09** (8 trades that day; the
  day's total outlay was ₹1,50,070). Against a ₹20,000 account **the options version is not
  executable as specified**: it needs ~5× the capital at peak, and even one lot at a time only
  32 of 42 trades are affordable. With 5× MIS leverage the equity engine controls ~₹1 lakh of
  stock per trade for ₹20k; option premium carries no leverage from the broker — you pay it in
  full.
- **Risk dispersion:** the equity engine sizes every trade to a ₹500 max loss (realised losers:
  median −₹474, worst −₹1,634). The option loser distribution is median **−₹1,496**, worst
  **−₹4,387** (MANAPPURAM 325PE, a 0.55% adverse stock move), and the theoretical max loss per
  trade is the full premium — median 29× and up to 92× the ₹500 the equity engine risks. The
  stopped equity losers (17) lost a median 8.3% of premium on the option (range −26% to +2%)
  for a median 0.45% adverse stock move: the stop-on-underlying still works as a stop, but the
  rupees at risk behind it are set by the lot size, not by the ATR.
- **Per-trade volatility:** σ ₹426 (equity) vs ₹1,968 (option B); mean −₹151 vs −₹271. Same
  sign, 4.6× the swing.

## 5. Framing — read before quoting any number above

1. **This is one month, n = 41 (42 taken, 6 unpriced by the 09-02/09-07 outages).** It is a
   counterfactual on a sample the registry's own protocol calls INSUFFICIENT EVIDENCE (< 6
   months). It answers "what would September have paid" — nothing more.
2. **The equity win rate in September was 48.8–50%, not 55%.** R65's full-sample rate is 53.3%
   (CI 48–59). Nothing here changes R62/R65's verdict that the signal has no measurable edge.
3. **Option win rate ≠ equity win rate.** Sign-agreement was 39/41 (B) but only 35/41 (C): a
   +0.2–0.3% equity winner is a ₹240–₹810 option gain that a 2–4% spread eats.
4. **The spread is the whole answer.** Gross the options made +₹2.2k on a month the stock lost
   −₹2.9k (leverage on the winners); every scenario with a non-zero spread is worse than the
   equity result, and the primary case is ~1.8× worse. Assumed: 1.47%-of-mid intraday
   (measured on 96 real captures), bracketed by 0% and 3.88% (measured post-close). Charges
   alone (₹3,519, with the new 0.15% STT) are already ~₹220 more than the equity legs paid.
5. **1 lot is a fixed, symbol-dependent notional.** It ignores the ₹500-risk rule, puts a
   median ₹14.6k and up to ₹46k on a single trade, and needed ₹1.04 lakh at peak on 09-09.
6. **Two contracts had no real market.** KAYNES excluded, BANDHANBNK flagged; the liquid
   40-trade cut reads −₹12,176 (B). Stock-option books at these strikes are thin: 9 of 41
   contracts traded under 1,000 lots all day.

**Verdict: REJECT as a vehicle change.** Buying 1 lot of the ATM option on this signal
converts a small, risk-capped loss into a larger, uncapped one, at several times the capital
and with worse execution. Not deployed; no engine code or parameter changed.

## Harness

Scratchpad scripts `step1_extract.py` (sqlite backup-API byte copy of `openalgo.db`,
September trade set, data-availability sweep of `historify.duckdb` / `openalgo.db` /
`option_liquidity_daily`), `step2_fetch.py` (ATM pick via `open15_option_shadow.pick_contract`
+ real 1m option bars via `services.history_service.get_history_with_auth`, read-only),
`step3_model.py` (pricing, live Zerodha charge schedule, spread scenarios), `step4_report.py`
(aggregates). The R65 trade-set construction and read-only broker-fetch pattern were reused;
the R65 sizing replica itself was not needed because the counterfactual uses the journal's
actual entry/exit timestamps rather than re-simulating the stop. Data-availability findings
for the record: `historify.duckdb` holds no option minute data (`market_data` NFO = 1,053
rows / 5 futures symbols; `fo_bhavcopy_eod` is EOD-only and ends 2026-05-29);
`openalgo.db` holds no premium series (only the `option_liquidity_daily` spread/OI scores);
the broker API serves 1m bars for any contract still in Kite's instrument list — i.e. the
current month, so **this analysis is only reproducible until the 29-SEP-26 expiry**.
