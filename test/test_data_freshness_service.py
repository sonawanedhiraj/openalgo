"""Unit tests for services/data_freshness_service.py.

Builds a throwaway DuckDB ``market_data`` table per test (no live feed, no
strategy DB), drives the pure freshness logic, and monkeypatches the
strategy→symbols resolver so the check is hermetic.
"""

from datetime import date, datetime, timedelta, timezone

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
# Issue #253 — holiday-aware trading-day predicate.
#
# The market_calendar_db seed data (database/market_calendar_db.py) ships
# 2026-01-26 (Republic Day, TRADING_HOLIDAY) — a real, already-seeded NSE
# holiday we can assert against without injecting fixture rows. The
# conftest.py `_isolate_databases` fixture calls `market_calendar_db.init_db()`
# for every test session, which seeds 2025+2026 into the isolated temp DB.
# --------------------------------------------------------------------------- #
def test_is_trading_day_true_for_ordinary_weekday():
    # 2026-01-27 (Tue) — ordinary trading day, no holiday row.
    assert dfs.is_trading_day(date(2026, 1, 27)) is True


def test_is_trading_day_false_for_weekend():
    assert dfs.is_trading_day(date(2026, 6, 13)) is False  # Saturday


def test_is_trading_day_false_for_seeded_nse_holiday():
    # 2026-01-26 is Republic Day — a seeded TRADING_HOLIDAY, and also happens
    # to be a Monday (a genuine weekday), so this proves the holiday check
    # fires independently of the weekend check.
    d = date(2026, 1, 26)
    assert d.weekday() < 5, "sanity: Republic Day 2026 must be a weekday for this test"
    assert dfs.is_trading_day(d) is False


def test_is_trading_day_fail_open_when_year_has_no_calendar_rows(caplog):
    """No seed_holidays_2027() exists yet — the predicate must degrade to
    weekday-only behavior and log a WARNING once, never raise or brick the
    freshness check (load-bearing per issue #253)."""
    caplog.set_level("WARNING", logger="services.data_freshness_service")
    # Clear this module's per-process caches so the test is independent of
    # ordering/caching from other tests in this file.
    dfs._year_has_rows_cache.pop(2027, None)
    dfs._warned_missing_years.discard(2027)

    tue = date(2027, 1, 5)  # an ordinary Tuesday in a year with zero rows
    sat = date(2027, 1, 9)  # a Saturday in the same unseeded year

    assert dfs.is_trading_day(tue) is True  # weekday fallback
    assert dfs.is_trading_day(sat) is False  # weekend still correctly rejected

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("2027" in r.message for r in warnings), (
        f"expected a WARNING naming the missing year 2027; got: {[r.message for r in warnings]}"
    )
    # Dedup: calling again must not add a second warning for the same year.
    n_before = len(caplog.records)
    dfs.is_trading_day(date(2027, 1, 6))
    assert len(caplog.records) == n_before, "expected the missing-year warning to be deduped"


def test_is_trading_day_fail_open_when_calendar_lookup_raises(monkeypatch, caplog):
    """A raising calendar backend must never propagate — fail open to the
    weekday check and log via logger.exception (not crash the caller)."""
    caplog.set_level("WARNING", logger="services.data_freshness_service")

    def _boom(*_a, **_kw):
        raise RuntimeError("calendar backend unavailable")

    # Force the "has rows" check to succeed (so we reach the is_market_holiday
    # call) but make the holiday lookup itself explode.
    dfs._year_has_rows_cache[2026] = True
    monkeypatch.setattr("database.market_calendar_db.is_market_holiday", _boom)

    # Tuesday, ordinary weekday — must resolve True despite the raise.
    assert dfs.is_trading_day(date(2026, 6, 16)) is True
    dfs._year_has_rows_cache.pop(2026, None)  # restore for later tests


# --------------------------------------------------------------------------- #
# Issue #253 — business-day math is now holiday-aware via is_trading_day.
# --------------------------------------------------------------------------- #
def test_prev_or_same_business_day_rolls_back_holiday():
    # 2026-01-26 (Mon) is Republic Day; the preceding trading day is Fri 01-23.
    holiday = date(2026, 1, 26)
    fri = date(2026, 1, 23)
    assert dfs._prev_or_same_business_day(holiday) == fri


