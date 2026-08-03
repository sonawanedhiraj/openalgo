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

**Env defaults:** `OPEN15_ENABLED` (true) · `OPEN15_MODE` (sandbox|observe) ·
`OPEN15_VOL_MULT` (1.5) · `OPEN15_SIZING_MODE` (fixed) · `OPEN15_TOP_N` (3) ·
`OPEN15_MARGIN_PER_SLOT` (30000) · `OPEN15_LEVERAGE` (5) ·
`OPEN15_TRADE_SIDE` (both) ·
`OPEN15_TICK_CAPTURE` (true). The `armed` decision-log event records the
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
`GET /open15_vol_breakout/logs` — **self-contained log-viewer page** (session
auth, auto-refreshing during the window, date picker for history).

## 8. Tick capture (backtest replay data)
`OPEN15_TICK_CAPTURE` (default true): ticks for **the day's selected symbols
only** are persisted to `tick_logs/open15/ticks-YYYYMMDD-<pid>.jsonl`
(`{ts, symbol, ltp, volume}`, cumulative day volume; retention 365d). The full
09:15 first minute is included (all universe symbols are buffered in memory for
that one minute; on selection, only the picked symbols' buffers are flushed and
the rest discarded). This makes every armed day fully replayable: first candle +
entry window + exit for exactly the strategy's watchlist, at tick resolution —
the dataset the offline salvage analysis was missing.
