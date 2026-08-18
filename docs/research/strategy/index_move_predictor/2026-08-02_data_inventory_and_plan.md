# Index Move Predictor — Data Inventory, Design, and Backtest Plan

**Date:** 2026-08-02
**Status:** RESEARCH PLAN (nothing built, nothing deployed)
**Author:** Claude Code session

---

## 1. What this is

A plan to build a **NIFTY index directional-move predictor** on the data OpenAlgo
already collects, plus the protocol to backtest it and measure its accuracy honestly.

Two things are deliberately kept separate throughout:

1. **The predictor** — a calibrated probability that the index moves up over a
   defined horizon. Judged on *accuracy and calibration*, not P&L.
2. **The trading application** — what you do with that probability. Judged on P&L
   after real costs.

Conflating them is how the project has produced false positives before (R58: an
"edge" that was an entry-timing artifact). Predictor first, application second.

### Why this is worth building

`futures_follow_cap50` is the live leveraged sleeve and its honest caveat is
recorded in `CLAUDE.md`: the signal does **not** predict NIFTY direction
(hit-rate 53.4%, corr 0.295) — the return is leveraged broad-market drift. A
working direction predictor is the missing piece for that sleeve, and it can be
applied as a **gate** (skip/size-down entries on predicted-down days) without
building a new strategy from scratch. That is the highest-value first application
and it is measurable against a live baseline.

---

## 2. Data inventory — verified, not assumed

All figures below were queried directly from a snapshot copy of
`db/historify.duckdb` (4.3 GB) taken 2026-08-02. The live app holds the file
open, so cross-process DuckDB reads fail on Windows; a file copy works and is the
working method for this research (see §7.1).

### 2.1 Historical bars — `historify.duckdb` → `market_data`

| Interval | Rows | Symbols | Range |
|---|---|---|---|
| `1m` | 41,950,133 | 238 | 2024-01-01 → 2026-07-31 (641 trading days) |
| `D` | 164,863 | 243 | 2022-01-03 → 2026-07-31 |

Exchange split: `NSE` 39.7M, `NSE_INDEX` 2.40M, `NFO` 1,053 (negligible).

**Index 1m coverage** (the prediction targets and the sector features):

| Symbol | 1m range | 1m bars | Daily range |
|---|---|---|---|
| NIFTY | 2024-01-01 → 2026-07-31 | 239,183 | 2022-01-03 → 2026-07-31 |
| BANKNIFTY | 2024-01-01 → 2026-07-31 | 239,198 | 2022-01-03 → 2026-07-31 |
| FINNIFTY | 2024-01-01 → 2026-07-31 | 239,199 | 2022-01-03 → 2026-07-31 |
| NIFTYAUTO / FMCG / IT / METAL / PSUBANK / PVTBANK | 2024-01-01 → 2026-07-31 | ~239,185 each | 2022-01-03 → 2026-06-04 |
| MIDCPNIFTY, NIFTYNXT50 | 2025-12-01 → 2026-07-31 | 61,860 | 2024-01-01 → 2026-07-31 |
| NIFTYPHARMA / REALTY / MEDIA / ENERGY / INFRA / and 6 more | **2026-04-27 → 2026-05-29 only** | 8,625 each | mostly to 2026-06-04 |

NIFTY 1m is clean: 375 bars/day, span 09:15 → 15:29, 641 distinct days.

**Usable sector 1m set is 6 indices** (AUTO, FMCG, IT, METAL, PSUBANK, PVTBANK)
plus BANKNIFTY/FINNIFTY. The other 11 sector indices have a one-month 1m window
and are **not usable** as intraday features without a backfill.

**Constituent 1m coverage** (the breadth engine):

| Coverage bucket | Symbols |
|---|---|
| Full (600+ days) | 147 |
| 300–599 days | 4 |
| 100–299 days | 61 |

