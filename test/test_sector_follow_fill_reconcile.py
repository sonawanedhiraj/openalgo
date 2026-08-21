"""Issue #562 — a broker acknowledgement is not a fill.

`sector_follow_trades.status='placed'` was written from the broker's
acknowledgement and never revisited. Seven live orders were acknowledged in
2026-07/08 and only one filled; the other six were rejected downstream by RMS
with nothing alerting and every row frozen at 'placed' forever.

The rules under test:
  * a terminal broker rejection CORRECTS the row and alerts loudly
  * a fill records the broker's average_price WITHOUT overwriting the decision
    price (so `fill_price - price` stays measurable as slippage)
  * an UNREADABLE status leaves the row alone and retries — it never invents a
    rejection the broker did not report
  * reconciliation is idempotent and never runs on the order path
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool


@pytest.fixture
def sf_db(monkeypatch, tmp_path):
    """Rebind database.sector_follow_db to a throwaway on-disk engine.

    A FILE, not ``sqlite://``: the module uses NullPool (per the project's
    connection-pooling rule), which opens a fresh connection per operation — and
    each fresh connection to an in-memory SQLite gets its own empty database, so
    the tables created here would be invisible to the code under test.
    """
    from database import sector_follow_db as sfdb

    engine = create_engine(
        f"sqlite:///{tmp_path / 'sf.db'}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
    sfdb.Base.metadata.create_all(engine)
    monkeypatch.setattr(sfdb, "engine", engine)
    monkeypatch.setattr(sfdb, "db_session", session)
    # NOTE: Base.query is deliberately NOT rebound — the module under test uses
    # db_session.query(), and monkeypatching the declarative Base's query
    # property raises rather than rebinding cleanly.
    yield sfdb, session
    session.remove()
    engine.dispose()


def _add(sfdb, session, **kw):
    row = sfdb.SectorFollowTrade(
        strategy_id=99,
        mode=kw.pop("mode", "live"),
        side=kw.pop("side", "BUY"),
        symbol=kw.pop("symbol", "SBIN"),
        exchange="NSE",
        product="CNC",
        quantity=kw.pop("quantity", 46),
        price=kw.pop("price", 1086.3),
        entry_date=kw.pop("entry_date", "2026-08-06"),
        order_id=kw.pop("order_id", "260806191325188"),
        status=kw.pop("status", "placed"),
        **kw,
    )
    session.add(row)
    session.commit()
    return row


def _patch_fill(monkeypatch, result):
    import services.sector_follow_fill_reconcile as mod

    monkeypatch.setattr(mod, "_resolve_api_key", lambda: "KEY")
    monkeypatch.setattr(mod, "fetch_fill", lambda order_id, api_key: result)
    alerts = []
    monkeypatch.setattr(mod, "_notify", alerts.append)
    return mod, alerts


def test_terminal_rejection_corrects_the_row_and_alerts(sf_db, monkeypatch):
    """The actual incident: acknowledged, then rejected, and nobody was told."""
    sfdb, session = sf_db
    _add(sfdb, session, symbol="RVNL", quantity=222)
    mod, alerts = _patch_fill(
        monkeypatch,
        {"price": None, "qty": 0, "order_status": "rejected", "message": "Insufficient funds"},
    )

    counts = mod.reconcile_unreconciled()

    assert counts["rejected"] == 1
    row = session.query(sfdb.SectorFollowTrade).one()
    assert row.status == "rejected"
    assert row.fill_reconcile_status == "unavailable"
    assert "Insufficient funds" in (row.error_message or "")
    # Six of these went unnoticed — silence is the bug.
    assert len(alerts) == 1
    assert "RVNL" in alerts[0]


def test_fill_is_recorded_without_overwriting_the_decision_price(sf_db, monkeypatch):
    """`fill_price - price` must stay measurable as slippage."""
    sfdb, session = sf_db
    _add(sfdb, session, price=1086.3)
    mod, alerts = _patch_fill(
        monkeypatch, {"price": 1087.05, "qty": 46, "order_status": "complete", "message": None}
    )

    counts = mod.reconcile_unreconciled()

    assert counts["reconciled"] == 1
    row = session.query(sfdb.SectorFollowTrade).one()
    assert row.status == "placed"  # a filled order is not re-labelled
    assert row.fill_price == pytest.approx(1087.05)
    assert row.fill_qty == 46
    assert row.price == pytest.approx(1086.3)  # decision price untouched
    assert row.fill_reconcile_status == "reconciled"
    assert alerts == []


def test_unreadable_status_never_invents_a_rejection(sf_db, monkeypatch):
    """'Could not read' is not 'rejected'. Leave the row alone and retry."""
    sfdb, session = sf_db
    _add(sfdb, session)
    mod, alerts = _patch_fill(monkeypatch, None)

    counts = mod.reconcile_unreconciled()

    assert counts["pending"] == 1
    assert counts["rejected"] == 0
    row = session.query(sfdb.SectorFollowTrade).one()
    assert row.status == "placed"
    assert row.fill_reconcile_status == "pending"
    assert alerts == []


def test_pending_rows_are_retried_and_can_resolve_later(sf_db, monkeypatch):
    """A row left pending must be picked up by the next run, not stranded."""
    sfdb, session = sf_db
    _add(sfdb, session)
    mod, _ = _patch_fill(monkeypatch, None)
    assert mod.reconcile_unreconciled()["pending"] == 1

    monkeypatch.setattr(
        mod,
        "fetch_fill",
        lambda order_id, api_key: {
            "price": 1087.0,
            "qty": 46,
            "order_status": "complete",
            "message": None,
        },
    )
    assert mod.reconcile_unreconciled()["reconciled"] == 1
    assert session.query(sfdb.SectorFollowTrade).one().fill_reconcile_status == "reconciled"


def test_reconciled_rows_are_not_rechecked(sf_db, monkeypatch):
    """Idempotent: a settled row is never asked about again."""
    sfdb, session = sf_db
    _add(sfdb, session)
    mod, _ = _patch_fill(
        monkeypatch, {"price": 1087.0, "qty": 46, "order_status": "complete", "message": None}
    )
    assert mod.reconcile_unreconciled()["reconciled"] == 1
    assert mod.reconcile_unreconciled()["checked"] == 0


def test_rows_without_an_order_id_are_skipped(sf_db, monkeypatch):
    """A row that never got an id has nothing to ask the broker about."""
    sfdb, session = sf_db
    _add(sfdb, session, order_id=None, status="placed")
    mod, _ = _patch_fill(
        monkeypatch, {"price": 1.0, "qty": 1, "order_status": "complete", "message": None}
    )
    assert mod.reconcile_unreconciled()["checked"] == 0


def test_zero_average_price_is_not_treated_as_a_fill(sf_db, monkeypatch):
    """average_price=0 means 'not reported', never 'filled at zero'."""
    sfdb, session = sf_db
    _add(sfdb, session)
    mod, _ = _patch_fill(
        monkeypatch, {"price": None, "qty": 0, "order_status": "open", "message": None}
    )

    counts = mod.reconcile_unreconciled()
    assert counts["reconciled"] == 0
    assert counts["pending"] == 1


def test_missing_api_key_skips_without_touching_rows(sf_db, monkeypatch):
    """No broker session must not corrupt the journal."""
    import services.sector_follow_fill_reconcile as mod

    sfdb, session = sf_db
    _add(sfdb, session)
    monkeypatch.setattr(mod, "_resolve_api_key", lambda: None)

    assert mod.reconcile_unreconciled() == {"skipped": "no_api_key"}
    assert session.query(sfdb.SectorFollowTrade).one().status == "placed"


def test_disabled_flag_is_a_no_op(sf_db, monkeypatch):
    import services.sector_follow_fill_reconcile as mod

    monkeypatch.setenv("SECTOR_FOLLOW_FILL_RECONCILE_ENABLED", "false")
    assert mod.reconcile_unreconciled() == {"skipped": "disabled"}
