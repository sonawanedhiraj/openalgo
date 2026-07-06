"""Tests for GET /scanner/api/currently-matching (blueprints/scanner_api.py, issue #342).

"Currently matching" is a read-only, ephemeral projection over scan_results:
per enabled scan_definition, the symbols with an in-house (source='inhouse')
row within the last SCANNER_ACTIVE_TTL_MIN minutes (default 12). It never
mutates scan_results — the underlying rows (and the /signals, /hits-by-symbol
history endpoints) are completely unaffected; this endpoint is purely additive.

Follows the fixture pattern of test_scanner_api.py: a bare Flask app with
scanner_api_bp mounted, an isolated per-test SQLite file for scanner_db, and
the session decorator bypassed via monkeypatch (tests run outside a real
logged-in browser session).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
import pytz
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

_IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _rebind_scanner_db(monkeypatch, tmp_path):
    """Point scanner_db at a fresh SQLite file for one test."""
    import database.scanner_db as sdb

    db_file = str(tmp_path / "scanner_test.db")
    eng = create_engine(
        f"sqlite:///{db_file}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(sdb, "engine", eng, raising=False)
    monkeypatch.setattr(sdb, "db_session", sess, raising=False)
    sdb.Base.metadata.create_all(bind=eng)
    return sdb, sess


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Bare Flask app with scanner_api_bp mounted + isolated scanner DB."""
    sdb, sess = _rebind_scanner_db(monkeypatch, tmp_path)

    # Bypass session decorator — tests run outside a real logged-in session
    monkeypatch.setattr("utils.session.is_session_valid", lambda: True)

    from blueprints.scanner_api import scanner_api_bp

    # Patch the LOCAL binding in the blueprint module (not just database.scanner_db)
    # because `from database.scanner_db import db_session` creates a local alias.
    monkeypatch.setattr("blueprints.scanner_api.db_session", sess)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-key"  # pragma: allowlist secret
    app.register_blueprint(scanner_api_bp)

    with app.test_client() as tc:
        # Inject a fake session user so the endpoint's session.get("user") check passes
        with tc.session_transaction() as flask_sess:
            flask_sess["user"] = "test_user"
        yield tc, sdb

    sess.remove()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _add_definition(sdb, name="test_def", screener_type="buy", enabled=1):
    from database.scanner_db import ScanDefinition

    sess = sdb.db_session()
    d = ScanDefinition(
        name=name,
        screener_type=screener_type,
        expression_json="{}",
        rule_module=None,
        enabled=enabled,
        created_at="2026-07-06T09:00:00+05:30",
        updated_at="2026-07-06T09:00:00+05:30",
    )
    sess.add(d)
    sess.commit()
    return d.id


def _add_result(sdb, definition_id, symbols, source="inhouse", run_at=None, posted=0):
    from database.scanner_db import ScanResult

    sess = sdb.db_session()
    r = ScanResult(
        scan_definition_id=definition_id,
        run_at=run_at,
        symbols=json.dumps(symbols),
        source=source,
        posted_to_engine=posted,
    )
    sess.add(r)
    sess.commit()
    return r.id


def _ago(minutes: float) -> str:
    """ISO-8601 IST timestamp `minutes` ago from now — matches the endpoint's
    own `datetime.now(_IST)` cutoff computation so tests don't need to freeze
    time."""
    return (datetime.now(_IST) - timedelta(minutes=minutes)).isoformat()


# ---------------------------------------------------------------------------
# GET /scanner/api/currently-matching
# ---------------------------------------------------------------------------


def test_empty_when_no_definitions(client):
    tc, _sdb = client
    res = tc.get("/scanner/api/currently-matching")
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "success"
    assert body["data"]["definitions"] == []
    assert body["data"]["ttl_minutes"] == 12  # default


def test_symbol_recent_row_is_active(client):
    """A row 3 minutes ago is well within the default 12-min TTL -> active."""
    tc, sdb = client
    did = _add_definition(sdb, "buy_def", "buy")
    _add_result(sdb, did, ["INFY"], run_at=_ago(3))

    res = tc.get("/scanner/api/currently-matching")
    assert res.status_code == 200
    data = res.get_json()["data"]
    assert len(data["definitions"]) == 1
    syms = data["definitions"][0]["symbols"]
    assert len(syms) == 1
    assert syms[0]["symbol"] == "INFY"


def test_symbol_stale_row_is_not_active(client):
    """A row 20 minutes ago is beyond the default 12-min TTL -> not active."""
    tc, sdb = client
    did = _add_definition(sdb, "buy_def", "buy")
    _add_result(sdb, did, ["INFY"], run_at=_ago(20))

    res = tc.get("/scanner/api/currently-matching")
    assert res.status_code == 200
    data = res.get_json()["data"]
    assert len(data["definitions"]) == 1
    assert data["definitions"][0]["symbols"] == []


def test_disabled_definition_excluded(client):
    """Only enabled definitions appear in currently-matching."""
    tc, sdb = client
    did = _add_definition(sdb, "disabled_def", "buy", enabled=0)
    _add_result(sdb, did, ["INFY"], run_at=_ago(1))

    res = tc.get("/scanner/api/currently-matching")
    assert res.status_code == 200
    data = res.get_json()["data"]
    assert data["definitions"] == []


def test_chartink_source_excluded(client):
    """source='chartink' rows must not count toward the in-house active list."""
    tc, sdb = client
    did = _add_definition(sdb, "buy_def", "buy")
    _add_result(sdb, did, ["INFY"], source="chartink", run_at=_ago(1))

    res = tc.get("/scanner/api/currently-matching")
    data = res.get_json()["data"]
    assert data["definitions"][0]["symbols"] == []


