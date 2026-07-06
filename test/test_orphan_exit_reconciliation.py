"""Tests for ``services.orphan_exit_reconciliation_service`` (issue #157 / R4).

Boot-time reconciliation that marks pre-existing trade_journal rows where
``exit_reason`` was set but ``exit_price`` never landed as
``abandoned_<original>`` so the engine stops re-attempting them on every
restart.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from services import orphan_exit_reconciliation_service as svc

_IST = timezone(timedelta(hours=5, minutes=30))
_TODAY = datetime.now(_IST).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# DB rebind fixture
# --------------------------------------------------------------------------- #


@pytest.fixture
def journal_db(monkeypatch):
    """Rebind trade_journal_db to a fresh in-memory engine per test."""
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


def _insert_journal_row(
    tjdb,
    *,
    symbol: str,
    placed_at: str,
    exit_reason: str | None,
    exit_price: float | None,
    direction: str = "LONG",
    entry_price: float = 100.0,
    exited_at: str | None = None,
) -> int:
    sess = tjdb.db_session()
    try:
        row = tjdb.TradeJournal(
            placed_at=placed_at,
            symbol=symbol,
            direction=direction,
            quantity=10,
            entry_price=entry_price,
            strategy_name="test",
            signal_source="inhouse",
            exit_reason=exit_reason,
            exit_price=exit_price,
            exited_at=exited_at,
            created_at=placed_at,
            updated_at=placed_at,
        )
        sess.add(row)
        sess.commit()
        return row.id
    finally:
        sess.close()


# --------------------------------------------------------------------------- #
# find_orphan_exits
# --------------------------------------------------------------------------- #


def test_find_orphan_exits_returns_only_matching_rows(journal_db):
    """Only rows with exit_reason SET, exit_price NULL, placed_at < today qualify."""
    # ORPHAN — reason set, price NULL, old
    orphan_id = _insert_journal_row(
        journal_db,
        symbol="TCS",
        placed_at="2026-06-19T11:49:00+05:30",
        exit_reason="eod_watchdog",
        exit_price=None,
    )
    # NOT orphan — has exit_price
    _insert_journal_row(
        journal_db,
        symbol="INFY",
        placed_at="2026-06-19T11:49:00+05:30",
        exit_reason="target",
        exit_price=120.0,
    )
    # NOT orphan — no exit_reason yet (legitimately open)
    _insert_journal_row(
        journal_db,
        symbol="HDFC",
        placed_at="2026-06-19T11:49:00+05:30",
        exit_reason=None,
        exit_price=None,
    )
    # NOT orphan — placed TODAY (legitimately may still be in flight)
    _insert_journal_row(
        journal_db,
        symbol="SBIN",
        placed_at=_TODAY + "T11:49:00+05:30",
        exit_reason="stop_loss",
        exit_price=None,
    )

    orphans = svc.find_orphan_exits(today_iso_prefix=_TODAY)
    assert len(orphans) == 1
    assert orphans[0]["id"] == orphan_id
    assert orphans[0]["symbol"] == "TCS"
    assert orphans[0]["exit_reason"] == "eod_watchdog"


def test_find_orphan_exits_skips_already_reconciled_rows(journal_db):
    """Rows whose exit_reason already starts with 'abandoned_' are filtered out."""
    _insert_journal_row(
        journal_db,
        symbol="OLD",
        placed_at="2026-06-12T14:14:00+05:30",
        exit_reason="abandoned_eod_watchdog",
        exit_price=None,
    )
    assert svc.find_orphan_exits(today_iso_prefix=_TODAY) == []


def test_find_orphan_exits_empty_when_db_clean(journal_db):
    """A fresh DB with no orphans returns []."""
    assert svc.find_orphan_exits(today_iso_prefix=_TODAY) == []


def test_find_orphan_exits_returns_empty_on_db_failure(monkeypatch):
    """A DB exception is swallowed → returns []."""
    with patch.object(svc, "find_orphan_exits", side_effect=None) as _:
        pass  # noqa: PIE790 — only here to keep the import linter happy
    # We can't easily simulate the trade_journal_db import-time failure cleanly,
    # but we can patch the inner db_session to raise.
    from database import trade_journal_db as tjdb

    monkeypatch.setattr(tjdb, "db_session", MagicMock(side_effect=RuntimeError("db down")))
    assert svc.find_orphan_exits(today_iso_prefix=_TODAY) == []


# --------------------------------------------------------------------------- #
# reconcile_orphan_exits
# --------------------------------------------------------------------------- #


def test_reconcile_marks_each_orphan_as_abandoned(journal_db):
    """The seven 2026-06-19 + earlier symbols from #157 → all 7 reclassified."""
    orphans = [
        ("TCS", "2026-06-19T11:49:00+05:30", "eod_watchdog"),
        ("LTM", "2026-06-19T11:43:00+05:30", "eod_watchdog"),
        ("PERSISTENT", "2026-06-19T12:08:00+05:30", "eod_watchdog"),
        ("TECHM", "2026-06-19T12:08:00+05:30", "eod_watchdog"),
        ("AUROPHARMA", "2026-06-19T14:08:00+05:30", "eod_watchdog"),
        ("OBEROIRLTY", "2026-06-24T15:03:00+05:30", "eod_watchdog"),
        ("ASHOKLEY", "2026-06-12T14:14:00+05:30", "eod_watchdog"),
    ]
    ids = [
        _insert_journal_row(journal_db, symbol=s, placed_at=p, exit_reason=r, exit_price=None)
        for s, p, r in orphans
    ]

    summary = svc.reconcile_orphan_exits(today_iso_prefix=_TODAY)
    assert summary["orphans"] == 7
    assert summary["reconciled"] == 7
    assert summary["errors"] == 0
    assert set(summary["symbols"]) == {s for s, _, _ in orphans}

    # All rows now carry the abandoned_ prefix.
    sess = journal_db.db_session()
    try:
        for jid in ids:
            row = sess.query(journal_db.TradeJournal).filter_by(id=jid).first()
            assert row.exit_reason.startswith("abandoned_"), f"id={jid} reason={row.exit_reason!r}"
    finally:
        sess.close()


