"""Tests for the Strategies Dashboard API (Tier 2, read-only).

Four endpoints:
  GET /strategies/api/list
  GET /strategies/api/<name>
  GET /strategies/api/<name>/pnl-curve
  GET /strategies/api/<name>/parameters/diff

Each test:
  - creates a bare Flask app with the blueprint mounted
  - seeds the Flask session so check_session_validity lets us through
  - monkeypatches the blueprint's module-level session aliases (sf_session,
    ff_session, mode_session, override_session) to in-memory engines so no
    live DB is ever touched
  - monkeypatches _STRATEGIES_DIR to a tmp_path so config reads are hermetic

Hermetic — no live DB, no filesystem writes.
test/conftest.py already redirects all DB env vars to a temp dir.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest
import pytz
from flask import Blueprint, Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_engine():
    """Return a fresh in-memory SQLite engine + session."""
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    return eng, sess


def _login(client) -> None:
    """Seed a valid session so check_session_validity lets us through."""
    now_ist = dt.datetime.now(pytz.timezone("Asia/Kolkata"))
    with client.session_transaction() as s:
        s["logged_in"] = True
        s["user"] = "test_user"
        s["login_time"] = now_ist.isoformat()


def _make_stub_auth_bp() -> Blueprint:
    """Minimal 'auth' blueprint with a /login route so url_for('auth.login') works."""
    bp = Blueprint("auth", __name__)

    @bp.route("/login")
    def login():
        return "Login required", 401

    return bp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def strategies_dir(tmp_path):
    """Create a minimal strategies/ directory with two fake strategies."""
    # sector_follow_cap5_vol
    sf = tmp_path / "sector_follow_cap5_vol"
    sf.mkdir()
    (sf / "config_snapshot.json").write_text(
        json.dumps(
            {
                "version": "0.1.0",
                "mode": "scaffold-only",
                "deployable": False,
                "capital_inr": 250000,
                "gate_sector_pct": 1.0,
                "parity_target": {
                    "sharpe_daily": 2.19,
                    "win_rate_pct": 56.3,
                    "n_trades_window": 625,
                    "window": "2024-01..2026-06",
                },
            }
        ),
        encoding="utf-8",
    )
    (sf / "VERSION_LOG.md").write_text(
        "# Sector Follow Version Log\n\n"
        "## v0.1.0 — 2026-06-01\n"
        "Initial version.\n\n"
        "- gate_sector_pct = 1.0\n",
        encoding="utf-8",
    )

    # simplified_engine — folder name only; journal rows live under
    # strategy_name='trending_equity_intraday' (bridged in the blueprint).
    se = tmp_path / "simplified_engine"
    se.mkdir()
    (se / "config_snapshot.json").write_text(
        json.dumps(
            {
                "version": "v1.1",
                "deployable": True,
                "config": {"mode": "sandbox", "capital": 20000},
            }
        ),
        encoding="utf-8",
    )

    # futures_follow_cap50
    ff = tmp_path / "futures_follow_cap50"
    ff.mkdir()
    (ff / "config_snapshot.json").write_text(
        json.dumps(
            {
                "version": "0.2.0",
                "mode": "sandbox",
                "deployable": True,
                "capital_inr": 1000000,
                "parity_target": {
                    "cagr_pct": 14.44,
                    "sharpe": 1.27,
                    "max_dd_pct": -8.0,
                    "win_rate_pct": 53.4,
                    "n_trades_window": 149,
                    "window": "2024-01..2026-06",
                },
            }
        ),
        encoding="utf-8",
    )
    (ff / "VERSION_LOG.md").write_text(
        "# Futures Follow Version Log\n\n"
        "## v0.2.0 — 2026-06-15\n"
        "Sandbox default.\n\n"
        "## v0.1.0 — 2026-06-14\n"
        "Initial scaffold.\n",
        encoding="utf-8",
    )

    return tmp_path


@pytest.fixture
def wired_dbs(monkeypatch):
    """Rebind all four DB session aliases to fresh in-memory engines.

    Patches both the database module AND the blueprint module's module-level
    aliases (sf_session, ff_session, mode_session, override_session) so routes
    that run inside the request context see the test engine, not the live one.
    """
    import blueprints.strategies_dashboard_api as sda
    from database import (
        futures_follow_db as ffdb,
    )
    from database import (
        sector_follow_db as sfdb,
    )
    from database import (
        strategy_mode_db as smdb,
    )
    from database import (
        strategy_runtime_override_db as srodb,
    )
    from database import (
        trade_journal_db as tjdb,
    )

    sf_eng, sf_sess = _mk_engine()
    ff_eng, ff_sess = _mk_engine()
    sm_eng, sm_sess = _mk_engine()
    sr_eng, sr_sess = _mk_engine()
    tj_eng, tj_sess = _mk_engine()

    # Create tables on the in-memory engines
    sfdb.Base.metadata.create_all(sf_eng)
    ffdb.Base.metadata.create_all(ff_eng)
    smdb.Base.metadata.create_all(sm_eng)
    srodb.Base.metadata.create_all(sr_eng)
    tjdb.Base.metadata.create_all(tj_eng)

    # Patch the database modules (for ORM lookups that go through the module)
    monkeypatch.setattr(sfdb, "engine", sf_eng)
    monkeypatch.setattr(sfdb, "db_session", sf_sess)
    monkeypatch.setattr(ffdb, "engine", ff_eng)
    monkeypatch.setattr(ffdb, "db_session", ff_sess)
    monkeypatch.setattr(smdb, "engine", sm_eng)
    monkeypatch.setattr(smdb, "db_session", sm_sess)
    monkeypatch.setattr(srodb, "engine", sr_eng)
    monkeypatch.setattr(srodb, "db_session", sr_sess)
    monkeypatch.setattr(tjdb, "engine", tj_eng)
    monkeypatch.setattr(tjdb, "db_session", tj_sess)

    # ALSO patch the blueprint's module-level session aliases — these are bound
    # at import time and would otherwise still point at the live-DB sessions.
    monkeypatch.setattr(sda, "sf_session", sf_sess)
    monkeypatch.setattr(sda, "ff_session", ff_sess)
    monkeypatch.setattr(sda, "mode_session", sm_sess)
    monkeypatch.setattr(sda, "override_session", sr_sess)
    monkeypatch.setattr(sda, "journal_session", tj_sess)

    yield {
        "sf": (sf_eng, sf_sess, sfdb),
        "ff": (ff_eng, ff_sess, ffdb),
        "sm": (sm_eng, sm_sess, smdb),
        "sr": (sr_eng, sr_sess, srodb),
        "tj": (tj_eng, tj_sess, tjdb),
    }

    for sess in (sf_sess, ff_sess, sm_sess, sr_sess, tj_sess):
        sess.remove()
    for eng in (sf_eng, ff_eng, sm_eng, sr_eng, tj_eng):
        eng.dispose()


@pytest.fixture
def app(strategies_dir, wired_dbs, monkeypatch):
    """Flask app with strategies_dashboard_bp mounted, pointing at temp strategies_dir."""
    import blueprints.strategies_dashboard_api as sda

    monkeypatch.setattr(sda, "_STRATEGIES_DIR", strategies_dir)

    from blueprints.strategies_dashboard_api import strategies_dashboard_bp
    from limiter import limiter

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    flask_app.config["SECRET_KEY"] = "test-secret-strategies"  # pragma: allowlist secret
    limiter.init_app(flask_app)
    # Register stub auth blueprint so url_for('auth.login') works in check_session_validity
    flask_app.register_blueprint(_make_stub_auth_bp())
    flask_app.register_blueprint(strategies_dashboard_bp)
    return flask_app


# ---------------------------------------------------------------------------
# Auth gate tests
# ---------------------------------------------------------------------------


def test_list_requires_session(app):
    """Without a session, the auth gate should deny (redirect or 401)."""
    with app.test_client() as client:
        resp = client.get("/strategies/api/list")
    # check_session_validity redirects to auth.login (301/302) or returns 401
    assert resp.status_code in (301, 302, 401)


def test_detail_requires_session(app):
    """Without a session, strategy detail should deny access."""
    with app.test_client() as client:
        resp = client.get("/strategies/api/sector_follow_cap5_vol")
    assert resp.status_code in (301, 302, 401)


# ---------------------------------------------------------------------------
# GET /strategies/api/list
# ---------------------------------------------------------------------------


def test_list_returns_all_strategies(app):
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "success"
    names = {s["name"] for s in body["data"]}
    assert "sector_follow_cap5_vol" in names
    assert "futures_follow_cap50" in names


def test_list_summary_fields(app):
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")

    body = resp.get_json()
    sf = next(s for s in body["data"] if s["name"] == "sector_follow_cap5_vol")
    assert sf["version"] == "0.1.0"
    assert sf["deployable"] is False
    assert sf["health"] == "scaffold"
    assert "open_positions" in sf
    assert "active_overrides" in sf


def test_list_futures_is_healthy(app):
    """futures_follow_cap50 is deployable + sandbox → health='healthy'."""
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")

    body = resp.get_json()
    ff = next(s for s in body["data"] if s["name"] == "futures_follow_cap50")
    assert ff["deployable"] is True
    assert ff["health"] == "healthy"


# ---------------------------------------------------------------------------
# GET /strategies/api/<name>
# ---------------------------------------------------------------------------


def test_detail_404_for_unknown(app):
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/does_not_exist")
    assert resp.status_code == 404
    assert resp.get_json()["status"] == "error"


def test_detail_config_snapshot(app):
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/sector_follow_cap5_vol")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "success"
    data = body["data"]
    assert data["name"] == "sector_follow_cap5_vol"
    assert data["version"] == "0.1.0"
    snap = data["config_snapshot"]
    assert snap["gate_sector_pct"] == 1.0
    assert snap["capital_inr"] == 250000


def test_detail_performance_columns(app):
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/futures_follow_cap50")

    data = resp.get_json()["data"]
    bt = data["performance"]["backtest"]
    assert bt["cagr_pct"] == 14.44
    assert bt["sharpe"] == 1.27
    assert bt["max_dd_pct"] == -8.0
    assert bt["n_trades"] == 149


def test_detail_version_log_parsed(app):
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/futures_follow_cap50")

    vl = resp.get_json()["data"]["version_log"]
    assert len(vl) == 2
    versions = [e["version"] for e in vl]
    assert "v0.2.0" in versions
    assert "v0.1.0" in versions
    # entries are in file order (newest first in the .md)
    assert vl[0]["version"] == "v0.2.0"
    assert vl[0]["date"] == "2026-06-15"


def test_detail_recent_trades_empty(app):
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/futures_follow_cap50")

    trades = resp.get_json()["data"]["recent_trades"]
    assert trades == []


def test_detail_recent_trades_with_data(app, wired_dbs):
    """Seed a futures trade and verify it shows up in recent_trades."""
    ff_eng, ff_sess, ffdb = wired_dbs["ff"]
    trade = ffdb.FuturesFollowTrade(
        mode="sandbox",
        side="BUY",
        nifty_symbol="NIFTY30JUN26FUT",
        exchange="NFO",
        product="NRML",
        lots=1,
        quantity=75,
        entry_price=24500.0,
        entry_date="2026-06-21",
        status="placed",
        created_at=dt.datetime(2026, 6, 21, 10, 0, 0),
    )
    ff_sess.add(trade)
    ff_sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/futures_follow_cap50")

    trades = resp.get_json()["data"]["recent_trades"]
    assert len(trades) == 1
    assert trades[0]["side"] == "BUY"
    assert trades[0]["symbol"] == "NIFTY30JUN26FUT"
    assert trades[0]["lots"] == 1


# ---------------------------------------------------------------------------
# GET /strategies/api/<name>/pnl-curve
# ---------------------------------------------------------------------------


def test_pnl_curve_empty_for_scaffold(app):
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/sector_follow_cap5_vol/pnl-curve")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "success"
    assert body["data"]["window"] == "all"
    assert body["data"]["points"] == []


def test_pnl_curve_window_param(app):
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/futures_follow_cap50/pnl-curve?window=1w")

    body = resp.get_json()
    assert body["data"]["window"] == "1w"


def test_pnl_curve_with_futures_data(app, wired_dbs):
    """Seed a net_pnl exit row and verify the daily point appears."""
    ff_eng, ff_sess, ffdb = wired_dbs["ff"]
    exit_trade = ffdb.FuturesFollowTrade(
        mode="sandbox",
        side="SELL",
        nifty_symbol="NIFTY30JUN26FUT",
        exchange="NFO",
        product="NRML",
        lots=1,
        quantity=75,
        entry_date="2026-06-21",
        gross_pnl=1200.0,
        charges_inr=530.0,
        net_pnl=670.0,
        status="placed",
        created_at=dt.datetime(2026, 6, 21, 10, 30, 0),
    )
    ff_sess.add(exit_trade)
    ff_sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/futures_follow_cap50/pnl-curve")

    points = resp.get_json()["data"]["points"]
    assert len(points) == 1
    assert points[0]["date"] == "2026-06-21"
    assert points[0]["pnl"] == 670.0


def test_pnl_curve_404_unknown(app):
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/no_such_strategy/pnl-curve")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /strategies/api/<name>/parameters/diff
# ---------------------------------------------------------------------------


def test_parameters_diff_no_vs(app):
    """Without ?vs= the diff should return current config and empty previous."""
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/sector_follow_cap5_vol/parameters/diff")

    assert resp.status_code == 200
    body = resp.get_json()["data"]
    assert body["current_version"] == "0.1.0"
    assert body["vs_version"] is None
    assert body["current"]["gate_sector_pct"] == 1.0
    assert body["previous"] == {}


def test_parameters_diff_unknown_version(app):
    """?vs=v99.0 doesn't exist → previous is empty."""
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/sector_follow_cap5_vol/parameters/diff?vs=v99.0")

    body = resp.get_json()["data"]
    assert body["vs_version"] == "v99.0"
    assert body["previous"] == {}


