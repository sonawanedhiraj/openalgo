# R81 — Simplified stock engine: replace the ENTRY, keep the engine (scope check + pre-registration)

- **Issue:** #785.
- **Stage:** Phase 1 (scope check) + Phase 2 (pre-registration). **No candidate has been tested;** they run after Dheeraj approves the list.
- **Pre-registration:** `backtest/r81_entry/PREREGISTRATION.md`, committed with this doc before any result.
- **Supersedes** the broad R81 survey of the same day (`docs/research/strategy/r81_new_entry/…`, commit 2a6cf06c4). It was redirected before any test ran because Dheeraj's focus is this engine's signal quality.

## The distinction this round rests on

**Filtering the existing ChartInk breakout signal is exhausted.** R62, R65, R67, R68, R76, R78, R79 and R80 all tried to improve it by filtering, gating, labelling or changing its exit.
- R80 settled it on 18 clean, never-used months (2024-01 → 2025-06), running the real scanner rules through the real engine: 1,655 trades.
- The entry earns **0.3 bps a trade before costs**.
- Three of Dheeraj's four signal-quality criteria fail outright. The fourth (index alignment) is a regime effect: positive in one period, negative in the other.

**What has NOT been tried is replacing the signal GENERATION while keeping the engine.**
- **The engine is sound and stays:** order execution, ATR sizing, the ₹500 risk cap, stop handling, the trail and EOD flatten, the journal, the charge model.
- **The entry rule is the component to swap:** "ChartInk arms a symbol, then the engine's reference-candle breakout fires".
- **R81 tests new entry rules** through the same engine harness built for R80.

## Phase 1 — entry rules already tried for this engine (do not repeat)

| Round | Entry rule (or change to it) | Verdict |
|---|---|---|
| R1 | 5m intraday breakout engine, parameter sweep | REJECT: loses gross and net, win ~25% |
| R2 | Exact ChartInk F&O rule on real 5m bars | REJECT: PF 0.21 |
| pre-R3, R3 | Sector-aligned movers + low-vol-base breakout | REJECT: PF 0.28 / 0.69 |
| R4, R5 | Wyckoff colour filter; regime + doji filter | REJECT: fewer trades, not an edge |
| R12 | 3-state Markov-chain entry | REJECT: gross ≈ 0 |
| R13 | Bull flag, intraday | REJECT: 9.5% win |
| R15 | Fade 3%+ intraday moves | REJECT: −₹8.4 lakh |
| R29 (+v2) | Sector-momentum gate on the intraday entry | REJECT out of sample |
| R44 | News-stock delayed entries: bull-flag, no-supply reversal (engine mirror), VWAP reclaim, ORB | REJECT: all gross-negative |
| R48 | Time-of-day volume gate | REJECT: selectivity, not latency |
| R56 | In-house scanner arming the engine (full history) | REJECT: −₹2.52 lakh, 0/14 months green |
| R57, R60 | Pullback / retrace-limit entry on scanner signals | PROMISING → `intraday_pullback_top2` sandbox: −₹2.7k on 13 trades |
| R58 | Opening 15-minute volume breakout | REJECT at bar level (look-ahead). The tick version, open15, is live and losing. |
| R61, R62 | Hold-to-EOD, skip-midday and other live-journal fixes | REJECT out of sample |
| R65, R68 | Entry filters (lunch, gap, ATR, NIFTY direction, LLM confidence…); counter-NIFTY label | REJECT; label only. Not replicated in R80 (P(1R) 0.41 counter vs 0.46 with). |
| R67 | 106 stop/exit configurations | REJECT: no out-of-sample-positive config |
| R76 | False positives on a fresh window | REJECT: payoff, not win rate, is the constraint |
| R79 | Signal-to-outcome path study | Long entry REJECT; the trail leaks |
| R80 | 18-month backfill + target space, structure, alignment, extension | REJECT: gross 0.3 bps a trade |

## Phase 1 — the bar every candidate must clear

