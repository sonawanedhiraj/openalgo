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
    ok, details = dfs.check_strategy_data_ready("x", date="2026-06-11", duckdb_path=path)
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
    ok_details = {
        "AAA": {"ok": True, "last_date": "2026-06-10", "staleness_days": 1, "kind": "stock"}
    }
    assert "✅ OK" in dfs.format_freshness_report("s", ok_details)

    stale_details = {
        "NIFTY": {"ok": False, "last_date": "2026-05-29", "staleness_days": 9, "kind": "index"},
        "AAA": {"ok": True, "last_date": "2026-06-10", "staleness_days": 1, "kind": "stock"},
    }
    report = dfs.format_freshness_report("s", stale_details)
    assert "STALE" in report
    assert "NIFTY" in report
    assert "2026-05-29" in report


# --------------------------------------------------------------------------- #
# DuckDB lock-tolerant connection helpers
# --------------------------------------------------------------------------- #
def test_is_transient_lock_error_classifies_known_messages():
    cfg = duckdb.ConnectionException(
        "Can't open a connection to same database file with a different "
        "configuration than existing connections"
    )
    assert dfs.is_transient_lock_error(cfg) is True
    assert dfs.is_transient_lock_error(RuntimeError("Could not set lock on file")) is True
    assert dfs.is_transient_lock_error(RuntimeError("Conflicting lock is held")) is True
    # Genuine faults must NOT be swallowed as transient.
    assert dfs.is_transient_lock_error(RuntimeError("token expired")) is False
    assert dfs.is_transient_lock_error(ValueError("no api key available")) is False


def test_connect_historify_readonly_falls_back_on_config_mismatch(tmp_duckdb, monkeypatch):
    """When read_only is refused by an in-process read-write holder, fall back to a
    config-matching connect so the read still succeeds (the lock-contention fix)."""
    path = tmp_duckdb({"AAA": (2026, 6, 11)})
    calls = []
    real_connect = duckdb.connect

    def fake_connect(p, *args, read_only=False, **kwargs):
        calls.append(read_only)
        if read_only:
            raise duckdb.ConnectionException(
                "Can't open a connection to same database file with a different "
                "configuration than existing connections"
            )
        return real_connect(p, *args, read_only=read_only, **kwargs)

    monkeypatch.setattr(duckdb, "connect", fake_connect)

    con = dfs.connect_historify_readonly(path)
    try:
        rows = con.execute("SELECT symbol FROM market_data").fetchall()
    finally:
        con.close()
    assert rows == [("AAA",)]
    # Tried read_only first (refused), then fell back to the shared config.
    assert calls == [True, False]


def test_connect_historify_readonly_reraises_unrelated_connection_error(monkeypatch):
    """A non-config-mismatch ConnectionException must propagate (no silent fallback)."""

    def fake_connect(p, *args, read_only=False, **kwargs):
        raise duckdb.ConnectionException("database does not exist")

    monkeypatch.setattr(duckdb, "connect", fake_connect)
    with pytest.raises(duckdb.ConnectionException, match="does not exist"):
        dfs.connect_historify_readonly("nope.duckdb")


# --------------------------------------------------------------------------- #
# Issue #126 — broadened fallback for IOException + BinderException + retry
# --------------------------------------------------------------------------- #
def test_is_transient_lock_error_recognises_attach_conflict():
    """BinderException attach-conflict is now classified as transient (issue #126)."""
    err = duckdb.BinderException(
        'Unique file handle conflict: Cannot attach "historify" - the database '
        'file is already attached by database "historify"'
    )
    assert dfs.is_transient_lock_error(err) is True


