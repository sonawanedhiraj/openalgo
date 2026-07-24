"""Tests for the pre-#447 echo-pollution backfill (issue #449).

Covers the heuristic classifier (single-symbol + time-correlation), the
reclassification write (idempotent), and the dry-run/apply CLI split.
"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from services.scanner_comparison_echo_backfill import (
    apply_reclassification,
    classify_echoes,
    main,
)

DATE = "2026-07-24"


@pytest.fixture
def fresh_dbs(monkeypatch):
    """Rebind scan_cycle_db + scanner_db to fresh in-memory SQLite DBs."""
    from database import scan_cycle_db as ccdb
    from database import scanner_db as sdb

    out = {}
    for mod in (ccdb, sdb):
        eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
        monkeypatch.setattr(mod, "engine", eng)
        monkeypatch.setattr(mod, "db_session", session)
        mod.Base.metadata.create_all(eng)
        out[mod.__name__] = (mod, eng, session)

    yield ccdb, sdb

    for _mod, eng, session in out.values():
        session.remove()
        eng.dispose()


def _insert_cycle(ccdb, started_at, buy=None, sell=None, cycle_kind="chartink"):
    sess = ccdb.db_session
    row = ccdb.ScanCycle(
        started_at=started_at,
        cycle_kind=cycle_kind,
        post_status="ok",
        screener_buy=json.dumps(buy or []),
        screener_sell=json.dumps(sell or []),
    )
    sess.add(row)
    sess.commit()
    return row.id


def _insert_inhouse_result(sdb, run_at, symbol, definition_id=2):
    sess = sdb.db_session
    row = sdb.ScanResult(
        scan_definition_id=definition_id,
        run_at=run_at,
        symbols=json.dumps([symbol]),
        source="inhouse",
        posted_to_engine=1,
    )
    sess.add(row)
    sess.commit()
    return row.id


def test_echo_row_classified_by_symbol_and_time(fresh_dbs):
    """A single-symbol chartink row within 3s of a matching in-house hit is
    an echo — even though the SELL echo landed in screener_buy (the pre-#447
    misfiling)."""
    ccdb, sdb = fresh_dbs
    _insert_inhouse_result(sdb, f"{DATE}T09:40:01.144818+05:30", "SWIGGY")
    echo_id = _insert_cycle(ccdb, f"{DATE}T09:40:01.210575+05:30", buy=["SWIGGY"])

    cls = classify_echoes(DATE)
    assert cls.echo_row_ids == [echo_id]
    assert cls.n_chartink_rows == 1
    assert cls.genuine_buy == set() and cls.genuine_sell == set()


def test_genuine_rows_not_classified(fresh_dbs):
    """Multi-symbol rows, time-distant rows, and symbol-mismatched rows all
    stay genuine and feed the projected unions."""
    ccdb, sdb = fresh_dbs
    _insert_inhouse_result(sdb, f"{DATE}T09:40:01+05:30", "SWIGGY")

    # Multi-symbol: never an echo (poster posts one symbol per event).
    _insert_cycle(ccdb, f"{DATE}T09:40:01+05:30", buy=["SWIGGY", "INFY"])
    # Single-symbol but 15 min away from any in-house hit.
    _insert_cycle(ccdb, f"{DATE}T09:55:30+05:30", sell=["SWIGGY"])
    # Single-symbol, close in time, but different symbol.
    _insert_cycle(ccdb, f"{DATE}T09:40:02+05:30", sell=["INDIGO"])

    cls = classify_echoes(DATE)
    assert cls.echo_row_ids == []
    assert cls.n_chartink_rows == 3
    assert cls.genuine_buy == {"SWIGGY", "INFY"}
    assert cls.genuine_sell == {"SWIGGY", "INDIGO"}


def test_already_reclassified_rows_excluded(fresh_dbs):
    """cycle_kind='inhouse_echo' rows are not rescanned — idempotent."""
    ccdb, sdb = fresh_dbs
    _insert_inhouse_result(sdb, f"{DATE}T09:40:01+05:30", "SWIGGY")
    _insert_cycle(ccdb, f"{DATE}T09:40:01+05:30", buy=["SWIGGY"], cycle_kind="inhouse_echo")

    cls = classify_echoes(DATE)
    assert cls.echo_row_ids == []
    assert cls.n_chartink_rows == 0


def test_apply_reclassification_flips_and_is_idempotent(fresh_dbs):
    ccdb, sdb = fresh_dbs
    _insert_inhouse_result(sdb, f"{DATE}T09:40:01+05:30", "SWIGGY")
    echo_id = _insert_cycle(ccdb, f"{DATE}T09:40:01+05:30", buy=["SWIGGY"])

    assert apply_reclassification([echo_id]) == 1
    sess = ccdb.db_session
    row = sess.query(ccdb.ScanCycle).filter_by(id=echo_id).first()
    assert row.cycle_kind == "inhouse_echo"
    sess.remove()

    # Second apply touches nothing (row is no longer 'chartink').
    assert apply_reclassification([echo_id]) == 0
    assert apply_reclassification([]) == 0


def test_cli_dry_run_writes_nothing(fresh_dbs, capsys):
    ccdb, sdb = fresh_dbs
    _insert_inhouse_result(sdb, f"{DATE}T09:40:01+05:30", "SWIGGY")
    echo_id = _insert_cycle(ccdb, f"{DATE}T09:40:01+05:30", buy=["SWIGGY"])

    rc = main(["--from", DATE, "--to", DATE])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN: would reclassify 1 rows" in out

    sess = ccdb.db_session
    row = sess.query(ccdb.ScanCycle).filter_by(id=echo_id).first()
    assert row.cycle_kind == "chartink"  # untouched
    sess.remove()


def test_cli_apply_reclassifies_and_recomputes(fresh_dbs, capsys, monkeypatch):
    ccdb, sdb = fresh_dbs
    _insert_inhouse_result(sdb, f"{DATE}T09:40:01+05:30", "SWIGGY")
    echo_id = _insert_cycle(ccdb, f"{DATE}T09:40:01+05:30", buy=["SWIGGY"])

    recompute_calls = []

    def _fake_run(date, dispatch_telegram=True):
        recompute_calls.append((date, dispatch_telegram))
        empty = {
            "inhouse_count": 0,
            "chartink_count": 0,
            "intersection_count": 0,
        }
        return {"BUY": dict(empty), "SELL": dict(empty)}

    monkeypatch.setattr(
        "services.scanner_comparison_eod_service.run_comparison_for_date", _fake_run
    )

    rc = main(["--from", DATE, "--to", DATE, "--apply"])
    assert rc == 0

    sess = ccdb.db_session
    row = sess.query(ccdb.ScanCycle).filter_by(id=echo_id).first()
    assert row.cycle_kind == "inhouse_echo"
    sess.remove()

    # Recompute ran for the affected date, with Telegram suppressed.
    assert recompute_calls == [(DATE, False)]