Per-day distinct NSE symbols with 1m bars: 147 (2024-03), 151 (2025-01, 2025-09),
212 (2026-03), 211 (2026-07). **The universe grew mid-sample.** Any breadth
feature must be a *fraction* of that day's available universe, never a raw count,
or the model will learn a 2026 regime shift that is a data artifact. This is a
hard requirement, not a nicety.

### 2.2 Options / open interest — EOD

| Table | Rows | Symbols | Range | Fields |
|---|---|---|---|---|
| `index_options_eod` | 1,637,499 | NIFTY (1,095 days), BANKNIFTY (1,076 days) | 2022-01-03 → **2026-06-04** | OHLC, settle, underlying, volume, **oi**, lot_size |
| `fo_bhavcopy_eod` | 4,662,672 | 218 stocks | 2024-01-01 → **2026-05-29** | OHLC, settle, volume, **oi**, lot_size |

This is the strongest under-used asset in the repo: 4.5 years of strike-level
NIFTY/BANKNIFTY option OI. **Chain density is high across the whole span**, which
is what makes the `/tools` reconstruction in §3.5 viable:

| Year | NIFTY days | Avg strikes/day | Avg expiries/day | BANKNIFTY strikes/day |
|---|---|---|---|---|
| 2022 | 248 | 151.6 | 14.0 | 137.9 |
| 2023 | 246 | 109.5 | 13.7 | 102.2 |
| 2024 | 249 | 126.4 | 13.7 | 116.7 |
| 2025 | 249 | 123.8 | 10.5 | 153.5 |
| 2026 (to 06-04) | 103 | 141.5 | 7.4 | 178.9 |

Spot check 2026-06-04: 817 rows, 149 strikes, 8 expiries; nearest expiry
(2026-06-30) carries CE OI 64.60M vs PE OI 66.66M ⇒ **PCR 1.032**. A full chain,
not a stub.

Source split matters for one column: `bhavcopy_legacy` (2022-01-03 → 2024-07-05,
927,186 rows) has `underlying` **100% NULL**; `bhavcopy_udiff` (2024-07-08 →
2026-06-04, 710,313 rows) has it fully populated. For the legacy era, take spot
from the NIFTY daily bar in `market_data` — which covers 2022-01-03 onward, so
there is no actual gap.

**Both are stale** — `index_options_eod` stops 2026-06-04, `fo_bhavcopy_eod`
stops 2026-05-29. Neither is on the boot/periodic convergence loop that keeps
`market_data` fresh. Refreshing them is a prerequisite work item (§7.2), not a
detail.

### 2.3 India VIX

`INDIAVIX` `NSE_INDEX`, **daily only**, 607 rows, 2024-01-01 → **2026-06-12**.
No 1m series. Also stale.

`market_regime_service` documents this explicitly: it reads `REGIME_VIX_FALLBACK`
from env because "`INDIAVIX` quote isn't on the WS subscription list yet." VIX is
the single most obviously predictive input we do **not** have live. Adding
`INDIAVIX` to `SCANNER_SYMBOLS` and backfilling the daily series is cheap and
high-value.

### 2.4 Tick data

`tick_logs/` — 4.8 GB, 30 files, `ticks-YYYYMMDD-*.jsonl`, format
`{"ts","symbol","ltp","volume"}` at ~sub-second cadence.

**Constituents only — no index symbols in the tick logs.** Useful for realistic
execution modelling of a *stock-leg* application and for validating that 1m bars
match the tape, but it cannot serve as the index price series. Retention is ~30
days, so it is not a training source.

### 2.5 Sidecar / operational data (`db/openalgo.db`)