def test_connect_historify_readonly_falls_back_on_io_error(tmp_duckdb, monkeypatch):
    """IOException 'being used by another process' must trigger the fallback (issue #126).

    Before this fix, only ConnectionException was caught — IOException re-raised,
    producing 40+ 'Cannot open file ... being used by another process' errors in
    log/errors.jsonl despite PR #118's fallback existing.
    """
    path = tmp_duckdb({"AAA": (2026, 6, 11)})
    calls = []
    real_connect = duckdb.connect

    def fake_connect(p, *args, read_only=False, **kwargs):
        calls.append(read_only)
        if read_only:
            raise duckdb.IOException(
                f'IO Error: Cannot open file "{p}": The process cannot access '
                "the file because it is being used by another process."
            )
        return real_connect(p, *args, read_only=read_only, **kwargs)

    monkeypatch.setattr(duckdb, "connect", fake_connect)

    con = dfs.connect_historify_readonly(path)
    try:
        rows = con.execute("SELECT symbol FROM market_data").fetchall()
    finally:
        con.close()
    assert rows == [("AAA",)]
    # First read_only attempt refused with IOException, fallback succeeded.
    assert calls == [True, False]


def test_connect_historify_readonly_falls_back_on_binder_attach_conflict(tmp_duckdb, monkeypatch):
    """BinderException attach-conflict must trigger the fallback (issue #126)."""
    path = tmp_duckdb({"BBB": (2026, 6, 11)})
    calls = []
    real_connect = duckdb.connect

    def fake_connect(p, *args, read_only=False, **kwargs):
        calls.append(read_only)
        if read_only:
            raise duckdb.BinderException(
                'Unique file handle conflict: Cannot attach "historify" - '
                "the database file is already attached"
            )
        return real_connect(p, *args, read_only=read_only, **kwargs)

    monkeypatch.setattr(duckdb, "connect", fake_connect)

    con = dfs.connect_historify_readonly(path)
    try:
        rows = con.execute("SELECT symbol FROM market_data").fetchall()
    finally:
        con.close()
    assert rows == [("BBB",)]
    assert calls == [True, False]


def test_connect_historify_readonly_retries_fallback_under_contention(monkeypatch):
    """When the fallback ALSO fails transiently, retry with backoff (issue #126).

    Simulates: read_only refused → fallback default-config connect also racing
    with a brief write → 2 fallback failures → 3rd succeeds.
    """
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    attempts = {"read_only_attempts": 0, "default_attempts": 0}
    sentinel_conn = object()

    def fake_connect(p, *args, read_only=False, **kwargs):
        if read_only:
            attempts["read_only_attempts"] += 1
            raise duckdb.ConnectionException("different configuration than existing connections")
        attempts["default_attempts"] += 1
        if attempts["default_attempts"] < 3:
            raise duckdb.IOException("Cannot open file: being used by another process")
        return sentinel_conn

    monkeypatch.setattr(duckdb, "connect", fake_connect)

    result = dfs.connect_historify_readonly("any.duckdb", max_retries=3)
    assert result is sentinel_conn
    assert attempts["read_only_attempts"] == 1  # tried once
    assert attempts["default_attempts"] == 3  # retried until success
    # Backoff observed: 100ms, 200ms (the 3rd attempt succeeds so no sleep after)
    assert sleeps == [0.1, 0.2]


def test_connect_historify_readonly_raises_after_max_retries(monkeypatch):
    """If contention persists past max_retries, raise the last DuckDB error (issue #126)."""
    monkeypatch.setattr("time.sleep", lambda s: None)

    def fake_connect(p, *args, read_only=False, **kwargs):
        if read_only:
            raise duckdb.ConnectionException("different configuration")
        raise duckdb.IOException("being used by another process")

    monkeypatch.setattr(duckdb, "connect", fake_connect)

    with pytest.raises(duckdb.IOException, match="being used by another process"):
        dfs.connect_historify_readonly("any.duckdb", max_retries=2)


def test_connect_historify_readonly_does_not_retry_non_transient_fallback_error(monkeypatch):
    """A non-transient error in the fallback path must propagate immediately (issue #126)."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    attempts = {"default": 0}

    def fake_connect(p, *args, read_only=False, **kwargs):
        if read_only:
            raise duckdb.ConnectionException("different configuration")
        attempts["default"] += 1
        raise duckdb.ConnectionException("database does not exist")  # not transient

    monkeypatch.setattr(duckdb, "connect", fake_connect)

    with pytest.raises(duckdb.ConnectionException, match="does not exist"):
        dfs.connect_historify_readonly("any.duckdb")
    assert attempts["default"] == 1  # no retry on non-transient error
