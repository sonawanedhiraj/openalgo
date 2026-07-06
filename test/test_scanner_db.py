"""Tests for the Stage 1.5 scanner data layer (scan_definitions, scan_results).

Uses an in-memory SQLite engine and monkeypatches the scanner_db module's
``engine`` and ``db_session`` so each test starts from a clean slate.
"""

import json
import time

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_scanner_db(monkeypatch):
    """Point database.scanner_db at a fresh in-memory SQLite for one test."""
    from database import scanner_db as sdb

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=test_engine))

    monkeypatch.setattr(sdb, "engine", test_engine)
    monkeypatch.setattr(sdb, "db_session", test_session)

    yield sdb

    test_session.remove()
    test_engine.dispose()


def test_init_scanner_db_creates_tables(fresh_scanner_db):
    from services import scanner_service

    scanner_service.init_scanner_db()

    inspector = inspect(fresh_scanner_db.engine)
    tables = set(inspector.get_table_names())
    assert "scan_definitions" in tables
    assert "scan_results" in tables

    # Idempotent — calling twice should not raise.
    scanner_service.init_scanner_db()
    tables_after = set(inspect(fresh_scanner_db.engine).get_table_names())
    assert tables == tables_after


def test_create_scan_definition_and_retrieve(fresh_scanner_db):
    from services import scanner_service

    scanner_service.init_scanner_db()

    def_id = scanner_service.create_scan_definition(
        name="fno_intraday_buy",
        screener_type="buy",
        expression_json={"rule": "close > vwap and rsi(14) > 60"},
        enabled=True,
    )
    assert def_id > 0

    rows = scanner_service.get_scan_definitions(enabled_only=True)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "fno_intraday_buy"
    assert row["screener_type"] == "buy"
    assert row["enabled"] is True
    # expression_json round-trips as a JSON string.
    assert json.loads(row["expression_json"]) == {"rule": "close > vwap and rsi(14) > 60"}


def test_create_duplicate_name_raises(fresh_scanner_db):
    from services import scanner_service

    scanner_service.init_scanner_db()

    scanner_service.create_scan_definition(
        name="duplicate_me", screener_type="buy", expression_json={}
    )
    with pytest.raises(IntegrityError):
        scanner_service.create_scan_definition(
            name="duplicate_me", screener_type="sell", expression_json={}
        )


def test_record_scan_result_and_retrieve(fresh_scanner_db):
    from services import scanner_service

    scanner_service.init_scanner_db()

    def_id = scanner_service.create_scan_definition(
        name="chartink_buy", screener_type="buy", expression_json={}
    )

    res_id = scanner_service.record_scan_result(
        scan_definition_id=def_id,
        symbols=["RELIANCE", "INFY", "SBIN"],
        source="chartink",
        posted_to_engine=True,
        notes="seeded by webhook test",
    )
    assert res_id > 0

    results = scanner_service.get_scan_results(hours=24)
    assert len(results) == 1
    r = results[0]
    assert r["scan_definition_id"] == def_id
    assert r["symbols"] == ["RELIANCE", "INFY", "SBIN"]
    assert r["source"] == "chartink"
    assert r["posted_to_engine"] is True
    assert r["notes"] == "seeded by webhook test"


def test_get_scan_results_filters_by_source(fresh_scanner_db):
    from services import scanner_service

    scanner_service.init_scanner_db()

    def_id = scanner_service.create_scan_definition(
        name="mixed_source", screener_type="buy", expression_json={}
    )

    scanner_service.record_scan_result(
        scan_definition_id=def_id, symbols=["A", "B"], source="chartink"
    )
    scanner_service.record_scan_result(scan_definition_id=def_id, symbols=["C"], source="inhouse")
    scanner_service.record_scan_result(
        scan_definition_id=def_id, symbols=["D", "E"], source="shadow"
    )

    chartink_rows = scanner_service.get_scan_results(hours=24, source="chartink")
    inhouse_rows = scanner_service.get_scan_results(hours=24, source="inhouse")
    shadow_rows = scanner_service.get_scan_results(hours=24, source="shadow")
    all_rows = scanner_service.get_scan_results(hours=24)

    assert len(chartink_rows) == 1 and chartink_rows[0]["symbols"] == ["A", "B"]
    assert len(inhouse_rows) == 1 and inhouse_rows[0]["symbols"] == ["C"]
    assert len(shadow_rows) == 1 and shadow_rows[0]["symbols"] == ["D", "E"]
    assert len(all_rows) == 3


