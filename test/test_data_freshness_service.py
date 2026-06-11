"""Unit tests for services/data_freshness_service.py.

Builds a throwaway DuckDB ``market_data`` table per test (no live feed, no
strategy DB), drives the pure freshness logic, and monkeypatches the
strategy→symbols resolver so the check is hermetic.
"""

from datetime import datetime, timedelta, timezone

import duckdb
import pytest

import services.data_freshness_service as dfs

_IST = timezone(timedelta(hours=5, minutes=30))


def _epoch(y, m, d, hh=15, mm=29):
    """UTC epoch for an IST wall-clock time (matches market_data convention)."""
    return int(datetime(y, m, d, hh, mm, tzinfo=_IST).timestamp())


@pytest.fixture
def tmp_duckdb(tmp_path):
    """Factory: write {symbol: last_ist_date} into a fresh DuckDB, return its path.

    Each symbol gets a single 1m bar at its given date's 15:29 IST close.
    ``date`` may be a (y,m,d) tuple, or None to omit the symbol entirely.
    """

    def _make(symbol_dates):
        path = str(tmp_path / "fresh.duckdb")
        con = duckdb.connect(path)
        con.execute(
            "CREATE TABLE market_data ("
            "symbol VARCHAR, interval VARCHAR, timestamp BIGINT, close DOUBLE)"
        )
        for sym, ymd in symbol_dates.items():
            if ymd is None:
                continue
            con.execute(
                "INSERT INTO market_data VALUES (?, '1m', ?, 100.0)",
                [sym, _epoch(*ymd)],
            )
        con.close()
        return path

    return _make


# --------------------------------------------------------------------------- #
# Business-day helpers
# --------------------------------------------------------------------------- #
def test_business_days_between_counts_weekdays_only():
    fri = datetime(2026, 6, 12).date()
    mon = datetime(2026, 6, 15).date()
    # Fri -> Mon spans a weekend: only Monday is a new business day.
    assert dfs.business_days_between(fri, mon) == 1


def test_business_days_between_zero_when_not_behind():
    d = datetime(2026, 6, 10).date()
    assert dfs.business_days_between(d, d) == 0
    assert dfs.business_days_between(d, datetime(2026, 6, 9).date()) == 0  # ahead


def test_prev_or_same_business_day_rolls_back_weekend():
    sat = datetime(2026, 6, 13).date()
    sun = datetime(2026, 6, 14).date()
    fri = datetime(2026, 6, 12).date()
    assert dfs._prev_or_same_business_day(sat) == fri
    assert dfs._prev_or_same_business_day(sun) == fri
    assert dfs._prev_or_same_business_day(fri) == fri  # weekday unchanged


# --------------------------------------------------------------------------- #
# get_data_freshness
# --------------------------------------------------------------------------- #
def test_get_data_freshness_returns_last_ts_and_none_for_missing(tmp_duckdb):
    path = tmp_duckdb({"AAA": (2026, 6, 10), "BBB": None})
    out = dfs.get_data_freshness(path, ["AAA", "BBB", "CCC"])
    assert out["AAA"] == _epoch(2026, 6, 10)
    assert out["BBB"] is None  # present in table-less? no rows -> None
    assert out["CCC"] is None  # never queried symbol -> None


# --------------------------------------------------------------------------- #
# check_strategy_data_ready
# --------------------------------------------------------------------------- #
@pytest.fixture
def patch_symbols(monkeypatch):
    """Make the strategy resolve to a tiny stock+index set."""

    def _patch(stocks, indices):
        monkeypatch.setattr(
            dfs,
            "_resolve_strategy_symbols",
            lambda name: {"stock": list(stocks), "index": list(indices)},
        )

    return _patch


def test_fresh_data_returns_ok(tmp_duckdb, patch_symbols):
    patch_symbols(["AAA", "BBB"], ["NIFTY"])
    # Reference Thu 06-11; all bars at 06-10 close -> staleness 1 (threshold 1).
    path = tmp_duckdb({"AAA": (2026, 6, 10), "BBB": (2026, 6, 10), "NIFTY": (2026, 6, 10)})
    ok, details = dfs.check_strategy_data_ready(
        "x", date="2026-06-11", duckdb_path=path, max_staleness_business_days=1
    )
    assert ok is True
    assert all(d["ok"] for d in details.values())
    assert details["NIFTY"]["kind"] == "index"
    assert details["AAA"]["kind"] == "stock"


