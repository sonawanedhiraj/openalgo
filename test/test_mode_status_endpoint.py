"""Mocked E2E for ``GET /mode/status`` after the Phase-B unified migration.

The endpoint historically read the effective mode via the legacy global
``resolve_effective_mode`` (date-keyed ``daily_intent`` table). Phase B
(2026-06-11) migrated it to the unified path
``mode_service.resolve_strategy_mode('simplified_engine')`` which honours the
``strategy_daily_intent`` table with the documented fall-through
``unified row → legacy daily_intent → env flag → default``.

Two cases prove the migration:

* a unified ``strategy_daily_intent`` row drives the response and is attributed
  ``source='unified'``;
* with NO unified row, the documented fall-through to the legacy ``daily_intent``
  table still works and is attributed ``source='legacy'``.

Fully hermetic: ``strategy_daily_intent_db`` and ``daily_intent_db`` are rebound
to temp SQLite files; ``is_session_valid`` and ``get_analyze_mode`` are
monkeypatched so no real session / settings DB is needed. The global
``test/conftest.py`` redirect + tripwire are an additional safety net.
"""

from __future__ import annotations

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool


def _rebind(module, monkeypatch, tmp_path, fname):
    db_file = str(tmp_path / fname)
    eng = create_engine(
        f"sqlite:///{db_file}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(module, "engine", eng, raising=False)
    monkeypatch.setattr(module, "db_session", sess, raising=False)
    module.Base.metadata.create_all(bind=eng)
    return sess


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Bare Flask app with mode_status_bp mounted + isolated intent DBs."""
    import database.daily_intent_db as didb
    import database.strategy_daily_intent_db as sdidb
    from blueprints.mode_status import mode_status_bp

    didb_sess = _rebind(didb, monkeypatch, tmp_path, "daily_intent.db")
    sdidb_sess = _rebind(sdidb, monkeypatch, tmp_path, "strategy_daily_intent.db")

    # Unified flag on (its default) — the unified row must be consulted first.
    monkeypatch.setenv("STRATEGY_DAILY_INTENT_ENABLED", "true")
    # Bypass the auth decorator and the settings DB read.
    monkeypatch.setattr("utils.session.is_session_valid", lambda: True)
    monkeypatch.setattr("blueprints.mode_status.get_analyze_mode", lambda: False)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(mode_status_bp)

    yield app.test_client(), didb, sdidb

    didb_sess.remove()
    sdidb_sess.remove()


def _today(didb):
    return didb._today_ist_str()


# --------------------------------------------------------------------------- #
# 1. A unified strategy_daily_intent row drives the response (source='unified').
# --------------------------------------------------------------------------- #


def test_mode_status_sources_from_unified_row(client):
    test_client, didb, sdidb = client
    today = _today(didb)

    sdidb.set_intent(
        "simplified_engine",
        today,
        mode="live",
        intent="pause",
        daily_capital_cap=75000.0,
        updated_by="test",
    )

    resp = test_client.get("/mode/status")
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["source"] == "unified"
    assert data["effective_mode"] == "live"
    assert data["intent"] == "pause"
    assert data["daily_capital_cap"] == pytest.approx(75000.0)
    assert data["effective"]["source"] == "unified"
    # Backward-compatible keys are still present.
    assert data["today"] == today
    assert "daily_intent" in data
    assert "analyze_mode" in data


# --------------------------------------------------------------------------- #
# 2. No unified row → documented fall-through to legacy daily_intent works.
# --------------------------------------------------------------------------- #


def test_mode_status_falls_through_to_legacy(client):
    test_client, didb, sdidb = client
    today = _today(didb)

    # No unified row inserted. Only a legacy daily_intent row exists.
    didb.set_daily_intent("sandbox", set_by="test", date_str=today)

    resp = test_client.get("/mode/status")
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["source"] == "legacy"
    assert data["effective_mode"] == "sandbox"
    assert data["intent"] == "run"  # legacy rows always map to intent='run'
    # The legacy row is also surfaced for observability.
    assert data["daily_intent"] is not None
    assert data["daily_intent"]["intent"] == "sandbox"
