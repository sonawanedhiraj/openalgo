# NIFTY Options Intraday — New Strategy Build Plan (greenfield)

**Status:** PLAN ONLY — no implementation yet.
**Date:** 2026-07-14
**Mandate:** Build a **new** NIFTY-options intraday strategy **from scratch**. The items
below are **references, not code to port**:
- **Seed idea / reference strategy:** `…\src\optionBuyTradingEngine.py` (NIFTY monthly ATM
  CE opening-range breakout). `OptionBuyingStrategy.py` is ignored (retired variant).
- **Reference results (baseline to beat):** `…\monthly-options-buying-strategy\*.xlsx`.
- **Building-block palette:** the OpenAlgo `/tools` suite + greeks stack (§6, §6b).

**We are NOT bound to the reference's exact rules** (CE-only, 8% stop, round-ATM,
fund-sizing). Those are the *starting hypothesis*; the design — direction (CE/PE/both),
instrument (single leg / straddle / spread), strike selection, entry filters, exits — is
decided by the backtest evidence, using the tools as the enhancement kit.

Agreed workflow, written before any code:
**(1) discuss the reference & fix the seed design → (2) build a faithful backtest harness →
(3) analyze the reference results + engineer tool/greek features → (4) design & improve the
NEW strategy over multiple rounds → (5) only then implement into OpenAlgo.** No OpenAlgo
runtime code is written until Phase 5.

---

## 0. Executive framing (read first)

We are **building a new strategy from scratch**, seeded by the reference's idea and armed
with the `/tools` greeks/OI kit. The reference is a *starting point to beat*, not a spec.

The bar is set by hard prior evidence: OpenAlgo's own registry has **rejected directional
option BUYING five times** on real-cost data — R29v2, R36, R36-real, R44-addendum, R55. The
recurring mechanism: theta bleed + the structural **IV floor** (market IV runs above
realized vol, so buyers overpay), turning a thin gross edge cost-negative. The reference's
own Excel backtest is consistent with that: **gross ~₹50/day per 1 lot at a 34–39% win
rate** (§2), *before* real Zerodha option charges.

So the design goal is explicit: **use the tools (GEX regime, IV-regime, skew, OI walls) and
greeks (delta-strike, DTE/theta) to manufacture an edge the naïve breakout lacks** — or, if
the evidence says none survives, produce a clean documented REJECT. Every design choice is
earned by cost-honest backtest, and live code is the last step, gated on clearing the 12%
net floor with acceptable monthly consistency.

---

## 1. Reference strategy — the seed idea (from `optionBuyTradingEngine.py`)

The reference we build *from* (and aim to beat). A NIFTY **monthly-expiry ATM Call (CE)**
opening-range-breakout, intraday. We keep the parts with a rationale (opening-range trigger,
monthly expiry for lower theta, intraday flat) and treat everything else as an open design
choice for Phase 4.