| Item | Value |
|---|---|
| **MIS round trip (Kite calculator, 2026-11-15)** | ₹47.2 fixed (₹20 brokerage × 2 orders + 18% GST) + **3.55 bps** proportional |
| At ₹86k notional | ₹78 = **9.1 bps** (the live journal measures ₹77 = 8.89 bps) |
| Slippage | 2.4 bps per side measured (R79), so ~4.8 bps a round trip |
| **Gross edge needed per trade** | **≈ 14 bps ≈ ₹120 ≈ 0.24R** (R = ₹500 price risk) |
| At 5.1 trades a day | ≈ ₹612 a day before the strategy earns anything |
| Break-even hit rate | 2R target vs 1R stop: (1 + 0.24)/3 = **41%**. R80 measured 15–25% reaching 2R before the stop. Symmetric 1R/1R: 62%. |
| Measured gross so far | 0.3 bps (R80 backfill), 2.2 bps (live journal, R78): **6–45 times too small** |

**Why "bigger trades" barely helps on this engine.** The ₹500 risk cap pins the notional to about 500 / stop distance.
- R80's median stop is 54 bps, so notional is about ₹92k; the capital setting (₹20k × 5) caps it at ₹1L anyway.
- More notional per trade requires a tighter stop (R67: tighter stops are worse) or a higher risk cap.
- At ₹1,000 risk (≈ ₹1.85L notional) the bar falls only from ~14 to ~11 bps.
- The proportional floor of 3.55 bps + ~4.8 bps slippage ≈ **8.4 bps is unbeatable at any size**.

**Why "fewer trades" helps only through selection.** Trading less does not lower the cost per trade; it only removes trades. It pays only if the trades kept carry more edge per trade. That is why two candidates (E2, E5) are deliberately capped at one or two trades a day and take only the strongest setup.

**Implied minimum move:** the entry must produce ≥ **14 bps (₹120, 0.24R) of gross expectancy per trade**. In outcome terms: ≥ 41% of trades reaching 2R before the stop, or ≥ 62% reaching 1R first under a symmetric exit.

## Phase 1 — a sizing bug to fix regardless (separate engine change, not part of R81)

