import datetime as dt

from services.simplified_stock_engine_core import Candle
from services.simplified_stock_engine_service import (
    SimplifiedStockEngineService,
    normalize_chartink_symbol,
    parse_chartink_symbols,
)


def test_parse_chartink_symbols_normalizes_and_deduplicates():
    payload = {
        "stocks": "NSE:RELIANCE, INFY.NS, TCS-EQ",
        "symbol": "RELIANCE",
        "nsecode": ["WIPRO"],
    }

    assert parse_chartink_symbols(payload) == ["RELIANCE", "INFY", "TCS", "WIPRO"]


def test_normalize_chartink_symbol_removes_common_chartink_suffixes():
    assert normalize_chartink_symbol("NSE:RELIANCE-EQ") == "RELIANCE"
    assert normalize_chartink_symbol("infy.ns") == "INFY"


def test_history_row_to_candle_accepts_epoch_millis():
    row = {
        "timestamp": 1_777_433_100_000,
        "open": 100,
        "high": 102,
        "low": 99,
        "close": 101,
        "volume": 1000,
    }

    candle = SimplifiedStockEngineService._row_to_candle(row)

    assert isinstance(candle, Candle)
    assert candle.elapsed_pct == 1.0
    assert candle.ts.second == 0
    assert candle.ts.microsecond == 0


def test_history_row_to_candle_accepts_datetime_string():
    row = {
        "datetime": "2026-04-29T10:17:00+05:30",
        "open": 100,
        "high": 102,
        "low": 99,
        "close": 101,
        "volume": 1000,
    }

    candle = SimplifiedStockEngineService._row_to_candle(row)

    assert candle.ts == dt.datetime(2026, 4, 29, 10, 15)
