"""Data freshness service cross-source contract — the freshness check is the
ONLY observable signal we have that historify has gone stale relative to
the current trading day. Its verdict cannot be silent.

What this test exercises that existing tests do NOT
---------------------------------------------------
``test/test_data_freshness_service.py`` already drives ``compute_stale_symbols``
against a fabricated DuckDB. Its scenarios are: weekend gap is fresh,
fresh-today is fresh, multi-day-old is stale. Those are correctness tests
on the in-bounds case.

This file pins the cross-source contract — the two sources here are:

* the LAST stored ``market_data.MAX(timestamp)`` for each symbol
  (historify's view of "what data we have")
* the REFERENCE day from ``now`` (the trading-day clock's view of "what
  data we should have")

Bug class: the freshness check silently treating "no row at all" the
same as "fresh" — i.e. a symbol that has never been ingested looking
identical in the verdict to one freshly written today. This was the
class of failure that produced the 2026-05-29 → 06-10 incident
(``services/data_freshness_service.py`` module docstring): the index 1m
feed sat 12 days stale because the backfill job didn't exist, and a
silent-fresh verdict from compute_stale_symbols meant the strategy
greenlit trading on stale prices.

The connect helper's path-mismatch fallthrough is a SEPARATE source
divergence: the helper opens a fresh read-only connection when the
requested DB path doesn't match the singleton — a test ergonomic that
intentionally diverges from production. We pin that the helper emits a
WARNING when the fallthrough fires so a misrouted production caller
shows up loudly in errors.jsonl.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import duckdb
import pytest

import services.data_freshness_service as dfs

_IST = timezone(timedelta(hours=5, minutes=30))


def _epoch_at(y: int, m: int, d: int, hh: int = 15, mm: int = 29) -> int:
    """UTC epoch for an IST wall-clock time — same convention as market_data."""
    return int(datetime(y, m, d, hh, mm, tzinfo=_IST).timestamp())


@pytest.fixture
def make_historify(tmp_path):
    """Factory: write {symbol: epoch_ts | None} into a fresh DuckDB.

    ``None`` means "symbol omitted" — no row at all in market_data.
    """

    def _make(symbol_to_ts: dict[str, int | None]) -> str:
        path = str(tmp_path / "fresh.duckdb")
        con = duckdb.connect(path)
        con.execute(
            "CREATE TABLE market_data ("
            "symbol VARCHAR, interval VARCHAR, timestamp BIGINT, close DOUBLE)"
        )
        for sym, ts in symbol_to_ts.items():
            if ts is None:
                continue
            con.execute(
                "INSERT INTO market_data VALUES (?, '1m', ?, 100.0)",
                [sym, ts],
            )
        con.close()
        return path

    return _make


# ----------------------------------------------------------------------- #
# Contract A — DIVERGENT sources (a row 5 business days old vs today's
# reference) is flagged STALE. The bug class is silent-fresh on a
# many-day-old row.
# ----------------------------------------------------------------------- #
def test_5_business_day_old_row_is_stale_not_silently_fresh(make_historify):
    """A symbol whose last bar is 2026-06-22 (Monday), evaluated on
    2026-06-29 (Monday), is 5 business days behind. ``compute_stale_symbols``
    must report it as stale at threshold=1 — never silently fresh.

    The divergence: market_data thinks "last bar = 2026-06-22"; the
    reference clock thinks "today = 2026-06-29". The verdict must reflect
    that gap.
    """
    last_bar = date(2026, 6, 22)  # Monday, 7 calendar days = 5 business days back.
    ref_today = date(2026, 6, 29)  # Monday.

    path = make_historify({"STALE_SYM": _epoch_at(last_bar.year, last_bar.month, last_bar.day)})

    stale, fresh, details = dfs.compute_stale_symbols(
        path,
        ["STALE_SYM"],
        today=ref_today,
        max_staleness_business_days=1,
    )

    assert "STALE_SYM" in stale, (
        f"5-business-day-old row was not flagged stale; details={details!r} — "
        f"silent-fresh bug class"
    )
    assert "STALE_SYM" not in fresh
    assert details["STALE_SYM"]["staleness_days"] == 5
    assert details["STALE_SYM"]["stale"] is True


# ----------------------------------------------------------------------- #
# Contract B — a never-ingested symbol (NO row at all) must be flagged
# stale, NOT silently classified as fresh because MAX(timestamp) is NULL.
# ----------------------------------------------------------------------- #
def test_never_ingested_symbol_is_stale_not_silently_fresh(make_historify):
    """Symbol omitted from market_data entirely — MAX(timestamp) is NULL.
    The 2026-05-29 incident's root mode: a symbol the backfill never wrote
    must NOT be treated as "fresh by absence". The verdict has to call it
    stale loudly so the strategy can refuse to act on no data.
    """
    ref_today = date(2026, 6, 29)
    path = make_historify({"PRESENT": _epoch_at(2026, 6, 27)})  # one symbol present

    stale, fresh, details = dfs.compute_stale_symbols(
        path,
        ["PRESENT", "NEVER_INGESTED"],
        today=ref_today,
        max_staleness_business_days=1,
    )

    assert "NEVER_INGESTED" in stale
    assert "NEVER_INGESTED" not in fresh
    # Distinct shape from "stale with a known last_date" so the caller can
    # tell "never ingested" apart from "old" if it wants to.
    assert details["NEVER_INGESTED"]["last_ts"] is None
    assert details["NEVER_INGESTED"]["last_date"] is None
    assert details["NEVER_INGESTED"]["stale"] is True
    # And the present symbol is treated correctly (sanity).
    assert "PRESENT" in fresh, f"PRESENT was misclassified stale; details={details!r}"


# ----------------------------------------------------------------------- #
# Contract C — weekend gap is NOT a divergence. The freshness check is
# business-day aware; a Friday-close timestamp on a Monday-morning probe
# must be fresh (0 business days behind).
# ----------------------------------------------------------------------- #
def test_friday_close_on_monday_morning_at_default_threshold_is_fresh(make_historify):
    """Sanity / regression guard: the business-day arithmetic must NOT
    inflate a weekend gap into multi-day staleness. Friday-close → Monday
    is exactly 1 business day behind (one new business day, Mon, since
    the last bar). At the default 1-business-day threshold that's fresh.

    If a future change inflates this to ``staleness_days > 1`` (e.g. by
    counting calendar days rather than business days), this assertion
    flips — the cross-source verdict would be broken in the
    over-alerting direction (every Monday morning).
    """
    fri_close = _epoch_at(2026, 6, 26, hh=15, mm=29)  # Friday 15:29 IST
    monday = date(2026, 6, 29)

    path = make_historify({"WEEKEND_OK": fri_close})

    stale, fresh, details = dfs.compute_stale_symbols(
        path,
        ["WEEKEND_OK"],
        today=monday,
        max_staleness_business_days=1,
    )

    assert "WEEKEND_OK" in fresh, (
        f"Friday-close on Monday-morning probe was misclassified stale at "
        f"threshold=1; details={details!r}"
    )
    # Exactly 1 business day behind — NOT calendar 3 days. If a future
    # change uses calendar days this number flips to 3 and the assertion
    # surfaces the regression loudly.
    assert details["WEEKEND_OK"]["staleness_days"] == 1, (
        f"weekend gap was counted as {details['WEEKEND_OK']['staleness_days']} "
        f"business days — business-day arithmetic regression (calendar-days bug?)"
    )


# ----------------------------------------------------------------------- #
# Contract D — the path-mismatch fallthrough in connect_historify_readonly
# is observably loud. A misrouted production caller would be silent
# corruption otherwise.
# ----------------------------------------------------------------------- #
def test_path_mismatch_fallthrough_logs_warning(make_historify, caplog):
    """When the requested path != the singleton path, connect_historify_readonly
    falls through to a fresh read-only DuckDB connection. That fallthrough
    is a test ergonomic — in production every caller uses the singleton
    path. The contract: the fallthrough MUST log a WARNING so a misrouted
    production caller surfaces in errors.jsonl rather than silently opening
    a wrong DB.

    Bug class: a future refactor that drops the WARNING would re-introduce
    the #191 config-mismatch risk silently.
    """
    # A tmp DuckDB at a path that will never match the live singleton's path.
    tmp = make_historify({"ANY": _epoch_at(2026, 6, 29)})

    with caplog.at_level(logging.WARNING, logger=dfs.logger.name):
        # Drive any path-via-helper read; compute_stale_symbols goes through
        # connect_historify_readonly internally for the MAX(timestamp) query.
        dfs.compute_stale_symbols(tmp, ["ANY"], today=date(2026, 6, 29))

    # Find the warning message about the path mismatch.
    warned = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "connect_historify_readonly" in rec.getMessage()
        and "singleton" in rec.getMessage()
    ]
    assert warned, (
        f"connect_historify_readonly path-mismatch fallthrough produced no WARNING; "
        f"caplog records: {[(r.levelname, r.getMessage()) for r in caplog.records]} — "
        f"silent-divergence bug class"
    )