**27.6% of R80's trades lost more than ₹500 once charges were counted.**
- `calculate_qty` sizes on price risk alone: qty = ₹500 / risk per share. It ignores the ~₹78 of charges, slippage and gaps through the stop.
- The ₹1 minimum risk per share (12% of R80's trades) makes low-priced names worse.
- A ₹500 *all-in* limit needs qty = (₹500 − estimated charges − estimated slippage) / risk per share.
- This is an engine code change, so it is a separate issue. R81 reports the breach rate for every candidate under today's sizing.

## Phase 2 — five candidate entry rules (full spec in `backtest/r81_entry/PREREGISTRATION.md`)

| # | Entry rule | Economic rationale (written before testing) | Frequency cap | Thresholds (plateau neighbours) |
|---|---|---|---|---|
| **E1** | **VWAP trend-day pullback**: stock ≥ 1 ATR from previous close and ≥ 80% of 5m closes on one side of VWAP; enter on the first 5m bar that touches VWAP and closes back on the trend side (10:15–14:30) | Institutional algorithms benchmark to VWAP and absorb pullbacks to it on trend days | 3/day | trend 0.75/1.0/1.25 ATR; side share 70/80/90% |
| **E2** | **Sector-relative strength at 09:45**: own sector index moving ≥ 0.30%; stock beating it by ≥ 0.5 ATR; take the single best long and single best short | Lagged within-industry information diffusion; isolates stock-specific demand from the sector/NIFTY beta the ChartInk book was secretly betting on (R62/R65) | **≤ 1 long + 1 short a day** | sector 0.20/0.30/0.40%; RS 0.35/0.5/0.65 ATR |
| **E3** | **Large-gap fade**: gap ≥ 1.0 daily ATR, and the first 15 minutes close against the gap; enter at 09:30 against the gap | Opening gaps are partly overnight liquidity imbalance that retraces once the book refills. R58 already measured gap-direction breakouts mean-reverting. | 2/day, largest gap first | gap 0.75/1.0/1.25 ATR; confirmation 10/15/20 min |
| **E4** | **First-hour compression → expansion**: first-hour range ≤ 0.6 × its 20-day norm; a 5m close beyond it after 10:15 on ≥ 1.5 × slot-normal volume | Volatility clustering; breaking a compressed range gives a smaller stop relative to the move, the cost-floor lever | 3/day | compression 0.5/0.6/0.7; volume 1.25/1.5/1.75 |
| **E5** | **Opening-drive efficiency**: at 09:45, path efficiency ≥ 0.40 and move ≥ 0.5 ATR; take only the single best stock | Informed flow moves price efficiently; efficient openings mark trend days (Kaufman ER; Gao et al. 2018) | **1/day** | ER 0.30/0.40/0.50; move 0.35/0.5/0.65 ATR |

Two candidates are deliberately low frequency (E2 ≤ 2 a day, E5 1 a day). The other three are capped at 2–3 a day, against the ChartInk book's 5.1.

**Excluded, and why:**
- Any ChartInk/scanner arming, and the engine's own reference-candle breakout trigger.
- The opening 15-minute range breakout, with or without volume. R58 found every honest bar-level variant loses. **Tick data does not reopen it for a backtest:** Kite serves no historical ticks, and the in-app tick capture only starts in 2026-08. The tick-level version already exists as open15, which is live and losing.
- Gap-and-go continuation (R58's breakouts mean-revert).
- VWAP *reversion* and generic move fades (R15, R32).
- Wyckoff, doji, Markov, bull flag, news-triggered, volume-gate latency, closing-window and index noise-boundary entries (R4, R5, R12, R13, R43, R44, R48, R70, R71, R77).

**Overlaps disclosed:**
- E1 is near R57/R60's pullback idea, but universe-wide with no scanner input.
- E2's selection logic is near R3's sector movers, but with no breakout trigger.
- E3 is near R15's fade, but only the opening gap after a failed first 15 minutes.

## Phase 3 — protocol (unchanged from R80, applied to every candidate)

| Item | Rule |
|---|---|
| Train | 2019-01 → 2022-12 |
| Validate | 2023 |
| **Sealed holdout** | **2024-01 → 2025-06-19**, not computed until all five specs are frozen by a commit after VALIDATE |
| Excluded window | 2025-06-20 onward (R56/R80 territory) |
| Holdout disclosure | R80 saw the holdout's *regime* through the ChartInk book; it never ran these rules on it |
| Data | Kite serves stock 1m bars from **2015-02**, not ~2021-10 as assumed: probed on RELIANCE and TATASTEEL. 2019–2023 needs ~5,600 requests (~35 minutes); 2024–25 is already fetched (R80). |
| Engine | Unchanged. Entries are decided on completed bars only; the reference price is the bar close (the R58 look-ahead lesson). |
| Costs | Corrected charges, 2.4 bps slippage per side, zero-slippage bound, 5 bps stress |
| Primary metric | Net expectancy per trade under the engine's own exit; long and short reported separately |
| Statistics | Day-clustered t and day-block bootstrap CI. **|t| < 2 is never called a result;** 2.58 (Bonferroni over 5) to confirm. |
| Regime | Positive in TRAIN, VALIDATE and HOLDOUT, and in ≥ 7 of 10 half-years |
| Plateau | All four single-step neighbours positive on VALIDATE |
| Placebo (R67) | Timing and selection, 1,000 draws each, both p < 0.05 |
| Ledger | Every variant, including failures, with every field of handover §17.6 |

## Decisions for Dheeraj before anything runs

1. **Approve, cut or amend E1–E5.** Each costs about the same to test once the 2019–2023 fetch is done. E2 and E5 go first because they are the cheapest and lowest frequency.
2. **Universe.** Either:
   - keep the approximate 2024-03 F&O list (survivorship-biased before 2024; the default); or
   - approve downloading NSE's public month-end F&O lists for point-in-time membership (cleaner; it is a download, so it needs a yes).
3. **The ₹500 all-in sizing fix** is worth doing regardless, as its own issue.