def test_business_days_between_excludes_seeded_holiday():
    # Fri 2026-01-23 -> Tue 2026-01-27, spanning weekend (Sat/Sun) + Republic
    # Day (Mon 01-26). Only Tue counts as a new business day.
    fri = date(2026, 1, 23)
    tue = date(2026, 1, 27)
    assert dfs.business_days_between(fri, tue) == 1


# --------------------------------------------------------------------------- #
# Issue #193 — compute_incremental_start_date: smallest necessary catch-up
# window using per-symbol last_date from compute_stale_symbols' details dict.
# Pre-#193 the convergence schedulers re-fetched a fixed [today - LOOKBACK,
# today] window on every boot regardless of what was already on disk.
# --------------------------------------------------------------------------- #
from datetime import date as _D  # noqa: E402 — keep tests self-contained


def test_incremental_start_all_symbols_at_friday_returns_saturday():
    """Sunday boot with every stale symbol holding Friday's bars → start = Sat,
    NOT Wed (pre-fix behavior). Smallest possible re-fetch window."""
    sun = _D(2026, 6, 28)
    fri = _D(2026, 6, 26)
    details = {f"S{i}": {"last_date": fri.isoformat()} for i in range(5)}
    got = dfs.compute_incremental_start_date(details, list(details), sun, lookback_days=4)
    assert got == fri + timedelta(days=1) == _D(2026, 6, 27)


def test_incremental_start_no_data_falls_back_to_full_lookback():
    """At least one symbol has no stored bars → fall back to full LOOKBACK so the
    initial fetch is wide enough. (Mixing per-symbol windows would require an
    API change to the backfill helpers.)"""
    sun = _D(2026, 6, 28)
    details = {f"S{i}": {"last_date": None} for i in range(3)}
    got = dfs.compute_incremental_start_date(details, list(details), sun, lookback_days=4)
    assert got == sun - timedelta(days=4)


def test_incremental_start_mixed_last_dates_driven_by_earliest():
    """When stale symbols are at different staleness levels, the earliest one
    drives the window. (Symbols already past that point just re-upsert overlap
    — no extra broker cost beyond what the slowest stale symbol needs.)"""
    sun = _D(2026, 6, 28)
    details = {
        "OLD": {"last_date": _D(2026, 6, 24).isoformat()},  # Wednesday
        "MID": {"last_date": _D(2026, 6, 25).isoformat()},  # Thursday
        "NEW": {"last_date": _D(2026, 6, 26).isoformat()},  # Friday
    }
    got = dfs.compute_incremental_start_date(details, list(details), sun, lookback_days=4)
    # OLD's last bar is Wed → start = Thu (Wed + 1).
    assert got == _D(2026, 6, 25)


def test_incremental_start_deep_gap_is_capped_at_lookback_floor():
    """If last_date is months old (a recovery situation), the helper still caps
    the window at LOOKBACK_DAYS so we don't accidentally pull years of data
    through the live convergence path. Deep gaps are CLI/manual territory."""
    sun = _D(2026, 6, 28)
    details = {"OLD": {"last_date": "2026-01-01"}}
    got = dfs.compute_incremental_start_date(details, list(details), sun, lookback_days=4)
    assert got == sun - timedelta(days=4)


def test_incremental_start_clamps_at_ref_date_when_last_is_today():
    """If last_date == today already, start should never exceed the reference
    (impossible-but-defensive: a same-day re-run still wants today's bars)."""
    sun = _D(2026, 6, 28)
    details = {"X": {"last_date": sun.isoformat()}}
    got = dfs.compute_incremental_start_date(details, list(details), sun, lookback_days=4)
    assert got == sun


def test_incremental_start_malformed_last_date_falls_back_safely():
    """A junk ``last_date`` is treated as ``no-data`` (full LOOKBACK fallback)
    so a single bad row can never crash the convergence."""
    sun = _D(2026, 6, 28)
    details = {"BAD": {"last_date": "not-a-date"}}
    got = dfs.compute_incremental_start_date(details, list(details), sun, lookback_days=4)
    assert got == sun - timedelta(days=4)


def test_incremental_start_with_missing_symbol_in_details():
    """A stale symbol with NO entry in ``details`` is treated as no-data and
    triggers the LOOKBACK fallback (defensive — should never happen in
    practice since ``compute_stale_symbols`` populates details for every input
    symbol, but the helper must not KeyError)."""
    sun = _D(2026, 6, 28)
    details = {}  # entirely empty — caller bug, but helper must survive
    got = dfs.compute_incremental_start_date(details, ["GHOST"], sun, lookback_days=4)
    assert got == sun - timedelta(days=4)


