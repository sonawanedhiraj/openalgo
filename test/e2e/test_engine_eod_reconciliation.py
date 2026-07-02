"""E2E: engine EOD reconciliation — sandbox square-offs back into the journal.

Reproduces the 2026-06-10 reporting gap (4 entries, only 1 engine exit journaled,
3 sandbox MIS square-offs invisible to Telegram) and proves the reconciliation
service closes it. Fully hermetic: both ``trade_journal_db`` and ``sandbox_db``
are rebound to fresh temp SQLite files; no broker, no network, no live DB.

The DB modules bind their ``engine`` / ``db_session`` at import time, but every
service resolves the session by global name at *call* time (see
``trade_journal_service._session`` and ``engine_eod_reconciliation_service._sandbox``),
so the rebind below is transparent.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest
import pytz
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

IST = pytz.timezone("Asia/Kolkata")


def _rebind(module, base, monkeypatch, tmp_path, fname):
    db_file = os.path.join(tmp_path, fname)
    eng = create_engine(
        f"sqlite:///{db_file}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(module, "engine", eng, raising=False)
    monkeypatch.setattr(module, "db_session", sess, raising=False)
    # NOTE: deliberately do NOT touch ``base.query``. A bare ``base.query = ...``
    # assignment isn't reverted by monkeypatch and would leak this temp session
    # into later tests that use ``Model.query`` (the sandbox suite does). The
    # services under test query via ``session.query(...)`` directly, so the
    # ``Model.query`` binding is irrelevant here.
    base.metadata.create_all(bind=eng)
    return eng, sess


@pytest.fixture
def journal_db(monkeypatch, tmp_db_dir):
    import database.trade_journal_db as tjdb

    _rebind(tjdb, tjdb.Base, monkeypatch, tmp_db_dir, "journal.db")
    return tjdb


@pytest.fixture
def sandbox_db(monkeypatch, tmp_db_dir):
    import database.sandbox_db as sdb

    _rebind(sdb, sdb.Base, monkeypatch, tmp_db_dir, "sandbox.db")
    return sdb


@pytest.fixture
def today():
    return dt.datetime.now(IST).date()


# --------------------------------------------------------------------------- #
# Helpers — build the two-DB world a reconcile pass reads.
# --------------------------------------------------------------------------- #


def _record_entry(symbol, direction, qty, entry_price, order_id="E-" + "0"):
    """Insert an OPEN journal row via the real service (stamps placed_at=now IST)."""
    from services import trade_journal_service

    return trade_journal_service.record_entry(
        symbol=symbol,
        direction=direction,
        quantity=qty,
        strategy_name="trending_equity_intraday",
        signal_source="chartink",
        entry_price=entry_price,
        entry_order_id=order_id,
    )


def _add_sandbox_fill(sandbox_db, symbol, action, qty, price, orderid, when=None):
    """Insert a sandbox executed-trade (fill) row.

    ``trade_timestamp`` defaults to IST **wall-clock** now (naive), matching the
    live sandbox convention. Using a bare naive ``datetime.now()`` here was a
    time-of-day flake: on a CI runner (UTC) a run after 18:30 UTC (= IST
    midnight) stamped the fill on the IST-previous day, so the reconciled exit's
    ``exited_at`` fell outside ``get_today_summary``'s IST-today window and the
    summary count came back 0 (``assert 0 == 1``). The reconciliation service and
    the today-summary both work in IST, so the fill must be IST-consistent too.
    """
    sess = sandbox_db.db_session
    tid = f"T-{symbol}-{action}-{orderid}"
    row = sandbox_db.SandboxTrades(
        tradeid=tid,
        orderid=orderid,
        user_id="default",
        symbol=symbol,
        exchange="NSE",
        action=action,
        quantity=qty,
        price=price,
        product="MIS",
        strategy="trending_equity_intraday",
        trade_timestamp=when or dt.datetime.now(IST).replace(tzinfo=None),
    )
    sess.add(row)
    sess.commit()
    sess.remove()


def _set_sandbox_position(sandbox_db, symbol, qty, avg_price):
    """Insert/replace a sandbox position row with net ``qty`` (0 == flat)."""
    sess = sandbox_db.db_session
    row = sandbox_db.SandboxPositions(
        user_id="default",
        symbol=symbol,
        exchange="NSE",
        product="MIS",
        quantity=qty,
        average_price=avg_price,
    )
    sess.add(row)
    sess.commit()
    sess.remove()


def _journal_summary():
    from services import trade_journal_service

    return trade_journal_service.get_today_summary()


# --------------------------------------------------------------------------- #
# 1. Engine-driven exit already written → reconciliation is a no-op.
# --------------------------------------------------------------------------- #


def test_engine_exit_is_noop(journal_db, sandbox_db, today):
    from services import trade_journal_service
    from services.engine_eod_reconciliation_service import reconcile_engine_journal

    jid = _record_entry("JINDALSTEL", "SHORT", 88, 1129.0, order_id="E-JIN")
    # Engine fired its own stop-loss exit.
    trade_journal_service.record_exit(
        jid, exit_price=1125.0, exit_order_id="X-JIN", exit_reason="stop_loss"
    )

    result = reconcile_engine_journal(today)

    assert result.exits_added == 0
    assert result.entries_checked == 0  # no open rows remain


# --------------------------------------------------------------------------- #
# 2. Sandbox EOD square-off → reconciliation writes the missing exit row.
# --------------------------------------------------------------------------- #


def test_sandbox_squareoff_is_journaled(journal_db, sandbox_db, today):
    from services.engine_eod_reconciliation_service import (
        EXIT_REASON_SANDBOX_EOD,
        reconcile_engine_journal,
    )

    _record_entry("OIL", "SHORT", 217, 460.30, order_id="E-OIL")
    # Sandbox flattened the SHORT with a covering BUY fill, position now flat.
    _add_sandbox_fill(sandbox_db, "OIL", "BUY", 217, 427.95, "X-OIL")
    _set_sandbox_position(sandbox_db, "OIL", 0, 460.30)

    result = reconcile_engine_journal(today)

    assert result.exits_added == 1
    detail = result.exit_details[0]
    assert detail["symbol"] == "OIL"
    assert detail["exit_price"] == pytest.approx(427.95)
    # SHORT pnl = (entry - exit) * qty = (460.30 - 427.95) * 217
    assert detail["pnl"] == pytest.approx((460.30 - 427.95) * 217, abs=0.01)

    summary = _journal_summary()
    assert summary["count"] == 1
    assert summary["by_exit_reason"][EXIT_REASON_SANDBOX_EOD]["count"] == 1


# --------------------------------------------------------------------------- #
# 3. The full 2026-06-10 scenario: 4 entries, 1 engine exit + 3 square-offs.
# --------------------------------------------------------------------------- #


def test_mixed_day_2026_06_10(journal_db, sandbox_db, today):
    from services import trade_journal_service
    from services.engine_eod_reconciliation_service import reconcile_engine_journal

    # 1 engine-driven exit (JINDALSTEL stop-loss).
    jid = _record_entry("JINDALSTEL", "SHORT", 88, 1129.0, order_id="E-JIN")
    trade_journal_service.record_exit(
        jid, exit_price=1125.0, exit_order_id="X-JIN", exit_reason="stop_loss"
    )

    # 3 sandbox square-offs the engine never journaled.
    squareoffs = [
        ("OIL", 217, 460.30, 427.95),
        ("HINDZINC", 182, 547.15, 546.90),
        ("TATAELXSI", 24, 4130.1, 4092.20),
    ]
    for sym, qty, entry, exit_px in squareoffs:
        _record_entry(sym, "SHORT", qty, entry, order_id=f"E-{sym}")
        _add_sandbox_fill(sandbox_db, sym, "BUY", qty, exit_px, f"X-{sym}")
        _set_sandbox_position(sandbox_db, sym, 0, entry)

    result = reconcile_engine_journal(today)
    assert result.exits_added == 3

    summary = _journal_summary()
    assert summary["count"] == 4  # was 1 before reconcile

    # Total realized matches the investigation report (~+₹8,327), not +₹352.
    expected = (1129.0 - 1125.0) * 88
    for _sym, qty, entry, exit_px in squareoffs:
        expected += (entry - exit_px) * qty
    assert summary["total_pnl"] == pytest.approx(expected, abs=1.0)
    assert summary["total_pnl"] > 8000  # not the under-reported 352


# --------------------------------------------------------------------------- #
# 4. Idempotency — second run is a no-op.
# --------------------------------------------------------------------------- #


def test_idempotent(journal_db, sandbox_db, today):
    from services.engine_eod_reconciliation_service import reconcile_engine_journal

    _record_entry("OIL", "SHORT", 217, 460.30, order_id="E-OIL")
    _add_sandbox_fill(sandbox_db, "OIL", "BUY", 217, 427.95, "X-OIL")
    _set_sandbox_position(sandbox_db, "OIL", 0, 460.30)

    first = reconcile_engine_journal(today)
    assert first.exits_added == 1

    second = reconcile_engine_journal(today)
    assert second.exits_added == 0
    assert second.entries_checked == 0  # row no longer open

    assert _journal_summary()["count"] == 1  # not duplicated


# --------------------------------------------------------------------------- #
# 5. Mid-day — position still open → reconciliation writes nothing.
# --------------------------------------------------------------------------- #


def test_midday_still_open_is_noop(journal_db, sandbox_db, today):
    from services.engine_eod_reconciliation_service import reconcile_engine_journal

    _record_entry("OIL", "SHORT", 217, 460.30, order_id="E-OIL")
    # Position still open in sandbox (net qty non-zero), no closing fill yet.
    _set_sandbox_position(sandbox_db, "OIL", -217, 460.30)

    result = reconcile_engine_journal(today)

    assert result.exits_added == 0
    assert any(s["reason"] == "still_open" for s in result.skipped)
    assert _journal_summary()["count"] == 0


# --------------------------------------------------------------------------- #
# 6. Multiple partial fills on close → one summed exit row, weighted avg price.
# --------------------------------------------------------------------------- #


def test_multiple_partial_close_fills_summed(journal_db, sandbox_db, today):
    from services.engine_eod_reconciliation_service import reconcile_engine_journal

    _record_entry("OIL", "SHORT", 90, 460.30, order_id="E-OIL")
    # Three BUY fills of 30 each, distinct prices.
    _add_sandbox_fill(sandbox_db, "OIL", "BUY", 30, 428.00, "X-OIL-1")
    _add_sandbox_fill(sandbox_db, "OIL", "BUY", 30, 430.00, "X-OIL-2")
    _add_sandbox_fill(sandbox_db, "OIL", "BUY", 30, 432.00, "X-OIL-3")
    _set_sandbox_position(sandbox_db, "OIL", 0, 460.30)

    result = reconcile_engine_journal(today)

    assert result.exits_added == 1
    detail = result.exit_details[0]
    assert detail["quantity"] == 90
    assert detail["fills"] == 3
    # Qty-weighted avg = (428+430+432)/3 = 430.00
    assert detail["exit_price"] == pytest.approx(430.00)
    assert detail["pnl"] == pytest.approx((460.30 - 430.00) * 90, abs=0.01)


# --------------------------------------------------------------------------- #
# 7. Orphan fill — sandbox fill but NO journal entry → no entry created, no crash.
# --------------------------------------------------------------------------- #


def test_backfill_past_date(journal_db, sandbox_db):
    """Backfill path: an entry placed on a *past* IST date is still found and
    reconciled (the live job's get_open_trades_today would miss it)."""
    from database.trade_journal_db import TradeJournal
    from services.engine_eod_reconciliation_service import reconcile_engine_journal

    past = dt.date(2026, 6, 10)
    sess = journal_db.db_session
    placed = "2026-06-10T15:05:00+05:30"
    row = TradeJournal(
        placed_at=placed,
        symbol="OIL",
        direction="SHORT",
        quantity=217,
        strategy_name="trending_equity_intraday",
        signal_source="chartink",
        entry_price=460.30,
        entry_order_id="E-OIL",
        created_at=placed,
        updated_at=placed,
    )
    sess.add(row)
    sess.commit()
    sess.remove()

    # Closing fill dated on the same past day.
    _add_sandbox_fill(
        sandbox_db,
        "OIL",
        "BUY",
        217,
        427.95,
        "X-OIL",
        when=dt.datetime(2026, 6, 10, 15, 16, 0),
    )
    _set_sandbox_position(sandbox_db, "OIL", 0, 460.30)

    result = reconcile_engine_journal(past)
    assert result.entries_checked == 1
    assert result.exits_added == 1
    assert result.exit_details[0]["pnl"] == pytest.approx((460.30 - 427.95) * 217, abs=0.01)


def test_orphan_fill_does_not_create_entry(journal_db, sandbox_db, today):
    from services.engine_eod_reconciliation_service import reconcile_engine_journal

    # A fill exists in sandbox but the engine never journaled an entry.
    _add_sandbox_fill(sandbox_db, "GHOST", "BUY", 50, 100.0, "X-GHOST")
    _set_sandbox_position(sandbox_db, "GHOST", 0, 110.0)

    result = reconcile_engine_journal(today)

    assert result.exits_added == 0
    assert result.entries_checked == 0  # nothing in the journal to examine
    # Entry creation is the engine's job — reconciliation must not invent one.
    assert _journal_summary()["count"] == 0
