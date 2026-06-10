# Data Coverage — sector_follow_cap5_vol (Phase 0 Deliverable B)

Source: `db/historify.duckdb` (`read_only=True`), checked 2026-06-10.

## Headline
- **Stored daily (`interval='D'`) for the 30 stocks is sparse** — only ~63 bars,
  2025-04-01 .. 2025-07-02. **Do NOT use stored daily for stocks.** The backtest
  derives daily from 1m (the live signal must do the same). This matches the known
  `historify-daily-shorter-than-1m` behaviour.
- **1m for all 30 stocks: ~224,800 bars each (~600 trading days), reaching
  2026-06-08** — fully sufficient. Running 1m → today's OHLC at 15:20 IST is viable.
- **Sector indices have full native daily history** (2022-01-03 .. 2026-06-04,
  ~1095 bars). All indices in the map are well covered.

## Per-stock 1m coverage (derived daily ≈ rows/375)
All 30 stocks: 1m rows in **224,782–224,818**, last bar **2026-06-08**. No gaps,
no stock below the equivalent of 500 daily bars. ✅ (Stored `D` for stocks is the
sparse 63-bar set above and is intentionally unused.)

| Symbol | 1m rows | last 1m | Symbol | 1m rows | last 1m |
|---|---|---|---|---|---|
| HDFCBANK | 224817 | 2026-06-08 | MAZDOCK | 224816 | 2026-06-08 |
| RELIANCE | 224814 | 2026-06-08 | BEL | 224815 | 2026-06-08 |
| ICICIBANK | 224815 | 2026-06-08 | IDEA | 224814 | 2026-06-08 |
| INFY | 224817 | 2026-06-08 | ITC | 224794 | 2026-06-08 |
| ETERNAL | 224814 | 2026-06-08 | VEDL | 224790 | 2026-06-08 |
| SBIN | 224817 | 2026-06-08 | DIXON | 224817 | 2026-06-08 |
| BHARTIARTL | 224817 | 2026-06-08 | MARUTI | 224815 | 2026-06-08 |
| BSE | 224815 | 2026-06-08 | JIOFIN | 224814 | 2026-06-08 |
| TCS | 224815 | 2026-06-08 | INDIGO | 224816 | 2026-06-08 |
| AXISBANK | 224816 | 2026-06-08 | TATASTEEL | 224817 | 2026-06-08 |
| TMPV | 224782 | 2026-06-08 | RVNL | 224816 | 2026-06-08 |
| KOTAKBANK | 224815 | 2026-06-08 | TRENT | 224818 | 2026-06-08 |
| M&M | 224816 | 2026-06-08 | INDUSINDBK | 224817 | 2026-06-08 |
| BAJFINANCE | 224816 | 2026-06-08 | IRFC | 224816 | 2026-06-08 |
| LT | 224816 | 2026-06-08 | HAL | 224817 | 2026-06-08 |

## Sector indices in the map (native `interval='D'`)
All ✅ full history, ~1095 daily bars, 2022-01-03 .. 2026-06-04:
NIFTY, NIFTYPVTBANK, NIFTYPSUBANK, NIFTYIT, NIFTYAUTO, NIFTYMETAL, NIFTYFMCG,
NIFTYOILANDGAS (1084), NIFTYCONSRDURBL (1096), FINNIFTY.
(Unused but present: BANKNIFTY, NIFTYENERGY, NIFTYPHARMA, NIFTYHEALTHCARE,
NIFTYREALTY, NIFTYMEDIA.)

**Gotcha:** three indices appear twice — spaced-name short-history duplicates
(`NIFTY OIL AND GAS`, `NIFTY HEALTHCARE`, `NIFTY CONSR DURBL`, 290 bars from
2025-04) vs the long-history no-space forms (`NIFTYOILANDGAS`, `NIFTYHEALTHCARE`,
`NIFTYCONSRDURBL`). **Use the no-space forms.**

## Verdict
No blocking gaps. 1m through 2026-06-08 confirms the 15:20-IST running-OHLC signal
is feasible. Backtest and live signal both build daily from 1m, never from stored `D`.
