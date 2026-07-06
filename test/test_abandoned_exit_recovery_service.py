"""Tests for ``services.abandoned_exit_recovery_service`` (issue #262).

Recover the real exit price + gross P&L for ``abandoned_% AND exit_price IS
NULL`` journal rows from the actual sandbox square-off fills, so the
/strategies dashboard net P&L stops under-reporting.

All DB effects are rebound to in-memory engines — no live DB, no writes to
db/openalgo.db or db/sandbox.db.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from services import abandoned_exit_recovery_service as svc


@pytest.fixture
def journal_db(monkeypatch):
    """Rebind trade_journal_db + trade_journal_service session to in-memory."""
    from database import trade_journal_db as tjdb

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess_factory = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(tjdb, "engine", eng)
    monkeypatch.setattr(tjdb, "db_session", sess_factory)
    tjdb.Base.query = sess_factory.query_property()
    tjdb.Base.metadata.create_all(eng)
    yield tjdb
    sess_factory.remove()
    eng.dispose()


@pytest.fixture
def sandbox_db(monkeypatch):
    """Rebind sandbox_db to an in-memory engine and route the service to it."""
    from database import sandbox_db as sbdb

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess_factory = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(sbdb, "engine", eng)
    monkeypatch.setattr(sbdb, "db_session", sess_factory)
    sbdb.Base.query = sess_factory.query_property()
    sbdb.Base.metadata.create_all(eng)
    yield sbdb
    sess_factory.remove()
    eng.dispose()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _insert_abandoned_row(
    tjdb,
    *,
    symbol: str,
    direction: str,
    quantity: int,
    entry_price: float,
    placed_at: str,
    exit_reason: str = "abandoned_eod_watchdog",
    exit_price: float | None = None,
    strategy_name: str = svc.DEFAULT_STRATEGY_NAME,
) -> int:
    sess = tjdb.db_session()
    try:
        row = tjdb.TradeJournal(
            placed_at=placed_at,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            entry_price=entry_price,
            strategy_name=strategy_name,
            signal_source="inhouse",
            exit_reason=exit_reason,
            exit_price=exit_price,
            exited_at=f"{placed_at[:10]}T15:14:02+05:30",
            created_at=placed_at,
            updated_at=placed_at,
        )
        sess.add(row)
        sess.commit()
        return row.id
    finally:
        sess.close()


def _insert_sandbox_trade(sbdb, *, symbol, action, quantity, price, ts, orderid="OID"):
    sess = sbdb.db_session()
    try:
        row = sbdb.SandboxTrades(
            tradeid=f"T-{orderid}",
            orderid=orderid,
            user_id="u",
            symbol=symbol,
            exchange="NSE",
            action=action,
            quantity=quantity,
            price=price,
            product="MIS",
            strategy="x",
            trade_timestamp=dt.datetime.fromisoformat(ts),
        )
        sess.add(row)
        sess.commit()
    finally:
        sess.close()


def _row(tjdb, jid):
    sess = tjdb.db_session()
    try:
        r = sess.query(tjdb.TradeJournal).filter_by(id=jid).first()
        return {
            "exit_price": r.exit_price,
            "pnl": r.pnl,
            "exit_reason": r.exit_reason,
            "exit_order_id": r.exit_order_id,
            "exited_at": r.exited_at,
        }
    finally:
        sess.close()


# --------------------------------------------------------------------------- #
# core recovery
# --------------------------------------------------------------------------- #


def test_recovers_simple_short_from_single_squareoff_fill(journal_db, sandbox_db):
    jid = _insert_abandoned_row(
        journal_db,
        symbol="ASTRAL",
        direction="SHORT",
        quantity=62,
        entry_price=1360.0,
        placed_at="2026-06-29T13:44:04+05:30",
    )
    # SHORT closes with a BUY square-off at 15:14.
    _insert_sandbox_trade(
        sandbox_db,
        symbol="ASTRAL",
        action="BUY",
        quantity=62,
        price=1366.3,
        ts="2026-06-29 15:14:02.337838",
        orderid="SQ1",
    )
    result = svc.recover_abandoned_exits(strategy_name=svc.DEFAULT_STRATEGY_NAME)
    assert result.rows_recovered == 1
    row = _row(journal_db, jid)
    assert row["exit_price"] == pytest.approx(1366.3)
    # SHORT pnl = (entry - exit) * qty = (1360 - 1366.3) * 62 = -390.6
    assert row["pnl"] == pytest.approx((1360.0 - 1366.3) * 62, abs=0.01)
    assert row["exit_reason"] == svc.EXIT_REASON_RECOVERED
    assert row["exit_order_id"] == "SQ1"


def test_recovers_long_from_squareoff_fill(journal_db, sandbox_db):
    jid = _insert_abandoned_row(
        journal_db,
        symbol="ASHOKLEY",
        direction="LONG",
        quantity=500,
        entry_price=149.87,
        placed_at="2026-06-12T11:00:00+05:30",
    )
    _insert_sandbox_trade(
        sandbox_db,
        symbol="ASHOKLEY",
        action="SELL",
        quantity=500,
        price=150.5,
        ts="2026-06-12 15:15:00.000000",
        orderid="SQL",
    )
    result = svc.recover_abandoned_exits()
    assert result.rows_recovered == 1
    row = _row(journal_db, jid)
    assert row["exit_price"] == pytest.approx(150.5)
    # LONG pnl = (exit - entry) * qty
    assert row["pnl"] == pytest.approx((150.5 - 149.87) * 500, abs=0.01)


def test_picks_earliest_covering_fill_and_caps_at_entry_qty(journal_db, sandbox_db):
    """TCS 2026-06-19 case: the abandoned SHORT was really closed by an operator
    UI exit (13:43); the 15:14 watchdog BUY re-opened a phantom position. The
    recovery must use the FIRST covering fill after entry (13:43), capped at the
    entry quantity — NOT sum both BUYs."""
    jid = _insert_abandoned_row(
        journal_db,
        symbol="TCS",
        direction="SHORT",
        quantity=48,
        entry_price=2070.5,
        placed_at="2026-06-19T11:49:50+05:30",
    )
    # Two closing-action (BUY) fills after entry — only the first flattens THIS row.
    _insert_sandbox_trade(
        sandbox_db,
        symbol="TCS",
        action="BUY",
        quantity=48,
        price=2073.0,
        ts="2026-06-19 13:43:27.621248",
        orderid="UIEXIT",
    )
    _insert_sandbox_trade(
        sandbox_db,
        symbol="TCS",
        action="BUY",
        quantity=48,
        price=2130.2,
        ts="2026-06-19 15:14:02.247993",
        orderid="WATCHDOG",
    )
    result = svc.recover_abandoned_exits()
    assert result.rows_recovered == 1
    row = _row(journal_db, jid)
    assert row["exit_price"] == pytest.approx(2073.0)  # the UI exit, not 2130.2
    assert row["exit_order_id"] == "UIEXIT"
    assert row["pnl"] == pytest.approx((2070.5 - 2073.0) * 48, abs=0.01)


def test_ignores_earlier_roundtrip_fills_before_entry(journal_db, sandbox_db):
    """TECHM 2026-06-19 case: an earlier stop-loss round-trip (jid not ours)
    left a BUY fill at 11:10 that belongs to a different entry. Our abandoned
    row entered at 12:08 — only the 15:14 fill after it counts."""
    jid = _insert_abandoned_row(
        journal_db,
        symbol="TECHM",
        direction="SHORT",
        quantity=72,
        entry_price=1376.7,
        placed_at="2026-06-19T12:08:41+05:30",
    )
    # Earlier round-trip's cover (before our entry) — must be ignored.
    _insert_sandbox_trade(
        sandbox_db,
        symbol="TECHM",
        action="BUY",
        quantity=51,
        price=1375.7,
        ts="2026-06-19 11:10:33.814389",
        orderid="EARLY",
    )
    # Our covering fill after entry.
    _insert_sandbox_trade(
        sandbox_db,
        symbol="TECHM",
        action="BUY",
        quantity=72,
        price=1409.7,
        ts="2026-06-19 15:14:01.565520",
        orderid="OURS",
    )
    result = svc.recover_abandoned_exits()
    assert result.rows_recovered == 1
    row = _row(journal_db, jid)
    assert row["exit_price"] == pytest.approx(1409.7)
    assert row["exit_order_id"] == "OURS"


def test_partial_fills_weighted_average(journal_db, sandbox_db):
    """Two covering fills together flatten the entry — price is qty-weighted."""
    jid = _insert_abandoned_row(
        journal_db,
        symbol="XYZ",
        direction="LONG",
        quantity=100,
        entry_price=200.0,
        placed_at="2026-06-20T12:00:00+05:30",
    )
    _insert_sandbox_trade(
        sandbox_db,
        symbol="XYZ",
        action="SELL",
        quantity=40,
        price=210.0,
        ts="2026-06-20 15:14:00.000000",
        orderid="F1",
    )
    _insert_sandbox_trade(
        sandbox_db,
        symbol="XYZ",
        action="SELL",
        quantity=60,
        price=205.0,
        ts="2026-06-20 15:15:00.000000",
        orderid="F2",
    )
    result = svc.recover_abandoned_exits()
    assert result.rows_recovered == 1
    row = _row(journal_db, jid)
    # (40*210 + 60*205) / 100 = 207.0
    assert row["exit_price"] == pytest.approx(207.0)
    assert row["exit_order_id"] == "F2"  # last matched fill


# --------------------------------------------------------------------------- #
# skips + safety
# --------------------------------------------------------------------------- #


def test_skips_when_no_covering_fill(journal_db, sandbox_db):
    """A genuinely un-flattened position (no sandbox close) stays abandoned/NULL."""
    jid = _insert_abandoned_row(
        journal_db,
        symbol="NOFILL",
        direction="SHORT",
        quantity=10,
        entry_price=100.0,
        placed_at="2026-06-20T12:00:00+05:30",
    )
    result = svc.recover_abandoned_exits()
    assert result.rows_recovered == 0
    assert any(s["reason"] == "no_covering_close_fill" for s in result.skipped)
    row = _row(journal_db, jid)
    assert row["exit_price"] is None
    assert row["exit_reason"] == "abandoned_eod_watchdog"


def test_skips_when_fills_undersize(journal_db, sandbox_db):
    """A close fill that doesn't cover the entry qty is not enough to price."""
    _insert_abandoned_row(
        journal_db,
        symbol="PART",
        direction="LONG",
        quantity=100,
        entry_price=100.0,
        placed_at="2026-06-20T12:00:00+05:30",
    )
    _insert_sandbox_trade(
        sandbox_db,
        symbol="PART",
        action="SELL",
        quantity=30,
        price=105.0,
        ts="2026-06-20 15:14:00.000000",
        orderid="P1",
    )
    result = svc.recover_abandoned_exits()
    assert result.rows_recovered == 0
    assert any(s["reason"] == "no_covering_close_fill" for s in result.skipped)


