# R43 — News-Event Momentum Study (NSE corporate announcements)

**Date:** 2026-07-07 · **Issue:** #361 · **Verdict: REJECT (momentum-chasing); PROMISING lead: fade-the-spike (untested as a strategy)**

## Question

Can a live "stocks in the news" scanner — detect NSE corporate announcement →
confirm with price reaction + volume surge → ride the wave — clear real Zerodha
charges at realistic detection latency? Tested BEFORE building any live
plumbing (poller, dynamic WS subscription, warm-up replay).

## Data

- **News tape:** 181,536 NSE corporate announcements, 2025-07-01 → 2026-07-06
  (371 days, 0 failed days), harvested day-by-day from
  `nseindia.com/api/corporate-announcements` (symbol, category, summary,
  timestamp to the second). Harvester: `backtest/news_event_study/harvest_announcements.py`.
- **Event set:** 16,151 tagged events → **13,590 unique (symbol, effective-date)
  pairs** after category→polarity mapping and NSE master-contract eligibility.
  Polarity groups: positive (order wins/agreements/dividend), negative
  (resignations, regulatory actions, litigation, insolvency), results
  (board-meeting outcomes containing financial results), tape_decide
  (acquisition, rating revision).
- **Prices:** Zerodha historical via `services.history_service` — 7.59M 1m bars
  (event day + T+1) + 480k daily bars (baselines), 27,111/27,180 fetches ok.
  Fetcher: `backtest/news_event_study/fetch_event_prices.py`.
- **68% of announcements land post-market** → modelled as next-open
  ("at_open") events; 32% intraday.

## Method

`backtest/news_event_study/simulate.py`, two passes:

1. **Pure event study** (no trading): return at detection (announcement +
   latency; at-open events detected 09:15+latency) vs prev close, then drift to
   +30m/+60m/same-day 15:15/T+1 15:15. Circuit-locked days flagged and
   excluded.
2. **Trading simulation**, 108-cell grid: latency {30/90/300s} × reaction gate
   {1/2/3%} × time-of-day-adjusted volume surge {1.5/2.5/4×} (self-calibrated
   intraday cumulative-volume curve) × exits {same-day 15:15, T+1 15:15,
   3% trail, 3% stop + T+1}. Entry next 1m bar open + 0.25% adverse slippage
   per leg; ₹50k/position; penny floor prev_close ≥ ₹50; shorts intraday-only;
   real Zerodha charges (MIS and CNC models, GST/STT/stamp/DP to the rupee —
   hand-verified). 377,760 simulated trades.

## Result — momentum-chasing is uniformly dead

**Every one of the 108 cells is net-negative.** Best cell (n≥100):
−0.66%/trade (results × trail3). Hit rates 34–39% everywhere. Pooled by
polarity (all cells): positive −1.1 to −1.6%, results −0.7 to −1.0%,
tape_decide −0.9 to −1.1%, negative(shorts) −0.4 to −0.6% per trade.

**The loss is GROSS, not costs:** avg gross per trade is −0.36% (negative) to
−1.18% (positive); charges only add ~0.14–0.17pp. No cost model rescues it.

**Root cause (event study, no trading mechanics):** after a confirmed ≥2%
spike in the news direction, Indian single names **mean-revert ~1% by the same
close** (medians worse: −1.1 to −1.6%). Volume surge ≥2.5× — the "confirmation"
— makes it WORSE (−1.15 to −1.46% to EOD): high-participation spikes revert
harder. Per-category check: **no category survives** — even
Bagging/Receiving-of-orders fades (n=393, T+1 −0.35%, median −0.96%);
Dividend/Acquisition/Agreements/Results all −1.0 to −1.5%. Latency is
irrelevant (30s ≈ 300s): the edge isn't decaying over our latency range — it
was never there at retail-reachable timescales. Consistent with R42
(sell-signal stocks bounce next day) and the MIS-leverage finding (late
intraday momentum mean-reverts).

## The inverse lead — fading the spike (NOT yet a strategy)

The same tables read backwards: shorting a ≥2%+vol-surge news pop is ~+1.2–1.5%
gross to EOD on n≈1,700 events (medians stronger than means). After ~0.6%
round-trip costs+slippage that's ~+0.5–0.9%/trade, intraday-only,
capacity-limited, in hard-to-short smallcaps — execution-fragile and
counterintuitive (shorting good news). Parked as a candidate for a future
round with proper borrow/liquidity/execution modelling. Do NOT deploy off this
table.

## Decision for the live news scanner (issue #361 plan)

- **Do NOT build** the momentum-confirmation trading path (Phase 2/4 of the
  original plan). The thesis "news + price/volume confirmation → ride the wave"
  is refuted at every tested horizon, category, and latency.
- The **ingestion layer** (poller + classification + Telegram "stock in news"
  alerts, Phase 1) remains optional as an *informational* tool — it just must
  not auto-trade momentum.
- Reusable assets: 12-month news tape (`outputs/news_event_study/announcements.duckdb`),
  event-price store (`prices.duckdb`, 7.6M 1m bars incl. non-F&O smallcaps),
  results grid (`results.duckdb`), and the three scripts (rerunnable /
  extendable for the fade study).

## Caveats

- 0.25%/leg slippage is an estimate; for the REJECT verdict this is
  conservative in the right direction (real fills in spiking smallcaps are
  worse, making momentum results even more negative).
- Master-contract eligibility uses today's listing → ~year-old renamed/delisted
  symbols drop out (survivorship; 67 no-data / 2 errors — immaterial at n=13.5k).
- At-open events use 09:15+latency detection; pre-open auction dynamics not
  modelled.
- Event-study `ret_fwd_*` columns share the prev-close baseline with
  `ret_at_detection` (deltas quoted above account for this).
