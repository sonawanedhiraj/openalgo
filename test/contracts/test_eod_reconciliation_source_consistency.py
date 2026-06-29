"""EOD reconciliation cross-source contract — trade_journal and sandbox.db
are two views of the same trade; their disagreement is the entire point
of the service.

What this test exercises that existing tests do NOT
---------------------------------------------------
``test/e2e/test_engine_eod_reconciliation.py`` already drives
``reconcile_engine_journal`` through many in-bounds scenarios (engine
exit no-op, sandbox square-off journaled, mixed days, idempotency,
mid-day still-open, partial fills, backfill past date, orphan fill). All
of them assert the right outcome for one particular state of the two
DBs.

The cross-source contract is sharper than any of those: when the two
sources DISAGREE about whether a position is closed, reconcile_engine_journal
must NEVER silently leave the journal row open. It must either:

  * close it (sandbox flat AND covering close fills present), OR
  * record a structured skip with a reason (still_open / no_covering_close_fill
    / position_read_error / fills_read_error / record_exit_error /
    malformed_journal_row)

A silent no-op on a disagreement — entries_checked > 0 but no row touched
AND no skip reason recorded — is the bug class.

Concrete divergence under test: sandbox shows flat + a covering BUY fill;
journal shows an OPEN SHORT. Reconcile MUST close the journal row with
``exit_reason='sandbox_eod_squareoff'``. This is the 2026-06-10 OIL/HINDZINC/
TATAELXSI bug class — three orphan rows that under-reported P&L until the
service shipped.

Symmetric divergence: sandbox shows position STILL OPEN; journal shows
OPEN too. Reconcile MUST NOT close it — it must record a "still_open"
skip so the operator can see WHY the row stayed open.
"""

from __future__ import annotations

import datetime as dt

import pytest
import pytz
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

IST = pytz.timezone("Asia/Kolkata")

# Synthetic strategy scope — disjoint from the engine's real one so the
# tests don't depend on or pollute any real strategy's rows.
SYNTH_STRATEGY = "synthetic_contract_strat"


def _rebind(module, base, monkeypatch, tmp_path, fname):
    """Rebind a database module's ``engine`` + ``db_session`` to a fresh tmp
    SQLite file, and create its tables. Mirrors the rebind helper in
    ``test/e2e/test_engine_eod_reconciliation.py``; deliberately does NOT
    touch ``base.query`` to avoid leaking the temp session into Model.query
    references for later tests.
    """
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


@pytest.fixture
def today():
    return dt.datetime.now(IST).date()


# ----------------------------------------------------------------------- #
# Helpers — build the two-DB world the contract probes.
# ----------------------------------------------------------------------- #
def _record_entry(symbol: str, direction: str, qty: int, entry_price: float, order_id: str) -> int:
    """Insert an OPEN journal row via the real service (records placed_at=now IST)."""
    from services import trade_journal_service

    return trade_journal_service.record_entry(
        symbol=symbol,
        direction=direction,
        quantity=qty,
        strategy_name=SYNTH_STRATEGY,
        signal_source="chartink",
        entry_price=entry_price,
        entry_order_id=order_id,
    )


def _add_sandbox_close_fill(
    sandbox_db, symbol: str, action: str, qty: int, price: float, orderid: str
):
    sess = sandbox_db.db_session
    row = sandbox_db.SandboxTrades(
        tradeid=f"T-{symbol}-{action}-{orderid}",
        orderid=orderid,
        user_id="default",
        symbol=symbol,
        exchange="NSE",
        action=action,
        quantity=qty,
        price=price,
        product="MIS",
        strategy=SYNTH_STRATEGY,
        trade_timestamp=dt.datetime.now(),
    )
    sess.add(row)
    sess.commit()
    sess.remove()


def _set_sandbox_position(sandbox_db, symbol: str, qty: int, avg_price: float):
    """Insert a position row with net ``qty`` (0 == flat)."""
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


def _read_journal_row(journal_db, jid: int) -> dict:
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