def test_dry_run_writes_nothing(journal_db, sandbox_db):
    jid = _insert_abandoned_row(
        journal_db,
        symbol="DRY",
        direction="SHORT",
        quantity=10,
        entry_price=100.0,
        placed_at="2026-06-20T12:00:00+05:30",
    )
    _insert_sandbox_trade(
        sandbox_db,
        symbol="DRY",
        action="BUY",
        quantity=10,
        price=95.0,
        ts="2026-06-20 15:14:00.000000",
        orderid="D1",
    )
    result = svc.recover_abandoned_exits(dry_run=True)
    assert result.rows_recovered == 1
    assert result.total_pnl == pytest.approx((100.0 - 95.0) * 10)
    # Nothing written.
    row = _row(journal_db, jid)
    assert row["exit_price"] is None
    assert row["exit_reason"] == "abandoned_eod_watchdog"


def test_idempotent_second_run_is_noop(journal_db, sandbox_db):
    _insert_abandoned_row(
        journal_db,
        symbol="IDEM",
        direction="SHORT",
        quantity=10,
        entry_price=100.0,
        placed_at="2026-06-20T12:00:00+05:30",
    )
    _insert_sandbox_trade(
        sandbox_db,
        symbol="IDEM",
        action="BUY",
        quantity=10,
        price=95.0,
        ts="2026-06-20 15:14:00.000000",
        orderid="I1",
    )
    first = svc.recover_abandoned_exits()
    assert first.rows_recovered == 1
    second = svc.recover_abandoned_exits()
    assert second.rows_checked == 0  # row no longer matches (exit_price set)
    assert second.rows_recovered == 0


