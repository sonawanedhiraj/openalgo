"""Mocked E2E for the operator backfill CLI ``services.engine_eod_reconciliation_backfill``.

The service-level reconciliation is covered hermetically in
``test/e2e/test_engine_eod_reconciliation.py``. This file exercises the *CLI
wrapper* (``main()``) end to end — the exact path the operator runs as
``uv run python -m services.engine_eod_reconciliation_backfill --from ... --apply``
— and asserts:

* ``--apply`` stamps the open journal row with ``exited_at`` / ``exit_price`` /
  ``exit_reason='sandbox_eod_squareoff'`` and a populated P&L.
* the run is idempotent: a second ``--apply`` is a no-op (no duplicate writes).
* the default (no ``--apply``) is a dry run that writes nothing.

Fully hermetic: both ``trade_journal_db`` and ``sandbox_db`` are rebound to fresh
temp SQLite files via ``tmp_path``; no broker, no network, no live DB. The DB
modules bind ``engine`` / ``db_session`` at import time, but every service
resolves the session by global name at *call* time, so the rebind is
transparent. The global ``test/conftest.py`` redirect + tripwire are an
additional safety net guaranteeing pytest can never touch the live DBs.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

# The synthetic strategy this backfill is scoped to (kept distinct from the
# engine default so the test never depends on a real strategy's rows).
SYNTH_STRATEGY = "synthetic_backfill_strat"


def _rebind(module, base, monkeypatch, tmp_path, fname):
    db_file = str(tmp_path / fname)
    eng = create_engine(
        f"sqlite:///{db_file}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(module, "engine", eng, raising=False)
    monkeypatch.setattr(module, "db_session", sess, raising=False)
    base.metadata.create_all(bind=eng)
    return eng, sess


@pytest.fixture
def journal_db(monkeypatch, tmp_path):
    import database.trade_journal_db as tjdb

    _rebind(tjdb, tjdb.Base, monkeypatch, tmp_path, "journal.db")
    return tjdb


@pytest.fixture
def sandbox_db(monkeypatch, tmp_path):
    import database.sandbox_db as sdb

    _rebind(sdb, sdb.Base, monkeypatch, tmp_path, "sandbox.db")
    return sdb


# --------------------------------------------------------------------------- #
# Helpers — build the two-DB world the backfill reads, mirroring the 06-09
# ONGC orphan: an open SHORT journal row + a sandbox closing BUY fill.
# --------------------------------------------------------------------------- #

PAST = dt.date(2026, 6, 9)
ENTRY_PRICE = 258.70
EXIT_PRICE = 259.00
QTY = 386


def _insert_open_entry(journal_db):
    """Insert an OPEN (exited_at NULL) journal row dated on PAST for the synthetic
    strategy — the shape a real entry that never got an exit row would have.
    """
    from database.trade_journal_db import TradeJournal

    placed = f"{PAST.isoformat()}T14:44:57+05:30"
    sess = journal_db.db_session
    row = TradeJournal(
        placed_at=placed,
        symbol="ONGC",
        direction="SHORT",
        quantity=QTY,
        strategy_name=SYNTH_STRATEGY,
        signal_source="chartink",
        entry_price=ENTRY_PRICE,
        entry_order_id="E-ONGC",
        created_at=placed,
        updated_at=placed,
    )
    sess.add(row)
    sess.commit()
    jid = int(row.id)
    sess.remove()
    return jid


def _insert_sandbox_squareoff(sandbox_db):
    """Sandbox flattened the SHORT with a covering BUY fill; position now flat."""
    sess = sandbox_db.db_session
    sess.add(
        sandbox_db.SandboxTrades(
            tradeid="T-ONGC-BUY",
            orderid="X-ONGC",
            user_id="default",
            symbol="ONGC",
            exchange="NSE",
            action="BUY",
            quantity=QTY,
            price=EXIT_PRICE,
            product="MIS",
            strategy=SYNTH_STRATEGY,
            trade_timestamp=dt.datetime(2026, 6, 9, 16, 46, 0),
        )
    )
    sess.add(
        sandbox_db.SandboxPositions(
            user_id="default",
            symbol="ONGC",
            exchange="NSE",
            product="MIS",
            quantity=0,
            average_price=ENTRY_PRICE,
        )
    )
    sess.commit()
    sess.remove()


def _read_row(journal_db, jid):
    from database.trade_journal_db import TradeJournal

    sess = journal_db.db_session
    try:
        row = sess.query(TradeJournal).filter_by(id=jid).first()
        return {
            "exited_at": row.exited_at,
            "exit_price": row.exit_price,
            "exit_reason": row.exit_reason,
            "pnl": row.pnl,
        }
    finally:
        sess.remove()


def _argv(apply: bool):
    a = ["--from", PAST.isoformat(), "--to", PAST.isoformat(), "--strategy", SYNTH_STRATEGY]
    if apply:
        a.append("--apply")
    return a


# --------------------------------------------------------------------------- #
# 1. --apply stamps the exit columns end to end through the CLI main().
# --------------------------------------------------------------------------- #


def test_cli_apply_writes_exit_row(journal_db, sandbox_db):
    from services.engine_eod_reconciliation_backfill import main

    jid = _insert_open_entry(journal_db)
    _insert_sandbox_squareoff(sandbox_db)

    # Pre-condition: row is genuinely open.
    assert _read_row(journal_db, jid)["exited_at"] is None

    rc = main(_argv(apply=True))
    assert rc == 0

    after = _read_row(journal_db, jid)
    assert after["exited_at"] is not None
    assert after["exit_price"] == pytest.approx(EXIT_PRICE)
    assert after["exit_reason"] == "sandbox_eod_squareoff"
    # SHORT gross P&L = (entry - exit) * qty.
    assert after["pnl"] == pytest.approx((ENTRY_PRICE - EXIT_PRICE) * QTY, abs=0.01)


# --------------------------------------------------------------------------- #
# 2. Idempotency — a second --apply is a no-op (row already closed).
# --------------------------------------------------------------------------- #


def test_cli_apply_is_idempotent(journal_db, sandbox_db):
    from database.trade_journal_db import TradeJournal
    from services.engine_eod_reconciliation_backfill import main

    jid = _insert_open_entry(journal_db)
    _insert_sandbox_squareoff(sandbox_db)

    assert main(_argv(apply=True)) == 0
    first = _read_row(journal_db, jid)

    # Second apply must not touch the now-closed row or create a duplicate.
    assert main(_argv(apply=True)) == 0
    second = _read_row(journal_db, jid)
    assert second == first

    sess = journal_db.db_session
    try:
        total = sess.query(TradeJournal).count()
    finally:
        sess.remove()
    assert total == 1  # no duplicate exit/entry rows


# --------------------------------------------------------------------------- #
# 3. Default (no --apply) is a dry run — writes nothing.
# --------------------------------------------------------------------------- #


def test_cli_dry_run_writes_nothing(journal_db, sandbox_db):
    from services.engine_eod_reconciliation_backfill import main

    jid = _insert_open_entry(journal_db)
    _insert_sandbox_squareoff(sandbox_db)

    rc = main(_argv(apply=False))
    assert rc == 0

    after = _read_row(journal_db, jid)
    assert after["exited_at"] is None
    assert after["exit_reason"] is None