def test_parameters_diff_404_unknown(app):
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/does_not_exist/parameters/diff")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Active overrides reflected in list + detail
# ---------------------------------------------------------------------------


def test_paused_override_reflects_health(app, wired_dbs):
    """Insert an active pause override → health should be 'paused'."""
    sr_eng, sr_sess, srodb = wired_dbs["sr"]

    tomorrow = dt.datetime.utcnow() + dt.timedelta(hours=24)
    override = srodb.StrategyRuntimeOverride(
        strategy_name="futures_follow_cap50",
        override_type="pause",
        reason="unit test",
        expires_at=tomorrow,
        set_by="test",
    )
    sr_sess.add(override)
    sr_sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")

    body = resp.get_json()
    ff = next(s for s in body["data"] if s["name"] == "futures_follow_cap50")
    assert ff["health"] == "paused"
    assert len(ff["active_overrides"]) == 1
    assert ff["active_overrides"][0]["type"] == "pause"


def test_expired_override_is_ignored(app, wired_dbs):
    """An expired override should NOT affect health."""
    sr_eng, sr_sess, srodb = wired_dbs["sr"]

    yesterday = dt.datetime.utcnow() - dt.timedelta(hours=24)
    override = srodb.StrategyRuntimeOverride(
        strategy_name="futures_follow_cap50",
        override_type="pause",
        reason="expired",
        expires_at=yesterday,
        set_by="test",
    )
    sr_sess.add(override)
    sr_sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")

    body = resp.get_json()
    ff = next(s for s in body["data"] if s["name"] == "futures_follow_cap50")
    # Expired → should be healthy (no active overrides)
    assert ff["health"] == "healthy"
    assert ff["active_overrides"] == []


