# R38 — NSE In-Play Intraday Momentum (IPM) — Specification

> Source: Dheeraj uploaded `R38_inplay_momentum_spec.pdf` on 2026-06-28.
> Status: **draft for backtest** · Benchmark gate: must clear V_BLD_B (Sharpe ≳ 1.41,
> green months ≳ 70%, payoff ≳ 1.67)

---

## 1. Thesis

Direct port of Pradeep Bonde's "in-play" detection, decomposed into two orthogonal
signals:

- **9M-volume → abnormal *participation*.** "Where is the crowd?" Ported as `RVOL`
  (relative volume) + traded value, not a raw share count (a flat share count is
  price-biased and skews to cheap high-float names).

- **60 new highs in <3 min → abnormal *velocity*.** "Is it running *now*?" Ported as a
  **new-intraday-high burst count** and an **ATR-normalized thrust**.

Edge premise: a name showing *simultaneously* abnormal participation AND velocity,
aligned with intraday trend, continues for a measurable intraday horizon. This is a
continuation/ignition play, not mean-reversion.

**Why F&O universe, not cash:** NSE cash circuit bands (2/5/10/20%) truncate exactly
the 30–40% vertical days Bonde trades. The F&O stock list is liquid, largely unbanded
intraday, and carries the institutional flow. Trade that universe (or index/stock
futures) to make the signal portable.

---

## 2. Universe

- Current NSE F&O stock constituents (record the as-of date — F&O list changes are a
  survivorship source).
- Optional second sleeve: NIFTY / BANKNIFTY futures.
- **Exclude:** names in F&O ban period; the name's own scheduled-results date (event
  filter, per the volume-surge critique); optionally flag expiry days.

---

## 3. Data

- **1-minute OHLCV per symbol** (Kite historical). *Distinct from the daily F&O
  bhavcopy — intraday signals need minute bars.*
- **Trailing ≥20 sessions** of minute data per symbol for the time-of-day `RVOL`
  baseline.

---

## 4. Gates (ALL must pass)

1. **Time gate** — no entries 09:15–09:30 (skip first 15 min); no new entries after
   14:45; force-flat 15:15.

2. **RVOL gate** — `cumVol(T) / median(cumVol(T) over last 20 sessions at the same
   minute) ≥ RVOL_MIN` (default 2.5). Self-normalizing per stock; kills the price bias.

3. **Liquidity gate** — cumulative traded value ≥ `TURNOVER_MIN` (₹ cr) so the name
   is actually tradable at size.

4. **Velocity gate** (either sub-signal):
   - *New-high burst:* count of last `NHB_WINDOW` (15) one-min bars whose high >
     running session high ≥ `NHB_MIN` (5); **or**
   - *Thrust:* return over last `THRUST_WIN` (3) min ≥ `THRUST_K × ATR%`.

5. **Trend filter** — price > VWAP AND VWAP slope ≥ 0 (long sleeve; inverse for
   short). Echoes the VWAP/EMA filter from the pullback critique.

6. **Pullback entry (optional, Bonde structure)** — after the gate fires, wait for one
   low-volume pullback bar, enter on the next bar that breaks the pullback bar's high on
   ≥ `PB_VOL_X × the pullback bar's volume`. Mirrors his "revisit pre-market low, then
   enter" tactic.

---

## 5. Entry / sizing

- Enter at close of the confirmation bar (model slippage explicitly — fast vertical
  moves slip).
- One position per symbol per day; max `MAX_CONCURRENT` (default 5).
- Size from risk: `qty = (RISK_FRAC × equity) / stop_distance`.

---

## 6. Stops & exits

- **Initial stop:** `entry − ATR_MULT × ATR(14, 1-min)` (long). ATR-based, not an
  arbitrary tick stop.
- **Scale (Bonde's rule):** on `+SCALE_TGT` within `SCALE_BARS`, sell 80%, trail the
  remaining 20% on VWAP-loss or a chandelier stop.
- **Time stop:** force-flat at the close-flat time.
- **Daily circuit breaker:** halt new entries for the day after `DAILY_LOSS_CB`
  (e.g. −2R cumulative).

---

## 7. Costs (NSE intraday) — configurable, must be tuned

Round-trip = brokerage + STT (sell-side) + exchange txn + GST + stamp (buy) + SEBI +
**slippage**. Equity-intraday vs futures differ — keep them as separate config blocks.
Given the BS-premium lesson (synthetic underpriced real entry by ~38%), bias
costs/slippage **conservative**.

---

## 8. Validation

Report and gate against **V_BLD_B**: Sharpe, % green months, payoff ratio, plus win
rate, avg R, max DD, exposure, trade count, and a by-month table. **Reject if
qualifying trades < `MIN_TRADES`** — this is the explicit guard against the R37
failure mode where near-mutually-exclusive gates collapsed the sample to single digits.

---

## 9. Calibration protocol (do this before trusting any result)

The defaults in the harness are placeholders. Fit `RVOL_MIN`, `NHB_MIN`, `THRUST_K`,
`ATR_MULT` against the distribution of *your own* historical in-play movers (deep
dive), with a walk-forward / out-of-sample split. If tightening gates to lift Sharpe
also collapses trade count → that's the R37 trap, not an edge.

---

## 10. Known risks

Cash circuit bands (→ use F&O), minute-data gaps/auction periods, F&O-list
survivorship, and slippage on ignition bars. Model the last one pessimistically.
