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

    # simplified_engine — folder carries config; journal rows live under the
    # registered name "trending_equity_intraday" (issue #235 bridge).
    se = tmp_path / "simplified_engine"
    se.mkdir()
    (se / "config_snapshot.json").write_text(
        json.dumps(
            {
                "version": "v1.1",
                "mode": "sandbox",
                "deployable": True,
                "config": {"capital": 20000, "max_trades_per_day": 4},
            }
        ),
        encoding="utf-8",
    )

    # trending_equity_intraday — the journal-name twin of simplified_engine.
    # It has no config_snapshot of its own and MUST be hidden from the list so
    # it doesn't show up as an empty duplicate row.
    tei = tmp_path / "trending_equity_intraday"
    tei.mkdir()
    (tei / "LEARNINGS.md").write_text("# Trending Equity Intraday\n", encoding="utf-8")

    # open15_vol_breakout — measurement deployment: parity_target is
    # deliberately null (R58), so its Backtest column stays empty while the
    # Sandbox/Live columns aggregate open15_trades (issue #442).
    o15 = tmp_path / "open15_vol_breakout"
    o15.mkdir()
    (o15 / "config_snapshot.json").write_text(
        json.dumps(
            {
                "version": "0.1.0",
                "mode": "sandbox",
                "deployable": True,
                "parity_target": None,
            }
        ),
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
        open15_breakout_db as o15db,
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
    o15_eng, o15_sess = _mk_engine()

    # Create tables on the in-memory engines
    sfdb.Base.metadata.create_all(sf_eng)
    ffdb.Base.metadata.create_all(ff_eng)
    smdb.Base.metadata.create_all(sm_eng)
    srodb.Base.metadata.create_all(sr_eng)
    tjdb.Base.metadata.create_all(tj_eng)
    o15db.Base.metadata.create_all(o15_eng)

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
    # open15 is imported lazily inside the routes/helpers, so patching the
    # database module alone is sufficient (no blueprint-level alias exists).
    monkeypatch.setattr(o15db, "engine", o15_eng)
    monkeypatch.setattr(o15db, "db_session", o15_sess)

    # ALSO patch the blueprint's module-level session aliases — these are bound
    # at import time and would otherwise still point at the live-DB sessions.
    monkeypatch.setattr(sda, "sf_session", sf_sess)
    monkeypatch.setattr(sda, "ff_session", ff_sess)
    monkeypatch.setattr(sda, "mode_session", sm_sess)
    monkeypatch.setattr(sda, "override_session", sr_sess)
    monkeypatch.setattr(sda, "tj_session", tj_sess)

    yield {
        "sf": (sf_eng, sf_sess, sfdb),
        "ff": (ff_eng, ff_sess, ffdb),
        "sm": (sm_eng, sm_sess, smdb),
        "sr": (sr_eng, sr_sess, srodb),
        "tj": (tj_eng, tj_sess, tjdb),
        "o15": (o15_eng, o15_sess, o15db),
    }

    for sess in (sf_sess, ff_sess, sm_sess, sr_sess, tj_sess, o15_sess):
        sess.remove()
    for eng in (sf_eng, ff_eng, sm_eng, sr_eng, tj_eng, o15_eng):
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


def test_detail_sandbox_lifetime_pnl_and_winrate(app, wired_dbs):
    """Sandbox column shows since-inception cumulative realized P&L, running
    win-rate, and the closed-trade denominator; live history stays isolated in
    the Live column (issue #323)."""
    _ff_eng, ff_sess, ffdb = wired_dbs["ff"]
    # Two closed sandbox exits: one win (+10802.84), one loss (-1500.0).
    for npnl in (10802.84, -1500.0):
        ff_sess.add(
            ffdb.FuturesFollowTrade(
                mode="sandbox",
                side="SELL",
                nifty_symbol="NIFTY28JUL26FUT",
                exchange="NFO",
                product="NRML",
                lots=1,
                quantity=65,
                entry_date="2026-06-20",
                net_pnl=npnl,
                status="placed",
                created_at=_utc_naive_for_ist_today(),
            )
        )
    # A live-mode exit must NOT leak into the sandbox column.
    ff_sess.add(
        ffdb.FuturesFollowTrade(
            mode="live",
            side="SELL",
            nifty_symbol="NIFTY28JUL26FUT",
            exchange="NFO",
            product="NRML",
            lots=1,
            quantity=65,
            entry_date="2026-06-20",
            net_pnl=999.0,
            status="placed",
            created_at=_utc_naive_for_ist_today(),
        )
    )
    ff_sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/futures_follow_cap50")

    perf = resp.get_json()["data"]["performance"]
    sb = perf["sandbox"]
    assert sb["cum_net_pnl"] == 9302.84  # 10802.84 - 1500.0
    assert sb["closed_trades"] == 2
    assert sb["win_rate_pct"] == 50.0
    # Live column surfaces its own realized history, isolated from sandbox.
    assert perf["live"]["cum_net_pnl"] == 999.0
    assert perf["live"]["closed_trades"] == 1
    assert perf["live"]["win_rate_pct"] == 100.0


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
    entry = ffdb.FuturesFollowTrade(
        mode="sandbox",
        side="BUY",
        nifty_symbol="NIFTY30JUN26FUT",
        exchange="NFO",
        product="NRML",
        lots=1,
        quantity=75,
        entry_price=24500.0,
        entry_date="2026-06-21",
        margin_inr=250000.0,
        status="placed",
        created_at=dt.datetime(2026, 6, 21, 10, 0, 0),
    )
    exit_row = ffdb.FuturesFollowTrade(
        mode="sandbox",
        side="SELL",
        nifty_symbol="NIFTY30JUN26FUT",
        exchange="NFO",
        product="NRML",
        lots=1,
        quantity=75,
        entry_price=24500.0,
        exit_price=24650.0,
        entry_date="2026-06-21",
        margin_inr=250000.0,
        gross_pnl=11250.0,
        charges_inr=468.16,
        net_pnl=10781.84,
        status="placed",
        created_at=dt.datetime(2026, 6, 22, 15, 25, 0),
    )
    ff_sess.add_all([entry, exit_row])
    ff_sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/futures_follow_cap50")

    trades = resp.get_json()["data"]["recent_trades"]
    assert len(trades) == 2
    # Ordered created_at DESC -> the SELL exit is first and carries the financials.
    sell = trades[0]
    assert sell["side"] == "SELL"
    assert sell["symbol"] == "NIFTY30JUN26FUT"
    assert sell["lots"] == 1
    assert sell["gross_pnl"] == 11250.0
    assert sell["charges_inr"] == 468.16
    assert sell["net_pnl"] == 10781.84
    assert sell["margin_inr"] == 250000.0
    # The exit leg carries both the buy and sell price of the NIFTY future.
    assert sell["entry_price"] == 24500.0
    assert sell["exit_price"] == 24650.0
    # The BUY entry has no realized P&L but still reports capital deployed and
    # its buy price (no sell price yet).
    buy = trades[1]
    assert buy["side"] == "BUY"
    assert buy["net_pnl"] is None
    assert buy["margin_inr"] == 250000.0
    assert buy["entry_price"] == 24500.0
    assert buy["exit_price"] is None


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
# T+1 exit "today" attribution (issue #301)
# ---------------------------------------------------------------------------


def _utc_naive_for_ist_today(hour_ist: int = 15, minute_ist: int = 14) -> dt.datetime:
    """A naive-UTC datetime whose IST calendar date is *today* (mirrors how the
    engine stamps created_at via datetime.utcnow at ~15:14 IST)."""
    now_ist = dt.datetime.now(pytz.timezone("Asia/Kolkata"))
    ist_ts = now_ist.replace(hour=hour_ist, minute=minute_ist, second=0, microsecond=0)
    return ist_ts.astimezone(pytz.utc).replace(tzinfo=None)


def test_futures_today_pnl_counts_t1_exit_filled_today(app, wired_dbs):
    """A T+1 SELL that fills TODAY (entry_date = yesterday) must be counted in
    today's realized P&L and trade count, and its yesterday-entered position must
    NOT still show as open (issue #301)."""
    _ff_eng, ff_sess, ffdb = wired_dbs["ff"]
    today = dt.datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    yesterday = (dt.datetime.now(pytz.timezone("Asia/Kolkata")) - dt.timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )

    # Yesterday's entry (BUY, placed yesterday).
    ff_sess.add(
        ffdb.FuturesFollowTrade(
            mode="sandbox",
            side="BUY",
            nifty_symbol="NIFTY28JUL26FUT",
            exchange="NFO",
            product="NRML",
            lots=1,
            quantity=65,
            entry_price=24090.0,
            entry_date=yesterday,
            status="placed",
            created_at=_utc_naive_for_ist_today() - dt.timedelta(days=1),
        )
    )
    # Today's T+1 exit (SELL) — entry_date carries YESTERDAY (the entry session).
    ff_sess.add(
        ffdb.FuturesFollowTrade(
            mode="sandbox",
            side="SELL",
            nifty_symbol="NIFTY28JUL26FUT",
            exchange="NFO",
            product="NRML",
            lots=1,
            quantity=65,
            entry_price=24092.6,
            exit_price=24266.0,
            entry_date=yesterday,
            gross_pnl=11271.0,
            charges_inr=468.16,
            net_pnl=10802.84,
            status="placed",
            created_at=_utc_naive_for_ist_today(),
        )
    )
    # Two fresh entries opened today (still open).
    for _ in range(2):
        ff_sess.add(
            ffdb.FuturesFollowTrade(
                mode="sandbox",
                side="BUY",
                nifty_symbol="NIFTY28JUL26FUT",
                exchange="NFO",
                product="NRML",
                lots=1,
                quantity=65,
                entry_price=24254.0,
                entry_date=today,
                status="placed",
                created_at=_utc_naive_for_ist_today(hour_ist=15, minute_ist=20),
            )
        )
    ff_sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")

    ff = next(s for s in resp.get_json()["data"] if s["name"] == "futures_follow_cap50")
    # Realized P&L from today's T+1 exit is attributed to today.
    assert ff["today_net_pnl"] == 10802.84
    # 3 placed BUYs − 1 placed SELL = 2 net open positions.
    assert ff["open_positions"] == 2
    # 2 entries today + 1 exit today = 3 legs executed today.
    assert ff["today_trade_count"] == 3


# ---------------------------------------------------------------------------
# Batched-exit open-count fix (issue #353)
# ---------------------------------------------------------------------------


def test_futures_batched_exit_leaves_no_phantom_open_position(app, wired_dbs):
    """Row-count parity (BUY rows − SELL rows) overcounts open positions when a
    single exit order squares off more than one BUY lot. Mirrors the 2026-07-06
    evidence: BUY 65 (07-01) + BUY 65 + BUY 65 (07-02) = 3 rows / 195 qty, closed
    by SELL 65 (07-02) + SELL 130 (07-03, one order covering both 07-02 lots) =
    2 rows / 195 qty. Book is flat (195 - 195 = 0), but the old
    ``len(BUYs) - len(SELLs)`` = 3 - 2 = 1 phantom open position. The fix sums
    signed quantity instead, so a flat book always reads 0 regardless of how
    many order legs the exits were split across."""
    _ff_eng, ff_sess, ffdb = wired_dbs["ff"]
    rows = [
        # 07-01 entry
        {"side": "BUY", "quantity": 65, "entry_date": "2026-07-01"},
        # 07-02 entries (two lots opened same day)
        {"side": "BUY", "quantity": 65, "entry_date": "2026-07-02"},
        {"side": "BUY", "quantity": 65, "entry_date": "2026-07-02"},
        # 07-02 exit of the 07-01 lot
        {"side": "SELL", "quantity": 65, "entry_date": "2026-07-01"},
        # 07-03 BATCHED exit — one order closes BOTH 07-02 lots (qty=130)
        {"side": "SELL", "quantity": 130, "entry_date": "2026-07-02"},
    ]
    for i, r in enumerate(rows):
        ff_sess.add(
            ffdb.FuturesFollowTrade(
                mode="sandbox",
                side=r["side"],
                nifty_symbol="NIFTY28JUL26FUT",
                exchange="NFO",
                product="NRML",
                lots=1,
                quantity=r["quantity"],
                entry_price=24000.0,
                exit_price=24100.0 if r["side"] == "SELL" else None,
                entry_date=r["entry_date"],
                status="placed",
                created_at=_utc_naive_for_ist_today() - dt.timedelta(days=(4 - i)),
            )
        )
    ff_sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")

    ff = next(s for s in resp.get_json()["data"] if s["name"] == "futures_follow_cap50")
    assert ff["open_positions"] == 0


def test_futures_single_lot_lifecycle_open_then_flat(app, wired_dbs):
    """A single BUY then its matching SELL: open goes 1 -> 0 (sanity check that
    the quantity-sum rewrite doesn't regress the simple, non-batched case)."""
    _ff_eng, ff_sess, ffdb = wired_dbs["ff"]

    ff_sess.add(
        ffdb.FuturesFollowTrade(
            mode="sandbox",
            side="BUY",
            nifty_symbol="NIFTY28JUL26FUT",
            exchange="NFO",
            product="NRML",
            lots=1,
            quantity=75,
            entry_price=24000.0,
            entry_date="2026-07-05",
            status="placed",
            created_at=_utc_naive_for_ist_today() - dt.timedelta(days=1),
        )
    )
    ff_sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")
    ff = next(s for s in resp.get_json()["data"] if s["name"] == "futures_follow_cap50")
    assert ff["open_positions"] == 1

    ff_sess.add(
        ffdb.FuturesFollowTrade(
            mode="sandbox",
            side="SELL",
            nifty_symbol="NIFTY28JUL26FUT",
            exchange="NFO",
            product="NRML",
            lots=1,
            quantity=75,
            entry_price=24000.0,
            exit_price=24100.0,
            entry_date="2026-07-05",
            net_pnl=7500.0,
            status="placed",
            created_at=_utc_naive_for_ist_today(),
        )
    )
    ff_sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")
    ff = next(s for s in resp.get_json()["data"] if s["name"] == "futures_follow_cap50")
    assert ff["open_positions"] == 0


def test_sector_follow_open_positions_per_symbol_signed_quantity(app, wired_dbs):
    """sector_follow's open-position count must also be quantity-based, not a
    same-day-entry_date row pairing (which — for a T+1 strategy where an exit
    row's entry_date carries the ORIGINAL entry session — never actually
    matched, see the docstring on _sector_follow_stats). Two symbols entered
    on different days, one since exited: exactly 1 should read open."""
    _sf_eng, sf_sess, sfdb = wired_dbs["sf"]

    sf_sess.add(
        sfdb.SectorFollowTrade(
            mode="sandbox",
            side="BUY",
            symbol="RELIANCE",
            exchange="NSE",
            product="CNC",
            quantity=20,
            price=2500.0,
            entry_date="2026-07-01",
            status="placed",
            created_at=_utc_naive_for_ist_today() - dt.timedelta(days=3),
        )
    )
    sf_sess.add(
        sfdb.SectorFollowTrade(
            mode="sandbox",
            side="SELL",
            symbol="RELIANCE",
            exchange="NSE",
            product="CNC",
            quantity=20,
            price=2550.0,
            entry_date="2026-07-01",
            status="placed",
            created_at=_utc_naive_for_ist_today() - dt.timedelta(days=2),
        )
    )
    sf_sess.add(
        sfdb.SectorFollowTrade(
            mode="sandbox",
            side="BUY",
            symbol="TCS",
            exchange="NSE",
            product="CNC",
            quantity=10,
            price=3800.0,
            entry_date="2026-07-05",
            status="placed",
            created_at=_utc_naive_for_ist_today() - dt.timedelta(days=1),
        )
    )
    sf_sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")

    sf = next(s for s in resp.get_json()["data"] if s["name"] == "sector_follow_cap5_vol")
    # RELIANCE round-tripped (flat), TCS still open -> exactly 1 open position.
    assert sf["open_positions"] == 1


def test_futures_pnl_curve_keys_exit_by_execution_date(app, wired_dbs):
    """The P&L curve must attribute a T+1 exit's net_pnl to the day it FILLED
    (created_at IST), not to entry_date (yesterday) — issue #301."""
    _ff_eng, ff_sess, ffdb = wired_dbs["ff"]
    today = dt.datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    yesterday = (dt.datetime.now(pytz.timezone("Asia/Kolkata")) - dt.timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )
    ff_sess.add(
        ffdb.FuturesFollowTrade(
            mode="sandbox",
            side="SELL",
            nifty_symbol="NIFTY28JUL26FUT",
            exchange="NFO",
            product="NRML",
            lots=1,
            quantity=65,
            entry_date=yesterday,
            gross_pnl=11271.0,
            charges_inr=468.16,
            net_pnl=10802.84,
            status="placed",
            created_at=_utc_naive_for_ist_today(),
        )
    )
    ff_sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/futures_follow_cap50/pnl-curve")

    points = resp.get_json()["data"]["points"]
    assert len(points) == 1
    assert points[0]["date"] == today  # exit date, NOT yesterday
    assert points[0]["pnl"] == 10802.84


# ---------------------------------------------------------------------------
# open15_vol_breakout — Sandbox/Live performance columns (issue #442)
# ---------------------------------------------------------------------------


def _o15_row(o15db, **kw):
    """An Open15Trade row with sensible closed-sandbox defaults."""
    today = dt.datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    defaults = {
        "trade_date": today,
        "symbol": "RELIANCE",
        "side": "L",
        "mode": "sandbox",
        "quantity": 100,
        "trigger_price": 1500.0,
        "exit_price": 1510.0,
        "status": "closed",
        "pnl": 1000.0,
        "charges_inr": 60.0,
        "created_at": _utc_naive_for_ist_today(hour_ist=9, minute_ist=21),
    }
    defaults.update(kw)
    return o15db.Open15Trade(**defaults)


def test_open15_sandbox_column_populated(app, wired_dbs):
    """The Sandbox column aggregates open15_trades: cumulative NET P&L
    (gross pnl minus modelled charges, the #433 convention), running win-rate,
    closed-trade count, today's net P&L, and open positions."""
    _eng, sess, o15db = wired_dbs["o15"]
    # One win (+1000 gross - 60 charges), one loss (-500 gross - 40 charges).
    sess.add(_o15_row(o15db))
    sess.add(_o15_row(o15db, symbol="TCS", side="S", pnl=-500.0, charges_inr=40.0))
    # An open row (entered, not yet flattened) counts as an open position.
    sess.add(_o15_row(o15db, symbol="INFY", status="open", pnl=None, charges_inr=None))
    sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/open15_vol_breakout")

    assert resp.status_code == 200
    perf = resp.get_json()["data"]["performance"]
    sb = perf["sandbox"]
    assert sb["cum_net_pnl"] == 400.0  # (1000-60) + (-500-40)
    assert sb["closed_trades"] == 2
    assert sb["win_rate_pct"] == 50.0
    assert sb["today_net_pnl"] == 400.0  # all rows are today's
    assert sb["open_positions"] == 1
    # Never flipped live, no live rows → Live column stays empty ('—' in UI).
    assert perf["live"] is None
    # Backtest column stays empty by design (parity_target: null, R58).
    assert perf["backtest"]["win_rate_pct"] is None


def test_open15_live_column_isolated_from_sandbox(app, wired_dbs):
    """A live-mode closed row populates the Live column with its own history;
    sandbox history never leaks across (futures_follow precedent)."""
    _eng, sess, o15db = wired_dbs["o15"]
    sess.add(_o15_row(o15db))  # sandbox win, net +940
    sess.add(_o15_row(o15db, symbol="SBIN", mode="live", pnl=300.0, charges_inr=50.0))
    sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/open15_vol_breakout")

    perf = resp.get_json()["data"]["performance"]
    assert perf["sandbox"]["cum_net_pnl"] == 940.0
    assert perf["sandbox"]["closed_trades"] == 1
    assert perf["live"]["cum_net_pnl"] == 250.0  # 300 - 50
    assert perf["live"]["closed_trades"] == 1
    assert perf["live"]["win_rate_pct"] == 100.0


def test_open15_observe_rows_excluded(app, wired_dbs):
    """observe-mode rows are journal-only dry runs (no orders placed) — they
    must not inflate either column or the card stats."""
    _eng, sess, o15db = wired_dbs["o15"]
    sess.add(_o15_row(o15db, mode="observe"))
    sess.add(_o15_row(o15db, symbol="INFY", mode="observe", status="open", pnl=None))
    sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/open15_vol_breakout")

    perf = resp.get_json()["data"]["performance"]
    sb = perf["sandbox"]
    assert sb["closed_trades"] == 0
    assert sb["cum_net_pnl"] is None  # UI renders '—', not a misleading ₹0
    assert sb["open_positions"] == 0
    assert perf["live"] is None


def test_open15_unpriced_exit_surfaced_not_summed(app, wired_dbs):
    """A closed-today row with pnl=None (exit tick unavailable at flatten) is
    counted as an unpriced exit, not silently folded into today's ₹ figure."""
    _eng, sess, o15db = wired_dbs["o15"]
    sess.add(_o15_row(o15db))
    sess.add(_o15_row(o15db, symbol="TCS", pnl=None, charges_inr=None))
    sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/open15_vol_breakout")

    sb = resp.get_json()["data"]["performance"]["sandbox"]
    assert sb["today_net_pnl"] == 940.0
    assert sb["today_unpriced_exits"] == 1


def test_open15_list_card_carries_today_stats(app, wired_dbs):
    """The /api/list summary card shows today's trade count / net P&L / open
    positions for open15 (previously all defaults — issue #442)."""
    _eng, sess, o15db = wired_dbs["o15"]
    sess.add(_o15_row(o15db))
    sess.add(_o15_row(o15db, symbol="INFY", status="open", pnl=None, charges_inr=None))
    sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")

    card = next(s for s in resp.get_json()["data"] if s["name"] == "open15_vol_breakout")
    assert card["today_trade_count"] == 2
    assert card["today_net_pnl"] == 940.0
    assert card["open_positions"] == 1
    assert card["last_trade_at"] is not None


def test_open15_sandbox_long_short_split(app, wired_dbs):
    """Sandbox column carries long/short sub-aggregates keyed off the journal's
    side column, for both the stock leg and the option shadow (issue #458)."""
    _eng, sess, o15db = wired_dbs["o15"]
    # Longs: one win (+940 net), one loss (-540 net). Short: one win (+260 net).
    sess.add(_o15_row(o15db, opt_pnl=500.0))
    sess.add(_o15_row(o15db, symbol="TCS", pnl=-500.0, charges_inr=40.0, opt_pnl=-200.0))
    sess.add(_o15_row(o15db, symbol="SBIN", side="S", pnl=300.0, charges_inr=40.0))
    sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/open15_vol_breakout")

    sb = resp.get_json()["data"]["performance"]["sandbox"]
    assert sb["long"] == {
        "n_trades": 2,
        "wins": 1,
        "win_rate_pct": 50.0,
        "net_pnl_inr": 400.0,  # 940 - 540
    }
    assert sb["short"] == {
        "n_trades": 1,
        "wins": 1,
        "win_rate_pct": 100.0,
        "net_pnl_inr": 260.0,
    }
    # Option shadow split: only the two long rows carry opt_pnl.
    opt = sb["options"]
    assert opt["long"] == {
        "n_trades": 2,
        "wins": 1,
        "win_rate_pct": 50.0,
        "net_pnl_inr": 300.0,  # 500 - 200
    }
    assert opt["short"]["n_trades"] == 0
    assert opt["short"]["win_rate_pct"] is None


def test_open15_side_split_empty_side_renders_none(app, wired_dbs):
    """A side with no closed trades reports n_trades=0 and None stats — the UI
    must render '—', never 0% (issue #458)."""
    _eng, sess, o15db = wired_dbs["o15"]
    sess.add(_o15_row(o15db))  # single long win
    sess.commit()

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/open15_vol_breakout")

    sb = resp.get_json()["data"]["performance"]["sandbox"]
    assert sb["long"]["n_trades"] == 1
    assert sb["short"] == {
        "n_trades": 0,
        "wins": 0,
        "win_rate_pct": None,
        "net_pnl_inr": None,
    }


def test_open15_backtest_side_split_passthrough(app, wired_dbs, strategies_dir):
    """parity_target.long/.short (and options_variant.long/.short) pass through
    to the Backtest column verbatim (issue #458)."""
    import json as _json

    (strategies_dir / "open15_vol_breakout" / "config_snapshot.json").write_text(
        _json.dumps(
            {
                "version": "0.1.0",
                "mode": "sandbox",
                "deployable": True,
                "parity_target": {
                    "window": "Jul 2026 R59",
                    "n_trades": 15,
                    "win_rate_pct": 60.0,
                    "net_pnl_inr": 2564,
                    "long": {"n_trades": 8, "wins": 6, "win_rate_pct": 75.0, "net_pnl_inr": 2707},
                    "short": {"n_trades": 7, "wins": 3, "win_rate_pct": 42.9, "net_pnl_inr": -143},
                    "options_variant": {
                        "n_trades": 13,
                        "win_rate_pct": 62.0,
                        "net_pnl_inr": 11195,
                        "long": {
                            "n_trades": 8,
                            "wins": 7,
                            "win_rate_pct": 87.5,
                            "net_pnl_inr": 13680,
                        },
                        "short": {
                            "n_trades": 5,
                            "wins": 1,
                            "win_rate_pct": 20.0,
                            "net_pnl_inr": -2485,
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/open15_vol_breakout")

    bt = resp.get_json()["data"]["performance"]["backtest"]
    assert bt["long"]["win_rate_pct"] == 75.0
    assert bt["short"]["net_pnl_inr"] == -143
    assert bt["options"]["long"]["win_rate_pct"] == 87.5
    assert bt["options"]["short"]["n_trades"] == 5
    # Long + short must reconcile with the aggregate.
    assert bt["long"]["net_pnl_inr"] + bt["short"]["net_pnl_inr"] == bt["net_pnl_inr"]


def test_other_strategies_have_no_side_split(app, wired_dbs):
    """Strategies without a long/short split carry no long/short keys — the UI
    renders no sub-rows for them (issue #458)."""
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/futures_follow_cap50")

    perf = resp.get_json()["data"]["performance"]
    assert "long" not in (perf["sandbox"] or {})
    assert perf["backtest"].get("long") is None


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
# simplified_engine ↔ trending_equity_intraday journal-name bridge (issue #235)
# ---------------------------------------------------------------------------


def _seed_simplified_journal_row(
    tj_sess,
    tjdb,
    *,
    symbol: str,
    direction: str = "LONG",
    quantity: int = 10,
    entry_price: float | None = 100.0,
    placed_at: str,
    exited_at: str | None = None,
    exit_price: float | None = None,
    pnl: float | None = None,
    exit_reason: str | None = None,
    signal_decision_id: int | None = None,
):
    """Insert one trade_journal row under the simplified engine's REGISTERED
    journal name (trending_equity_intraday) — the real persisted name."""
    row = tjdb.TradeJournal(
        placed_at=placed_at,
        symbol=symbol,
        direction=direction,
        quantity=quantity,
        strategy_name="trending_equity_intraday",
        signal_source="chartink",
        entry_price=entry_price,
        exited_at=exited_at,
        exit_price=exit_price,
        exit_reason=exit_reason,
        pnl=pnl,
        signal_decision_id=signal_decision_id,
        created_at=placed_at,
        updated_at=placed_at,
    )
    tj_sess.add(row)
    tj_sess.commit()
    return row


def test_trending_equity_intraday_hidden_from_list(app):
    """The journal-name twin folder must NOT appear as its own list row."""
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")

    names = {s["name"] for s in resp.get_json()["data"]}
    assert "simplified_engine" in names
    assert "trending_equity_intraday" not in names


def test_list_resolves_simplified_engine_journal(app, wired_dbs):
    """A trade_journal row tagged 'trending_equity_intraday' must surface under
    the 'simplified_engine' list entry (the folder↔journal name mismatch fix).
    """
    _tj_eng, tj_sess, tjdb = wired_dbs["tj"]
    today = dt.datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    # One open entry today + one closed-today with realized P&L.
    _seed_simplified_journal_row(tj_sess, tjdb, symbol="RVNL", placed_at=f"{today}T09:30:00+05:30")
    _seed_simplified_journal_row(
        tj_sess,
        tjdb,
        symbol="INDIANB",
        placed_at=f"{today}T09:45:00+05:30",
        exited_at=f"{today}T15:14:00+05:30",
        exit_price=110.0,
        pnl=1244.5,
        exit_reason="eod_squareoff",
    )

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")

    body = resp.get_json()
    se = next(s for s in body["data"] if s["name"] == "simplified_engine")
    assert se["today_trade_count"] == 2
    assert se["open_positions"] == 1  # only the RVNL entry is still open
    assert se["today_net_pnl"] == 1244.5
    # last_trade_at is normalized to naive-UTC for the frontend's `+ 'Z'` parse
    # (issue #317): 09:45 IST → 04:15 UTC, no offset suffix.
    assert se["last_trade_at"] == f"{today}T04:15:00"


def test_simplified_last_trade_at_is_naive_utc_for_frontend(app, wired_dbs):
    """The card renders `new Date(last_trade_at + 'Z')`, so last_trade_at must be
    naive-UTC (no offset). The simplified engine's placed_at is tz-aware IST
    (…+05:30); it must be normalized or the UI shows "Invalid Date" (issue #317).
    """
    import datetime as _dt

    _tj_eng, tj_sess, tjdb = wired_dbs["tj"]
    today = dt.datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    # placed_at carries a +05:30 offset, exactly like production rows.
    _seed_simplified_journal_row(
        tj_sess, tjdb, symbol="RVNL", placed_at=f"{today}T10:59:06.567689+05:30"
    )

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")

    se = next(s for s in resp.get_json()["data"] if s["name"] == "simplified_engine")
    lta = se["last_trade_at"]
    # No timezone suffix — appending 'Z' must yield a valid UTC instant.
    assert "+" not in lta and not lta.endswith("Z")
    parsed = _dt.datetime.fromisoformat(lta + "+00:00")  # frontend's `+ 'Z'`, in Python
    # 10:59 IST == 05:29 UTC.
    assert parsed.hour == 5 and parsed.minute == 29


def test_detail_resolves_simplified_engine_recent_trades(app, wired_dbs):
    """strategy_detail must return simplified_engine recent_trades from the
    trending_equity_intraday journal rows."""
    _tj_eng, tj_sess, tjdb = wired_dbs["tj"]
    today = dt.datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    _seed_simplified_journal_row(
        tj_sess,
        tjdb,
        symbol="PERSISTENT",
        placed_at=f"{today}T10:00:00+05:30",
        exited_at=f"{today}T15:14:00+05:30",
        exit_price=120.0,
        pnl=200.0,
        exit_reason="target",
    )

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/simplified_engine")

    data = resp.get_json()["data"]
    assert data["name"] == "simplified_engine"
    # sandbox performance column is populated from the journal
    assert data["performance"]["sandbox"]["today_net_pnl"] == 200.0
    trades = data["recent_trades"]
    assert len(trades) == 1
    assert trades[0]["symbol"] == "PERSISTENT"
    assert trades[0]["pnl"] == 200.0
    assert trades[0]["exit_reason"] == "target"


def test_pnl_curve_resolves_simplified_engine(app, wired_dbs):
    """The P&L curve for simplified_engine must aggregate closed journal rows by
    exit date."""
    _tj_eng, tj_sess, tjdb = wired_dbs["tj"]
    _seed_simplified_journal_row(
        tj_sess,
        tjdb,
        symbol="RVNL",
        placed_at="2026-06-29T09:30:00+05:30",
        exited_at="2026-06-29T15:14:00+05:30",
        exit_price=110.0,
        pnl=500.0,
        exit_reason="eod_squareoff",
    )
    _seed_simplified_journal_row(
        tj_sess,
        tjdb,
        symbol="INDIANB",
        placed_at="2026-06-29T09:45:00+05:30",
        exited_at="2026-06-29T15:14:00+05:30",
        exit_price=90.0,
        pnl=-150.0,
        exit_reason="stop_loss",
    )

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/simplified_engine/pnl-curve")

    points = resp.get_json()["data"]["points"]
    assert len(points) == 1
    assert points[0]["date"] == "2026-06-29"
    assert points[0]["pnl"] == 350.0  # 500 + (-150)


def test_other_strategies_unaffected_by_simplified_bridge(app, wired_dbs):
    """A trending_equity_intraday journal row must NOT leak into sector_follow
    or futures_follow stats (no regression on the existing branches)."""
    _tj_eng, tj_sess, tjdb = wired_dbs["tj"]
    today = dt.datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    _seed_simplified_journal_row(tj_sess, tjdb, symbol="RVNL", placed_at=f"{today}T09:30:00+05:30")

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")

    body = resp.get_json()["data"]
    sf = next(s for s in body if s["name"] == "sector_follow_cap5_vol")
    ff = next(s for s in body if s["name"] == "futures_follow_cap50")
    assert sf["today_trade_count"] == 0
    assert ff["today_trade_count"] == 0


# ---------------------------------------------------------------------------
# LLM mode toggle + decisions history (issue #266 Phase 2)
# ---------------------------------------------------------------------------


@pytest.fixture
def wired_llm_dbs(monkeypatch):
    """Rebind strategy_llm_config_db + signal_decision_db to in-memory engines."""
    from database import signal_decision_db as sddb
    from database import strategy_llm_config_db as llmdb

    llm_eng, llm_sess = _mk_engine()
    sd_eng, sd_sess = _mk_engine()
    llmdb.Base.metadata.create_all(llm_eng)
    sddb.Base.metadata.create_all(sd_eng)

    monkeypatch.setattr(llmdb, "engine", llm_eng)
    monkeypatch.setattr(llmdb, "db_session", llm_sess)
    monkeypatch.setattr(sddb, "engine", sd_eng)
    monkeypatch.setattr(sddb, "db_session", sd_sess)
    monkeypatch.setattr(sddb, "_tables_ensured_for_engine", None)

    yield {"llm": (llm_eng, llm_sess, llmdb), "sd": (sd_eng, sd_sess, sddb)}

    for sess in (llm_sess, sd_sess):
        sess.remove()
    for eng in (llm_eng, sd_eng):
        eng.dispose()


def _seed_decision(sddb, symbol, source, decision):
    sddb.insert_signal_decision(
        symbol=symbol,
        source=source,
        decision=decision,
        reasoning="because",
        confidence=0.6,
        enforcement_mode="shadow",
        context_snapshot=None,
        bridge_latency_ms=12,
        bridge_session_id="sess",
        raw_bridge_output=None,
    )


def test_detail_includes_llm_fields(app, wired_llm_dbs):
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/simplified_engine")
    body = resp.get_json()["data"]
    assert body["llm_mode"] == "off"  # default, no row
    assert body["llm_veto_enabled"] is True


def test_list_includes_llm_fields(app, wired_llm_dbs):
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/list")
    body = resp.get_json()["data"]
    se = next(s for s in body if s["name"] == "simplified_engine")
    assert se["llm_veto_enabled"] is True
    sf = next(s for s in body if s["name"] == "sector_follow_cap5_vol")
    assert sf["llm_veto_enabled"] is False


def test_post_llm_mode_accepts_veto(app, wired_llm_dbs, monkeypatch):
    from services import strategy_llm_config_service as svc

    monkeypatch.setattr(svc, "_telegram_notify", lambda *a, **k: None)
    monkeypatch.setattr(svc._default_bus, "publish", lambda ev: None)

    with app.test_client() as client:
        _login(client)
        resp = client.post("/strategies/api/simplified_engine/llm-mode", json={"llm_mode": "veto"})
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["accepted"] is True
    assert body["new_llm_mode"] == "veto"
    # Row persisted → subsequent GET reflects it.
    with app.test_client() as client:
        _login(client)
        detail = client.get("/strategies/api/simplified_engine").get_json()["data"]
    assert detail["llm_mode"] == "veto"


def test_post_llm_mode_rejects_bad_value(app, wired_llm_dbs):
    with app.test_client() as client:
        _login(client)
        resp = client.post(
            "/strategies/api/simplified_engine/llm-mode", json={"llm_mode": "shadow"}
        )
    assert resp.status_code == 400


def test_llm_decisions_returns_rows_for_veto_strategy(app, wired_llm_dbs):
    _sd_eng, _sd_sess, sddb = wired_llm_dbs["sd"]
    _seed_decision(sddb, "ASTRAL", "chartink_FnO_intraday_buy", "take")
    _seed_decision(sddb, "FORTIS", "trend-up", "skip")
    _seed_decision(sddb, "RBLBANK", "trend-up", "review_failed")

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/simplified_engine/llm-decisions")
    body = resp.get_json()["data"]
    assert body["veto_enabled"] is True
    assert body["total"] == 3
    assert len(body["rows"]) == 3
    assert body["summary"]["review_failed"] == 1
    assert body["summary"]["recent_review_failed"] >= 1


def test_llm_decisions_pagination(app, wired_llm_dbs):
    _sd_eng, _sd_sess, sddb = wired_llm_dbs["sd"]
    for i in range(5):
        _seed_decision(sddb, f"SYM{i}", "trend-up", "take")

    with app.test_client() as client:
        _login(client)
        p1 = client.get(
            "/strategies/api/simplified_engine/llm-decisions?limit=2&offset=0"
        ).get_json()["data"]
        p2 = client.get(
            "/strategies/api/simplified_engine/llm-decisions?limit=2&offset=2"
        ).get_json()["data"]
    assert p1["total"] == 5
    assert len(p1["rows"]) == 2 and len(p2["rows"]) == 2
    assert {r["id"] for r in p1["rows"]}.isdisjoint({r["id"] for r in p2["rows"]})


def test_detail_includes_llm_fields_futures_follow(app, wired_llm_dbs):
    """#318: the futures_follow card's LLM Control panel enables (veto wired)."""
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/futures_follow_cap50")
    body = resp.get_json()["data"]
    assert body["llm_veto_enabled"] is True


def test_llm_decisions_futures_follow_filters_to_own_source(app, wired_llm_dbs):
    """#318: the futures view is a clean inclusion filter on its own source."""
    _sd_eng, _sd_sess, sddb = wired_llm_dbs["sd"]
    _seed_decision(sddb, "ASTRAL", "chartink_FnO_intraday_buy", "take")
    _seed_decision(sddb, "RELIANCE", "futures_follow_cap50", "skip")
    _seed_decision(sddb, "FORTIS", "trend-up", "take")

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/futures_follow_cap50/llm-decisions")
    body = resp.get_json()["data"]
    assert body["veto_enabled"] is True
    assert body["total"] == 1
    assert len(body["rows"]) == 1
    assert body["rows"][0]["source"] == "futures_follow_cap50"
    assert body["rows"][0]["symbol"] == "RELIANCE"
    assert body["summary"]["skip"] == 1
    assert body["source_filtered"] is True


def test_llm_decisions_simplified_excludes_futures_rows(app, wired_llm_dbs):
    """R1 (#318): futures_follow rows must NOT leak into the simplified
    engine's all-sources decisions view."""
    _sd_eng, _sd_sess, sddb = wired_llm_dbs["sd"]
    _seed_decision(sddb, "ASTRAL", "chartink_FnO_intraday_buy", "take")
    _seed_decision(sddb, "RELIANCE", "futures_follow_cap50", "skip")
    _seed_decision(sddb, "FORTIS", "trend-up", "take")

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/simplified_engine/llm-decisions")
    body = resp.get_json()["data"]
    assert body["total"] == 2
    assert all(r["source"] != "futures_follow_cap50" for r in body["rows"])
    assert body["summary"]["total"] == 2
    assert body["summary"]["skip"] == 0  # the skip row was the futures one
    # Simplified is an exclusion view, not a clean per-strategy source filter.
    assert body["source_filtered"] is False


def test_llm_decisions_empty_for_non_veto_strategy(app, wired_llm_dbs):
    _sd_eng, _sd_sess, sddb = wired_llm_dbs["sd"]
    _seed_decision(sddb, "ASTRAL", "chartink_FnO_intraday_buy", "take")

    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/sector_follow_cap5_vol/llm-decisions")
    body = resp.get_json()["data"]
    assert body["veto_enabled"] is False
    assert body["rows"] == []
    assert body["total"] == 0
    assert body["summary"] is None


# --------------------------------------------------------------------------- #
# _data_health_summary — dashboard data-freshness tile (issue #237 Part 3)
# --------------------------------------------------------------------------- #


def test_data_health_summary_ok(monkeypatch):
    import blueprints.strategies_dashboard_api as sda

    monkeypatch.setattr(
        "database.data_health_db.get_latest_check",
        lambda feed: {
            "check_at": "2026-07-02T11:00:00",
            "overall_ok": True,
            "stale_symbols": [],
        },
    )
    out = sda._data_health_summary("sector_follow_cap5_vol")
    assert out["available"] is True
    assert out["overall_ok"] is True
    assert out["feed"] == "sector_follow_cap5_vol"
    assert out["shared"] is False
    assert out["stale_count"] == 0


def test_data_health_summary_stale(monkeypatch):
    import blueprints.strategies_dashboard_api as sda

    monkeypatch.setattr(
        "database.data_health_db.get_latest_check",
        lambda feed: {
            "check_at": "2026-07-02T16:30:00",
            "overall_ok": False,
            "stale_symbols": ["NIFTYAUTO", "NIFTYIT"],
        },
    )
    out = sda._data_health_summary("sector_follow_cap5_vol")
    assert out["available"] is True
    assert out["overall_ok"] is False
    assert out["stale_count"] == 2
    assert out["stale_symbols"] == ["NIFTYAUTO", "NIFTYIT"]


def test_data_health_summary_futures_is_shared_feed(monkeypatch):
    """futures_follow reuses the sector_follow feed → shared=True, feed relabelled."""
    import blueprints.strategies_dashboard_api as sda

    monkeypatch.setattr(
        "database.data_health_db.get_latest_check",
        lambda feed: {"check_at": "x", "overall_ok": True, "stale_symbols": []},
    )
    out = sda._data_health_summary("futures_follow_cap50")
    assert out["available"] is True
    assert out["feed"] == "sector_follow_cap5_vol"
    assert out["shared"] is True


def test_data_health_summary_no_feed_check_for_simplified():
    import blueprints.strategies_dashboard_api as sda

    out = sda._data_health_summary("simplified_engine")
    assert out["available"] is False
    assert out["reason"] == "no_feed_check"


def test_data_health_summary_no_row_yet(monkeypatch):
    import blueprints.strategies_dashboard_api as sda

    monkeypatch.setattr("database.data_health_db.get_latest_check", lambda feed: None)
    out = sda._data_health_summary("sector_follow_cap5_vol")
    assert out["available"] is False
    assert out["reason"] == "no_check_yet"


def test_data_health_summary_read_error_is_swallowed(monkeypatch):
    import blueprints.strategies_dashboard_api as sda

    def boom(feed):
        raise RuntimeError("db down")

    monkeypatch.setattr("database.data_health_db.get_latest_check", boom)
    out = sda._data_health_summary("sector_follow_cap5_vol")
    assert out["available"] is False
    assert out["reason"] == "read_error"


# ---------------------------------------------------------------------------
# GET /strategies/api/llm/health (issue #297)
# ---------------------------------------------------------------------------


def test_llm_health_requires_session(app):
    with app.test_client() as client:
        resp = client.get("/strategies/api/llm/health")
    assert resp.status_code in (301, 302, 401)


def test_llm_health_reachable(app, monkeypatch):
    """A clean probe reply surfaces reachable=true + latency + checked_at."""
    monkeypatch.setattr(
        "services.llm_review_client.probe_claude_health",
        lambda timeout_s: {
            "reachable": True,
            "latency_ms": 1234,
            "reason": "ok",
            "detail": "OK",
        },
    )
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/llm/health")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["reachable"] is True
    assert data["reason"] == "ok"
    assert data["latency_ms"] == 1234
    assert data["checked_at"]  # server-stamped IST timestamp


def test_llm_health_not_logged_in(app, monkeypatch):
    """An auth failure classifies as not_logged_in and is a 200 (not a 5xx)."""
    monkeypatch.setattr(
        "services.llm_review_client.probe_claude_health",
        lambda timeout_s: {
            "reachable": False,
            "latency_ms": 40,
            "reason": "not_logged_in",
            "detail": "claude review exited 1: not logged in",
        },
    )
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/llm/health")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["reachable"] is False
    assert data["reason"] == "not_logged_in"


def test_llm_health_probe_exception_is_swallowed(app, monkeypatch):
    """If the probe itself raises, the endpoint still returns a clean error view."""

    def boom(timeout_s):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr("services.llm_review_client.probe_claude_health", boom)
    with app.test_client() as client:
        _login(client)
        resp = client.get("/strategies/api/llm/health")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["reachable"] is False
    assert data["reason"] == "error"


def test_llm_health_probe_timeout_clamped(monkeypatch):
    import blueprints.strategies_dashboard_api as sda

    monkeypatch.setenv("LLM_HEALTH_PROBE_TIMEOUT_SECONDS", "999")
    assert sda._llm_health_probe_timeout() == 60.0
    monkeypatch.setenv("LLM_HEALTH_PROBE_TIMEOUT_SECONDS", "0.1")
    assert sda._llm_health_probe_timeout() == 3.0
    monkeypatch.setenv("LLM_HEALTH_PROBE_TIMEOUT_SECONDS", "not-a-number")
    assert sda._llm_health_probe_timeout() == 12.0


# ---------------------------------------------------------------------------
# Merged trades + LLM decisions (issue #358)
# ---------------------------------------------------------------------------


def _seed_decision_full(
    sddb,
    symbol,
    source,
    decision,
    *,
    enforcement_mode="shadow",
    direction="BUY",
    reasoning="regime fine",
):
    """Seed one signal_decision row and return its id."""
    return sddb.insert_signal_decision(
        symbol=symbol,
        source=source,
        decision=decision,
        direction=direction,
        reasoning=reasoning,
        confidence=0.8,
        enforcement_mode=enforcement_mode,
        context_snapshot=None,
        bridge_latency_ms=10,
        bridge_session_id="s",
        raw_bridge_output=None,
    )


def test_futures_trades_carry_llm_decision_via_fk(app, wired_dbs, wired_llm_dbs):
    """A futures_follow BUY row with a decision_id FK embeds the veto verdict;
    the SELL exit leg carries no LLM fields (exits are never reviewed)."""
    _sd_eng, _sd_sess, sddb = wired_llm_dbs["sd"]
    _ff_eng, ff_sess, ffdb = wired_dbs["ff"]
    did = _seed_decision_full(sddb, "TCS", "futures_follow_cap50", "take")

    ff_sess.add_all(
        [
            ffdb.FuturesFollowTrade(
                mode="sandbox",
                side="BUY",
                nifty_symbol="NIFTY28JUL26FUT",
                lots=1,
                quantity=75,
                entry_price=24500.0,
                entry_date="2026-07-06",
                signal_id="TCS",
                decision_id=did,
                status="placed",
                created_at=dt.datetime(2026, 7, 6, 9, 50, 0),
            ),
            ffdb.FuturesFollowTrade(
                mode="sandbox",
                side="SELL",
                nifty_symbol="NIFTY28JUL26FUT",
                lots=1,
                quantity=75,
                entry_price=24500.0,
                exit_price=24600.0,
                entry_date="2026-07-06",
                net_pnl=7000.0,
                status="placed",
                created_at=dt.datetime(2026, 7, 7, 9, 55, 0),
            ),
        ]
    )
    ff_sess.commit()

    with app.test_client() as client:
        _login(client)
        data = client.get("/strategies/api/futures_follow_cap50").get_json()["data"]

    trades = data["recent_trades"]
    sell, buy = trades[0], trades[1]
    assert sell["llm"] is None
    assert buy["llm"]["decision"] == "take"
    assert buy["llm"]["decision_id"] == did
    assert buy["llm"]["confidence"] == 0.8
    # The matched decision must NOT re-surface as an unmatched pseudo-row.
    assert data["llm_unmatched_skips"] == []


def test_futures_trades_fuzzy_match_pre_fk_rows(app, wired_dbs, wired_llm_dbs):
    """A pre-FK BUY row (decision_id NULL) still gets its verdict via the
    symbol+day fuzzy fallback (decision.symbol == row.signal_id)."""
    _sd_eng, _sd_sess, sddb = wired_llm_dbs["sd"]
    _ff_eng, ff_sess, ffdb = wired_dbs["ff"]
    did = _seed_decision_full(sddb, "INFY", "futures_follow_cap50", "take")
    day = sddb.get_signal_decision(did)["candidate_at"][:10]

    ff_sess.add(
        ffdb.FuturesFollowTrade(
            mode="sandbox",
            side="BUY",
            nifty_symbol="NIFTY28JUL26FUT",
            lots=1,
            quantity=75,
            entry_price=24500.0,
            entry_date=day,
            signal_id="INFY",
            decision_id=None,
            status="placed",
            created_at=dt.datetime(2026, 7, 6, 9, 50, 0),
        )
    )
    ff_sess.commit()

    with app.test_client() as client:
        _login(client)
        data = client.get("/strategies/api/futures_follow_cap50").get_json()["data"]

    assert data["recent_trades"][0]["llm"]["decision_id"] == did


def test_simplified_trades_carry_llm_decision_via_fk(app, wired_dbs, wired_llm_dbs):
    """A trade_journal row's existing signal_decision_id FK embeds the verdict,
    and the normalized aliases (net_pnl/created_at/mode/status) are present."""
    _sd_eng, _sd_sess, sddb = wired_llm_dbs["sd"]
    _tj_eng, tj_sess, tjdb = wired_dbs["tj"]
    did = _seed_decision_full(sddb, "PERSISTENT", "chartink_FnO_intraday_buy", "take")
    _seed_simplified_journal_row(
        tj_sess,
        tjdb,
        symbol="PERSISTENT",
        placed_at="2026-07-06T10:00:00+05:30",
        exited_at="2026-07-06T15:14:00+05:30",
        exit_price=120.0,
        pnl=200.0,
        exit_reason="target",
        signal_decision_id=did,
    )

    with app.test_client() as client:
        _login(client)
        data = client.get("/strategies/api/simplified_engine").get_json()["data"]

    row = data["recent_trades"][0]
    assert row["llm"]["decision_id"] == did
    assert row["llm"]["decision"] == "take"
    # Normalized aliases for the shared merged-table UI (#358).
    assert row["net_pnl"] == 200.0
    assert row["created_at"] == "2026-07-06T10:00:00+05:30"
    assert row["mode"] == "chartink"
    assert row["status"] == "closed"


def test_unmatched_active_skips_surface_as_pseudo_rows(app, wired_dbs, wired_llm_dbs):
    """An enforced (active) skip with no journal row surfaces in
    llm_unmatched_skips; a shadow skip (order still placed) does not."""
    _sd_eng, _sd_sess, sddb = wired_llm_dbs["sd"]
    active_id = _seed_decision_full(
        sddb,
        "ASTRAL",
        "trend-up",
        "skip",
        enforcement_mode="active",
        reasoning="regime hostile",
    )
    _seed_decision_full(sddb, "FORTIS", "trend-up", "skip", enforcement_mode="shadow")

    with app.test_client() as client:
        _login(client)
        data = client.get("/strategies/api/simplified_engine").get_json()["data"]

    skips = data["llm_unmatched_skips"]
    assert [s["decision_id"] for s in skips] == [active_id]
    assert skips[0]["symbol"] == "ASTRAL"
    assert skips[0]["decision"] == "skip"
    assert skips[0]["reasoning"] == "regime hostile"


def test_non_veto_strategy_has_no_llm_enrichment(app, wired_dbs, wired_llm_dbs):
    """sector_follow (no veto wired) returns no llm fields and no pseudo-rows."""
    _sf_eng, sf_sess, sfdb = wired_dbs["sf"]
    sf_sess.add(
        sfdb.SectorFollowTrade(
            mode="sandbox",
            side="BUY",
            symbol="TCS",
            quantity=10,
            price=4000.0,
            entry_date="2026-07-06",
            status="placed",
            created_at=dt.datetime(2026, 7, 6, 15, 20, 0),
        )
    )
    sf_sess.commit()

    with app.test_client() as client:
        _login(client)
        data = client.get("/strategies/api/sector_follow_cap5_vol").get_json()["data"]

    assert data["llm_unmatched_skips"] == []
    assert "llm" not in data["recent_trades"][0] or data["recent_trades"][0]["llm"] is None
