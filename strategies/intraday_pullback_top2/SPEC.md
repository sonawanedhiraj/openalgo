# Intraday Pullback Top-2 (Combined Long+Short) — Spec

**Status:** SANDBOX implementation (issue #394). Long-only precursor was #389 (closed, superseded).
**Research (all numbers, rejected variants):** [`docs/research/strategy/screener/2026-07-09_r53_loss_month_filters.md`](../../docs/research/strategy/screener/2026-07-09_r53_loss_month_filters.md).
**Canonical params:** [`config_snapshot.json`](config_snapshot.json). This doc is the human-readable rules; the JSON is the source of truth for values.

## 1. One-liner
Long-only-*style* intraday (T+0) on Indian F&O. On up-market days buy mid-strength sector-aligned
movers on a no-supply pullback-breakout; on down-market days short deep-loser names on a no-supply
bounce-breakdown. Everything flattens at 15:15. Two mutually-exclusive books share one ₹60k / 2-slot pool.

## 2. Day gate (which book runs)
Measured at **09:30 IST**: NIFTY up (>0%) → **LONG** book only; NIFTY down (<0%) → **SHORT** book only;
exactly 0 → no trading. The books never run on the same day, so they never contend for margin.

## 3. LONG book (up-days)
- **Select @ 09:30:** NIFTY up · sector index green (>0%) · stock 09:30 gain ∈ **[+1.0%, +2.5%)** · rank by gain desc → **top-2**.
- **Trigger (5m, windows 09:30–11:00 and 13:00–15:00; reuse the 09:30 picks):** red low-volume reference candle (vol ≤ each of prior 2), then a breakout candle with **vol ≥ 2.5× avg of last 6** AND **close > ref open**.
- **Fresh gate at entry:** NIFTY still ≥ +0.3% · sector still green · **nf_mom** (NIFTY intraday ≥ its 09:30 gain).
- **noreentrySL:** no further attempt on a stock after it stops out that day. Max 2 attempts/stock, margin-aware.
- **Stop:** ref-candle low, 0.3% floor. **EOD flatten 15:15.** No target/trailing/BE/partial.

## 4. SHORT book (down-days) — deep-loser sleeve
- **Select @ 09:30:** NIFTY down · sector index red (<0%) · stock 09:30 loss ∈ **(−5.0%, −3.0%]** (deep losers) · rank by most-negative → **top-2**.
- **Trigger (5m, same windows; reuse 09:30 picks):** green low-volume reference candle, then breakdown candle **vol ≥ 2.5× avg** AND **close < ref open**.
- **Fresh gate:** NIFTY still ≤ −0.3% · sector still red. **No nf_mom, no noreentrySL** (both hurt the short).
- **Stop:** ref-candle (green) high, 0.3% floor. **EOD flatten 15:15.**

## 5. Sizing & margin
Capital ₹60,000 · 2 slots · margin ₹30k/slot · notional ₹150k/slot (5× MIS) · equal-weight · MARKET orders.
Max **2 concurrent positions** (₹60k margin cap — never exceeded, days disjoint). Sizing mode `fixed`
(default) / `compound` (net capital carried forward daily) / `capped` (min(equity, base)).

## 6. Editable via UI (per-strategy config, applied at 09:00 reset)
`base_capital`, `no_trade` window, `afternoon` window, `sizing_mode`, `trade_side`. Strategy-logic
params (bands, 2.5×, stop, filters, morning window, EOD) are read-only — changing them invalidates
the backtest.

**`trade_side`** (issue #509) ∈ `both` (default, backtested) / `long_only` / `short_only`, env default
`INTRADAY_PULLBACK_TRADE_SIDE`. Enforced in `run_selection` immediately after the §2 day gate: an
excluded side is never selected, never watched, never triggers, never journals. **Because the two
books are mutually exclusive by that day gate, this is NOT a rebalance — excluding a side means the
strategy does not trade at all on the days that side would have run** (`long_only` gives up every
NIFTY-down day). A skip is recorded as `skip_reason='trade_side=…'` on `get_status()` /
`entry_breakdown()` so it stays distinguishable from a data gap. Invalid stored/env values fall back
to `both` with a WARNING — a typo must never silently dark a book.

## 7. Data pipeline (09:30 selection)
Full-universe exact 09:30 returns (not a top-N list). Source chain (deterministic, no LLM/scrape):
**aggregator → broker `get_multiquotes` → historify**, WARNING per hop. Use the live aggregator for
*today*; historify only for prior days. Prev-close on the **broker adjustment basis** (matches live LTP).
Broker session down → 09:18 smoke check **holds entries + alerts** (fail-safe, never guess).

## 8. REJECTED — do not implement
1:2 partial · breakeven-at-1R · trailing/target · vol >2.5× · 10/15m candles · static stop/ext caps ·
long band-widening · mid-strength short (−1..−2.5) · nf_mom/noreentry on short · **1pm re-selection (both sides)** · 65/35 tilt.

## 9. Mode control (aligned with the other strategies)
`INTRADAY_PULLBACK_MODE` env is the default; a persistent `strategy_mode['intraday_pullback_top2']`
row overrides it (set via the strategies-dashboard toggle / `strategy_mode_service.flip_mode`) — same
mechanism as `futures_follow` / `sector_follow`. Actual **sandbox-vs-live order routing is the
platform-global gate** (`place_order` → `resolve_effective_mode(__global__)` + `analyze_mode`), shared
by all strategies — a resolved `live` still routes to sandbox while the global gate is sandbox. `observe`
is an env-only dry-run (journal signals, place no orders).

## 9a. Resume after late boot / mid-session restart
The strategy self-heals if OpenAlgo starts after 09:30 or restarts intraday (boot daemon +
every 5m-eval tick, once past 09:30 while `not selected`):
- **Restart with prior trades today** → reconstruct side / picks / per-stock attempts / open
  positions **from the journal** (authoritative, no re-selection). Reconciled open positions are
  managed (stop + 15:15 flatten) so nothing is orphaned; prior attempts count toward max-2 so
  nothing is double-placed.
- **Late boot, never traded today** → re-select using the **historical 09:30 price** (`get_history`,
  broker `source='api'`) so the "09:30 gain" is measured at 09:30, not at boot time — then trade the
  remaining windows.
- No-chase: past breakouts during downtime are **not** back-filled at the current price; the
  aggregator only has post-restart bars, so entries resume cleanly from now forward.
- Flag `INTRADAY_PULLBACK_BOOT_RESUME_ENABLED` (default true). Caveat: a restart **after 15:15** with
  an open position relies on sandbox MIS auto-square-off (the eval tick is past its window).

## 9b. Entry-breakdown observability (why no trades)
`GET /intraday_pullback_top2/api/entry_breakdown` (also in `get_status().today_evaluation`) explains
a zero-signal day per pick: its 09:30 gain / sector / sector-return, running diagnostics (references
formed, breakouts seen, gate-blocks, slot-blocks, entries/exits) and a one-line **reason** — e.g.
"no low-volume reference candle formed", "reference formed but no ≥2.5×-volume breakout", "breakout
formed but the live NIFTY/sector gate blocked it". Live from in-memory state; a snapshot is persisted
at the 15:30 EOD summary (`intraday_pullback_eval_snapshots`) so it survives restarts and is queryable
historically (`?date=YYYY-MM-DD`). The strategy is selective (~0.7 trades/day in backtest) — many days
are legitimately zero, and this makes that explicit rather than indistinguishable from a data outage.

## 10. Deployment gate
Run in **sandbox** → paper-trade forward → **measure realized slippage per sleeve** (deep-loser shorts
are the most fragile) → only then consider `live` with small capital. Long is the validated primary;
the short is promising-but-unproven.
