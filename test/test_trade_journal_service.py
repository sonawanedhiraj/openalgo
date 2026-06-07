"""Tests for the fail-safe trade journal service.

The service must never raise into the engine entry / exit path. We confirm:

* Happy round-trip writes — entry → fill → exit produces a closed row with
  derived pnl, pnl_pct, and hold_duration.
* ``get_open_journal_id_for_symbol`` correctly identifies the open row
  before exit and returns ``None`` after exit.
* ``get_recent_trades`` and ``get_trades_for_symbol`` filter by window /
  symbol.
* ``get_today_summary`` aggregates count, winners/losers, and totals by
  strategy + exit reason.
* Every helper survives a broken session: ``record_entry`` returns ``0``,
  ``update_entry_fill`` / ``record_exit`` swallow the error, and read
  helpers return empty containers.
"""

import datetime as dt

import pytest
import pytz
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture
def fresh_journal_db(monkeypatch):
    """Point trade_journal_db at a fresh in-memory SQLite for one test."""
    from database import trade_journal_db as tjdb

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    )

    monkeypatch.setattr(tjdb, "engine", test_engine)
    monkeypatch.setattr(tjdb, "db_session", test_session)
    tjdb.Base.metadata.create_all(test_engine)

    yield tjdb

    test_session.remove()
    test_engine.dispose()


# ---------------------------------------------------------------------------
# Happy-path round trip
# ---------------------------------------------------------------------------


def test_record_entry_returns_positive_id(fresh_journal_db):
    from services import trade_journal_service as tjs

    jid = tjs.record_entry(
        symbol="RELIANCE",
        direction="LONG",
        quantity=10,
        strategy_name="simplified_stock_engine",
        signal_source="chartink",
        entry_price=2500.0,
        entry_order_id="ORD-1",
    )
    assert jid > 0

    row = (
        fresh_journal_db.db_session.query(fresh_journal_db.TradeJournal)
        .filter_by(id=jid)
        .first()
    )
    assert row is not None
    assert row.symbol == "RELIANCE"
    assert row.direction == "LONG"
    assert row.quantity == 10
    assert row.entry_price == 2500.0
    assert row.entry_order_id == "ORD-1"
    assert row.exited_at is None
    assert row.exit_price is None
    assert row.pnl is None


def test_record_entry_persists_ltp_at_signal(fresh_journal_db):
    """ltp_at_signal is captured at entry and survives an entry-fill overwrite,
    so realized slippage can later be computed against the actual fill price.
    """
    from services import trade_journal_service as tjs

    jid = tjs.record_entry(
        symbol="TATAMOTORS",
        direction="LONG",
        quantity=4,
        strategy_name="simplified_stock_engine",
        signal_source="chartink",
        entry_price=950.0,
        ltp_at_signal=950.0,
        entry_order_id="ORD-LTP",
    )
    assert jid > 0

    # Fill comes in at a worse price; ltp_at_signal must stay pinned.
    tjs.update_entry_fill(jid, entry_price=951.10)

    row = (
        fresh_journal_db.db_session.query(fresh_journal_db.TradeJournal)
        .filter_by(id=jid)
        .first()
    )
    assert row.ltp_at_signal == 950.0
    assert row.entry_price == 951.10

    # ltp_at_signal is nullable: an entry that omits it stores NULL.
    jid2 = tjs.record_entry(
        symbol="SBIN",
        direction="SHORT",
        quantity=1,
        strategy_name="simplified_stock_engine",
        signal_source="manual",
    )
    row2 = (
        fresh_journal_db.db_session.query(fresh_journal_db.TradeJournal)
        .filter_by(id=jid2)
        .first()
    )
    assert row2.ltp_at_signal is None


def test_update_entry_fill_patches_row(fresh_journal_db):
    from services import trade_journal_service as tjs

    jid = tjs.record_entry(
        symbol="INFY",
        direction="LONG",
        quantity=5,
        strategy_name="simplified_stock_engine",
        signal_source="chartink",
    )

    tjs.update_entry_fill(jid, entry_price=1450.25, entry_fill_at="2026-05-30T11:30:00+05:30")

    row = (
        fresh_journal_db.db_session.query(fresh_journal_db.TradeJournal)
        .filter_by(id=jid)
        .first()
    )
    assert row.entry_price == 1450.25
    assert row.entry_fill_at == "2026-05-30T11:30:00+05:30"


def test_open_journal_id_lookup(fresh_journal_db):
    from services import trade_journal_service as tjs

    assert tjs.get_open_journal_id_for_symbol("TCS") is None

    jid = tjs.record_entry(
        symbol="TCS",
        direction="LONG",
        quantity=3,
        strategy_name="simplified_stock_engine",
        signal_source="chartink",
        entry_price=3800.0,
    )
    assert tjs.get_open_journal_id_for_symbol("TCS") == jid

    tjs.record_exit(jid, exit_price=3850.0, exit_reason="stop_loss")
    # After exit, no open row remains for the symbol.
    assert tjs.get_open_journal_id_for_symbol("TCS") is None


