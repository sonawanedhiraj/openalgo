"""Tests for the shadow-diff query ``scanner_service.get_scan_comparison``.

Compares in-house scanner BUY hits (``scan_results``, ``source='inhouse'``)
against live Chartink BUY hits (``scan_cycle.screener_buy``, ``cycle_kind=
'chartink'``) for a single IST trading day, and grades precision/recall/F1.

Both DB modules (``scanner_db`` and ``scan_cycle_db``) are pointed at clean
in-memory SQLite engines and seeded via the ORM — mirroring the monkeypatch
fixture in ``test_scanner_service.py``.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from services import scanner_service

DATE = "2026-06-04"
SCAN_NAME = "fno_intraday_buy_chartink"


@pytest.fixture
def dbs(monkeypatch):
    """Point both scanner_db and scan_cycle_db at fresh in-memory SQLite."""
    from database import scan_cycle_db as scdb
    from database import scanner_db as sdb

    for mod in (sdb, scdb):
        eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
        mod.Base.metadata.create_all(eng)
        monkeypatch.setattr(mod, "engine", eng)
        monkeypatch.setattr(mod, "db_session", sess)

    yield sdb, scdb

    sdb.db_session.remove()
    scdb.db_session.remove()


def _make_definition(sdb, name=SCAN_NAME):
    sess = sdb.db_session
    row = sdb.ScanDefinition(
        name=name,
        screener_type="buy",
        expression_json="{}",
        rule_module=None,
        enabled=1,
        created_at=f"{DATE}T09:00:00+05:30",
        updated_at=f"{DATE}T09:00:00+05:30",
    )
    sess.add(row)
    sess.commit()
    return row.id


def _seed_inhouse(sdb, symbols, *, definition_id=None, date=DATE, source="inhouse"):
    if definition_id is None:
        definition_id = _make_definition(sdb)
    sess = sdb.db_session
    sess.add(
        sdb.ScanResult(
            scan_definition_id=definition_id,
            run_at=f"{date}T10:15:00+05:30",
            symbols=json.dumps(symbols),
            source=source,
            posted_to_engine=0,
            notes=None,
        )
    )
    sess.commit()
    return definition_id


def _seed_chartink(scdb, *, buy=None, sell=None, date=DATE, cycle_kind="chartink"):
    sess = scdb.db_session
    sess.add(
        scdb.ScanCycle(
            started_at=f"{date}T09:30:00+05:30",
            completed_at=f"{date}T09:30:14+05:30",
            cycle_kind=cycle_kind,
            screener_buy=json.dumps(buy) if buy is not None else None,
            screener_sell=json.dumps(sell) if sell is not None else None,
            post_status="ok",
        )
    )
    sess.commit()


def test_empty_data(dbs):
    res = scanner_service.get_scan_comparison(date=DATE, scan_name=SCAN_NAME)
    assert res["inhouse_count"] == 0
    assert res["chartink_count"] == 0
    assert res["intersection_count"] == 0
    assert res["precision"] is None
    assert res["recall"] is None
    assert res["f1"] is None
    assert "error" not in res


def test_chartink_only(dbs):
    sdb, scdb = dbs
    _seed_chartink(scdb, buy=["A", "B", "C"])
    res = scanner_service.get_scan_comparison(date=DATE)
    assert res["inhouse_count"] == 0
    assert res["chartink_count"] == 3
    assert res["recall"] == 0.0
    assert res["precision"] is None
    assert res["f1"] is None
    assert res["chartink_only"] == ["A", "B", "C"]


def test_inhouse_only(dbs):
    sdb, scdb = dbs
    _seed_inhouse(sdb, ["A", "B", "C"])
    res = scanner_service.get_scan_comparison(date=DATE)
    assert res["inhouse_count"] == 3
    assert res["chartink_count"] == 0
    assert res["precision"] == 0.0
    assert res["recall"] is None
    assert res["f1"] is None
    assert res["inhouse_only"] == ["A", "B", "C"]


def test_perfect_overlap(dbs):
    sdb, scdb = dbs
    _seed_inhouse(sdb, ["A", "B", "C"])
    _seed_chartink(scdb, buy=["A", "B", "C"])
    res = scanner_service.get_scan_comparison(date=DATE)
    assert res["intersection"] == ["A", "B", "C"]
    assert res["precision"] == 1.0
    assert res["recall"] == 1.0
    assert res["f1"] == 1.0
    assert res["inhouse_only"] == []
    assert res["chartink_only"] == []


def test_partial_overlap(dbs):
    sdb, scdb = dbs
    _seed_inhouse(sdb, ["A", "B", "C"])
    _seed_chartink(scdb, buy=["B", "C", "D"])
    res = scanner_service.get_scan_comparison(date=DATE)
    assert res["intersection"] == ["B", "C"]
    assert res["intersection_count"] == 2
    assert res["precision"] == pytest.approx(2 / 3)
    assert res["recall"] == pytest.approx(2 / 3)
    assert res["f1"] == pytest.approx(2 / 3)
    assert res["inhouse_only"] == ["A"]
    assert res["chartink_only"] == ["D"]


def test_pytest_noise_filtered(dbs):
    """A non-chartink cycle (e.g. 'trend-up' test pollution) must be excluded."""
    sdb, scdb = dbs
    _seed_chartink(scdb, buy=["A", "B"])
    _seed_chartink(scdb, buy=["NOISE1", "NOISE2"], cycle_kind="trend-up")
    res = scanner_service.get_scan_comparison(date=DATE)
    assert res["chartink_count"] == 2
    assert "NOISE1" not in res["chartink_only"]
    assert res["chartink_only"] == ["A", "B"]


def test_buy_sell_legs(dbs):
    """Only the BUY leg (screener_buy) is counted; SELL leg is ignored."""
    sdb, scdb = dbs
    _seed_inhouse(sdb, ["A", "B"])
    # BUY leg row.
    _seed_chartink(scdb, buy=["A", "B"])
    # SELL leg row ~14s later — screener_sell populated, must be ignored.
    _seed_chartink(scdb, sell=["X", "Y", "Z"])
    res = scanner_service.get_scan_comparison(date=DATE)
    assert res["chartink_count"] == 2
    assert "X" not in (res["chartink_only"] + res["intersection"])
    assert res["precision"] == 1.0
    assert res["recall"] == 1.0


def test_date_and_definition_isolation(dbs):
    """Rows on other dates or under a different definition name are excluded."""
    sdb, scdb = dbs
    def_id = _seed_inhouse(sdb, ["A", "B", "C"])
    # Same definition, different day — must not leak in.
    _seed_inhouse(sdb, ["OLD1"], definition_id=def_id, date="2026-06-03")
    # Different definition name, same day — must not leak in.
    other = _make_definition(sdb, name="some_other_scan")
    _seed_inhouse(sdb, ["OTHER1"], definition_id=other)
    _seed_chartink(scdb, buy=["A", "B", "C"])
    # Chartink row on another day.
    _seed_chartink(scdb, buy=["OLDBUY"], date="2026-06-03")
    res = scanner_service.get_scan_comparison(date=DATE)
    assert res["inhouse_count"] == 3
    assert res["chartink_count"] == 3
    assert res["f1"] == 1.0
