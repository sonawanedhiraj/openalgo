# open15_vol_breakout — Mid-bar volume-surge breakout (Spec)

**Status:** SANDBOX (issue #425). This deployment is BOTH a strategy and a
measurement instrument — it is the one legal, untested variant left from
Round 58 (`docs/research/strategy/open15_vol_breakout/2026-07-19_opening_vol_breakout_and_options_bs.md`).
**Canonical params:** [`config_snapshot.json`](config_snapshot.json).

## 1. One-liner
In the first 15 minutes (09:15–09:30 IST), buy the top-3 pre-open gainers /
short the top-3 losers the *moment* a first-candle breakout happens **on a
volume surge that is already measurable mid-minute** — exit everything at 09:30.

## 2. Why this exists (honest context — read before touching)
Round 58 proved the bar-level version of this signal has **no honest edge**:
- The volume gate (breaking candle vol ≥ 1.5× running avg) is only knowable at
  the candle's **close**; entering at the trigger level is **look-ahead**
  (the entire published +0.38%/trade edge was this artifact).
- Honest close-entry loses −0.16%/trade. Every 1m-bar variant converges to
  ≈ zero edge minus costs.
- The intra-bar burst (level→close) averages **+0.54%**. A tick-driven entry
  *during* the surge is legal and captures an unknown fraction of it.
  **1-minute bars cannot resolve that fraction — only live ticks can.**
This sandbox run measures it with real fills. The journal is the experiment.

## 3. Rules (all real-time legal)
- **Universe:** `SCANNER_SYMBOLS` F&O stocks (indices excluded).
- **First candle (issue #502):** open/H1/L1 come from ONE batched broker quote
  snapshot taken by the `open15_first_candles` job at 09:16:00 — the quote's
  `open` is the exchange's official day open, and at 09:16:00 the running day
  `high`/`low` ARE the 09:15 candle's extremes. Selection is deferred until the
  snapshot lands (hard fail-open deadline 09:17; per-symbol fail-open to the
  tick-built candle). **Ticks are NOT a valid source for the candle** — they are
  a ~1/sec sample that begins whenever the first tick arrives, which produced
  the wrong open (MPHASIS 2026-07-31: first tick 8 s late and 3.24% below the
  real open → phantom −4.15% gap, watched as the #1 short when it was really
  #11 at −0.94%) and a systematically NARROW candle (high understated 24/24,
  low overstated 24/24 — a level easier to break than the real one). Ticks
  remain the source of the WITHIN-minute volume accumulation, which no bar can
  provide and which is the entire reason this strategy is tick-driven.
  Rollback: `OPEN15_FIRST_CANDLE_SOURCE=ticks`.
- **Minute volumes** derived from cumulative tick volume. The **09:15 minute is
  excluded from the baseline** (issue #502): it is the day's busiest minute AND
  its tick cumulative carries the pre-open auction, which inflated the baseline
  1.06×–1.67× and made the configured 1.5× gate behave like ~2.5× (zero entries
  on 3 of 4 sessions). Excluding it also removes the auction, since every later
  minute is a cumulative difference. Rollback:
  `OPEN15_BASELINE_INCLUDE_FIRST_MINUTE=true`.
- **Selection @ 09:16:** gap = 09:15 open ÷ prev daily close (historify D) − 1;
  top-3 positive → LONG watch, top-3 negative → SHORT watch.
  **Prev-close verification (issue #456):** the 09:10 arm sources prev-closes
  quote-first: ONE batched broker quote call (`get_multiquotes`; `prev_close`
  = the settled T-1 close, fetched at the moment of use) is the PRIMARY —
  its values win the merge and are recorded into the #305 broker prev-close
  registry. Symbols the call missed stay on historify-D and are cross-checked
  against the registry; divergence > `OPEN15_PREVCLOSE_DIVERGENCE_MAX_PCT`
  (default 0.05%) → the broker settled value WINS (WARNING logged), fail-open
  per symbol when no registry entry exists today. Provenance
  (`prev_close_check` incl. `from_live_quotes`) rides the `armed` event; the
  `selection` event records each pick's prev-close. Guards the 2026-07-23
  class: arm racing the daily-D resettle (#299) and reading provisional
  closes. Flags `OPEN15_PREVCLOSE_QUOTES_ENABLED` +
  `OPEN15_PREVCLOSE_REGISTRY_CHECK_ENABLED` (both default true).
- **Rolling additive watch list (issue #529, DEFAULT OFF):** when
  `rolling_watchlist_enabled` is on, every `rolling_cadence_s` (default 30 s,
  clamped 10–300) inside `[09:16, no_entry_after]` the universe is re-ranked by
  `ltp / prev_close − 1` over symbols with a live tick, and the current top-N
  gainers (LONG) / losers (SHORT) are **appended** to the watch list. **Purely
  additive** — a symbol, once watched, stays watched, so the 09:16 seed picks
  are never dropped and never re-sided. `trade_side` is honoured (an excluded
  side is never added), and a symbol with no usable breakout level is skipped.
  The re-rank is pure in-process arithmetic over state already held (the 09:16
  quote snapshot covers the whole universe, prev-closes are already fetched, and
  the LTP arrives on the service's own ZMQ SUB) — **no new broker load**.
  Rationale: the 2026-08-03 replay put the day's four biggest movers at ranks
  #22/#106/#130/#134 in the 09:16 gap ranking. ⚠ **The same study could not
  show the added names pay** (3 incremental trades, +₹162, 1 win of 3, on 4
  usable days) — this is a MEASUREMENT, which is why every row carries
  `watch_source ∈ {seed, rolling}`. Promotion waits on the #528 sample.
- **Shadowing the excluded side (issue #581, DEFAULT OFF):** when
  `shadow_excluded_side` is on AND `trade_side` is one-sided, the excluded side
  is selected, watched and triggered **exactly as the traded side is** — and
  **no order is ever placed for it**. The trigger must be decided identically or
  the two cohorts are not comparable, so the gate lives in the service
  (`_journal_shadow`, checked before anything can reach `order_placer`), never
  in the core. Rows carry `fill='shadow'`, `status='skipped'`,
  `reason='side_excluded'`, `quantity=0` (nothing was ordered) and
  `sim_quantity` = the **full slot size** a real entry would have used — NOT the
  1-lot `sim` convention, because the point is comparability with the traded
  cohort and with `parity_target`. An unaffordable contract falls back to 1 lot
  and says so (`reason='side_excluded_unaffordable'`) rather than hiding a
  second sizing convention inside one bucket. Own daily cap `shadow_max_trades`
  (default 3, clamped 0–10): shadow rows never consume the real `max_trades`
  budget, which is a real-money budget. Both **seed and rolling** additions are
  shadowed (operator decision, 2026-08-08). At the exit time the row is priced
  like any other non-traded row and **the position book is never read** — nothing
  was sent, so a non-zero quantity could only be an unrelated position, and
  acting on it would open a real square-off for a trade that never existed.
  ⚠ This strategy is **live** (real money) as of 2026-07-24; that is precisely
  why the excluded side is measured this way rather than simply switched on.
- **Entry (once per symbol, 09:16–09:29):** at tick time t inside minute m:
  `cumvol_within_m(t) ≥ 1.5 × mean(completed minute volumes since 09:15)`
  AND ltp beyond the level (>H1 long / <L1 short) → MARKET MIS immediately.
  Both facts are known at t — no bar-close information is used.
- **Exit:** flatten ALL at 09:30:00 (retry backstop 09:32). No stop, no target.
- **Sizing:** ₹30k margin/slot × 5× = ₹150k notional/trade; max 6 concurrent.
- **Modes:** `sandbox` (default — orders to virtual book) / `observe` (journal
  only, no orders). No `live` until the measurement supports it.

## 4. The measurement (what the journal answers)
Per entry: `level`, `trigger_minute`+`trigger_second`, `trigger_price`,
`entry_minute_close`, exit price. Key metrics after ~3–4 weeks (~15 signals/mo):
- **captured drift** = (trigger_price − level)/level — how much of the burst was
  already gone at the legal trigger (backtest phantom pocketed ALL of it);
- **residual edge** = trigger→09:30 return net of costs. Decision rule agreed in
  Round 58: trigger entry must beat the close entry by ≥0.4pp/trade to be a
  real strategy; otherwise final REJECT.

### 4a. Which prices the decision rule may use (issue #555)

The columns above are **decision-moment observations**, not transactions:
`trigger_price` is the tick that fired the volume gate and `opt_entry_premium`
is the option quote at that instant. Scoring the strategy on them measures the
signal, not the trade.

`entry_fill_price` / `exit_fill_price` carry what the broker actually filled
(reconciled at exit+5, retried at the next arm; `pnl_source='fill'` once both
legs report, else `quote`). **`pnl` is always the one gross number** and is
re-derived from the fills in place when they land — there is no second P&L
convention (the #552 rule). Read the pair as:

- **signal quality** — quote/tick columns, the `captured drift` metric above;
- **realisable P&L** — `net_pnl_of_row` on a `pnl_source='fill'` row;
- **slippage** — `fill − quote`, which is why the quote columns are never
  overwritten. The ≥0.4pp decision rule is a claim about *tradeable* return, so
  it must be evaluated on fill-sourced rows once enough exist.

**Charges are modelled, always.** No broker exposes per-order charges through
its API (Zerodha publishes them on the next-day contract note), so even a fully
reconciled row's net is gross-from-fills minus a modelled deduction. `broker_pnl`
records the position book's own realized figure as an independent cross-check;
a gap over ₹1 is surfaced on the logs page rather than smoothed over.

**Four P&L buckets, never summed** — `fill='real'` (money; the only bucket that
compounds), `fill='paper'` (the broker rejected the entry, #548), `fill='sim'`
(no order was ever attempted — `unaffordable` / `max_trades_cap` — priced at
1 lot), `fill='shadow'` (the side is switched off by `trade_side`, #581 — priced
at full slot size). The sim bucket answers whether the *slot capital* or the
*signal* caps the strategy; the shadow bucket answers whether the signal works
on the side we do not trade. One blended figure answers neither. Only real rows
may enter any published performance number.

**Reading the shadow bucket honestly.** Shadow rows price both legs at the quote
LTP, so their net is optimistic by roughly the round-trip spread — the same
caveat as sim and paper (§4b), and it matters more here because the whole
purpose is a long-vs-short comparison where only ONE side carries real fills.
Compare shadow shorts against the *quote-priced* long figures, or discount the
spread, before concluding anything. The decision this data feeds is whether to
set `trade_side='both'`; the July parity it must beat is short −₹2,485 at a 20%
win rate (options) / −₹143 (stock).

### 4b. Contract liquidity (issue #555)

**Read every count in LOTS, never in contracts.** Lot sizes across this universe
differ by ~30x (measured 2026-08-06: HAL 150, SAIL 4700), so the raw
`opt_*_volume` / `opt_*_oi` columns from #488 were never comparable between
contracts. On raw counts SAIL looked 26x more liquid than HAL; in lots it is the
*smaller* book, and in rupee turnover smaller by 2.2x. That inversion is the
simplest explanation for #488's own note that "every ex-ante metric ranked the
two live trades backwards" — no exotic cause required. `services/open15_liquidity.py`
holds the derivations; nothing reports a bare contract count.

**The spread is the cost nobody was measuring.** The strategy sends MARKET
orders, so it crosses the book at entry and again at exit. Same instant,
2026-08-06: HAL 0.67% of mid, SAIL **2.11%** — a ~1.3% vs ~4.2% round-trip drag.
`opt_*_bid` / `opt_*_ask` are captured at both decision moments; they arrive in
the quote response the strategy **already fetches** and were simply discarded,
so capture costs no broker call. Spread % is always of the **mid**, never the
LTP (the LTP is whichever side last traded, so quoting against it makes the same
book look wider or narrower depending only on who traded last). `opt_tick_size`
travels with the contract because it is not constant (0.05 and 0.01 both
observed), and a spread is only comparable across contracts in ticks.

⚠ **Spread cost is reported, never deducted** (operator decision, 2026-08-06).
Sim and paper rows price both legs at the quote LTP, so **their net P&L is
optimistic by roughly `spread_cost_inr`**; real rows use broker fills, where the
spread is already inside the fill price and the figure is a counterfactual.
Keeping it out of `pnl` preserves the #552 single convention and keeps every
previously-written row comparable.

`opt_liquidity_path` stores per-minute `{m, v, oi}` over the hold, built from the
1m bars the option-shadow already fetches (`volume` and `oi` are on every bar —
the historical endpoint is called with `oi=1` — and were being discarded). Its
value is *direction*: whether open interest was **building or unwinding** while
the position was held, which two endpoint snapshots structurally cannot show.
Per-bar `v` is incremental and may be summed; the quote's `volume` is cumulative
for the day and must not be.

**Nothing gates on any of it** — the #488 rule, restated. R58 and #488 both
showed that inventing a threshold before the data supports one makes this
strategy worse.

**Available but NOT captured**, should a later round want it: Kite's `/quote`
also returns `average_price` (day VWAP), `last_trade_time` (quote staleness),
`buy_quantity`/`sell_quantity` (whole-book totals — note OpenAlgo's
`totalbuyqty` is only the sum of the 5 visible levels: 750 vs 105,150 for HAL at
the same instant), `oi_day_high`/`oi_day_low`, circuit limits, the
**`low/high_limit_price_protection` band** (NSE rejects MARKET orders outside
it — execution safety, not research), and per-level `orders` counts. All are
dropped by OpenAlgo's shared broker mappers, so capturing them means changing a
mapper every broker and every quote consumer uses. The 5-level book is reachable
today via `depth_service.get_depth` at one extra REST call per contract per
moment. The WS feed is **not** a source: open15 subscribes `NSE_<sym>_LTP`
topics only, and LTP mode carries neither depth nor OI.

## 5. Ops constraints (load-bearing)
- **OpenAlgo must be running before 09:15 IST.** The arm job runs 09:10; a boot
  after 09:15:30 marks the day `skipped_late_boot` with a loud WARNING —
  the first candle cannot be reconstructed after the fact.
- Tick source: own ZMQ SUB on the proxy bus (5555) — additive, touches no
  scanner code; processes ticks only 09:14:50–09:30:05.
- Same-day flatten means no overnight risk; kill = set `OPEN15_ENABLED=false`
  and restart (or flip `OPEN15_MODE=observe` for signals-only).

## 6. Config (UI-editable) + flags (see docs/PARAMETER_LOG.md)
**UI-editable** (settings panel on `/open15_vol_breakout/logs`, or
`GET/POST /open15_vol_breakout/api/config` — stored in the `open15_config` row,
NULL field = env default; **applies at the next 09:10 arm**):
- `margin_per_slot` (₹, capital per trade slot; 5k–500k)
- `sizing_mode` — `fixed` (same base every day) | `compound` (base +
  cumulative realized research P&L from `open15_trades`, floored at 25% of
  base so a drawdown can shrink but never zero the strategy)
- `vol_mult` — the volume-surge filter (1.0–5.0×)
- `trade_side` (issue #503) — `both` (default) | `long_only` | `short_only`.
  Gates the 09:15 **selection**: an excluded side is never picked, so it is
  never watched, never triggers and never journals a row (same shape as
  `top_n`). The parity targets in `config_snapshot.json` are both-sides
  numbers, so a one-sided day is not comparable to them — the logs page flags
  it.
- `shadow_excluded_side` (issue #581) — `false` (default) | `true`. Shadow-logs
  the side `trade_side` excludes, per §3. Meaningless (and disabled in the UI)
  when `trade_side` is `both`, since nothing is excluded — the server derives
  the same answer independently in `shadow_side_for`, so the greyed-out control
  is the explanation, not the enforcement.
- `shadow_max_trades` — daily cap on shadow rows. Default `3`, **clamped 0–10**
  server-side. `0` is legal and means "shadow nothing". Independent of
  `max_trades`: a shadow row places no order, so it must not spend a real-money
  slot, and this cap is what bounds the per-trigger broker quote calls the tick
  thread does for it.
- `rolling_watchlist_enabled` (issue #529) — `false` (default) | `true`. The
  rolling additive watch list described in §3.
- `rolling_cadence_s` — how often the re-rank runs, in seconds. Default `30`,
  **clamped 10–300 server-side** on both the env read and the saved row, so
  neither a bad `.env` value nor a hand-crafted POST can set a 1-second re-rank
  (a hot loop over ~211 symbols on the tick thread) or a cadence longer than
  the entry window itself.
- `rolling_top_n` — movers appended per side per cycle. Default `3`, clamped
  1–10. Note this is INDEPENDENT of `top_n` (the 09:16 seed count).

**Env defaults:** `OPEN15_ENABLED` (true) · `OPEN15_MODE` (sandbox|observe) ·
`OPEN15_VOL_MULT` (1.5) · `OPEN15_SIZING_MODE` (fixed) · `OPEN15_TOP_N` (3) ·
`OPEN15_MARGIN_PER_SLOT` (30000) · `OPEN15_LEVERAGE` (5) ·
`OPEN15_TRADE_SIDE` (both) ·
`OPEN15_SHADOW_EXCLUDED_SIDE` (**false**) · `OPEN15_SHADOW_MAX_TRADES` (3) ·
`OPEN15_ROLLING_WATCHLIST_ENABLED` (**false**) · `OPEN15_ROLLING_CADENCE_S`
(30) · `OPEN15_ROLLING_TOP_N` (3) ·
`OPEN15_TICK_CAPTURE` (true) · `OPEN15_TICK_CAPTURE_UNIVERSE` (true).
The `armed` decision-log event records the
effective day-config (incl. `config_source: ui | env_defaults`), so every day's
sizing/filter is auditable.

## 7. API / UI
`GET /open15_vol_breakout/api/status` — live state (selection, entries, day
status, last 100 decision-log events), plus `watch_stats` + `vol_needed`: the
**running** max cumvol/baseline ratio per selected symbol, readable DURING the
window (issue #524). `max_vol_ratio` is `None` until the symbol's first
in-window tick — blank means "no data", not a 0.0 ratio — and freezes at entry
for a symbol that triggered (its max IS its ratio at trigger). It is final at
the entry cutoff either way, since tracking stops there. `vol_mult` on this
endpoint now also reports the **effective** multiplier (day config, falling back
to env) rather than the raw env read, so it can no longer disagree with the
threshold the run actually gated on.
`GET /open15_vol_breakout/api/trades?date=&limit=` — journal incl. research fields.
`GET /open15_vol_breakout/api/decision_log?date=` — the 15-min decision timeline
(armed → selection+gaps → entries with trigger detail → exits → `watch_stats`
for every selected symbol → per-watch no-entry near-miss stats → summary with
captured-drift). Today is served live from memory; past days from the
`open15_day_logs` snapshot (written at 09:30 and 09:35).

The `watch_stats` event (issue #524) carries `{stats: {symbol: {max_vol_ratio,
max_vol_ratio_beyond, level_broken, entered}}, needed}` for **every** selected
symbol, entered ones included — `no_entry` covers non-entered symbols only, so
before #524 an entered symbol's max was never published at all. `no_entry`
keeps precedence when both are present, so pre-#524 stored days parse
unchanged. `needed` is the day config's `vol_mult` (the multiplier that
actually gated entries), not the raw env default. The `max vol×` column shows
the peak-**anywhere** ratio but colours on `max_vol_ratio_beyond` — the gate is
`beyond and cum_in_min >= vol_mult*baseline`, so a peak-anywhere number can sit
above the threshold on a symbol that correctly never entered (issue #525).
The `entry_shadow` / `exit_shadow` events (issue #581) carry the same fields as
their traded counterparts plus `fill='shadow'` and the reason, under **distinct
event names** — the digest sums events by name, so folding them into
`entry`/`exit` (or into the sim events) would silently merge buckets that exist
precisely to be read apart.
The `watchlist_add` event (issue #529) carries `{symbol, side, pct_change,
rank, watch_size, at, shadow}` — one per rolling addition, so a day is replayable
from its own log and `watch_size` is auditable as monotonically non-decreasing.
The effective rolling config rides the `armed` event
(`rolling_watchlist_enabled` / `rolling_cadence_s` / `rolling_top_n`). A day
with NO such events is either a pre-#529 day or a day the feature was off.
`GET /open15_vol_breakout/logs` — **self-contained log-viewer page** (session
auth, auto-refreshing during the window, date picker for history). Reachable by
clicking from `/strategies` → the open15 card (its `console_url`). Carries the
config form (including the rolling cadence/top-N/on-off inputs), a **Rolling
watch-list** panel listing each addition, and a seed-vs-rolling `source` column
on the selection-outcome table.

## 8. Tick capture (backtest replay data)
`OPEN15_TICK_CAPTURE` (default true) is the master switch; ticks are persisted
to `tick_logs/open15/ticks-YYYYMMDD-<pid>.jsonl` (`{ts, symbol, ltp, volume}`,
cumulative day volume; retention 365d).

`OPEN15_TICK_CAPTURE_UNIVERSE` (default true, issue #528): **every universe
symbol's** ticks are written across the whole processing window (09:14:50 →
`exit_time`+5s), not just the day's picks. This makes the strategy's own entry
window replayable for symbols outside the 09:16 gap ranking — the prerequisite
for testing watch-list changes such as adding intraday top gainers (on
2026-08-03 the day's four biggest movers ranked #22/#106/#130/#134 on the 09:16
gap list). No extra broker load: the ticks already arrive on the service's own
ZMQ SUB and are parsed before the filter, so only the disk write changes
(~211 symbols ⇒ ~120k ticks/day ≈ 10 MB/day).

Set it `false` for the pre-#528 behaviour: **selected symbols only**, with the
09:15 first minute buffered in memory for all universe symbols and, on
selection, only the picked symbols' buffers flushed and the rest discarded.

Capture is instrumentation, never trade logic — a writer failure is logged and
swallowed inside `_handle_raw` so it can never cost an entry. The `armed`
decision-log event and `/api/status` both report `tick_capture` and
`tick_capture_universe` so the day's capture mode is auditable.