def test_get_scan_results_orders_by_run_at_desc(fresh_scanner_db):
    from services import scanner_service

    scanner_service.init_scanner_db()

    def_id = scanner_service.create_scan_definition(
        name="ordering", screener_type="buy", expression_json={}
    )

    # Insert three results with a small sleep so run_at strings sort
    # distinctly. ISO timestamps with microseconds make this reliable.
    scanner_service.record_scan_result(def_id, ["FIRST"], source="chartink")
    time.sleep(0.01)
    scanner_service.record_scan_result(def_id, ["SECOND"], source="chartink")
    time.sleep(0.01)
    scanner_service.record_scan_result(def_id, ["THIRD"], source="chartink")

    rows = scanner_service.get_scan_results(hours=24)
    assert len(rows) == 3
    assert rows[0]["symbols"] == ["THIRD"]
    assert rows[1]["symbols"] == ["SECOND"]
    assert rows[2]["symbols"] == ["FIRST"]


def test_get_scan_definitions_enabled_filter(fresh_scanner_db):
    from services import scanner_service

    scanner_service.init_scanner_db()

    scanner_service.create_scan_definition(
        name="on", screener_type="buy", expression_json={}, enabled=True
    )
    scanner_service.create_scan_definition(
        name="off", screener_type="sell", expression_json={}, enabled=False
    )

    enabled_only = scanner_service.get_scan_definitions(enabled_only=True)
    all_defs = scanner_service.get_scan_definitions(enabled_only=False)

    assert {r["name"] for r in enabled_only} == {"on"}
    assert {r["name"] for r in all_defs} == {"on", "off"}


def test_create_scan_definition_rejects_bad_screener_type(fresh_scanner_db):
    from services import scanner_service

    scanner_service.init_scanner_db()

    with pytest.raises(ValueError):
        scanner_service.create_scan_definition(name="bad", screener_type="hold", expression_json={})


def test_record_scan_result_rejects_bad_source(fresh_scanner_db):
    from services import scanner_service

    scanner_service.init_scanner_db()
    def_id = scanner_service.create_scan_definition(
        name="x", screener_type="buy", expression_json={}
    )

    with pytest.raises(ValueError):
        scanner_service.record_scan_result(def_id, ["A"], source="bogus")


# ---------------------------------------------------------------------------
# Tier-3: parameters_json + parent_definition_id schema tests
# ---------------------------------------------------------------------------


def test_schema_has_new_columns(fresh_scanner_db):
    """Both Tier-3 columns exist in scan_definitions after init."""
    from sqlalchemy import inspect as sa_inspect

    from services import scanner_service

    scanner_service.init_scanner_db()
    cols = {c["name"] for c in sa_inspect(fresh_scanner_db.engine).get_columns("scan_definitions")}
    assert "parameters_json" in cols
    assert "parent_definition_id" in cols


def test_create_definition_without_params(fresh_scanner_db):
    """Creating a definition without Tier-3 kwargs stores NULL and is backwards-compatible."""
    from services import scanner_service

    scanner_service.init_scanner_db()
    def_id = scanner_service.create_scan_definition(
        name="base_buy",
        screener_type="buy",
        rule_module="fno_intraday_buy_chartink",
    )
    rows = scanner_service.get_scan_definitions(enabled_only=False)
    row = next(r for r in rows if r["id"] == def_id)
    assert row["parameters_json"] is None
    assert row["parent_definition_id"] is None


def test_create_definition_with_params_dict(fresh_scanner_db):
    """parameters_json dict and parent_definition_id round-trip correctly."""
    import json

    from services import scanner_service

    scanner_service.init_scanner_db()
    parent_id = scanner_service.create_scan_definition(
        name="base", screener_type="buy", rule_module="fno_intraday_buy_chartink"
    )
    clone_id = scanner_service.create_scan_definition(
        name="custom_gap1",
        screener_type="buy",
        rule_module="fno_intraday_buy_chartink",
        parameters_json={"gap_pct": 1.5, "vol_5m_mult": 1.8},
        parent_definition_id=parent_id,
    )
    rows = scanner_service.get_scan_definitions(enabled_only=False)
    clone = next(r for r in rows if r["id"] == clone_id)
    assert clone["parent_definition_id"] == parent_id
    assert json.loads(clone["parameters_json"]) == {"gap_pct": 1.5, "vol_5m_mult": 1.8}


def test_init_db_idempotent_with_existing_columns(fresh_scanner_db):
    """Calling init_scanner_db() twice does not raise even after columns exist."""
    from services import scanner_service

    scanner_service.init_scanner_db()
    scanner_service.init_scanner_db()  # second call must not raise
