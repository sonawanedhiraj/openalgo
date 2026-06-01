# Simplified Engine Strategy

Chartink FnO Intraday Breakout — scans for top gainers/losers (>3%) and trades
5-minute candle breakouts with ATR-based stop-loss and trailing.

## Files

| File | Purpose |
|------|---------|
| `LEARNINGS.md` | Cumulative strategy knowledge — what works, what doesn't |
| `VERSION_LOG.md` | Changelog of parameter/logic changes with evidence |
| `config_snapshot.json` | Current live config values |
| `README.md` | This file |

## Related Code

- `services/simplified_stock_engine_core.py` — broker-agnostic engine logic
- `services/simplified_stock_engine_service.py` — OpenAlgo integration
- `services/simplified_stock_engine_ticklog.py` — tick log writer
- `blueprints/chartink.py:947+` — webhook and API routes
- `backtest/run_backtest.py` — day replay / backtester

## How to Backtest

```bash
# Always use --from-engine to match live config
uv run python backtest/run_backtest.py --date YYYY-MM-DD --from-engine

# With tick data
uv run python backtest/run_backtest.py --date YYYY-MM-DD --from-engine --tick-data tick_logs

# Test parameter changes before going live
uv run python backtest/run_backtest.py --date YYYY-MM-DD --from-engine --atr-sl-mult 1.8
```

## How to Change Config

1. Backtest current config across multiple days
2. Backtest proposed change across the same days
3. Compare results — record in VERSION_LOG.md
4. Update `.env` with new `SIMPLIFIED_ENGINE_*` values
5. Restart OpenAlgo
6. Update `config_snapshot.json` with new values