| Element | Rule (as coded) |
| --- | --- |
| Underlying | NIFTY 50 index (token 256265) |
| Opening range | High of the **09:16–09:20** index 1-min bars (`_compute_range_high`) |
| Entry trigger | Index **last price > range_high** → arm; enter next minute |
| Instrument | **ATM CE**, strike = `round(index/50)*50`, **monthly** expiry (front-month; rolls to next month once past this month's expiry via `_get_monthly_expiry`) |
| Entry order | MARKET buy, qty = lots × 75; **lots sized off available funds** (`live_balance // (price×75)`, 1% buffer) |
| Stop-loss | `entry × SL_MULTIPLIER` where `SL_MULTIPLIER = 0.92` (**an 8% hard stop on premium**, *not* the 30% the README text claims — the code is canonical) |
| Target | `entry × TP_MULTIPLIER = 2.8` (**+180%**) |
| Trailing | Milestone ratchet: profit ≥10/15/20/25% raises the multiplier to 0.92/0.93/0.95/0.97; `trail_stop = high × multiplier`, monotonic up |
| EOD exit | Cutoff **15:20** (`_handle_cutoff` / `_handle_monitor_trade`), MARKET |
| Direction | **Long calls only** — puts deliberately excluded ("the fall is sudden, can't capture it") |

**Known issues / ambiguities to settle in Phase 1 discussion:**

1. **Params are backtest-derived, not pre-committed (operator directive).** SL/TP/trail
   are *outputs* of the tuning rounds, not fixed inputs. Start the faithful-reproduction
   run from the code's values (8% stop `0.92`, +180% target `2.8×`, milestone trail) purely
   to validate the harness against the xlsx; then let Phase-4 sweeps choose the live values.
   Note the **8% (code) vs 30% (README)** divergence but do not resolve it by decree —
   the sweep does. This single number dominates win-rate vs payoff.
2. **Range window off-by-one.** `_compute_range_high` uses `between_time('09:16','09:20')`
   (inclusive of the 09:20 bar) while the doc says "09:16 open to 09:19 close." Pin it.
3. **Bar granularity.** Live path aggregates ticks into **1-min** bars; the target/SL/trail
   are checked on **bar high/low**, so a backtest must be **≥1-min OHLC**, not daily.
4. **Fund-based lot sizing** makes P&L path-dependent on account balance. For research,
   normalize to **fixed 1 lot** (matching the xlsx `N Lot Size`) so results are comparable
   across rounds; treat sizing as a separate, later lever.
5. **Same-minute fill assumption.** Entry price = the entry-minute bar open / avg fill.
   Backtest must model slippage on both legs (MARKET in/out on options).

---

## 2. Existing backtest results (parsed from the `.xlsx`, 1 lot/day)

Parsed from each file's `Result` sheet, `Profit` column (traded days only):

| Window | Traded days | Net ₹ | Win % | Avg ₹/day | Best day | Worst day |
| --- | --- | --- | --- | --- | --- | --- |
| 1 month | 21 | 7,794 | 33% | 371 | 7,484 | −2,443 |
| 3 months | 58 | 24,760 | 43% | 427 | 8,143 | −3,003 |
| 6 months | 120 | 15,436 | 36% | 129 | 8,143 | −3,003 |
| 1 year | 238 | 15,971 | 34% | 67 | 9,655 | −4,838 |
| 2 years | 482 | 34,546 | 38% | 72 | 9,655 | −4,838 |
| 3 years | 730 | 35,508 | 39% | 49 | 9,655 | −4,838 |
| Feb-2019 (~6y) | 1,525 | 84,881 | 39% | 56 | 9,655 | −5,194 |

**Read:** classic long-option profile — **low win rate (~34–39%), positive payoff
carried by a few big up-days**. The edge *thins with window length* (₹427/day over
3 months → ₹49/day over 3 years), i.e. the strong short windows are regime luck. The
worst day (−₹4.8k to −₹5.2k on 1 lot) is ~1.5× a good day. **These are gross figures**;
the xlsx does not appear to net Zerodha option charges (brokerage + STT on sell premium +
exchange txn + GST + stamp), which on a ~₹100–200 premium round-trip run ~₹40–90/lot —
i.e. potentially **most of the ₹49–67/day long-run edge.** Confirming that is Phase 3.

The Excel `Result` sheet also carries rich per-day covariates we should mine in Phase 3:
`India VIX` (range), `N Gap Up/Down`, `Day Open vs PDH/PDL`, `N Spot/Fut change`, `DTE`,
`CW DTE`. These are candidate **entry filters** for Phase 4.

---

## 3. Backtest data sourcing — feasibility (the hard part)

The strategy is **intraday-entry / intraday-exit** and prices SL/TP/trail off **1-min
option OHLC**. So the backtest needs **real intraday (≤1-min) option premium history**
for the front-month ATM CE across the test window. That is the scarce input. Options:

| Source | Granularity | Real lookback | Cost | Verdict for this strategy |
| --- | --- | --- | --- | --- |
| **`index_options_eod`** (already in `historify.duckdb`, 1.64M rows NIFTY+BANKNIFTY 2022→2026, real bhavcopy) | **EOD daily only** | 2022-01→2026-06 | free, in-hand | **Cannot** price a 09:20 breakout + intraday SL/trail. Use only for a coarse daily sanity bound + charge modelling. |
| **Shoonya / Finvasia free historical** (**PRIMARY path**) | 1/3/5/…min via `get_time_price_series`; daily EOD | **TBD — must probe empirically** | **free** (broker API) | **Chosen source, conditional on lookback.** Finvasia gives free intraday history, options included on NFO. The unknowns that decide feasibility: (a) how far back **expired-contract** option minute data is retained, and (b) getting each past front-month ATM-CE **token** needs the **historical NFO scrip-master** for that date. **Use it if easily available; if retention is too short, fall back to the two-tier plan below.** |
| **Zerodha Kite historical API** | 1-min | options ~**60 days** rolling only | free (have session) | Good for a *recent* faithful replay (last ~2 months), not multi-year. This is exactly what the live engine's `fetch_data` uses. |
| **Paid vendors** (GDFL/Global Datafeeds, TrueData, AlgoTest) | 1-min / tick | multi-year | paid | Fallback if a long, faithful intraday backtest is required. |
| **Synthetic intraday from EOD + spot-minute + Black-76** | 1-min (reconstructed) | 2022→2026 | free | **Flagged OPTIMISTIC by R36-real** (synthetic IV=RV×1.10 underprices vs the real IV floor). Screening only, never the final gate. |

**First data action — a Shoonya feasibility probe (step 1 of Phase 2):** write a tiny
throwaway script that, for **one past month's** front-month expiry, (1) loads that period's
Finvasia NFO scrip-master to resolve the ATM-CE token, then (2) calls
`get_time_price_series` for a 1-min window on a trading day in that month. This answers the
two unknowns directly — *does expired-contract intraday come back, and how far?* Probe a
recent month, then step back (3m, 6m, 1y, 2y) until it returns empty. **That empty-boundary
is the usable backtest window.**

**Phase-2 progress (2026-07-14):**
- ✅ **Token-resolution solved without a historical Shoonya master.** Verified on a
  live-contract cross-check that **Zerodha master `exchange_token` == Shoonya master
  `Token` (100% match)**. So any past NIFTY option's Shoonya token comes straight from
  the **local cached Zerodha NFO masters** in `pythonTradingAutomator/instruments/`
  (dated **2025-06-15 → 2025-11-07**, ~104 files). This bounds the *token-resolvable*
  window to ~Jun 2025→present unless older masters are sourced.
- ✅ **Probe written & preflight passes:** `scratchpad/probe_shoonya.py` resolves the
  Jun/Jul/Aug/Sep/Oct-2025 front-month ATM-CE tokens cleanly (no login needed for that
  part). The **retention ladder** (does `get_time_price_series` return 1-min bars for
  those *expired* tokens, and how far back) is the remaining question — needs a Shoonya
  login (operator-run; creds+TOTP).
- Finvasia **public NFO/NSE symbol masters are reachable with no auth**
  (`https://api.shoonya.com/NFO_symbols.txt.zip`) — useful for live contracts.
- Note: preflight ATM uses a median-strike proxy (no spot). The real backtest needs
  **NIFTY spot per day** for true ATM — get it from Shoonya index history, or
  `index_options_eod` / historify.

**Decision rule for the data tier (settle right after the probe):**
- **If Shoonya returns a long enough window** (≥ ~1y of front-month ATM 1-min): it is the
  single faithful source — build the whole backtest on it.
- **If Shoonya retention is short** (only recent months): run a **two-tier backtest** —
  (A) the Shoonya/Zerodha real 1-min window as the *faithful intraday* anchor, plus
  (B) real-EOD `index_options_eod` as the *long-horizon directional-edge* bound (explicitly
  noting (B) can't see the intraday SL/trail).
- **Never** ship a live decision off synthetic intraday alone (registry rule from R36-real).

---

## 4. Backtest harness plan (how we replay)

Build a **standalone research harness** (in the source repo or a scratch dir — NOT wired
into OpenAlgo runtime), mirroring the project's `backtest/` conventions:

1. **Load** front-month ATM CE 1-min bars + NIFTY 1-min bars for each trading day in window.
2. **Replay** the exact `optionBuyTradingEngine` state machine per day:
   `monitor_index → range_high(09:16–09:20) → wait_breakout → resolve ATM CE → enter →
   monitor (SL/TP/trail) → EOD 15:20`. Reuse the *actual* handler math, not a paraphrase,
   so the replay is faithful (port the pure functions: `_compute_range_high`,
   `_get_atm_token`/strike, `_compute_targets`, `_update_trailing`).
3. **Cost model:** real Zerodha option charges per round trip — brokerage ₹20/order (or
   0.03%), STT 0.1% on **sell-side premium**, exchange txn ~0.03503% (NFO options), GST 18%
   on (brokerage+txn), SEBI + stamp. Parameterize so we can show gross vs net side-by-side
   (the registry's R40 lesson: flat-cost overstates ~3×).
4. **Slippage:** model MARKET in/out on ATM premium (e.g. 0.25–0.5% or ½-tick × spread).
5. **Outputs:** per-day trade log (matching the xlsx columns so we can diff against the
   user's numbers), plus summary: net P&L, CAGR on a stated capital, Sharpe, win%, payoff,
   max-DD, monthly-green%, worst-day. Match the STRATEGY_REGISTRY testing protocol so the
   round is comparable to R29v2/R36.
6. **Validation gate:** first reproduce the xlsx numbers within tolerance on the same window
   (proves the harness is faithful) BEFORE trusting any new variant.

---

## 5. Analysis of existing results (Phase 3 deliverable)

Once the harness reproduces the xlsx:
- **Net-of-cost re-statement** of every window in §2 (the single most important number).
- **Edge attribution:** how much of the P&L is the few big up-days? (drop top-5% days,
  see what remains). Regime split by year and by VIX bucket (columns already in the xlsx).
- **Entry-filter mining** on the xlsx covariates: does conditioning on `Gap`, `Day Open vs
  PDH/PDL`, VIX band, or `DTE` lift net payoff? (These become Phase-4 candidate rules.)
- **SL/TP sensitivity:** the 8%-vs-30% question, TP 2.8× vs lower, trail thresholds.
- Write it up as a research doc + a **STRATEGY_REGISTRY round entry** (REJECT or PROMISING),
  direct to `dev` per project rules.

---

## 6. Option-greeks improvement plan — leveraging `/ivchart`

The `/ivchart` page is backed by `services/iv_chart_service.py`, which via
`_calculate_iv_series()` already computes, **per candle**, the option's **IV + delta +
gamma + theta + vega** from `(option_close, underlying_close, strike, TTE)` using the
`opengreeks` Black-76 core (same engine as `option_greeks_service.calculate_greeks`).
That function is directly reusable in the backtest harness to **attach greeks to every
1-min bar** — turning a price-only replay into a greeks-aware one. Concrete levers to test
(each as its own round, kept vs baseline):

1. **IV-regime entry gate (highest priority — attacks the known failure mode).** The
   5× rejections all trace to buying when IV is rich. Compute ATM IV at 09:20 (or an IV
   percentile vs trailing 20–60 days) and **only enter when IV is low/normal**. Hypothesis:
   filters out the expensive-premium days that create the fat left tail.
2. **Delta-based strike selection.** Instead of pure round-ATM, pick a **target-delta**
   strike (e.g. 0.55–0.60 delta ≈ slightly ITM). Higher delta = more spot participation,
   **lower theta as % of premium**, less IV sensitivity. Test ATM vs 0.5Δ vs 0.6Δ vs 0.4Δ.
3. **Theta / DTE gating.** Monthly expiry already lowers theta vs weekly. Quantify theta
   drag by DTE bucket and test **skipping the last N DTE** of the cycle (theta accelerates
   into expiry) — the xlsx already carries `DTE`/`CW DTE` to bucket on.
4. **Vega-aware event avoidance.** Long calls are long vega; entering into an IV spike
   (pre-event) then IV-crush is a silent loser. Gate out days where ATM IV is elevated vs
   its own recent regime (overlaps #1; the greeks series makes it precise).
5. **Greek-conditioned exit/trail.** Beyond price trailing, test exits when **theta burn
   per bar exceeds delta-driven gains**, or when delta collapses (spot rolled over). Uses
   the per-bar greeks series directly.
6. **Convexity sizing.** Size by gamma/vega exposure rather than raw funds, so risk per
   trade is comparable across IV regimes.

Each greek lever is a registry round: **baseline vs +lever**, net-of-cost, same window.
Keep only levers that lift **net payoff and monthly-green% together** (the registry's
consistent bar), not just Sharpe.

---

## 6b. The `/tools` suite — what each gives us and how we use it

The Options Trading Suite at `/tools` is ~12 analytical services. For THIS strategy they
split into two groups by **data source**, which decides whether a tool helps the *backtest*
or only the *live* system:

- **History-capable** (reads historical candles) → directly reusable in the backtest harness.
- **Live-chain-only** (reads the *current* broker option chain / OI) → can't be called for a
  past day. BUT most are **reconstructable at EOD from `index_options_eod`** (real bhavcopy
  OI + premiums, 2022→2026), so they define *features to engineer historically* as entry
  filters, and power the *live* entry-gate + monitoring dashboard.

| Tool (service) | Computes | Backtest use | Live use | Historical? |
| --- | --- | --- | --- | --- |
| **IV Chart** (`iv_chart_service`) | per-candle **IV + Δ/Γ/Θ/vega** (Black-76) | **core greeks-feature engine** — attach greeks to every 1-min bar | entry greeks snapshot | ✅ from history |
| **Greeks** (`option_greeks_service`) | point Δ/Γ/Θ/vega/ρ + IV | per-bar greeks; strike-by-delta selection | entry/exit greek gates | ✅ pure math |
| **Straddle Chart** (`straddle_chart_service`) | dynamic ATM **straddle value** + **synthetic future** from history | straddle = expected-move / premium-richness proxy; normalize breakout by straddle; cleaner ATM anchor | expected-move context | ✅ from history |
| **Custom Straddle** (`/straddlepnl/simulate`) | historical straddle **P&L sim** | A/B variant: "buy straddle" vs "CE-only" on the same signal | — | ✅ from history |
| **Symbol helpers** (`option_symbol_service`) | ATM strike, available strikes, symbol build, option exchange | resolve ATM + build CE symbol in the harness | same, in the live service | ✅ pure |
| **IV Smile** (`iv_smile_service`) | IV across strikes, **ATM IV, skew** | **IV-regime gate** (rich vs cheap ATM IV); **skew** as directional tilt | live IV/skew gate | ⚠️ live now; **EOD-reconstructable** |
| **Vol Surface** (`vol_surface_service`) | IV across strikes **and expiries** | **term structure** — front vs back-month IV = event-risk / DTE selection | live term-structure gate | ⚠️ live; EOD-reconstructable |
| **GEX** (`gex_service`) | **gamma exposure** by strike, net GEX | **breakout-quality filter**: −GEX = dealers amplify trend (good for breakout); +GEX = pinning/mean-revert (bad) | live GEX regime gate | ⚠️ live; **EOD-reconstructable** (Γ×OI×lot from bhavcopy) |
| **Max Pain / PCR** (`oi_tracker_service`) | max-pain strike, **PCR**, OI/strike | pin-target distance + PCR sentiment as filters | live pin/sentiment gate | ⚠️ live; **EOD-reconstructable** (bhavcopy OI) |
| **OI Profile** (`oi_profile_service`) | futures candles + **OI walls** + daily OI change | breakout into open space vs into a call-OI wall | live wall map | ⚠️ live; EOD-reconstructable |
| **Multi-strike OI** (`multi_strike_oi_service`) | per-leg OI time series | leg-level OI if we evolve to spreads | Strategy-Builder legs | partial |
| **Order tools** (`place_options_order_service`, `options_multiorder_service`) | place single/multi-leg option orders | — | **Phase-5 execution** (ATM CE now; spreads later) | — |

**Three highest-value tool-derived levers to test as entry filters (Phase 4):**
1. **GEX regime** — a breakout strategy should *love* negative-GEX days (dealers chase the
   move) and *fade* positive-GEX pinning days. This is the most novel filter the suite unlocks
   and is EOD-reconstructable for the whole 2022→2026 window.
2. **IV-regime + skew** (IV Smile / Vol Surface) — the direct attack on the option-buying
   failure mode: only buy when ATM IV is cheap-to-normal and front-month isn't event-inflated.
3. **Max-pain distance / PCR / OI walls** — is the 09:20 breakout heading into open space or
   into a heavy call-OI wall near max pain (likely to stall)?

**Reconstruction task (Phase 3):** build a small `tools_eod_features` table off
`index_options_eod` — per (date, expiry): ATM IV, skew, front-vs-back IV, net GEX, PCR,
max-pain strike, top call/put OI walls. This turns every live-only tool into a *daily
historical feature* the backtest can condition on. (Caveat: EOD snapshots, not the 09:20
intraday value — good enough to test whether a feature *has signal*; a live intraday version
comes in Phase 5.)

## 7. Iterative improvement loop (Phase 4)

Round-based, one change at a time, each logged to the registry:
`R-a` reproduce baseline → `R-b` cost-honest baseline → `R-c` SL/TP/trail sweep →
`R-d` xlsx-covariate entry filters → `R-e…` greek levers (§6) → `R-final` best combined
config vs baseline on OOS. Stop when either (a) a config clears the **12% floor net of
cost with acceptable monthly consistency**, or (b) evidence says reject (write it up and
stop — a clean REJECT is a valid, cheap outcome).

---

## 8. Implementation (Phase 5 — only if Phase 4 clears the gate)

Build the new strategy into OpenAlgo from scratch, using the `futures_follow_cap50` layout
as the structural template (the reference's raw `KiteTicker` engine is **not** reused —
we build on OpenAlgo primitives):

- `strategies/monthly_options_buy/` — README/PLAN/LEARNINGS/VERSION_LOG/config_snapshot.
- `services/monthly_options_buy_service.py` — evaluator on the in-process scanner
  aggregator (NIFTY index bars) + master-contract monthly-expiry resolver + ATM CE
  construction (`construct_option_symbol`) + greeks gate (`calculate_greeks`); orders via
  `place_order_service` (mode flag `MONTHLY_OPTIONS_BUY_MODE`, default **sandbox**,
  live operator-only).
- APScheduler jobs (reset / range-build / breakout-watch / monitor / EOD-flatten 15:20 /
  EOD-summary), a `database/monthly_options_buy_db.py` journal, and
  `blueprints/monthly_options_buy.py` control API.
- Tests + docs in the **same commits** (SYSTEM_MAP, CLAUDE.md strategy section,
  PARAMETER_LOG for every new tunable), GitHub-issue lifecycle via `/track`.
- Sandbox-first; live is a separate operator decision gated on sandbox-vs-backtest match.

---

## 9. Immediate next actions (Phase 2 kickoff)

1. **Shoonya feasibility probe** (§3) — the gating step. Resolve a past ATM-CE token from the
   historical NFO scrip-master and test how far back `get_time_price_series` returns 1-min
   option data. The empty-boundary sets the usable window.
2. **Params:** resolved — SL/TP/trail are backtest-derived; harness reproduction starts from
   the code's 8%/2.8×/milestone values only to validate against the xlsx.
3. **Fix research sizing** at 1 lot (matches xlsx `N Lot Size`) for cross-round comparability;
   sizing/compounding is a separate later lever.
4. **Backtest home:** standalone harness in the source repo / a scratch dir — keeps OpenAlgo
   runtime untouched until Phase 5.