def test_incremental_start_empty_stale_returns_ref():
    """Defensive — callers short-circuit on empty stale list before calling, but
    the helper should not divide-by-zero or return something silly if invoked."""
    sun = _D(2026, 6, 28)
    got = dfs.compute_incremental_start_date({}, [], sun, lookback_days=4)
    assert got == sun


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


def test_holiday_gap_is_not_stale(tmp_duckdb, patch_symbols):
    """Issue #253 — last bar Fri 2026-01-23 (D), Mon 2026-01-26 is Republic Day
    (D+1, a seeded NSE TRADING_HOLIDAY), checked on Tue 2026-01-27 (D+2).

    With max_staleness_business_days=1 this must read FRESH: the only new
    TRADING day between D and D+2 is D+2 itself (D+1 is a holiday, not a
    trading day), so staleness == 1 <= threshold 1.

    Pre-#253 (holidays not modelled), D+1 would count as a missed business
    day too, giving staleness == 2 > threshold 1 -> STALE. Verified by
    stashing services/data_freshness_service.py and re-running this test —
    it fails (``ok is False``) on the pre-fix tree.
    """
    patch_symbols(["AAA"], ["NIFTY"])
    path = tmp_duckdb({"AAA": (2026, 1, 23), "NIFTY": (2026, 1, 23)})
    ok, details = dfs.check_strategy_data_ready(
        "x", date="2026-01-27", duckdb_path=path, max_staleness_business_days=1
    )
    assert ok is True
    assert details["AAA"]["staleness_days"] == 1


def test_real_trading_gap_still_stale(tmp_duckdb, patch_symbols):
    """Control for the holiday test above — an ordinary (non-holiday) gap of
    the same shape must still read STALE. Last bar Mon 2026-01-19, reference
    Wed 2026-01-21: Tue 01-20 and Wed 01-21 are both ordinary trading days,
    so staleness == 2 > threshold 1 -> STALE."""
    patch_symbols(["AAA"], ["NIFTY"])
    path = tmp_duckdb({"AAA": (2026, 1, 19), "NIFTY": (2026, 1, 19)})
    ok, details = dfs.check_strategy_data_ready(
        "x", date="2026-01-21", duckdb_path=path, max_staleness_business_days=1
    )
    assert ok is False
    assert details["AAA"]["staleness_days"] == 2


def test_weekend_plus_holiday_bridge_is_not_stale(tmp_duckdb, patch_symbols):
    """Issue #253 — the weekend-then-holiday bridge: last bar Fri 2026-01-23,
    then Sat/Sun (weekend) immediately followed by Mon 2026-01-26 (Republic
    Day, a seeded NSE TRADING_HOLIDAY), checked on Tue 2026-01-27.

    Only Tue is a genuine trading day in the gap, so with threshold 0
    (the strictest — "today's close required") staleness must be exactly 1,
    never 2 (weekday-only counting would count Mon as a missed business day
    too: Mon+Tue == 2). This is the calendar shape the issue names explicitly
    ("Fri last bar, Sat/Sun + Mon holiday, check Tue").
    """
    patch_symbols(["AAA"], ["NIFTY"])
    path = tmp_duckdb({"AAA": (2026, 1, 23), "NIFTY": (2026, 1, 23)})
    ok, details = dfs.check_strategy_data_ready(
        "x", date="2026-01-27", duckdb_path=path, max_staleness_business_days=1
    )
    assert details["AAA"]["staleness_days"] == 1  # NOT 2 — the bridge collapses correctly
    assert ok is True  # threshold 1 -> staleness 1 is FRESH


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


# --------------------------------------------------------------------------- #
# Issue #191 / #156 Phase 1 — singleton + path-mismatch fallthrough
#
# The pre-#191 fallback-ladder tests (caught ConnectionException / IOException /
# BinderException via monkeypatched duckdb.connect, asserted backoff intervals,
# etc.) were deleted in the singleton commit because that whole code path no
# longer exists. The singleton makes the config-mismatch errors mathematically
# impossible in production, and the path-mismatch fallthrough is the only
# remaining branch (test ergonomics).
#
# ``is_transient_lock_error`` is kept and tested in the section above — it is
# still used by services/scanner_universe_backfill.py and
# services/sector_follow_backfill_scheduler.py to classify transient errors
# from the historify DOWNLOAD path (a different code path entirely).
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_historify_singleton_each_test():
    """Drop the singleton before and after every test in this module so
    no cursor state leaks between tests through the shared connection."""
    from database.historify_db import _reset_for_tests

    _reset_for_tests()
    yield
    _reset_for_tests()