| Table | Rows | Relevance |
|---|---|---|
| `market_intel` | 10,068 (`news` 10,067, `regime` 1) | News headlines 2026-06-01 → 2026-07-31, `{source,title,link,published_raw,dedup_hash}`. **No sentiment score, no symbol tagging, 2 months only.** |
| `scan_results` | 8,148 | In-house scanner hits — usable as a live breadth/momentum proxy from ~2026-06 onward |
| `scan_cycle` | 9,851 | Chartink + in-house cycle audit |
| `data_health_check` | 3,003 | Feed-freshness per universe — the gate for "is today's prediction trustworthy" |
| `backtest_trades` | 67,616 | Prior rounds' trade-level output |
| `market_holidays` | 31 | Trading-day calendar (already wired into `data_freshness_service.is_trading_day`) |

`market_intel.regime` has exactly **1 row** — the regime classifier is a scaffold
that was never put on a schedule. It is not a data source today.

### 2.6 Honest summary of gaps

| Gap | Impact | Fix cost |
|---|---|---|
| No intraday VIX; daily VIX stale 7 weeks | Loses the best volatility-regime feature | Low — add to `SCANNER_SYMBOLS` + daily backfill |
| `index_options_eod` stale 2 months | No recent PCR/max-pain features | Low–medium — bhavcopy fetcher exists |
| 11 of 17 sector indices lack 1m history | Sector-rotation breadth limited to 8 indices | Medium — 1m backfill via historify pipeline |
| Universe grew 147 → 211 mid-sample | Breadth features must be fractional | Zero — a modelling rule |
| No intraday OI (option OI is EOD only) | No intraday positioning feature | High — needs new live capture; WS *does* carry `oi` per tick for F&O |
| News has no sentiment/entity tagging, 2 months | Not usable as a training feature | High |
| 641 trading days total | Statistical power is the binding constraint (§4) | Cannot be fixed — 1m starts 2024-01-01 |

---

## 3. Tool inventory — what the platform already gives us

**Live market data**
- WebSocket proxy (port 8765) with LTP / Quote / **Depth (5-level)** modes; ZeroMQ
  bus on 5555. Zerodha adapter carries `oi`, `oi_day_high`, `oi_day_low` per tick
  for F&O instruments — intraday OI is *reachable* without new broker work.
- `MultiIntervalAggregator` (`services/bar_aggregator.py`) — in-process 1m/5m/15m
  bars for every subscribed symbol, with `replay_bars()` for gap healing. NIFTY,
  BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50 are all already in `SCANNER_SYMBOLS`,
  so index bars are live in memory today.
- `services/quotes_service.get_quotes`, `depth_service`, `market_data_service`.

**Options analytics — the `/tools` suite (see §3.5, this is the important one)**

**Historical / backtest**
- `historify_service` + `database/historify_db.py` (incremental download, upsert,
  `get_ohlcv`), `scanner_universe_backfill`, `sector_follow_*_backfill`.
- `backtest/run_backtest.py` (engine replay), `backtest/inhouse_scanner/`
  (5-stage harness: forward returns → aggregate → execution → robustness →
  portfolio OOS — **this is the template to reuse**), `backtest/news_event_study/`,
  `backtest/options_open15/`.
- `services/backtest_service.py` + `database/backtest_db.py` (`backtest_runs`,
  `backtest_trades`).

**Execution & safety (for a later live phase)**
- `strategy_mode` per-strategy Live/Sandbox routing via `resolve_order_mode`;
  `strategy_runtime_override` self-expiring pause; sandbox ₹1Cr book;
  `signal_review_service` LLM veto; `data_freshness_service` pre-entry gates;
  Telegram alerting via `notification_service`.

### 3.5 The `/tools` Options Suite — live-only as built, but historically reconstructable

`/tools` (`frontend/src/pages/Tools.tsx`) exposes 12 analytics. For an index-move
predictor these are the most directly relevant instruments in the platform, so
their data provenance matters more than any other tool group.

