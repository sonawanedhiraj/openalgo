# R48 — Time-of-day-adjusted volume gate for the Chartink-mirror BUY rule

**Date:** 2026-07-07 · **Issue:** [#374](https://github.com/sonawanedhiraj/openalgo/issues/374) · **Verdict: REJECT as in-place gate replacement; PROMISING as a separate early-watch tier (needs its own round + engine replay)**

## Question

The BUY rule's volume gates (gates 2/8 in `services/scan_rules/fno_intraday_buy_chartink.py`)
compare **today's running cumulative volume** against SMA50/SMA200 of **full-day**
volumes. Intraday, running cumvol cannot exceed a full-day average until hours
into a trend day — the 2026-07-07 NAUKRI case (news 09:49, +3% by 09:55, first
in-house PASS at 11:35 when +10%). Does a time-of-day-adjusted gate

```
cumvol_so_far >= K × f(minute) × SMA(fullday vol)      (both SMA50 and SMA200 legs)
```

with `f(t)` = the average cumulative intraday volume curve (floor 0.005,
implementation precedent `backtest/news_event_study/simulate.py`) fire true
PASSes earlier without adding unacceptable false positives?

## Data & method

- **Data:** broker (Zerodha) historical API — the live `historify.duckdb` is
  exclusively locked by the running app, so the R43 fetch pattern was reused
  (`backtest/tod_volume_gate/fetch_prices.py`, read-only on live DBs).
  211 scanner-universe NSE equities (SCANNER_SYMBOLS minus indices):
  1m bars 2025-06-20 → 2026-07-06 (20.27M rows), daily bars 2024-06-03 →
  2026-07-06 (108,788 rows; 200-bar SMA warm-up satisfied everywhere).
- **Replay** (`backtest/tod_volume_gate/replay.py`): every symbol-day at every
  5m bar close, full 12-gate replication with **live** parameters
  (`parameters_json` NULL → gap 1.5% per `CHARTINK_RULE_BUY_GAP_PCT`, price
  100–5000, weekly ATR(21) > 5%, closed-15m RSI(14) > 50, 5m Supertrend(7,3)
  current/prev gates, running daily snapshot from 5m aggregation exactly as
  `derive_today_and_yest`). Baseline volume arm vs f(t) arm at
  K ∈ {0.75, 1, 1.25, 1.5, 2, 3}. Eval window **2025-07-01 → 2026-07-06**
  (243 trading days). First fire per (symbol, day, arm) is the event.
- **f(t) curve:** computed from the same 1m dataset (symbol-days with ≥300
  bars). f(09:19)=4.8%, f(10:19)=24.1%, f(12:29)=49.9% — heavy open, U-shape.
  Asset: `backtest/tod_volume_gate/volume_curve_2025-07_2026-07.csv`.
  (Mild in-sample caveat: the curve is estimated over the eval window itself;
  it is a mechanical market-microstructure shape, stable across the halves.)
- **Fidelity caveats** (all symmetric across arms — latency deltas unaffected):
  RSI/Supertrend computed full-series causal vs production's rolling windows;
  daily references from broker-D vs production historify-D (see golden check).

### Golden check vs today's live log (2026-07-07)

- **NAUKRI:** harness baseline first-fire **10:20** — matches the case study's
  independently-derived "crossed the 50d avg at 10:19". Variant K=1.0 fires
  **09:40 at ₹1,048 (+2.2%)** vs baseline 10:20 at ₹1,084 (+5.8%).
- **Live "first PASS 11:35" was partly a boot artifact:** the app was restarted
  at 11:30:58 IST today, so every live first-PASS today (NAUKRI, PERSISTENT,
  JUBLFOOD, TRENT, KALYANKJIL at 11:35, TITAN 11:45) is clamped by scanner
  downtime, not gate timing. The replay is the cleaner timing source for the
  case study.
- PERSISTENT (+25 min) / PGEL (+10 min) harness-vs-live diffs trace to
  historify-D vs broker-D SMA reference values on knife-edge crossings (the
  #280/#299 stored-D class); TITAN's live weekly-ATR pass (232.8 vs needed
  230.0) is a knife-edge the broker-derived ATR misses. Rule logic validates.

## Results

### 1. The lateness diagnosis is structural — confirmed

Over 243 days, the baseline rule fires on **1,913 symbol-days** (7.9/day):

| Baseline first-fire time | value |
|---|---|
| p25 / median / p75 | 11:20 / **13:30** / 14:55 IST |
| fires at/after 14:00 | 43.2% |
| fires at/after 15:00 (≤30 min of session left) | 23.4% |

On **89.8%** of PASS days the volume gate is the last gate to clear, a median
**145 minutes** after the rest of the stack. The volume gate — not RSI/ST/gap —
is the latency bottleneck, exactly as hypothesized.

### 2. K sweep — latency vs coverage vs junk

(“both” = days baseline also fires; “v-only” = added days baseline never fires;
“b-only” = true-PASS days the variant misses)

| K | fires | both | v-only | b-only | median gain (min) | entry extension at fire (variant vs baseline) |
|---|---|---|---|---|---|---|
| 0.75 | 4,469 | 1,913 | 2,556 | 0 | **140** | +2.59% vs +3.41% |
| 1.0 | 3,579 | 1,913 | 1,666 | 0 | **125** | +2.68% vs +3.41% |
| 1.25 | 2,861 | 1,788 | 1,073 | 125 | 110 | +2.81% vs +3.46% |
| 1.5 | 2,276 | 1,570 | 706 | 343 | 103 | +2.95% vs +3.53% |
| 2.0 | 1,485 | 1,159 | 326 | 754 | 70 | +3.27% vs +3.70% |
| 3.0 | 759 | 676 | 83 | 1,237 | 30 | +3.82% vs +4.03% |

K ≤ 1.0 is a strict superset of baseline (misses nothing, fires ~2h earlier —
median first-fire 09:45 vs 13:30 on true-PASS days). K ≥ 1.5 misses 18–65% of
true passes at EOD (needs cumvol ≥ K× the full-day average).

### 3. Outcome quality — the two edges of the sword

Signal-time forward returns (gross, no execution model — the scanner feeds the
engine, it is not itself the trading layer):

| Arm | n | ret→close | win% | ret→T+1 close | win% |
|---|---|---|---|---|---|
| baseline, at its own fire | 1,913 | +0.02% | 47.5% | +0.09% | 49.8% |
| K=1.0 on **both**-days (earlier entry, same days) | 1,913 | **+0.74%** | **70.0%** | **+0.82%** | 61.2% |
| K=1.0 **variant-only** (added days) | 1,666 | **−0.72%** | 25.9% | −0.77% | 34.0% |
| K=1.25 both / v-only | 1,788 / 1,073 | +0.65% / −0.92% | 67.3% / 20.4% | +0.74% / −0.87% | 59.7% / 32.1% |
| K=1.5 both / v-only | 1,570 / 706 | +0.57% / −1.01% | 64.0% / 16.4% | +0.67% / −0.84% | 58.5% / 31.3% |
| K=3.0 both / v-only | 676 / 83 | +0.22% / −1.83% | 53.8% / 3.6% | +0.26% / −1.64% | 51.9% / 20.5% |

- **The earlier entry on genuinely-confirming days is worth ~+0.7pp** per
  signal (to close AND to T+1) — the meat the current gate gives away by
  arriving at 13:30+.
- **The added days are decisively adverse at every K, in both halves**
  (split-half A/B: v-only ret→close −0.62/−0.51% at K=0.75 … −1.86/−1.81% at
  K=3.0; never positive anywhere). They are morning spike-then-fades: 80–91%
  fire before 11:00, the day still closes +1.55% above prior close on average,
  but **73% of the fires are above the eventual close**. At K=1.0, 50% never
  volume-confirm by EOD (fullday vol < SMA50); the other half confirm volume
  but the *price* stack no longer holds by then.
- Junk quality **worsens** as K rises: demanding a more extreme early
  relative-volume surge selects *harder-fading* spikes. This extends R43's
  finding (raw early volume surge is anti-selective on news smallcaps) to the
  screened F&O universe.

### 4. Net effect and load

At K=1.0 the added junk (1,666 × −0.72%) almost exactly cancels the earlier-entry
gain (1,913 × +0.72pp): all-fires mean is +0.06%/fire vs baseline's +0.02% —
a wash before any costs, with signal load nearly doubled (14.7 vs 7.9
fires/day; K=1.25: 11.8/day). There is **no K that dominates**: K ≤ 1.25 buys
latency with ~0.6–1.0 junk fires per true fire; K ≥ 1.5 starts missing the
very passes the change exists to catch earlier.

## Verdict

**REJECT replacing gates 2/8 with K·f(t)·SMA in the live rule.** The full-day
volume SMA gate is doing real *selectivity* work disguised as latency: forcing
the signal to wait until cumulative volume beats a full-day average is a de
facto "sustained volume trend day" filter, and removing it converts ~2h of
latency into a 1:1 stream of negative-EV morning fades. It also breaks
Chartink-mirror parity, the rule's stated purpose.

**PROMISING as a two-tier design (future round):** the +0.7pp/signal capture on
true-PASS days is real, split-half stable, and large. The shape that preserves
the canonical PASS while monetizing it:

- keep the existing PASS unchanged (parity + selectivity);
- add a separate **early-watch tier** at ~K=1.25–1.5·f(t) that does NOT post
  hits, but pre-arms the downstream consumer (watchlist, engine pre-subscribe,
  tighter monitoring) so the eventual confirmed PASS is acted on with less
  slippage;
- any *tradeable* early tier must first kill the morning-fade cohort — e.g.
  require price to hold above the fire level for N bars, or defer to
  10:30+ (v-only fires are 80–91% pre-11:00) — and must be validated with an
  engine replay (out of scope here).

## Sample & protocol

1,913 baseline events / 243 days — clears the ≥30-trade bar. Split-half
consistency reported inline. Gross returns only (scanner is not the execution
layer). Hand-validation: NAUKRI 2026-07-07 reconciled bar-by-bar against the
live gate-snapshot log (SMA references within 3–8% — historify-D vs broker-D).

**Operational side-finding:** today's live "NAUKRI PASS at 11:35" was clamped
by the 11:30:58 app restart — on continuous data the baseline rule would have
fired at 10:20. Live first-PASS timestamps on restart days are not gate-timing
evidence.

## Assets

- `backtest/tod_volume_gate/fetch_prices.py` — resume-safe broker-API fetcher
  (1m + daily, scanner universe) into `outputs/tod_volume_gate/prices.duckdb`
- `backtest/tod_volume_gate/replay.py` — 12-gate 5m-close replay, K sweep
- `backtest/tod_volume_gate/analyze.py` — result tables (this report's numbers)
- `backtest/tod_volume_gate/volume_curve_2025-07_2026-07.csv` — f(t) curve,
  211-symbol F&O universe, 2025-07→2026-07 (reusable)
