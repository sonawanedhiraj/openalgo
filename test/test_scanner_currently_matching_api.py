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


# ---------------------------------------------------------------------------
# %-change enrichment + sort (issue #348)
# ---------------------------------------------------------------------------
#
# Enrichment reads two independent accessors:
#   - services.scanner_reference_data.get_broker_prev_close (registry lookup)
#   - services.scanner_service.get_scanner_service().get_today_ohlcv (aggregator)
# Both are patched at the blueprint's call sites so these tests never touch a
# real ScannerService/ZMQ singleton.


def _patch_prev_close(monkeypatch, values: dict[str, float]):
    """Patch blueprints.scanner_api._get_prev_close's underlying registry read."""

    def fake_get_broker_prev_close(symbol, today=None):
        if symbol in values:
            return values[symbol], None
        return None

    monkeypatch.setattr(
        "services.scanner_reference_data.get_broker_prev_close",
        fake_get_broker_prev_close,
    )


def _patch_last_price(monkeypatch, values: dict[str, float], scanner_running: bool = True):
    """Patch blueprints.scanner_api._get_last_price's underlying scanner accessor."""

    class _FakeScannerService:
        def get_today_ohlcv(self, symbol, as_of_date):
            if symbol in values:
                return values[symbol], 1000.0
            return None, None

    def fake_get_scanner_service():
        return _FakeScannerService() if scanner_running else None

    monkeypatch.setattr(
        "services.scanner_service.get_scanner_service",
        fake_get_scanner_service,
    )


def test_buy_side_sorted_pct_desc(client, monkeypatch):
    """BUY side: biggest gainers (highest pct_change) sort first."""
    tc, sdb = client
    did = _add_definition(sdb, "buy_def", "buy")
    _add_result(sdb, did, ["A", "B", "C"], run_at=_ago(1))

    _patch_prev_close(monkeypatch, {"A": 100.0, "B": 100.0, "C": 100.0})
    _patch_last_price(monkeypatch, {"A": 102.0, "B": 110.0, "C": 101.0})  # +2%, +10%, +1%

    res = tc.get("/scanner/api/currently-matching")
    data = res.get_json()["data"]
    syms = data["definitions"][0]["symbols"]
    assert [s["symbol"] for s in syms] == ["B", "A", "C"]
    assert syms[0]["pct_change"] == 10.0
    assert syms[1]["pct_change"] == 2.0
    assert syms[2]["pct_change"] == 1.0


def test_sell_side_sorted_pct_asc(client, monkeypatch):
    """SELL side: biggest losers (lowest/most-negative pct_change) sort first."""
    tc, sdb = client
    did = _add_definition(sdb, "sell_def", "sell")
    _add_result(sdb, did, ["A", "B", "C"], run_at=_ago(1))

    _patch_prev_close(monkeypatch, {"A": 100.0, "B": 100.0, "C": 100.0})
    _patch_last_price(monkeypatch, {"A": 98.0, "B": 90.0, "C": 99.0})  # -2%, -10%, -1%

    res = tc.get("/scanner/api/currently-matching")
    data = res.get_json()["data"]
    syms = data["definitions"][0]["symbols"]
    assert [s["symbol"] for s in syms] == ["B", "A", "C"]
    assert syms[0]["pct_change"] == -10.0
    assert syms[1]["pct_change"] == -2.0
    assert syms[2]["pct_change"] == -1.0


def test_null_pct_change_sorts_last(client, monkeypatch):
    """Symbols with a null pct_change (missing prev_close/last_price) always
    sort after every symbol with a real value, regardless of side, and are
    tie-broken alphabetically among themselves."""
    tc, sdb = client
    did = _add_definition(sdb, "buy_def", "buy")
    _add_result(sdb, did, ["ZNULL", "A", "ANULL"], run_at=_ago(1))

    # Only "A" gets both prev_close and last_price -> real pct_change.
    _patch_prev_close(monkeypatch, {"A": 100.0})
    _patch_last_price(monkeypatch, {"A": 105.0})

    res = tc.get("/scanner/api/currently-matching")
    data = res.get_json()["data"]
    syms = data["definitions"][0]["symbols"]
    assert [s["symbol"] for s in syms] == ["A", "ANULL", "ZNULL"]
    assert syms[0]["pct_change"] == 5.0
    assert syms[1]["pct_change"] is None
    assert syms[2]["pct_change"] is None