| Tool | Route / service | Data source as built | Historically reconstructable? |
|---|---|---|---|
| Option Chain | `/optionchain` · `option_chain_service` | live `get_multiquotes` | — (it *is* the chain) |
| OI Tracker (CE/PE OI, **PCR**) | `/oitracker` · `oi_tracker_service.get_oi_data` | live chain | **Yes** — OI per strike is in `index_options_eod` |
| **Max Pain** | `/maxpain` · `oi_tracker_service.calculate_max_pain` | live chain | **Yes** — algorithm is pure OI arithmetic (docstring documents it) |
| **GEX Dashboard** (OI walls, net GEX) | `/gex` · `gex_service` | live chain + Black-76 | **Yes** — `GEX = gamma × OI × lot_size`; all three columns present |
| **IV Smile** (ATM IV, skew) | `/ivsmile` · `iv_smile_service` | live chain + Black-76 | **Yes** — invert settle prices |
| **Vol Surface** (strikes × expiries) | `/volsurface` · `vol_surface_service` | live `get_multiquotes` | **Yes** — 7–14 expiries/day in the table |
| Option Greeks / IV history | `/ivchart` · `iv_chart_service` | `get_history` + Black-76 | Partly — broker history thins for expired contracts |
| OI Profile (daily OI change) | `/oiprofile` · `oi_profile_service` | live chain + daily `get_history` | **Yes** — day-over-day OI diff from the table |
| Straddle Chart / Straddle PnL | `/straddle`, `/straddlepnl` | live | Yes, for ATM straddle price |
| Strategy Builder / Portfolio | `/strategybuilder` | user-defined | N/A |
| Screener Comparison | `/screener-comparison` | `scanner_comparison` table | Already persisted |

**The finding:** as implemented, every one of these is a **live, on-demand
snapshot — none persists history**, so none can be backtested directly. But the
underlying maths is reconstructable from `index_options_eod` **using the same
library the tools themselves use**. `services/option_greeks_service.py` calls
`opengreeks.black76` (Rust core), whose signature is
`implied_volatility(price, F, K, r, t, flag)` — verified working this session:

```
ATM: price 150, F=K=24000, r=6.5%, t=0.02y  →  IV 11.09%, delta 0.5025, gamma 1.058e-3
```

So the pipeline is: `index_options_eod` settle price → Black-76 IV → greeks →
GEX / skew / surface, replayed for every trading day 2022-01-03 → 2026-06-04.

Two caveats, both important:

- **Direction of inversion is what keeps this honest.** Cross-cutting finding R36
  (synthetic Black-Scholes is systematically optimistic for option *buying*)
  applies to generating *prices from an assumed IV*. Here we go the other way —
  real settle price → implied IV — and use IV only as a **feature**, never to
  price a fill. If an options *execution* leg is ever tested, it must use the real
  settle column.
- **EOD only.** This reconstruction gives prior-day-close positioning, available
  as a lagged feature at the 09:45 decision point. It does **not** give intraday
  GEX or intraday PCR. Getting those means persisting live snapshots from these
  services going forward — a small change (they already compute it; nothing writes
  it down) that would start accumulating the intraday options dataset we lack.
  Worth doing now so the data exists in six months, but it cannot backfill.

**Verdict:** everything needed for a *bar-resolution daily-and-intraday* predictor
exists, **and** a full daily options-positioning feature set is reconstructable
from data already on disk. Only *intraday* options positioning is genuinely
missing, and only that requires new capture.

---

## 4. Baselines — what "accurate" has to mean

Measured on NIFTY, 2024-01-01 → 2026-07-31:

| Horizon | n | Mean | SD | Up-rate (base) |
|---|---|---|---|---|
| Daily close→close | 640 | +0.0215% | 0.866% | **52.34%** |
| 09:45 → 15:20 (rest of day) | 636 | −0.0099% | 0.607% | **49.37%** |
| 11:15 → 15:20 (afternoon) | 636 | −0.0103% | 0.503% | **46.86%** |
| 09:16 → 09:45 (opening drive) | 639 | −0.0011% | 0.241% | 52.58% |