def test_single_stale_symbol_flags_not_ok(tmp_duckdb, patch_symbols):
    patch_symbols(["AAA", "BBB"], ["NIFTY"])
    # BBB stuck at Mon 06-08 -> staleness 3 (Tue/Wed/Thu) vs ref Thu 06-11
    # (threshold 1) -> stale. AAA at 06-10 -> staleness 1 -> fresh.
    path = tmp_duckdb({"AAA": (2026, 6, 10), "BBB": (2026, 6, 8), "NIFTY": (2026, 6, 10)})
    ok, details = dfs.check_strategy_data_ready(
        "x", date="2026-06-11", duckdb_path=path, max_staleness_business_days=1
    )
    assert ok is False
    stale = [s for s, d in details.items() if not d["ok"]]
    assert stale == ["BBB"]
    assert details["BBB"]["staleness_days"] == 3


def test_missing_symbol_is_not_ok(tmp_duckdb, patch_symbols):
    patch_symbols(["AAA"], ["NIFTY"])
    path = tmp_duckdb({"AAA": (2026, 6, 10), "NIFTY": None})
    ok, details = dfs.check_strategy_data_ready(
        "x", date="2026-06-11", duckdb_path=path
    )
    assert ok is False
    assert details["NIFTY"]["ok"] is False
    assert details["NIFTY"]["staleness_days"] is None


def test_all_stale_returns_not_ok(tmp_duckdb, patch_symbols):
    patch_symbols(["AAA", "BBB"], ["NIFTY"])
    path = tmp_duckdb({"AAA": (2026, 5, 29), "BBB": (2026, 5, 29), "NIFTY": (2026, 5, 29)})
    ok, details = dfs.check_strategy_data_ready(
        "x", date="2026-06-11", duckdb_path=path, max_staleness_business_days=1
    )
    assert ok is False
    assert not any(d["ok"] for d in details.values())


def test_weekend_is_not_stale(tmp_duckdb, patch_symbols):
    patch_symbols(["AAA"], ["NIFTY"])
    # Last bar Fri 06-12; reference Mon 06-15 -> staleness 1 (weekend not counted).
    path = tmp_duckdb({"AAA": (2026, 6, 12), "NIFTY": (2026, 6, 12)})
    ok, details = dfs.check_strategy_data_ready(
        "x", date="2026-06-15", duckdb_path=path, max_staleness_business_days=1
    )
    assert ok is True
    assert details["AAA"]["staleness_days"] == 1


def test_threshold_is_configurable(tmp_duckdb, patch_symbols):
    patch_symbols(["AAA"], ["NIFTY"])
    # Bars at 06-10, reference 06-11 -> staleness 1.
    path = tmp_duckdb({"AAA": (2026, 6, 10), "NIFTY": (2026, 6, 10)})
    ok0, _ = dfs.check_strategy_data_ready(
        "x", date="2026-06-11", duckdb_path=path, max_staleness_business_days=0
    )
    ok2, _ = dfs.check_strategy_data_ready(
        "x", date="2026-06-11", duckdb_path=path, max_staleness_business_days=2
    )
    assert ok0 is False  # staleness 1 > threshold 0
    assert ok2 is True  # staleness 1 <= threshold 2


def test_index_only_skips_stocks(tmp_duckdb, patch_symbols):
    patch_symbols(["AAA"], ["NIFTY"])
    # Stock AAA badly stale, index NIFTY fresh; index_only -> ok (stocks ignored).
    path = tmp_duckdb({"AAA": (2026, 5, 1), "NIFTY": (2026, 6, 10)})
    ok, details = dfs.check_strategy_data_ready(
        "x", date="2026-06-11", duckdb_path=path, index_only=True
    )
    assert ok is True
    assert "AAA" not in details
    assert "NIFTY" in details


# --------------------------------------------------------------------------- #
# format_freshness_report
# --------------------------------------------------------------------------- #
def test_format_report_ok_and_stale():
    ok_details = {"AAA": {"ok": True, "last_date": "2026-06-10", "staleness_days": 1, "kind": "stock"}}
    assert "✅ OK" in dfs.format_freshness_report("s", ok_details)

    stale_details = {
        "NIFTY": {"ok": False, "last_date": "2026-05-29", "staleness_days": 9, "kind": "index"},
        "AAA": {"ok": True, "last_date": "2026-06-10", "staleness_days": 1, "kind": "stock"},
    }
    report = dfs.format_freshness_report("s", stale_details)
    assert "STALE" in report
    assert "NIFTY" in report
    assert "2026-05-29" in report