def test_reconcile_is_idempotent_on_second_call(journal_db):
    """Run reconcile twice → second call sees 0 orphans (already abandoned_)."""
    _insert_journal_row(
        journal_db,
        symbol="TCS",
        placed_at="2026-06-19T11:49:00+05:30",
        exit_reason="eod_watchdog",
        exit_price=None,
    )
    first = svc.reconcile_orphan_exits(today_iso_prefix=_TODAY)
    second = svc.reconcile_orphan_exits(today_iso_prefix=_TODAY)
    assert first["reconciled"] == 1
    assert second["orphans"] == 0
    assert second["reconciled"] == 0


def test_reconcile_handles_empty_db(journal_db):
    summary = svc.reconcile_orphan_exits(today_iso_prefix=_TODAY)
    assert summary == {"orphans": 0, "reconciled": 0, "errors": 0, "symbols": []}


def test_reconcile_skips_today_rows(journal_db):
    """A row placed TODAY whose exit might still be in flight is NOT reclassified."""
    _insert_journal_row(
        journal_db,
        symbol="LIVE",
        placed_at=_TODAY + "T15:14:00+05:30",
        exit_reason="eod_watchdog",
        exit_price=None,
    )
    summary = svc.reconcile_orphan_exits(today_iso_prefix=_TODAY)
    assert summary["orphans"] == 0


def test_reconcile_does_not_overwrite_exit_price_on_real_closes(journal_db):
    """A row with exit_price SET is never touched (it's a real fill)."""
    real_id = _insert_journal_row(
        journal_db,
        symbol="REAL",
        placed_at="2026-06-19T11:49:00+05:30",
        exit_reason="target",
        exit_price=120.0,
    )
    svc.reconcile_orphan_exits(today_iso_prefix=_TODAY)
    sess = journal_db.db_session()
    try:
        row = sess.query(journal_db.TradeJournal).filter_by(id=real_id).first()
        assert row.exit_reason == "target"  # unchanged
        assert row.exit_price == 120.0  # unchanged
    finally:
        sess.close()


# --------------------------------------------------------------------------- #
# Boot worker
# --------------------------------------------------------------------------- #


def test_boot_worker_skipped_when_flag_off(monkeypatch):
    monkeypatch.setenv("ORPHAN_EXIT_RECONCILE_ENABLED", "false")
    with (
        patch.object(svc, "_wait_for_broker_session") as wait_fn,
        patch.object(svc, "reconcile_orphan_exits") as reconcile_fn,
    ):
        svc._boot_worker()
    wait_fn.assert_not_called()
    reconcile_fn.assert_not_called()


def test_boot_worker_skipped_when_broker_session_never_up(monkeypatch):
    monkeypatch.setenv("ORPHAN_EXIT_RECONCILE_ENABLED", "true")
    monkeypatch.setenv("ORPHAN_EXIT_RECONCILE_TIMEOUT_SEC", "10")
    with (
        patch.object(svc, "_wait_for_broker_session", return_value=False),
        patch.object(svc, "reconcile_orphan_exits") as reconcile_fn,
    ):
        svc._boot_worker()
    reconcile_fn.assert_not_called()


def test_boot_worker_runs_reconcile_and_notifies_when_orphans_found(monkeypatch):
    monkeypatch.setenv("ORPHAN_EXIT_RECONCILE_ENABLED", "true")
    with (
        patch.object(svc, "_wait_for_broker_session", return_value=True),
        patch.object(
            svc,
            "reconcile_orphan_exits",
            return_value={
                "orphans": 3,
                "reconciled": 3,
                "errors": 0,
                "symbols": ["TCS", "LTM", "ASHOKLEY"],
            },
        ) as reconcile_fn,
        patch.object(svc, "_notify") as notify_fn,
    ):
        svc._boot_worker()
    reconcile_fn.assert_called_once()
    notify_fn.assert_called_once()
    msg = notify_fn.call_args.args[0]
    assert "3/3" in msg
    assert "TCS" in msg and "LTM" in msg


def test_boot_worker_no_notify_when_clean(monkeypatch):
    """Clean DB → reconcile fires but Telegram does NOT (no noise)."""
    monkeypatch.setenv("ORPHAN_EXIT_RECONCILE_ENABLED", "true")
    with (
        patch.object(svc, "_wait_for_broker_session", return_value=True),
        patch.object(
            svc,
            "reconcile_orphan_exits",
            return_value={"orphans": 0, "reconciled": 0, "errors": 0, "symbols": []},
        ),
        patch.object(svc, "_notify") as notify_fn,
    ):
        svc._boot_worker()
    notify_fn.assert_not_called()