Absolute move, 09:45 → 15:20: p25 0.149%, **median 0.326%**, p75 0.625%, p90 0.967%.

**Three consequences that shape the whole design:**

1. **The daily horizon has a 52.34% drift floor.** A model that predicts "up"
   every day scores 52.34%. Reporting 54% daily accuracy would be nearly
   meaningless. The intraday horizons are near coin-flip (49.4% / 46.9%), which
   makes accuracy a *clean* metric there. **Prefer the intraday horizon for the
   primary target**; report daily against the drift baseline, never against 50%.

2. **Statistical power is the binding constraint.** With n≈640 independent days,
   the standard error on an accuracy estimate is √(0.25/640) ≈ **1.98pp**. To
   claim a real edge at 2σ you need roughly:
   - intraday: **≥ 54%** (vs ~50% base)
   - daily: **≥ 56.3%** (vs 52.34% base)

   Anything between 50% and 54% is inside the noise band and must not be called
   an edge. Splitting the sample into train/test costs power directly — a 70/30
   split leaves n≈190 test days and an SE of 3.6pp, which needs ≥57% to clear 2σ.
   **This is the number that will kill most variants, and it should be stated in
   the round's conclusion whether it passes or fails.**

3. **The cost hurdle is small but the edge is smaller.** NIFTY futures round-trip
   is ~₹530/lot on ~₹18L notional ≈ **0.03%** (per `CLAUDE.md`). Expected value
   per trade at accuracy p on a mean absolute move E|r| ≈ 0.45%:

   | Accuracy | Gross EV | Net of 0.03% |
   |---|---|---|
   | 52% | 0.018% | −0.012% (loses) |
   | 54% | 0.036% | 0.006% (breakeven) |
   | 57% | 0.063% | 0.033% |
   | 60% | 0.090% | 0.060% |

   Trading *every* day at 54% accuracy earns nothing. **The design must therefore
   be a calibrated probability with a confidence threshold, trading only the
   high-conviction tail** — not a daily long/short flip. Accuracy conditional on
   the top confidence decile is the metric that matters commercially; overall
   accuracy is the metric that matters scientifically. Report both.

---

## 5. Predictor design

### 5.1 Target

**Primary (T1):** sign of NIFTY return from 09:45 IST to 15:20 IST, same day.
Chosen because the base rate is ~coin-flip, the decision point (09:45) is late
enough for real intraday features to exist and early enough to trade, and 15:20
matches the existing `futures_follow_cap50` decision minute.

**Secondary (T2):** sign of NIFTY close-to-close daily return, decided at 15:20
(i.e. an overnight-carry call). This is the target that directly gates
`futures_follow_cap50`.

**Tertiary (T3, regression):** magnitude bucket of |return| (low / normal / high),
which is a *much* easier target than direction and is independently useful for
position sizing. Volatility is more predictable than direction — expect this one
to work even if T1/T2 fail, and do not let its success be reported as directional
edge.

### 5.2 Feature families — all computable from verified data