def test_only_matches_abandoned_rows_not_clean_exits(journal_db, sandbox_db):
    """A row with a real exit_price (normal stop_loss close) is never touched."""
    _insert_abandoned_row(
        journal_db,
        symbol="CLEAN",
        direction="SHORT",
        quantity=10,
        entry_price=100.0,
        placed_at="2026-06-20T12:00:00+05:30",
        exit_reason="stop_loss",
        exit_price=98.0,
    )
    result = svc.recover_abandoned_exits(strategy_name=None)
    assert result.rows_checked == 0


def test_date_scoping_restricts_to_day(journal_db, sandbox_db):
    _insert_abandoned_row(
        journal_db,
        symbol="DAY1",
        direction="SHORT",
        quantity=10,
        entry_price=100.0,
        placed_at="2026-06-19T12:00:00+05:30",
    )
    _insert_abandoned_row(
        journal_db,
        symbol="DAY2",
        direction="SHORT",
        quantity=10,
        entry_price=100.0,
        placed_at="2026-06-20T12:00:00+05:30",
    )
    for sym, day in [("DAY1", "2026-06-19"), ("DAY2", "2026-06-20")]:
        _insert_sandbox_trade(
            sandbox_db,
            symbol=sym,
            action="BUY",
            quantity=10,
            price=95.0,
            ts=f"{day} 15:14:00.000000",
            orderid=f"{sym}1",
        )
    result = svc.recover_abandoned_exits("2026-06-19")
    assert result.rows_checked == 1
    assert result.rows_recovered == 1
    assert result.recovered[0]["symbol"] == "DAY1"


def test_strategy_scoping(journal_db, sandbox_db):
    _insert_abandoned_row(
        journal_db,
        symbol="MINE",
        direction="SHORT",
        quantity=10,
        entry_price=100.0,
        placed_at="2026-06-20T12:00:00+05:30",
        strategy_name=svc.DEFAULT_STRATEGY_NAME,
    )
    _insert_abandoned_row(
        journal_db,
        symbol="OTHER",
        direction="SHORT",
        quantity=10,
        entry_price=100.0,
        placed_at="2026-06-20T12:00:00+05:30",
        strategy_name="some_other_strategy",
    )
    for sym in ("MINE", "OTHER"):
        _insert_sandbox_trade(
            sandbox_db,
            symbol=sym,
            action="BUY",
            quantity=10,
            price=95.0,
            ts="2026-06-20 15:14:00.000000",
            orderid=f"{sym}1",
        )
    result = svc.recover_abandoned_exits(strategy_name=svc.DEFAULT_STRATEGY_NAME)
    assert result.rows_checked == 1
    assert result.recovered[0]["symbol"] == "MINE"
