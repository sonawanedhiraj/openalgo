"""Tests for the in-house scanner browser API (blueprints/scanner_api.py).

Tier 1 read-only endpoints:
  GET /scanner/api/definitions
  GET /scanner/api/definitions/<id>/signals

Tier 2 management + query endpoints:
  POST /scanner/api/definitions/<id>/toggle
  GET  /scanner/api/hits-by-symbol

Uses a bare Flask app (not create_app) to avoid the singleton guard, with an
isolated in-memory SQLite for scanner_db.  Flask session is mocked so tests
don't need a real login flow.
"""

from __future__ import annotations

import json

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

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


def _add_definition(sdb, name="test_def", screener_type="buy", rule_module=None, enabled=1):
    from database.scanner_db import ScanDefinition

    sess = sdb.db_session()
    d = ScanDefinition(
        name=name,
        screener_type=screener_type,
        expression_json="{}",
        rule_module=rule_module,
        enabled=enabled,
        created_at="2026-06-21T09:00:00+05:30",
        updated_at="2026-06-21T09:00:00+05:30",
    )
    sess.add(d)
    sess.commit()
    return d.id


def _add_result(
    sdb, definition_id, symbols=None, source="inhouse", posted=0, run_at="2026-06-21T14:00:00+05:30"
):
    from database.scanner_db import ScanResult

    sess = sdb.db_session()
    r = ScanResult(
        scan_definition_id=definition_id,
        run_at=run_at,
        symbols=json.dumps(symbols or ["RELIANCE", "INFY"]),
        source=source,
        posted_to_engine=posted,
    )
    sess.add(r)
    sess.commit()
    return r.id


# ---------------------------------------------------------------------------
# GET /scanner/api/definitions  (Tier 1 + Tier 2: returns ALL definitions)
# ---------------------------------------------------------------------------


def test_list_definitions_empty(client):
    tc, _sdb = client
    res = tc.get("/scanner/api/definitions")
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "success"
    assert body["data"] == []


def test_list_definitions_returns_all_including_disabled(client):
    """Tier 2: list returns ALL definitions (enabled + disabled), enabled-first."""
    tc, sdb = client
    _add_definition(sdb, "buy_rule", "buy", enabled=1)
    _add_definition(sdb, "disabled_rule", "sell", enabled=0)

    res = tc.get("/scanner/api/definitions")
    assert res.status_code == 200
    body = res.get_json()
    names = [d["name"] for d in body["data"]]
    # Both definitions are returned
    assert "buy_rule" in names
    assert "disabled_rule" in names
    # Enabled-first ordering: buy_rule (enabled=1) before disabled_rule (enabled=0)
    assert names.index("buy_rule") < names.index("disabled_rule")


def test_list_definitions_includes_latest_signals(client):
    tc, sdb = client
    did = _add_definition(sdb, "sig_def", "buy")
    _add_result(sdb, did, ["HDFCBANK", "ICICIBANK"])

    res = tc.get("/scanner/api/definitions")
    assert res.status_code == 200
    body = res.get_json()
    defs = body["data"]
    assert len(defs) == 1
    d = defs[0]
    assert isinstance(d["today_hit_count"], int)
    assert len(d["latest_signals"]) == 1
    assert "HDFCBANK" in d["latest_signals"][0]["symbols"]


def test_list_definitions_latest_signals_capped_at_5(client):
    tc, sdb = client
    did = _add_definition(sdb, "busy_def", "buy")
    for i in range(8):
        _add_result(sdb, did, [f"STOCK{i}"])

    res = tc.get("/scanner/api/definitions")
    assert res.status_code == 200
    body = res.get_json()
    assert len(body["data"][0]["latest_signals"]) <= 5


# ---------------------------------------------------------------------------
# GET /scanner/api/definitions/<id>/signals  (Tier 1 + Tier 2: until param)
# ---------------------------------------------------------------------------


def test_get_signals_not_found(client):
    tc, _sdb = client
    res = tc.get("/scanner/api/definitions/9999/signals")
    assert res.status_code == 404
    assert res.get_json()["status"] == "error"