| # | Family | Features | Source | Available at 09:45? |
|---|---|---|---|---|
| F1 | **Breadth** (the flagship) | % of universe above VWAP / above prior close / above 20d MA; advance-decline ratio; % making new intraday highs; net-breadth momentum over last 15/30 min | 147–211 stock 1m bars | Yes |
| F2 | **Sector dispersion** | cross-sectional SD of the 8 usable sector-index returns; leader/laggard spread; banking-vs-IT spread (defensive-vs-cyclical rotation); count of sectors green | 8 sector 1m series | Yes |
| F3 | **Index microstructure** | opening gap; first-15min range as % of ATR; 09:15 candle position; VWAP distance; realized vol of last 30 1m bars; overnight gap fill state | NIFTY/BANKNIFTY 1m | Yes |
| F4 | **Volume/participation** | index and constituent cumulative volume vs same-minute 20d average; volume-weighted breadth | 1m volume | Yes |
| F5 | **Volatility regime** | 5/10/20d realized vol; vol-of-vol; ATR percentile; **India VIX level and 1d change** | daily bars + VIX *(after backfill)* | Daily only |
| F6 | **Options positioning** (reconstructed from `/tools` maths, §3.5) | PCR (OI and volume); **max pain distance from spot**; OI walls — largest CE/PE OI strikes and their distance; OI concentration (Herfindahl over strikes); 1d OI change at ATM±2; **net GEX and zero-gamma level**; days-to-expiry | `index_options_eod` *(after refresh)* | Prior-day EOD only |
| F9 | **Volatility surface** (reconstructed, §3.5) | ATM IV; **25-delta risk reversal / put-call skew** (the classic directional-positioning feature); smile curvature; **IV term structure slope** (7–14 expiries/day available); IV minus realized vol | `index_options_eod` + Black-76 | Prior-day EOD only |
| F7 | **Calendar** | day of week; days to monthly expiry (last Tuesday); month-end; post-holiday | calendar | Yes |
| F8 | **Cross-index** | BANKNIFTY−NIFTY relative strength; MIDCPNIFTY−NIFTY (small-vs-large risk appetite, 2025-12+ only) | index 1m | Yes |

Three families carry the hypothesis, and they are close to independent of each
other — which is what makes the combination worth testing rather than just the
best single one:

- **F1/F2 (breadth + dispersion)** — the platform's per-constituent 1m history
  over a 200-name F&O universe is not something a retail feed gives you. Most
  defensible *intraday* hypothesis.
- **F6/F9 (options positioning + surface)** — 4.5 years of dense strike-level OI,
  entirely unused to date. Put-call skew and net GEX are the two features with the
  strongest prior literature for index direction and dealer-hedging flow, and both
  are now known-computable (§3.5). Highest upside, and the reason `/tools` turned
  out to matter to this round.
