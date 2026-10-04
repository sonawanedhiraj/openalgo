# R71 — What works in the closing session? (issue #751)

**Date:** 2026-10-04 · **Follow-up to:** R70 (#750: the open15 closing analogue
was REJECTED).
**Harness:** `backtest/close15/` (untracked, local):
- `closing_search.py`
- `overnight_portfolio.py`
- `overnight_decompose.py`
- `overnight_after_close.py`
- `auction_reversion*.py`
- `verify_auction_close.py`

**Data:**
- historify 1m + daily, 212 F&O stocks + indices, **pre-CAS 2024-01 → 2026-07
  (636 days)** and **post-CAS 2026-08-03 → 10-01 (41–42 days)**.
- All-day ticklog, 17 September days.

**Costs** (zerodha.com/charges, verified 2026-10-04), round trip at ~₹60k per
name:
- MIS intraday ≈ 0.10%;
- **same-day CNC = intraday charges** (STT 0.025% sell, no DP), ≈ 0.10% at
  ₹60k, ≈ 0.05% at ₹3–5L because brokerage caps at ₹20 an order;
- CNC delivery ≈ 0.25% (STT 0.1% each side + DP ₹15.34);
- stock futures ≈ 0.06% (STT 0.05% sell);
- NIFTY futures ≈ 0.06%.

## Answer

| Rank | What | Pre-CAS (636 d) | Post-CAS (41–42 d) | Status |
|---|---|---|---|---|
| 1 | **Overnight momentum entered in the closing session:** buy the day's top-5 gainers (decided at 15:04), sell at the T+1 open | **+0.437% gross; +0.275% hedged excess vs universe, t = 11, both halves, every year; net ≈ +0.38% long stock futures, +0.155% NIFTY-hedged** | ≈ 0 excess (+0.025%) at 15:04 entry; +0.06–0.07% if entered after the auction; H2 negative. Excess this weak occurred in only 0.2% of pre-CAS 41-day stretches | **Was the closing session's real edge. Broken or paused since CAS.** Do not deploy new; watch |
| 2 | **Auction reversion:** the market rises from 15:11 into the auction close (it reverses the 15:00–15:09 pre-square-off dip, R70c) | absent (placebo: universe −0.011%) | bars: universe +0.087%, biggest 15:00–15:11 fallers +0.126% (63% win, 8 of 9 weeks +); **ticks (Sep, 16 d): universe +0.028%**, 69% of days + | **PROMISING, not proven.** The effect weakened from August into September and is now at or below cost |
| 3 | Everything else intraday in 14:30–15:14 (open15 rule, A grade, top movers, scanner names, morning names, quintile features, NIFTY intraday momentum) | ≤ 0.07% | ≤ 0.08% | REJECT (R70, R70b, R70c, R70d) |

## 1. Overnight momentum from the close: what used to work

Decile map (`closing_search.py`):
- The only features that clear costs anywhere predict the **overnight** leg:
  - day return, intraday return and relative volume at 15:04 predict the
    15:04 → T+1 open move;
  - day_ret top decile +0.24% to T+1 09:30, decile spread t = 4.2;
  - relative-volume decile spread t = 10.2.
- Every intraday target (to 15:14, to the auction) is ≤ 0.08%.

Portfolio test (`overnight_portfolio.py`, `overnight_after_close.py`). Top-N
by day return at 15:04, equal weight:

| Pre-CAS | 15:04 → T+1 open | → T+1 09:15 bar close | → T+1 09:29 | → T+1 15:09 |
|---|---:|---:|---:|---:|
| top-5 gross | **+0.437** | +0.387 | +0.345 | — |
| top-10 gross | +0.388 | +0.320 | +0.280 | +0.226 |
| universe | +0.161 | +0.113 | +0.110 | +0.093 |
| top-5 excess (t) | +0.275 (11.1) | +0.274 (8.4) | +0.235 (5.9) | — |

Notes:
- **Exit at the open.** The return decays all through T+1.
- **Top-5 beats top-10, and a relative-volume filter hurts**
  (+0.334 → +0.172 excess).
- **Every year is positive:** 2024 +0.53, 2025 +0.27, 2026 H1 +0.27 (top-10).
- **The whole edge is overnight** (`overnight_decompose.py`): 15:04 → 15:29
  contributes +0.004; 15:29 → T+1 open contributes +0.384 (universe +0.158).

