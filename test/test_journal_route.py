"""Integration tests for the ``/journal/*`` Flask routes.

The blueprint is read-only and informational; HTTP 200 always, even when
the journal is empty (returns an empty list / zeroed summary). Non-200
is reserved for actual route-orchestrator errors.

A fresh in-memory SQLite trade_journal DB is wired in via monkeypatch
on the module-level ``db_session`` so each test starts with a clean
slate.
"""

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def app_with_journal(monkeypatch):
    from blueprints.journal import journal_bp
    from database import trade_journal_db as tjdb

    test_engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    test_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    )
    monkeypatch.setattr(tjdb, "engine", test_engine)
    monkeypatch.setattr(tjdb, "db_session", test_session)
    tjdb.Base.metadata.create_all(test_engine)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(journal_bp)

    yield app, tjdb

    test_session.remove()
    test_engine.dispose()


# ---------------------------------------------------------------------------
# /journal/today
# ---------------------------------------------------------------------------


def test_today_empty_returns_zeroed_summary(app_with_journal):
    """No trades → 200 + an empty-shape summary, not a 4xx."""
    app, _ = app_with_journal

    client = app.test_client()
    resp = client.get("/journal/today")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 0
    assert data["total_pnl"] == 0.0
    assert data["winners"] == 0
    assert data["losers"] == 0
    assert data["by_strategy"] == {}
    assert data["by_exit_reason"] == {}


def test_today_with_trades_aggregates(app_with_journal):
    from services import trade_journal_service as tjs

    app, _ = app_with_journal

    j1 = tjs.record_entry(
        symbol="RELIANCE",
        direction="LONG",
        quantity=10,
        strategy_name="trending_equity_intraday",
        signal_source="chartink",
        entry_price=100.0,
    )
    j2 = tjs.record_entry(
        symbol="INFY",
        direction="LONG",
        quantity=10,
        strategy_name="trending_equity_intraday",
        signal_source="chartink",
        entry_price=200.0,
    )
    tjs.record_exit(j1, exit_price=105.0, exit_reason="target")  # +50
    tjs.record_exit(j2, exit_price=195.0, exit_reason="stop_loss")  # -50

    client = app.test_client()
    resp = client.get("/journal/today")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 2
    assert data["winners"] == 1
    assert data["losers"] == 1
    assert data["total_pnl"] == pytest.approx(0.0)
    assert "trending_equity_intraday" in data["by_strategy"]
    assert set(data["by_exit_reason"].keys()) == {"target", "stop_loss"}


# ---------------------------------------------------------------------------
# /journal/recent
# ---------------------------------------------------------------------------


def test_recent_empty_returns_empty_list(app_with_journal):
    app, _ = app_with_journal

    client = app.test_client()
    resp = client.get("/journal/recent")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["hours"] == 24
    assert data["trades"] == []


def test_recent_returns_inserted_rows(app_with_journal):
    from services import trade_journal_service as tjs

    app, _ = app_with_journal
    tjs.record_entry(
        symbol="TCS",
        direction="LONG",
        quantity=3,
        strategy_name="trending_equity_intraday",
        signal_source="chartink",
        entry_price=3800.0,
    )

    client = app.test_client()
    resp = client.get("/journal/recent?hours=1")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["hours"] == 1
    assert len(data["trades"]) == 1
    assert data["trades"][0]["symbol"] == "TCS"


def test_recent_clamps_bad_hours_to_default(app_with_journal):
    """Malformed ?hours= falls back to the default rather than 4xx-ing."""
    app, _ = app_with_journal

    client = app.test_client()
    resp = client.get("/journal/recent?hours=not-a-number")

    assert resp.status_code == 200
    assert resp.get_json()["hours"] == 24


# ---------------------------------------------------------------------------
# /journal/symbol/<symbol>
# ---------------------------------------------------------------------------


def test_symbol_empty_returns_empty_list(app_with_journal):
    app, _ = app_with_journal

    client = app.test_client()
    resp = client.get("/journal/symbol/RELIANCE")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["symbol"] == "RELIANCE"
    assert data["days"] == 7
    assert data["trades"] == []


def test_symbol_returns_matching_rows(app_with_journal):
    from services import trade_journal_service as tjs

    app, _ = app_with_journal
    tjs.record_entry(
        symbol="RELIANCE",
        direction="LONG",
        quantity=10,
        strategy_name="trending_equity_intraday",
        signal_source="chartink",
        entry_price=2500.0,
    )
    tjs.record_entry(
        symbol="INFY",
        direction="LONG",
        quantity=10,
        strategy_name="trending_equity_intraday",
        signal_source="chartink",
        entry_price=1500.0,
    )

    client = app.test_client()
    resp = client.get("/journal/symbol/reliance")  # case-insensitive lookup

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["symbol"] == "RELIANCE"
    assert len(data["trades"]) == 1
    assert data["trades"][0]["symbol"] == "RELIANCE"