- **F5 (vol regime)** — weakest until India VIX is backfilled (#516).

F6/F9 are **prior-day EOD**, so they are lagged features at the 09:45 decision
point. That is a real limitation, not a formality: the positioning signal is ~18
hours stale by the time it is used. It also means F6/F9 are the natural features
for the **T2 overnight-carry target** decided at 15:20, where the lag is ~1 hour
instead — expect them to perform better on T2 than on T1.

### 5.3 Model progression — simplest first, stop when it stops improving

1. **Baseline 0:** always-up (52.34% daily / 49.37% intraday). Every result is
   quoted against this.
2. **Baseline 1:** single-feature logistic on breadth alone. If this does not beat
   Baseline 0, the hypothesis is likely dead and everything after it is curve-fitting.
3. **Model A:** L2-regularized logistic regression on ≤15 features, one family at a
   time then combined. Interpretable, calibrated by construction, hard to overfit
   at n=640.
4. **Model B:** gradient-boosted trees (shallow, depth ≤3, heavy regularization),
   only if A shows signal. Report feature importance and check it is not dominated
   by a single leaky feature.
5. **Stop rule:** if Model A does not clear the §4 threshold on walk-forward
   out-of-sample, **do not** escalate to B in the hope of rescuing it. That is how
   R39's conditional framings died on Bonferroni.

Output is a **calibrated probability**, validated with a reliability diagram and
Brier score — not a bare class label.

---

## 6. Backtest and accuracy plan

### 6.1 Guardrails against this project's own known failure modes

These are drawn from the registry's Cross-Cutting Findings and prior memory; each
one is a rule for this round:

| Prior failure | Rule for this round |
|---|---|
| **R58** — bar-close gate + trigger-level entry = look-ahead | Every feature at decision minute `t` uses only bars **closed at or before `t−1`**. Assert this programmatically in the feature builder, with a unit test that shifts the target forward and confirms accuracy collapses to ~50%. |
| **R40** — flat cost overstated Sharpe ~3× | Use real instrument costs (NIFTY futures ~0.03% round trip incl. STT/stamp/brokerage/exchange). Never a flat bps. |
| **R39/R41** — conditional per-asset framing dies on Bonferroni | Count every variant tested; apply a Bonferroni/BH correction to the headline p-value and report the corrected number. |
| **R45** — multi-day drift was a regime artifact; every bucket flipped sign between halves | **Split-half sign-stability is mandatory**: report accuracy on 2024, 2025, and 2026 H1 separately. A predictor that flips sign across halves is rejected regardless of full-sample accuracy. |
| **2026-06-09** — rule validation must use live `.env`, not code defaults | Any feature that mirrors a production threshold reads it from `.env`. |
| **R36** — synthetic BS pricing optimistic for option buying | If an options application is tested, use real `index_options_eod` settle prices, never Black-Scholes. |
| **#456** — 09:10 arm read historify-D mid-resettle | Feature builder reads **settled** daily bars only; assert bar date < today before use. |

### 6.2 Phased protocol

Each phase has an explicit kill criterion. Do not start phase N+1 if phase N fails.

**Phase 0 — Data preparation** *(prerequisite, no modelling)*
- Refresh `index_options_eod` (→ 2026-07-31) and `fo_bhavcopy_eod`.
- Backfill `INDIAVIX` daily to current; add `INDIAVIX` to `SCANNER_SYMBOLS` for live.
- Build a single point-in-time **feature matrix** parquet: one row per
  (date, decision-minute), all F1–F8 columns, target columns T1/T2/T3.
- **Leakage test:** shift the target one day forward; accuracy must fall to ~50%.
  If it does not, the feature builder leaks and everything downstream is void.
- *Kill:* if VIX and options refresh both prove infeasible, proceed with F1–F4+F7+F8
  only and say so in the report — do not silently drop features.

**Phase 1 — Signal detection (in-sample, honest)**
- Univariate: information coefficient and accuracy per feature, per family.
- Baseline 1 (breadth-only logistic), 5-fold *time-blocked* CV.
- *Kill:* no family reaches ≥53% CV accuracy on T1 → write the negative result to
  the registry and stop. This is a real and likely outcome.

**Phase 2 — Walk-forward out-of-sample**
- Expanding-window walk-forward: train on all data to month `M`, predict month
  `M+1`, roll. First train window ≥ 12 months → ~19 OOS months, ~400 OOS days.
- Metrics: **accuracy vs base rate**, AUC, **Brier score**, reliability diagram,
  accuracy by confidence decile, accuracy by year, accuracy by vol regime.
- Significance: day-block bootstrap (not i.i.d. — intraday obs are autocorrelated
  within a day), 10,000 resamples, report the CI on accuracy.
- *Kill:* OOS accuracy CI lower bound ≤ base rate → reject.

**Phase 3 — Economic value**
- Application A (**recommended first**): use the T2 probability as a **gate on
  `futures_follow_cap50`** — skip or half-size entries below a probability
  threshold. Replay the sleeve's actual historical signal days with and without
  the gate. This is the cleanest test because the baseline is a live, already-
  measured strategy.
- Application B: standalone NIFTY-futures directional trade on top-confidence days
  only, real costs, position size fixed.
- Report: CAGR, Sharpe, max DD, trade count, and **turnover** — plus the *counter-
  factual*: what the same rule earns on shuffled predictions.
- *Kill:* net-of-cost improvement not distinguishable from zero → the predictor may
  still be scientifically real but is not deployable; record it as such.

**Phase 4 — Robustness**
- Parameter sensitivity (thresholds ±30%), decision-minute sensitivity
  (09:30/09:45/10:15), feature-ablation, and a **shuffled-target control** that
  must produce ~50%.
- Regime split: 2024 / 2025 / 2026-H1 sign stability (per R45).

**Phase 5 — Paper/shadow deployment** *(only if 0–4 pass)*
- Compute and journal the prediction daily **without acting on it** for ≥ 30
  trading days; compare live accuracy to backtest accuracy before any capital.
  The existing `signal_decision` / `market_intel` tables are the natural sink.

### 6.3 Accuracy measurement — what gets reported

| Metric | Why |
|---|---|
| Accuracy vs base rate (never vs 50%) | The only honest framing at the daily horizon |
| AUC | Threshold-free discrimination |
| **Brier score + reliability diagram** | Calibration — a probability that is not calibrated cannot be thresholded |
| Accuracy by confidence decile | Whether the high-conviction tail is where the edge lives |
| Accuracy by year / by vol regime | R45 regime-artifact defence |
| Day-block bootstrap CI | Correct significance under intraday autocorrelation |
| Bonferroni-corrected p | Variant-count defence |
| Shuffled-target control | Leakage detector |

**Pre-registered success threshold:** OOS accuracy ≥ **54%** on T1 (bootstrap CI
lower bound above base rate), calibrated (Brier better than the base-rate model),
sign-stable across all three yearly sub-periods, and — for deployment — net-of-cost
positive in Phase 3. Anything less is written up as a negative result and added to
the registry.

---

## 7. Operational notes

### 7.1 Reading historify while the app runs

The live app (PID 22756) holds `db/historify.duckdb` open read-write; a
cross-process DuckDB open fails on Windows even read-only. **A plain file copy
works** and is the method for this research:

```bash
cp db/historify.duckdb "$SCRATCH/hist_copy.duckdb"
```

A 4.3 GB snapshot already exists in this session's scratchpad. It lacks the `.wal`,
so it is current to ~2026-07-31 15:30 — fine for training, not for live inference.
The previously-recorded browser-export workaround is no longer needed for this
volume of data.

### 7.2 Work items that must land before Phase 1

1. Refresh `index_options_eod` + `fo_bhavcopy_eod` to 2026-07-31 (both ~2 months stale).
2. Backfill `INDIAVIX` daily; add `INDIAVIX` to `SCANNER_SYMBOLS`.
3. *(Optional, unblocks F2 fully)* 1m backfill for the 11 sector indices that only
   have 2026-04-27 → 2026-05-29.

Items 1 and 2 are also standalone data-hygiene fixes worth doing regardless of
this research — three datasets are silently stale and nothing alerts on them,
which is exactly the failure class `data_freshness_service` was built to prevent.
They deserve their own issue.

### 7.3 Scope boundaries

- No live trading, no order placement, no strategy registration in this round.
- No changes to any running strategy. A Phase-3 gate on `futures_follow_cap50`
  is *simulated over history only*.
- Output is a research report + registry entry, per the project's backtest-round
  convention.

---

## 8. Expected outcome — stated up front

Honest prior: **intraday index direction is close to unpredictable at this
sample size**, and the most likely result is Phase 1 or Phase 2 rejection. The
things that give this round a better-than-usual chance are (a) per-constituent 1m
breadth over a 200-name universe, which most retail research cannot compute, and
(b) 4.5 years of dense strike-level index-option OI that — as of the §3.5 finding
— can be turned into full daily PCR / max-pain / GEX / skew / term-structure
series using the platform's own Black-76 library.

That second point is the material update to this plan: before checking `/tools`,
the options family looked like a maybe-if-we-backfill. It is now a
several-hundred-column, 1,095-day, ready-to-build feature set whose only real
constraint is the EOD lag. It also shifts weight toward **T2 (overnight carry)**
as a target, since that is where the lag hurts least and where the
`futures_follow_cap50` application lives.

The magnitude target (T3) is likely to work even if direction does not, and
volatility forecasting has real uses (position sizing, the sleeve's 50% margin
cap) — but it must not be reported as a directional edge.

Whichever way it lands, the round produces a registry entry.