def test_connect_historify_readonly_uses_singleton_when_path_matches(monkeypatch):
    """Production happy path: caller passes the configured HISTORIFY_DATABASE_PATH
    → cursor on the per-process shared connection, zero fresh ``duckdb.connect``."""
    from database.historify_db import _get_shared_conn, get_db_path

    # Track every duckdb.connect call — there should be exactly ONE (the
    # singleton's lazy init), not one per ``connect_historify_readonly`` call.
    calls = []
    real_connect = duckdb.connect

    def counting_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(duckdb, "connect", counting_connect)

    shared = _get_shared_conn()
    cur1 = dfs.connect_historify_readonly(get_db_path())
    cur2 = dfs.connect_historify_readonly(get_db_path())
    try:
        # Both cursors are distinct from each other AND from the singleton,
        # but they share the underlying database (proved by a write through
        # the singleton becoming visible on the cursors).
        assert cur1 is not cur2
        assert cur1 is not shared and cur2 is not shared
        shared.execute("CREATE OR REPLACE TABLE _probe (k INT)")
        n1 = cur1.execute("SELECT COUNT(*) FROM _probe").fetchone()[0]
        n2 = cur2.execute("SELECT COUNT(*) FROM _probe").fetchone()[0]
        assert n1 == 0 and n2 == 0
    finally:
        cur1.close()
        cur2.close()

    # Only the singleton's lazy init opened the file; the two readonly calls
    # returned cursors and did NOT invoke duckdb.connect again.
    assert len(calls) == 1, (
        f"expected exactly 1 duckdb.connect (singleton lazy init), got {len(calls)}"
    )


def test_connect_historify_readonly_falls_through_for_mismatched_path(tmp_duckdb, caplog):
    """Test ergonomic: a caller passing a per-test tmpdir DB gets a fresh
    read-only connection on THAT path, with a WARNING surfacing the bypass."""
    path = tmp_duckdb({"AAA": (2026, 6, 11)})

    caplog.set_level("WARNING", logger="services.data_freshness_service")
    con = dfs.connect_historify_readonly(path)
    try:
        rows = con.execute("SELECT symbol FROM market_data").fetchall()
    finally:
        con.close()
    assert rows == [("AAA",)]
    # The WARNING is mandatory — it's the only signal in production that a
    # caller is bypassing the singleton and re-introducing the #191 race.
    assert any(
        "singleton path" in r.message and "separate read-only connection" in r.message
        for r in caplog.records
    ), f"expected the path-mismatch WARNING; got: {[r.message for r in caplog.records]}"


def test_connect_historify_readonly_path_mismatch_actually_uses_requested_path(tmp_path):
    """Belt-and-braces: the fallthrough must read from the REQUESTED file, not
    accidentally fall through to the singleton's path. A test that populated
    its own tmpdir would otherwise silently see zero rows from a different DB.

    Builds the two DBs inline (instead of using the ``tmp_duckdb`` factory,
    which always writes to ``fresh.duckdb`` and would collide on a second
    call) so we can prove the fallthrough honors the path on a per-call basis.
    """
    path_a = str(tmp_path / "a.duckdb")
    path_b = str(tmp_path / "b.duckdb")
    for path, sym in ((path_a, "ONLY_IN_A"), (path_b, "ONLY_IN_B")):
        seed = duckdb.connect(path)
        seed.execute(
            "CREATE TABLE market_data ("
            "symbol VARCHAR, interval VARCHAR, timestamp BIGINT, close DOUBLE)"
        )
        seed.execute(
            "INSERT INTO market_data VALUES (?, '1m', ?, 100.0)",
            [sym, _epoch(2026, 6, 11)],
        )
        seed.close()

    con_a = dfs.connect_historify_readonly(path_a)
    try:
        rows_a = sorted(r[0] for r in con_a.execute("SELECT symbol FROM market_data").fetchall())
    finally:
        con_a.close()
    con_b = dfs.connect_historify_readonly(path_b)
    try:
        rows_b = sorted(r[0] for r in con_b.execute("SELECT symbol FROM market_data").fetchall())
    finally:
        con_b.close()

    assert rows_a == ["ONLY_IN_A"]
    assert rows_b == ["ONLY_IN_B"]