# ---------------------------------------------------------------------------
# simplified_engine: folder-name vs journal-name bridge (issue #235)
# ---------------------------------------------------------------------------
#
# The simplified_stock_engine writes every trade_journal row under
# strategy_name='trending_equity_intraday'. The dashboard surfaces these under
# the folder name 'simplified_engine' via the _SIMPLIFIED_ENGINE_JOURNAL_NAME
# constant. Regression-guards the bridge: if someone renames either side
# without updating the other, today's trades vanish from the dashboard.


def _seed_journal_row(tjdb, sess, **kwargs):
    """Insert a trade_journal row with sane defaults."""
    now_iso = dt.datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    defaults = {
        "placed_at": now_iso,
        "symbol": "RVNL",
        "direction": "LONG",
        "quantity": 100,
        "strategy_name": "trending_equity_intraday",
        "signal_source": "chartink",
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    defaults.update(kwargs)
    sess.add(tjdb.TradeJournal(**defaults))
    sess.commit()


def test_list_simplified_engine_surfaces_today_trades(app, wired_dbs):
    """A trade_journal row tagged trending_equity_intraday today should appear
    in the /list response under name='simplified_engine'."""
    tj_eng, tj_sess, tjdb = wired_dbs["tj"]
    today_iso = dt.datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    _seed_journal_row(
        tjdb,
        tj_sess,
        placed_at=today_iso,
        entry_price=240.0,
        entry_order_id="ord_open",
    )  # open
    _seed_journal_row(
        tjdb,
        tj_sess,
        placed_at=today_iso,
        symbol="INDIANB",
        entry_price=815.0,
        exited_at=today_iso,
        exit_price=820.0,
        exit_reason="stop_loss",
        pnl=500.0,
    )  # closed with realized P&L

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")

    body = resp.get_json()
    se = next(s for s in body["data"] if s["name"] == "simplified_engine")
    assert se["open_positions"] == 1
    assert se["today_trade_count"] == 2
    assert se["today_net_pnl"] == 500.0
    assert se["last_trade_at"] is not None


def test_detail_simplified_engine_recent_trades(app, wired_dbs):
    """A seeded journal row should appear in /<name>'s recent_trades."""
    tj_eng, tj_sess, tjdb = wired_dbs["tj"]
    today_iso = dt.datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    _seed_journal_row(
        tjdb,
        tj_sess,
        placed_at=today_iso,
        symbol="PERSISTENT",
        direction="SHORT",
        quantity=20,
        entry_price=4338.0,
        exited_at=today_iso,
        exit_price=4321.0,
        exit_reason="stop_loss",
        pnl=340.0,
    )

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/simplified_engine")

    data = resp.get_json()["data"]
    assert data["name"] == "simplified_engine"
    sandbox = data["performance"]["sandbox"]
    assert sandbox["open_positions"] == 0
    assert sandbox["today_net_pnl"] == 340.0
    trades = data["recent_trades"]
    assert len(trades) == 1
    assert trades[0]["symbol"] == "PERSISTENT"
    assert trades[0]["side"] == "SHORT"
    assert trades[0]["exit_reason"] == "stop_loss"
    assert trades[0]["pnl"] == 340.0


def test_pnl_curve_simplified_engine(app, wired_dbs):
    """Realized-pnl exit rows should aggregate per exit date."""
    tj_eng, tj_sess, tjdb = wired_dbs["tj"]
    _seed_journal_row(
        tjdb,
        tj_sess,
        placed_at="2026-06-28T10:00:00+05:30",
        exited_at="2026-06-28T14:00:00+05:30",
        pnl=200.0,
    )
    _seed_journal_row(
        tjdb,
        tj_sess,
        placed_at="2026-06-29T10:00:00+05:30",
        exited_at="2026-06-29T14:00:00+05:30",
        pnl=300.0,
    )
    # Abandoned-watchdog row (no pnl) — must be excluded from the curve
    _seed_journal_row(
        tjdb,
        tj_sess,
        placed_at="2026-06-29T15:08:00+05:30",
        exited_at="2026-06-29T15:14:00+05:30",
        exit_reason="abandoned_eod_watchdog",
        pnl=None,
    )

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/simplified_engine/pnl-curve")

    points = resp.get_json()["data"]["points"]
    assert points == [
        {"date": "2026-06-28", "pnl": 200.0},
        {"date": "2026-06-29", "pnl": 300.0},
    ]
