# Simplified Engine — Version Log

Track every parameter or logic change here with date, rationale, and evidence.

---

## v1.0 — Initial Defaults (pre May 20, 2026)

**Source**: `SimplifiedEngineConfig` dataclass defaults in `simplified_stock_engine_core.py`

| Parameter | Value |
|-----------|-------|
| atr_sl_mult | 1.2 |
| max_trades_per_day | 6 |
| cooldown_candles | 0 |
| capital | 20,000 |
| leverage | 5.0 |
| max_risk_per_trade | 500 |
| volume_multiplier | 2.5 |

**Notes**: These were the hardcoded dataclass defaults. Used by the backtester
before the `--from-engine` fix.

---

## v1.1 — Wider Stops + Trade Discipline (before May 20, 2026)

**Changed by**: Dheeraj (via .env configuration)

| Parameter | Old | New | Rationale |
|-----------|-----|-----|-----------|
| atr_sl_mult | 1.2 | 1.5 | Reduce whipsaw exits on intraday noise |
| max_trades_per_day | 6 | 4 | Prevent over-trading, improve entry quality |
| cooldown_candles | 0 | 3 | Force 15-min gap between trades on same stock |

**Evidence**: May 21 comparison showed v1.0 config produced -₹1,479 while v1.1
produced +₹621 on the same day. The wider ATR multiplier was the primary driver —
it prevented 3 false stop-outs.

**Backtest comparison** (May 21, same stock universe):
- v1.0: 6 trades, 33% win rate, -₹1,479.25
- v1.1: 6 trades, 83% win rate, +₹621.55

---

## Pending Changes (Not Yet Applied)

_None currently. See LEARNINGS.md "Open Questions" for research items._
