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
- **First candle:** built from live ticks 09:15:00–09:15:59 → open/H1/L1;
  minute volumes derived from cumulative tick volume (auction included in v0).
- **Selection @ 09:16:** gap = 09:15 open ÷ prev daily close (historify D) − 1;
  top-3 positive → LONG watch, top-3 negative → SHORT watch.
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

## 6. Flags (see docs/PARAMETER_LOG.md)
`OPEN15_ENABLED` (true) · `OPEN15_MODE` (sandbox|observe, default sandbox) ·
`OPEN15_VOL_MULT` (1.5) · `OPEN15_TOP_N` (3) · `OPEN15_MARGIN_PER_SLOT` (30000) ·
`OPEN15_LEVERAGE` (5).

## 7. API
`GET /open15_vol_breakout/api/status` — live state (selection, entries, day status).
`GET /open15_vol_breakout/api/trades?date=&limit=` — journal incl. research fields.