# ----------------------------------------------------------------------- #
# Contract A — sandbox FLAT + close fill, journal OPEN → divergence
# resolved by closing the journal row with sandbox_eod_squareoff.
# ----------------------------------------------------------------------- #
def test_sandbox_closed_journal_open_divergence_closes_journal(journal_db, sandbox_db, today):
    """The 2026-06-10 OIL bug class. sandbox.db says the position is flat
    and a covering BUY fill exists at 427.95; trade_journal still shows
    the row OPEN. The two sources DISAGREE about whether the trade closed.

    Contract: reconcile_engine_journal MUST stamp the journal row with
    ``exit_reason='sandbox_eod_squareoff'`` (the dedicated divergence-
    resolution reason). It MUST NOT silently leave the row open. The
    closing fills' price + the entry price determine the realized P&L.
    """
    from services.engine_eod_reconciliation_service import (
        EXIT_REASON_SANDBOX_EOD,
        reconcile_engine_journal,
    )

    entry_price = 460.30
    exit_price = 427.95
    qty = 217
    jid = _record_entry("OIL", "SHORT", qty, entry_price, order_id="E-OIL")

    # Sandbox: covering BUY fill + flat position row. The two sources now
    # divergently report the trade state (journal: open; sandbox: flat).
    _add_sandbox_close_fill(sandbox_db, "OIL", "BUY", qty, exit_price, "X-OIL")
    _set_sandbox_position(sandbox_db, "OIL", 0, entry_price)

    # Pre-condition: journal genuinely thinks it's open.
    assert _read_journal_row(journal_db, jid)["exited_at"] is None

    result = reconcile_engine_journal(today, strategy_name=SYNTH_STRATEGY)

    # Contract — divergence was resolved by closing the journal row.
    assert result.exits_added == 1, (
        f"divergence between sandbox-flat and journal-open was NOT resolved; "
        f"result={result!r} — silent-divergence bug class (the 2026-06-10 OIL "
        f"orphan)"
    )
    after = _read_journal_row(journal_db, jid)
    assert after["exited_at"] is not None
    assert after["exit_reason"] == EXIT_REASON_SANDBOX_EOD, (
        f"reconcile closed the row with the wrong reason: {after['exit_reason']!r} — "
        f"expected {EXIT_REASON_SANDBOX_EOD!r} so the operator can tell sandbox "
        f"square-offs apart from engine-driven exits in the journal"
    )
    assert after["exit_price"] == pytest.approx(exit_price)
    # SHORT P&L = (entry - exit) * qty.
    assert after["pnl"] == pytest.approx((entry_price - exit_price) * qty, abs=0.01)


# ----------------------------------------------------------------------- #
# Contract B — sandbox STILL OPEN, journal OPEN → no divergence in the
# direction the service handles; row stays open AND a structured skip is
# recorded. Silent no-op would be the bug class.
# ----------------------------------------------------------------------- #
def test_sandbox_still_open_journal_open_does_not_silently_close(journal_db, sandbox_db, today):
    """The mid-day case. sandbox still shows the position non-flat; journal
    still shows it open. The two views AGREE the trade is live, so reconcile
    must NOT close the journal row. But — and this is the contract — it
    must NOT silently no-op either. Structured skip with reason='still_open'
    is the trace the operator needs to confirm "nothing happened because
    the position really is still open", not "the service silently broke".
    """
    from services.engine_eod_reconciliation_service import reconcile_engine_journal

    jid = _record_entry("OIL", "SHORT", 217, 460.30, order_id="E-OIL")
    # Sandbox still SHORT 217 — position is non-flat.
    _set_sandbox_position(sandbox_db, "OIL", -217, 460.30)
    # No closing fill yet.

    result = reconcile_engine_journal(today, strategy_name=SYNTH_STRATEGY)

    # Contract: NO exit written.
    assert result.exits_added == 0
    after = _read_journal_row(journal_db, jid)
    assert after["exited_at"] is None
    assert after["exit_reason"] is None

    # But the open row WAS examined — not silently skipped.
    assert result.entries_checked == 1, (
        f"open journal row was not examined at all (entries_checked={result.entries_checked}); "
        f"silent-no-op bug class"
    )
    # AND a structured skip with the correct reason was recorded.
    still_open_skips = [s for s in result.skipped if s.get("reason") == "still_open"]
    assert still_open_skips, (
        f"row was examined but no still_open skip was recorded; result.skipped={result.skipped!r} — "
        f"silent-divergence bug class (the operator can't see WHY the row stayed open)"
    )
    assert any(s.get("symbol") == "OIL" for s in still_open_skips)


# ----------------------------------------------------------------------- #
# Contract C — sandbox FLAT but NO covering fill (orphan close path) → row
# stays open AND a structured skip is recorded ("no_covering_close_fill").
# This is the third divergence shape that must NOT silently close.
# ----------------------------------------------------------------------- #
def test_sandbox_flat_without_covering_fill_does_not_invent_exit(journal_db, sandbox_db, today):
    """Sandbox position row shows flat (qty=0) but there's no closing-action
    fill the service can price an exit from. The two sources are
    ambiguously divergent — the position is "gone" but the closing trade
    is missing. The safe choice is to leave the journal row open AND
    record a "no_covering_close_fill" skip so the operator sees the data
    gap.

    Bug class: a future "best-effort" patch that price-stamps the journal
    exit from the entry price (or worse, from a random sandbox fill) would
    silently fabricate an exit, looking like a clean reconcile.
    """
    from services.engine_eod_reconciliation_service import reconcile_engine_journal

    jid = _record_entry("OIL", "SHORT", 217, 460.30, order_id="E-OIL")
    _set_sandbox_position(sandbox_db, "OIL", 0, 460.30)
    # NO _add_sandbox_close_fill — the closing fill is missing.

    result = reconcile_engine_journal(today, strategy_name=SYNTH_STRATEGY)

    # Contract: row stays open.
    assert result.exits_added == 0
    after = _read_journal_row(journal_db, jid)
    assert after["exited_at"] is None
    assert after["exit_reason"] is None

    # AND the structured skip surfaces the divergence shape.
    no_fill_skips = [s for s in result.skipped if s.get("reason") == "no_covering_close_fill"]
    assert no_fill_skips, (
        f"flat-but-no-fill divergence produced no structured skip; result.skipped={result.skipped!r}"
    )
    assert any(s.get("symbol") == "OIL" for s in no_fill_skips)