**What CAS did to it.** Post-CAS, top-10 gainers:

| Segment | Top-10 gainers | Universe |
|---|---:|---:|
| 15:04 → 15:14 | +0.007% | — |
| 15:14 → auction close | +0.014% | +0.066% |
| auction → T+1 open | +0.067% | −0.002% |

The universe's overnight drift now happens *inside the auction*, the strong
names lag in it, and what remains overnight is about +0.07% excess.

- Entering after the auction (stock futures trade to 15:40) recovers some of
  it: +0.067% gross, +0.069% excess, below the 0.06% cost once hedged.
- By month: August +0.25% (universe +0.16%), September −0.09% (−0.04%).
- **41 days cannot distinguish "CAS broke it" from "a bad two months"**, but
  the 0.2-percentile result says to assume broken until shown otherwise.

**This is the same family as `sector_follow_cap5_vol`** (15:05 entry, T+1
15:10 exit, CNC) **and `futures_follow_cap50`** (T+1 overnight). Three
implications:
1. Their post-CAS results should be read against this collapse.
2. The data says **exit at the T+1 open, not 15:10.** Pre-CAS, the 15:09
   exit kept only about half the gross (+0.226 vs +0.388, top-10).
3. Stock futures (0.06%) instead of CNC (0.25%) is worth +0.19% per trade.

Check all three against R40/R41 before acting.

## 2. Auction reversion (post-CAS only): promising, not proven

Post-CAS the equal-weight F&O universe falls 15:00 → 15:09 (−0.05%, 74% of
days, R70c). That fits forced or pre-emptive MIS exits ahead of Zerodha's 15:12
square-off. Then it **recovers into the closing auction**.

- **Mechanism-consistent:** the pre-CAS placebo (15:11 → 15:29, no auction,
  no 15:12 square-off) is flat (universe −0.011%).
- **Monotonic by decile:** the biggest 15:00 → 15:11 fallers gain +0.157% into
  the auction, the biggest risers +0.006%.
- **The daily close IS the auction price:** 94% of after-auction ticks equal
  the historify daily close exactly.
- **But it is fading.** Bars over 42 days: universe +0.087%. Ticks over the 16
  September days: universe **+0.028%**, 69% of days positive, one −0.41% day
  (2026-09-29). Weekly fallers: +0.23, +0.25, +0.09, +0.17 in August; −0.01,
  +0.18, +0.05, +0.08, +0.06 since.

**Executable form, if it holds:**
- Buy CNC at about 15:11. MIS is squared off at 15:12, so it must be CNC.
- Sell into the auction with a 15:20–15:25 order, which gets exactly the
  auction price.
- The round trip is billed as intraday: ≈ 0.05% at ₹3–5L per name.
- An index version (NIFTY futures 15:09 → close +0.080%, 60% up;
  MIDCPNIFTY +0.109%, 69%) nets ≈ +0.02% after a ≈ 0.06% futures cost.

**Pre-registered next step (no code yet):** re-measure at ≥ 100 post-CAS
days from the ticklog. Promote to a sandbox measurement only if:
- the universe 15:11 → auction move is ≥ +0.08% on the new days alone, AND
- ≥ 65% of days are positive.

Otherwise drop it.

## What does not work in the closing session (consolidated)

All of these are below cost in every window, in both eras, on bars and on
ticks:
- the open15 rule in any 15-min window;
- the A grade;
- rolling top movers;
- scanner names;
- morning breakout names;
- tick-exact targets / stops / trails;
- reversal of the last 30 minutes;
- NIFTY intraday momentum.

Details: R70 report,
`docs/research/strategy/open15_vol_breakout/2026-09-30_r70_closing_window_analogue.md`.

## Caveats

- Post-CAS samples are 41–42 days (ticks: 16–17).
- Overnight exits at the T+1 open use the 09:15 1m open. The 09:15 bar close
  is the conservative fill and costs about 0.05–0.07%.
- Stock-futures capital: ~₹5–10L notional per lot, ~20% margin.
- Hedged numbers subtract the equal-weight universe, an approximation of a
  NIFTY-futures short.