def test_per_definition_separation(client):
    """Two definitions' active symbols must not bleed into each other."""
    tc, sdb = client
    buy_id = _add_definition(sdb, "buy_def", "buy")
    sell_id = _add_definition(sdb, "sell_def", "sell")
    _add_result(sdb, buy_id, ["INFY", "TCS"], run_at=_ago(2))
    _add_result(sdb, sell_id, ["WIPRO"], run_at=_ago(2))

    res = tc.get("/scanner/api/currently-matching")
    data = res.get_json()["data"]
    by_name = {d["name"]: d for d in data["definitions"]}

    buy_syms = {s["symbol"] for s in by_name["buy_def"]["symbols"]}
    sell_syms = {s["symbol"] for s in by_name["sell_def"]["symbols"]}
    assert buy_syms == {"INFY", "TCS"}
    assert sell_syms == {"WIPRO"}
    assert by_name["buy_def"]["screener_type"] == "buy"
    assert by_name["sell_def"]["screener_type"] == "sell"


def test_multiple_rows_aggregate_first_and_last(client):
    """Repeated re-fires for the same symbol collapse into one entry with the
    earliest run_at in the window as first_seen_at and the latest as
    last_confirmed_at (the Chartink-mirror re-fire-every-5m-bar behavior)."""
    tc, sdb = client
    did = _add_definition(sdb, "buy_def", "buy")
    t_oldest = _ago(10)
    t_middle = _ago(6)
    t_newest = _ago(1)
    # Insert out of chronological order to prove sorting isn't accidental
    _add_result(sdb, did, ["INFY"], run_at=t_middle)
    _add_result(sdb, did, ["INFY"], run_at=t_newest)
    _add_result(sdb, did, ["INFY"], run_at=t_oldest)

    res = tc.get("/scanner/api/currently-matching")
    data = res.get_json()["data"]
    syms = data["definitions"][0]["symbols"]
    assert len(syms) == 1
    entry = syms[0]
    assert entry["symbol"] == "INFY"
    assert entry["first_seen_at"] == t_oldest
    assert entry["last_confirmed_at"] == t_newest


def test_ttl_env_override_respected(client, monkeypatch):
    """SCANNER_ACTIVE_TTL_MIN read at request time — a row 8 min ago is active
    under the default 12-min TTL but drops out when the TTL is lowered to 5."""
    tc, sdb = client
    did = _add_definition(sdb, "buy_def", "buy")
    _add_result(sdb, did, ["INFY"], run_at=_ago(8))

    res_default = tc.get("/scanner/api/currently-matching")
    data_default = res_default.get_json()["data"]
    assert len(data_default["definitions"][0]["symbols"]) == 1

    monkeypatch.setenv("SCANNER_ACTIVE_TTL_MIN", "5")
    res_tight = tc.get("/scanner/api/currently-matching")
    data_tight = res_tight.get_json()["data"]
    assert data_tight["ttl_minutes"] == 5
    assert data_tight["definitions"][0]["symbols"] == []


def test_ttl_env_invalid_falls_back_to_default(client, monkeypatch):
    """A non-integer env value falls back to the default 12 rather than 500ing."""
    tc, sdb = client
    _add_definition(sdb, "buy_def", "buy")
    monkeypatch.setenv("SCANNER_ACTIVE_TTL_MIN", "not-a-number")

    res = tc.get("/scanner/api/currently-matching")
    assert res.status_code == 200
    assert res.get_json()["data"]["ttl_minutes"] == 12


def test_history_endpoints_unaffected_by_ttl(client):
    """Load-bearing: /signals and /hits-by-symbol must keep returning stale
    rows unchanged — the TTL only governs currently-matching. The permanent
    audit trail (scan_results, surfaced via /signals) is never filtered or
    truncated by this feature."""
    tc, sdb = client
    did = _add_definition(sdb, "buy_def", "buy")
    stale_ts = _ago(9999)  # ~a week ago — nowhere near any TTL
    _add_result(sdb, did, ["OLDSTOCK"], run_at=stale_ts)

    # currently-matching correctly excludes it (TTL expired)
    cm = tc.get("/scanner/api/currently-matching").get_json()["data"]
    assert cm["definitions"][0]["symbols"] == []

    # /signals still returns the historical row untouched
    sig_res = tc.get(
        f"/scanner/api/definitions/{did}/signals",
        query_string={"since": "2000-01-01T00:00:00+05:30"},
    )
    assert sig_res.status_code == 200
    sig_data = sig_res.get_json()["data"]
    assert sig_data["count"] == 1
    assert sig_data["signals"][0]["symbols"] == ["OLDSTOCK"]

    # /hits-by-symbol (today's date) is a separate day-scoped view and is
    # likewise untouched by the currently-matching TTL logic.
    date_str = stale_ts[:10]
    hbs_res = tc.get("/scanner/api/hits-by-symbol", query_string={"date": date_str})
    assert hbs_res.status_code == 200
    hbs_data = hbs_res.get_json()["data"]
    assert any(s["symbol"] == "OLDSTOCK" for s in hbs_data["symbols"])


def test_requires_session(client, monkeypatch):
    """No session user -> 401, matching the sibling endpoints' auth contract."""
    tc, _sdb = client
    with tc.session_transaction() as flask_sess:
        flask_sess.pop("user", None)

    res = tc.get("/scanner/api/currently-matching")
    assert res.status_code == 401
    assert res.get_json()["status"] == "error"