def test_get_signals_returns_rows(client):
    tc, sdb = client
    did = _add_definition(sdb, "sell_def", "sell")
    _add_result(sdb, did, ["WIPRO"])

    res = tc.get(
        f"/scanner/api/definitions/{did}/signals",
        query_string={"since": "2026-06-01T00:00:00+05:30"},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "success"
    data = body["data"]
    assert data["count"] == 1
    assert data["signals"][0]["symbols"] == ["WIPRO"]
    assert data["definition"]["id"] == did


def test_get_signals_limit_respected(client):
    tc, sdb = client
    did = _add_definition(sdb, "multi_sig", "buy")
    for i in range(10):
        _add_result(sdb, did, [f"S{i}"])

    res = tc.get(
        f"/scanner/api/definitions/{did}/signals",
        query_string={"since": "2026-06-01T00:00:00+05:30", "limit": "3"},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["data"]["count"] <= 3


def test_get_signals_max_limit_capped(client):
    tc, sdb = client
    did = _add_definition(sdb, "cap_def", "buy")

    res = tc.get(
        f"/scanner/api/definitions/{did}/signals",
        query_string={"since": "2026-06-01T00:00:00+05:30", "limit": "9999"},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["data"]["limit"] <= 500


def test_get_signals_response_shape(client):
    tc, sdb = client
    did = _add_definition(sdb, "shape_def", "buy", rule_module="services.scan_rules.fno_buy")
    _add_result(sdb, did, ["TCS"], source="inhouse", posted=1)

    res = tc.get(
        f"/scanner/api/definitions/{did}/signals",
        query_string={"since": "2026-06-01T00:00:00+05:30"},
    )
    assert res.status_code == 200
    data = res.get_json()["data"]
    assert data["definition"]["rule_module"] == "services.scan_rules.fno_buy"
    assert data["signals"][0]["posted_to_engine"] is True
    assert data["signals"][0]["source"] == "inhouse"


def test_get_signals_until_excludes_later_rows(client):
    """Rows after the until timestamp must not appear."""
    tc, sdb = client
    did = _add_definition(sdb, "until_def", "buy")
    _add_result(sdb, did, ["EARLY"], run_at="2026-06-21T10:00:00+05:30")
    _add_result(sdb, did, ["LATE"], run_at="2026-06-21T16:00:00+05:30")

    res = tc.get(
        f"/scanner/api/definitions/{did}/signals",
        query_string={
            "since": "2026-06-21T00:00:00+05:30",
            "until": "2026-06-21T12:00:00+05:30",
        },
    )
    assert res.status_code == 200
    data = res.get_json()["data"]
    syms = [s["symbols"] for s in data["signals"]]
    assert ["EARLY"] in syms
    assert ["LATE"] not in syms


def test_get_signals_response_includes_until_field(client):
    """Response data must carry the `until` field (null when not provided)."""
    tc, sdb = client
    did = _add_definition(sdb, "until_field_def", "buy")

    # Without until param → field is null
    res = tc.get(
        f"/scanner/api/definitions/{did}/signals",
        query_string={"since": "2026-06-01T00:00:00+05:30"},
    )
    assert res.status_code == 200
    assert res.get_json()["data"]["until"] is None

    # With until param → field echoes the value
    until_val = "2026-06-21T15:30:00+05:30"
    res2 = tc.get(
        f"/scanner/api/definitions/{did}/signals",
        query_string={"since": "2026-06-01T00:00:00+05:30", "until": until_val},
    )
    assert res2.status_code == 200
    assert res2.get_json()["data"]["until"] == until_val


# ---------------------------------------------------------------------------
# POST /scanner/api/definitions/<id>/toggle  (Tier 2)
# ---------------------------------------------------------------------------


def test_toggle_disables_enabled_definition(client):
    tc, sdb = client
    did = _add_definition(sdb, "on_def", "buy", enabled=1)

    res = tc.post(f"/scanner/api/definitions/{did}/toggle")
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "success"
    assert body["data"]["id"] == did
    assert body["data"]["enabled"] is False


def test_toggle_enables_disabled_definition(client):
    tc, sdb = client
    did = _add_definition(sdb, "off_def", "sell", enabled=0)

    res = tc.post(f"/scanner/api/definitions/{did}/toggle")
    assert res.status_code == 200
    assert res.get_json()["data"]["enabled"] is True


def test_toggle_persists_to_db(client):
    """After toggling, re-reading the list reflects the new enabled state."""
    tc, sdb = client
    did = _add_definition(sdb, "persist_def", "buy", enabled=1)

    tc.post(f"/scanner/api/definitions/{did}/toggle")  # → disabled

    # Verify via the list endpoint
    res = tc.get("/scanner/api/definitions")
    data = res.get_json()["data"]
    match = next((d for d in data if d["id"] == did), None)
    assert match is not None
    assert match["enabled"] is False


def test_toggle_returns_404_for_missing_definition(client):
    tc, _sdb = client
    res = tc.post("/scanner/api/definitions/9999/toggle")
    assert res.status_code == 404
    assert res.get_json()["status"] == "error"


def test_toggle_double_toggle_restores_original_state(client):
    """Two toggles must return the definition to its original enabled state."""
    tc, sdb = client
    did = _add_definition(sdb, "double_def", "buy", enabled=1)

    tc.post(f"/scanner/api/definitions/{did}/toggle")  # → False
    res = tc.post(f"/scanner/api/definitions/{did}/toggle")  # → True again
    assert res.get_json()["data"]["enabled"] is True


# ---------------------------------------------------------------------------
# GET /scanner/api/hits-by-symbol  (Tier 2)
# ---------------------------------------------------------------------------


def test_hits_by_symbol_empty(client):
    tc, _sdb = client
    res = tc.get("/scanner/api/hits-by-symbol", query_string={"date": "2026-06-21"})
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "success"
    assert body["data"]["symbols"] == []
    assert body["data"]["date"] == "2026-06-21"


def test_hits_by_symbol_aggregates_across_definitions(client):
    """Same symbol appearing in two definitions counts as 2 hits."""
    tc, sdb = client
    did1 = _add_definition(sdb, "def_a", "buy")
    did2 = _add_definition(sdb, "def_b", "sell")
    _add_result(sdb, did1, ["RELIANCE", "INFY"], run_at="2026-06-21T10:00:00+05:30")
    _add_result(sdb, did2, ["RELIANCE"], run_at="2026-06-21T11:00:00+05:30")

    res = tc.get("/scanner/api/hits-by-symbol", query_string={"date": "2026-06-21"})
    assert res.status_code == 200
    data = res.get_json()["data"]

    symbol_map = {s["symbol"]: s for s in data["symbols"]}
    assert "RELIANCE" in symbol_map
    assert symbol_map["RELIANCE"]["hit_count"] == 2
    assert set(symbol_map["RELIANCE"]["definitions"]) == {"def_a", "def_b"}
    assert "INFY" in symbol_map
    assert symbol_map["INFY"]["hit_count"] == 1


def test_hits_by_symbol_sorted_by_hit_count_desc(client):
    """Symbol with more hits comes first."""
    tc, sdb = client
    did = _add_definition(sdb, "sort_def", "buy")
    _add_result(sdb, did, ["TCS"], run_at="2026-06-21T09:30:00+05:30")
    _add_result(sdb, did, ["TCS"], run_at="2026-06-21T10:00:00+05:30")
    _add_result(sdb, did, ["INFOSYS"], run_at="2026-06-21T10:30:00+05:30")

    res = tc.get("/scanner/api/hits-by-symbol", query_string={"date": "2026-06-21"})
    data = res.get_json()["data"]
    syms = [s["symbol"] for s in data["symbols"]]
    assert syms[0] == "TCS"  # 2 hits before INFOSYS 1 hit


def test_hits_by_symbol_date_isolation(client):
    """Results for date=2026-06-20 must not include rows from 2026-06-21."""
    tc, sdb = client
    did = _add_definition(sdb, "iso_def", "buy")
    _add_result(sdb, did, ["YESTERDAY"], run_at="2026-06-20T14:00:00+05:30")
    _add_result(sdb, did, ["TODAY"], run_at="2026-06-21T14:00:00+05:30")

    res = tc.get("/scanner/api/hits-by-symbol", query_string={"date": "2026-06-20"})
    data = res.get_json()["data"]
    symbol_names = [s["symbol"] for s in data["symbols"]]
    assert "YESTERDAY" in symbol_names
    assert "TODAY" not in symbol_names


def test_hits_by_symbol_latest_hit_reflects_most_recent_run(client):
    """latest_hit must be the most recent run_at across all result rows for a symbol."""
    tc, sdb = client
    did = _add_definition(sdb, "lh_def", "buy")
    _add_result(sdb, did, ["SBIN"], run_at="2026-06-21T09:30:00+05:30")
    _add_result(sdb, did, ["SBIN"], run_at="2026-06-21T14:45:00+05:30")

    res = tc.get("/scanner/api/hits-by-symbol", query_string={"date": "2026-06-21"})
    data = res.get_json()["data"]
    sbin = next(s for s in data["symbols"] if s["symbol"] == "SBIN")
    assert sbin["hit_count"] == 2
    assert sbin["latest_hit"] == "2026-06-21T14:45:00+05:30"


def test_list_definitions_enabled_flag_correct_for_both_states(client):
    """enabled field in response must reflect actual DB state for enabled and disabled rows."""
    tc, sdb = client
    did_on = _add_definition(sdb, "on_flag", "buy", enabled=1)
    did_off = _add_definition(sdb, "off_flag", "sell", enabled=0)

    res = tc.get("/scanner/api/definitions")
    data = res.get_json()["data"]
    by_id = {d["id"]: d for d in data}
    assert by_id[did_on]["enabled"] is True
    assert by_id[did_off]["enabled"] is False


# ---------------------------------------------------------------------------
# Tier-3: GET /scanner/api/definitions/<id>  (single definition)
# ---------------------------------------------------------------------------


def test_get_single_definition(client):
    """GET /scanner/api/definitions/<id> returns 200 with correct shape including Tier-3 fields."""
    tc, sdb = client
    did = _add_definition(sdb, "single_def", "buy", rule_module="services.scan_rules.fno_buy")

    res = tc.get(f"/scanner/api/definitions/{did}")
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "success"
    data = body["data"]
    assert data["id"] == did
    assert data["name"] == "single_def"
    assert data["screener_type"] == "buy"
    assert data["rule_module"] == "services.scan_rules.fno_buy"
    assert data["enabled"] is True
    # Tier-3 fields must be present
    assert "parameters_json" in data
    assert "parent_definition_id" in data
    assert data["parameters_json"] is None
    assert data["parent_definition_id"] is None


def test_get_single_definition_not_found(client):
    """GET /scanner/api/definitions/999 returns 404."""
    tc, _sdb = client
    res = tc.get("/scanner/api/definitions/999")
    assert res.status_code == 404
    assert res.get_json()["status"] == "error"


# ---------------------------------------------------------------------------
# Tier-3: POST /scanner/api/definitions/<id>/clone
# ---------------------------------------------------------------------------


def test_clone_definition(client):
    """POST .../clone creates new row; response has id and name; new row has correct parent_definition_id."""
    tc, sdb = client
    did = _add_definition(sdb, "base_for_clone", "buy")

    res = tc.post(
        f"/scanner/api/definitions/{did}/clone",
        data=json.dumps({"name": "my_clone"}),
        content_type="application/json",
    )
    assert res.status_code == 201
    body = res.get_json()
    assert body["status"] == "success"
    assert body["data"]["name"] == "my_clone"
    new_id = body["data"]["id"]
    assert new_id != did

    # Verify the clone is retrievable and has the correct parent
    res2 = tc.get(f"/scanner/api/definitions/{new_id}")
    assert res2.status_code == 200
    clone_data = res2.get_json()["data"]
    assert clone_data["parent_definition_id"] == did
    assert clone_data["screener_type"] == "buy"


def test_clone_duplicate_name_returns_409(client):
    """Cloning with an existing name returns 409."""
    tc, sdb = client
    did = _add_definition(sdb, "orig_for_dup", "buy")
    _add_definition(sdb, "dup_name", "buy")

    res = tc.post(
        f"/scanner/api/definitions/{did}/clone",
        data=json.dumps({"name": "dup_name"}),
        content_type="application/json",
    )
    assert res.status_code == 409
    assert res.get_json()["status"] == "error"


def test_clone_missing_name_returns_400(client):
    """Cloning without a name field returns 400."""
    tc, sdb = client
    did = _add_definition(sdb, "no_name_base", "buy")

    res = tc.post(
        f"/scanner/api/definitions/{did}/clone",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert res.status_code == 400
    assert res.get_json()["status"] == "error"


# ---------------------------------------------------------------------------
# Tier-3: PUT /scanner/api/definitions/<id>/params
# ---------------------------------------------------------------------------


def test_update_params_on_clone(client):
    """PUT .../params with a dict updates parameters_json on a cloned row."""
    tc, sdb = client
    base_id = _add_definition(sdb, "base_for_params", "buy")

    # First clone it
    clone_res = tc.post(
        f"/scanner/api/definitions/{base_id}/clone",
        data=json.dumps({"name": "params_clone"}),
        content_type="application/json",
    )
    clone_id = clone_res.get_json()["data"]["id"]

    # Now update params
    params = {"threshold": 1.5, "volume_mult": 2.0}
    res = tc.put(
        f"/scanner/api/definitions/{clone_id}/params",
        data=json.dumps({"parameters_json": params}),
        content_type="application/json",
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "success"
    assert body["data"]["id"] == clone_id

    # Verify via GET
    get_res = tc.get(f"/scanner/api/definitions/{clone_id}")
    clone_data = get_res.get_json()["data"]
    assert clone_data["parameters_json"] == json.dumps(params)


def test_update_params_on_code_backed_returns_403(client):
    """PUT on a code-backed row (parent_definition_id IS NULL) returns 403."""
    tc, sdb = client
    did = _add_definition(sdb, "code_backed_put", "buy")  # parent_definition_id is NULL

    res = tc.put(
        f"/scanner/api/definitions/{did}/params",
        data=json.dumps({"parameters_json": {"x": 1}}),
        content_type="application/json",
    )
    assert res.status_code == 403
    assert res.get_json()["status"] == "error"


# ---------------------------------------------------------------------------
# Tier-3: DELETE /scanner/api/definitions/<id>
# ---------------------------------------------------------------------------


def test_delete_clone(client):
    """DELETE on a cloned row returns 200 and the row is gone from the list."""
    tc, sdb = client
    base_id = _add_definition(sdb, "base_for_delete", "buy")

    clone_res = tc.post(
        f"/scanner/api/definitions/{base_id}/clone",
        data=json.dumps({"name": "delete_me"}),
        content_type="application/json",
    )
    clone_id = clone_res.get_json()["data"]["id"]

    res = tc.delete(f"/scanner/api/definitions/{clone_id}")
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "success"
    assert body["data"]["id"] == clone_id

    # Verify it's gone
    get_res = tc.get(f"/scanner/api/definitions/{clone_id}")
    assert get_res.status_code == 404


def test_delete_code_backed_returns_403(client):
    """DELETE on a code-backed row (parent_definition_id IS NULL) returns 403."""
    tc, sdb = client
    did = _add_definition(sdb, "code_backed_del", "buy")

    res = tc.delete(f"/scanner/api/definitions/{did}")
    assert res.status_code == 403
    assert res.get_json()["status"] == "error"


def test_delete_with_children_returns_409(client):
    """DELETE on a row that has children returns 409."""
    tc, sdb = client
    base_id = _add_definition(sdb, "base_with_children", "buy")

    # Create a clone (child of base)
    clone_res = tc.post(
        f"/scanner/api/definitions/{base_id}/clone",
        data=json.dumps({"name": "child_clone"}),
        content_type="application/json",
    )
    child_id = clone_res.get_json()["data"]["id"]

    # Now create a grandchild (child of clone) so clone has children
    tc.post(
        f"/scanner/api/definitions/{child_id}/clone",
        data=json.dumps({"name": "grandchild_clone"}),
        content_type="application/json",
    )

    # Attempt to delete the child (which now has a grandchild)
    res = tc.delete(f"/scanner/api/definitions/{child_id}")
    assert res.status_code == 409
    assert res.get_json()["status"] == "error"


def test_force_delete_orphan_code_backed(client):
    """force=true deletes a code-backed ORPHAN (no registered rule) — the
    _p0_always_true leaked-test-rule case — and it leaves get_scan_definitions.
    """
    from services.scanner_service import _clear_rule_registry_for_tests, get_scan_definitions

    tc, sdb = client
    # Ensure the registry has no rule for this name → it is an orphan.
    _clear_rule_registry_for_tests()
    did = _add_definition(sdb, "_p0_always_true", "buy", rule_module="_p0_always_true")

    # get_scan_definitions resolves the DB session dynamically from
    # database.scanner_db.db_session, which the client fixture already rebinds
    # to this isolated DB — so no extra patching is needed here.

    # Without force → protected (403).
    res_noforce = tc.delete(f"/scanner/api/definitions/{did}")
    assert res_noforce.status_code == 403

    # With force → deleted (orphan).
    res = tc.delete(f"/scanner/api/definitions/{did}?force=true")
    assert res.status_code == 200
    assert res.get_json()["data"]["id"] == did

    # Gone from the list (enabled_only=True) and the single-get.
    names = {d["name"] for d in get_scan_definitions(enabled_only=True)}
    assert "_p0_always_true" not in names
    assert tc.get(f"/scanner/api/definitions/{did}").status_code == 404


def test_force_delete_live_code_backed_returns_403(client):
    """force=true on a code-backed row whose rule IS registered stays protected."""
    from services.scanner_service import _clear_rule_registry_for_tests, scan_rule

    tc, sdb = client
    _clear_rule_registry_for_tests()

    @scan_rule("_live_rule_for_test", "buy", "test: live registered rule")
    def _live(bars, indicators):
        return True

    try:
        did = _add_definition(sdb, "live_scanner", "buy", rule_module="_live_rule_for_test")

        res = tc.delete(f"/scanner/api/definitions/{did}?force=true")
        assert res.status_code == 403
        assert res.get_json()["status"] == "error"
        # Still present.
        assert tc.get(f"/scanner/api/definitions/{did}").status_code == 200
    finally:
        _clear_rule_registry_for_tests()


# ---------------------------------------------------------------------------
# Tier-3: auth check — all 4 new endpoints require a logged-in session
# ---------------------------------------------------------------------------


def test_all_new_endpoints_require_auth(monkeypatch, tmp_path):
    """All 4 new Tier-3 endpoints return 401 when no user is in the session."""
    sdb, sess = _rebind_scanner_db(monkeypatch, tmp_path)

    monkeypatch.setattr("utils.session.is_session_valid", lambda: True)

    from blueprints.scanner_api import scanner_api_bp

    monkeypatch.setattr("blueprints.scanner_api.db_session", sess)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-key-auth"  # pragma: allowlist secret
    app.register_blueprint(scanner_api_bp)

    # Seed a definition so the routes can be exercised (id=1 expected)
    did = _add_definition(sdb, "auth_test_def", "buy")

    with app.test_client() as tc:
        # No session user injected — session is empty

        endpoints = [
            ("GET", f"/scanner/api/definitions/{did}"),
            ("POST", f"/scanner/api/definitions/{did}/clone"),
            ("PUT", f"/scanner/api/definitions/{did}/params"),
            ("DELETE", f"/scanner/api/definitions/{did}"),
        ]
        for method, url in endpoints:
            if method == "GET":
                res = tc.get(url)
            elif method == "POST":
                res = tc.post(url, data=json.dumps({"name": "x"}), content_type="application/json")
            elif method == "PUT":
                res = tc.put(
                    url,
                    data=json.dumps({"parameters_json": None}),
                    content_type="application/json",
                )
            else:
                res = tc.delete(url)
            assert res.status_code == 401, f"{method} {url} expected 401, got {res.status_code}"

    sess.remove()