def test_registry_miss_yields_null_symbol_still_listed(client, monkeypatch):
    """A missing prev_close registry entry -> pct_change null, but the symbol
    is never dropped from the list."""
    tc, sdb = client
    did = _add_definition(sdb, "buy_def", "buy")
    _add_result(sdb, did, ["NOREG"], run_at=_ago(1))

    _patch_prev_close(monkeypatch, {})  # no registry entries at all
    _patch_last_price(monkeypatch, {"NOREG": 105.0})

    res = tc.get("/scanner/api/currently-matching")
    data = res.get_json()["data"]
    syms = data["definitions"][0]["symbols"]
    assert len(syms) == 1
    assert syms[0]["symbol"] == "NOREG"
    assert syms[0]["prev_close"] is None
    assert syms[0]["last_price"] == 105.0
    assert syms[0]["pct_change"] is None


def test_aggregator_down_all_null_but_200_ok(client, monkeypatch):
    """When the scanner singleton isn't running (e.g. headless/tests), every
    symbol's last_price/pct_change is null but the endpoint still returns
    200 with every symbol listed — enrichment never errors the endpoint."""
    tc, sdb = client
    did = _add_definition(sdb, "buy_def", "buy")
    _add_result(sdb, did, ["A", "B"], run_at=_ago(1))

    _patch_prev_close(monkeypatch, {"A": 100.0, "B": 100.0})
    _patch_last_price(monkeypatch, {}, scanner_running=False)

    res = tc.get("/scanner/api/currently-matching")
    assert res.status_code == 200
    data = res.get_json()["data"]
    syms = data["definitions"][0]["symbols"]
    assert len(syms) == 2
    for s in syms:
        assert s["last_price"] is None
        assert s["pct_change"] is None


def test_enrichment_values_correct_with_seeded_registry(client, monkeypatch):
    """End-to-end value correctness: prev_close from the (mocked) registry,
    last_price from the (mocked) aggregator accessor, pct_change computed and
    rounded to 2dp."""
    tc, sdb = client
    did = _add_definition(sdb, "buy_def", "buy")
    _add_result(sdb, did, ["INFY"], run_at=_ago(1))

    _patch_prev_close(monkeypatch, {"INFY": 1500.0})
    _patch_last_price(monkeypatch, {"INFY": 1523.456})

    res = tc.get("/scanner/api/currently-matching")
    data = res.get_json()["data"]
    entry = data["definitions"][0]["symbols"][0]
    assert entry["prev_close"] == 1500.0
    assert entry["last_price"] == 1523.456
    # (1523.456 / 1500 - 1) * 100 = 1.56373... -> rounds to 1.56
    assert entry["pct_change"] == 1.56


def test_history_endpoints_still_unaffected_by_enrichment(client, monkeypatch):
    """Load-bearing (extends the existing #342 guarantee): /signals and
    /hits-by-symbol are untouched by the #348 enrichment/sort — they never
    call the enrichment accessors and their payload shape is unchanged."""
    tc, sdb = client
    did = _add_definition(sdb, "buy_def", "buy")
    _add_result(sdb, did, ["INFY"], run_at=_ago(1))

    def _boom(*args, **kwargs):
        raise AssertionError("history endpoints must never call enrichment accessors")

    monkeypatch.setattr("services.scanner_reference_data.get_broker_prev_close", _boom)
    monkeypatch.setattr("services.scanner_service.get_scanner_service", _boom)

    sig_res = tc.get(
        f"/scanner/api/definitions/{did}/signals",
        query_string={"since": "2000-01-01T00:00:00+05:30"},
    )
    assert sig_res.status_code == 200

    hbs_res = tc.get("/scanner/api/hits-by-symbol")
    assert hbs_res.status_code == 200