def test_record_exit_derives_pnl_long(fresh_journal_db):
    from services import trade_journal_service as tjs

    jid = tjs.record_entry(
        symbol="HDFCBANK",
        direction="LONG",
        quantity=10,
        strategy_name="simplified_stock_engine",
        signal_source="chartink",
        entry_price=1600.0,
    )
    tjs.record_exit(jid, exit_price=1620.0, exit_reason="target", exit_order_id="EX-1")

    row = (
        fresh_journal_db.db_session.query(fresh_journal_db.TradeJournal)
        .filter_by(id=jid)
        .first()
    )
    # LONG: (exit - entry) * qty = (1620 - 1600) * 10 = 200
    assert row.pnl == pytest.approx(200.0)
    # pnl_pct = pnl / (entry * qty) = 200 / 16000 = 0.0125
    assert row.pnl_pct == pytest.approx(0.0125)
    assert row.exit_reason == "target"
    assert row.exit_order_id == "EX-1"
    assert row.exited_at is not None
    assert row.hold_duration_seconds is not None
    assert row.hold_duration_seconds >= 0


def test_record_exit_derives_pnl_short(fresh_journal_db):
    """SHORT direction inverts the sign: profit when exit < entry."""
    from services import trade_journal_service as tjs

    jid = tjs.record_entry(
        symbol="SBIN",
        direction="SHORT",
        quantity=20,
        strategy_name="simplified_stock_engine",
        signal_source="chartink",
        entry_price=850.0,
    )
    tjs.record_exit(jid, exit_price=840.0, exit_reason="stop_loss")

    row = (
        fresh_journal_db.db_session.query(fresh_journal_db.TradeJournal)
        .filter_by(id=jid)
        .first()
    )
    # SHORT: (entry - exit) * qty = (850 - 840) * 20 = 200
    assert row.pnl == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def test_get_recent_trades_filters_by_window(fresh_journal_db):
    from database import trade_journal_db as tjdb
    from services import trade_journal_service as tjs

    # Insert one trade with a placed_at well in the past (3 days ago) and
    # one fresh trade. get_recent_trades(hours=1) should only return the
    # fresh one.
    old_ts = (dt.datetime.now(IST) - dt.timedelta(days=3)).isoformat()
    now_ts = dt.datetime.now(IST).isoformat()

    old_row = tjdb.TradeJournal(
        placed_at=old_ts,
        symbol="OLD",
        direction="LONG",
        quantity=1,
        strategy_name="simplified_stock_engine",
        signal_source="chartink",
        created_at=old_ts,
        updated_at=old_ts,
    )
    fresh_row = tjdb.TradeJournal(
        placed_at=now_ts,
        symbol="FRESH",
        direction="LONG",
        quantity=1,
        strategy_name="simplified_stock_engine",
        signal_source="chartink",
        created_at=now_ts,
        updated_at=now_ts,
    )
    fresh_journal_db.db_session.add_all([old_row, fresh_row])
    fresh_journal_db.db_session.commit()

    rows = tjs.get_recent_trades(hours=1)
    symbols = [r["symbol"] for r in rows]
    assert "FRESH" in symbols
    assert "OLD" not in symbols


def test_get_trades_for_symbol_filters(fresh_journal_db):
    from services import trade_journal_service as tjs

    tjs.record_entry(
        symbol="RELIANCE",
        direction="LONG",
        quantity=1,
        strategy_name="simplified_stock_engine",
        signal_source="chartink",
    )
    tjs.record_entry(
        symbol="INFY",
        direction="LONG",
        quantity=1,
        strategy_name="simplified_stock_engine",
        signal_source="chartink",
    )

    reliance_rows = tjs.get_trades_for_symbol("RELIANCE", days=7)
    assert len(reliance_rows) == 1
    assert reliance_rows[0]["symbol"] == "RELIANCE"

    # Far-future cutoff (negative window) excludes everything by construction.
    no_rows = tjs.get_trades_for_symbol("UNKNOWN", days=7)
    assert no_rows == []


