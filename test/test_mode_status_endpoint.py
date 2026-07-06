"""Mocked E2E for ``GET /mode/status`` after the mode-only migration (B2,
2026-06-12).

The endpoint reads the effective mode via ``mode_service.resolve_strategy_mode``
(a back-compat shim over the mode-only ``resolve_mode``) scoped to
``simplified_engine``. Resolution is now ``strategy_mode row → env flag →
sandbox default`` — the retired ``strategy_daily_intent``/legacy ``daily_intent``
tables and the run/pause/halt ``intent`` axis no longer drive it (``intent`` is
hard-wired to ``'run'`` and ``daily_capital_cap`` to ``None`` in the shim).

Two cases prove the mode-only resolution:

* a persistent ``strategy_mode`` row drives the response, attributed
  ``source='strategy_mode'``;
* with NO ``strategy_mode`` row, resolution falls through to the env mode flag,
  attributed ``source='env'``.

Fully hermetic: ``strategy_mode_db``, ``strategy_daily_intent_db`` and
``daily_intent_db`` are rebound to temp SQLite files; ``is_session_valid`` and
``get_analyze_mode`` are monkeypatched so no real session / settings DB is
needed. The global ``test/conftest.py`` redirect + tripwire are an additional
safety net.
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
    """Bare Flask app with mode_status_bp mounted + isolated mode/intent DBs."""
    import database.daily_intent_db as didb
    import database.strategy_daily_intent_db as sdidb
    import database.strategy_mode_db as smdb
    from blueprints.mode_status import mode_status_bp

    didb_sess = _rebind(didb, monkeypatch, tmp_path, "daily_intent.db")
    sdidb_sess = _rebind(sdidb, monkeypatch, tmp_path, "strategy_daily_intent.db")
    smdb_sess = _rebind(smdb, monkeypatch, tmp_path, "strategy_mode.db")

    # Bypass the auth decorator and the settings DB read.
    monkeypatch.setattr("utils.session.is_session_valid", lambda: True)
    monkeypatch.setattr("blueprints.mode_status.get_analyze_mode", lambda: False)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(mode_status_bp)

    yield app.test_client(), didb, sdidb, smdb

    didb_sess.remove()
    sdidb_sess.remove()
    smdb_sess.remove()


def _today(didb):
    return didb._today_ist_str()


# --------------------------------------------------------------------------- #
# 1. A persistent strategy_mode row drives the response (source='strategy_mode').
# --------------------------------------------------------------------------- #


def test_mode_status_sources_from_strategy_mode_row(client):
    test_client, didb, sdidb, smdb = client
    today = _today(didb)

    smdb._set_mode_unchecked("simplified_engine", "live", updated_by="test")

    resp = test_client.get("/mode/status")
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["source"] == "strategy_mode"
    assert data["effective_mode"] == "live"
    # Mode-only: the intent axis is retired — the shim hard-wires these.
    assert data["intent"] == "run"
    assert data["daily_capital_cap"] is None
    assert data["effective"]["source"] == "strategy_mode"
    # Backward-compatible keys are still present.
    assert data["today"] == today
    assert "daily_intent" in data
    assert "analyze_mode" in data


# --------------------------------------------------------------------------- #
# 2. No strategy_mode row → fall-through to the env mode flag (source='env').
# --------------------------------------------------------------------------- #


def test_mode_status_falls_through_to_env(client, monkeypatch):
    test_client, didb, sdidb, smdb = client

    # No strategy_mode row inserted — resolution falls through to the env flag.
    monkeypatch.setenv("SIMPLIFIED_ENGINE_MODE", "sandbox")

    resp = test_client.get("/mode/status")
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["source"] == "env"
    assert data["effective_mode"] == "sandbox"
    assert data["intent"] == "run"  # mode-only: always 'run'
    assert data["daily_capital_cap"] is None