def test_get_today_summary_aggregates(fresh_journal_db):
    from services import trade_journal_service as tjs

    # Two winners, one loser, two strategies.
    j1 = tjs.record_entry(
        symbol="A",
        direction="LONG",
        quantity=10,
        strategy_name="simplified_stock_engine",
        signal_source="chartink",
        entry_price=100.0,
    )
    j2 = tjs.record_entry(
        symbol="B",
        direction="LONG",
        quantity=10,
        strategy_name="simplified_stock_engine",
        signal_source="chartink",
        entry_price=200.0,
    )
    j3 = tjs.record_entry(
        symbol="C",
        direction="SHORT",
        quantity=5,
        strategy_name="other_strategy",
        signal_source="chartink",
        entry_price=500.0,
    )
    tjs.record_exit(j1, exit_price=110.0, exit_reason="target")  # +100
    tjs.record_exit(j2, exit_price=190.0, exit_reason="stop_loss")  # -100
    tjs.record_exit(j3, exit_price=480.0, exit_reason="target")  # SHORT: +100

    summary = tjs.get_today_summary()
    assert summary["count"] == 3
    assert summary["winners"] == 2
    assert summary["losers"] == 1
    assert summary["total_pnl"] == pytest.approx(100.0)
    assert set(summary["by_strategy"].keys()) == {
        "simplified_stock_engine",
        "other_strategy",
    }
    assert summary["by_strategy"]["simplified_stock_engine"]["count"] == 2
    assert summary["by_strategy"]["other_strategy"]["count"] == 1
    assert "target" in summary["by_exit_reason"]
    assert summary["by_exit_reason"]["target"]["count"] == 2
    assert summary["by_exit_reason"]["target"]["pnl"] == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# Fail-safety: each helper must survive a broken session.
# ---------------------------------------------------------------------------


def test_record_entry_returns_zero_on_db_failure(monkeypatch):
    from database import trade_journal_db as tjdb
    from services import trade_journal_service as tjs

    class _BrokenSession:
        def add(self, *a, **kw):
            raise RuntimeError("simulated DB outage")

        def commit(self):
            raise RuntimeError("simulated DB outage")

        def rollback(self):
            return None

        def remove(self):
            return None

    monkeypatch.setattr(tjdb, "db_session", _BrokenSession())
    jid = tjs.record_entry(
        symbol="X",
        direction="LONG",
        quantity=1,
        strategy_name="simplified_stock_engine",
        signal_source="chartink",
    )
    assert jid == 0


def test_update_entry_fill_swallows_zero_id(fresh_journal_db):
    """journal_id=0 (record_entry sentinel) must silently no-op."""
    from services import trade_journal_service as tjs

    # Should NOT raise and should NOT insert anything.
    tjs.update_entry_fill(0, entry_price=100.0)
    rows = fresh_journal_db.db_session.query(fresh_journal_db.TradeJournal).all()
    assert rows == []


def test_record_exit_swallows_zero_id(fresh_journal_db):
    from services import trade_journal_service as tjs

    tjs.record_exit(0, exit_price=100.0, exit_reason="stop_loss")
    rows = fresh_journal_db.db_session.query(fresh_journal_db.TradeJournal).all()
    assert rows == []


def test_update_entry_fill_failure_does_not_raise(fresh_journal_db, monkeypatch):
    """A broken session in update_entry_fill must NOT bubble out."""
    from services import trade_journal_service as tjs

    jid = tjs.record_entry(
        symbol="Y",
        direction="LONG",
        quantity=1,
        strategy_name="simplified_stock_engine",
        signal_source="chartink",
    )
    assert jid > 0

    def _raise_on_commit():
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(fresh_journal_db.db_session, "commit", _raise_on_commit)
    # Must not raise.
    tjs.update_entry_fill(jid, entry_price=200.0)


def test_record_exit_failure_does_not_raise(fresh_journal_db, monkeypatch):
    from services import trade_journal_service as tjs

    jid = tjs.record_entry(
        symbol="Z",
        direction="LONG",
        quantity=1,
        strategy_name="simplified_stock_engine",
        signal_source="chartink",
        entry_price=100.0,
    )

    def _raise_on_commit():
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(fresh_journal_db.db_session, "commit", _raise_on_commit)
    tjs.record_exit(jid, exit_price=110.0, exit_reason="target")


def test_read_helpers_return_empty_on_failure(monkeypatch):
    """When the DB is broken, read helpers must produce empty containers."""
    from database import trade_journal_db as tjdb
    from services import trade_journal_service as tjs

    class _BrokenSession:
        def query(self, *a, **kw):
            raise RuntimeError("simulated DB outage")

        def remove(self):
            return None

    monkeypatch.setattr(tjdb, "db_session", _BrokenSession())

    assert tjs.get_recent_trades(hours=24) == []
    assert tjs.get_trades_for_symbol("X", days=7) == []
    assert tjs.get_open_journal_id_for_symbol("X") is None

    summary = tjs.get_today_summary()
    assert summary["count"] == 0
    assert summary["total_pnl"] == 0.0
    assert summary["by_strategy"] == {}
    assert summary["by_exit_reason"] == {}
