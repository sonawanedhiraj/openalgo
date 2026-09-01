"""Dashboard shutdown control (issue #694).

The exit worker is stubbed in every test — nothing here may actually call
``os._exit``. The stub records invocations on an Event so the fire-and-forget
thread can be awaited without patching ``threading`` globally.
"""

from __future__ import annotations

import datetime as dt
import threading

import pytest
import pytz
from flask import Blueprint, Flask

import blueprints.system_control as sc
from blueprints.system_control import system_bp

IST = pytz.timezone("Asia/Kolkata")


def _login(client) -> None:
    now_ist = dt.datetime.now(IST)
    with client.session_transaction() as s:
        s["logged_in"] = True
        s["user"] = "test_user"
        s["login_time"] = now_ist.isoformat()


def _stub_auth_bp() -> Blueprint:
    bp = Blueprint("auth", __name__)

    @bp.route("/login")
    def login():  # pragma: no cover - target for url_for only
        return "login"

    return bp


@pytest.fixture
def app():
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    flask_app.config["SECRET_KEY"] = "test-secret-system"  # pragma: allowlist secret
    flask_app.register_blueprint(_stub_auth_bp())
    flask_app.register_blueprint(system_bp)
    return flask_app


@pytest.fixture
def exit_stub(monkeypatch):
    """Replace the exit worker; returns (event, calls) to await/inspect."""
    fired = threading.Event()
    calls: list[str] = []

    def fake_exit(requested_by):
        calls.append(requested_by)
        fired.set()

    monkeypatch.setattr(sc, "_graceful_exit", fake_exit)
    return fired, calls


# ---------------------------------------------------------------------------
# auth + status
# ---------------------------------------------------------------------------


def test_endpoints_require_session(app):
    with app.test_client() as client:
        assert client.get("/system/api/status").status_code in (301, 302, 401)
        assert client.post("/system/api/shutdown", json={}).status_code in (301, 302, 401)


def test_status_payload(app):
    with app.test_client() as client:
        _login(client)
        j = client.get("/system/api/status").get_json()
    assert j["status"] == "running"
    assert isinstance(j["pid"], int)
    assert j["uptime_s"] >= 0
    assert "branch" in j and "commit" in j
    assert isinstance(j["market_guard_active"], bool)


# ---------------------------------------------------------------------------
# shutdown guard rails
# ---------------------------------------------------------------------------


def test_wrong_confirm_refused_and_no_exit(app, exit_stub):
    fired, _calls = exit_stub
    with app.test_client() as client:
        _login(client)
        r = client.post("/system/api/shutdown", json={"confirm": "shutdown"})
    assert r.status_code == 400
    assert not fired.wait(0.3)


def test_market_hours_refused_without_override(app, exit_stub, monkeypatch):
    fired, _calls = exit_stub
    monkeypatch.setattr(sc, "_market_guard_active", lambda now=None: True)
    with app.test_client() as client:
        _login(client)
        r = client.post("/system/api/shutdown", json={"confirm": "SHUTDOWN"})
    assert r.status_code == 409
    j = r.get_json()
    assert j["reason"] == "market_hours"
    assert "open_position_hints" in j
    assert not fired.wait(0.3)


def test_market_hours_override_proceeds(app, exit_stub, monkeypatch):
    fired, calls = exit_stub
    monkeypatch.setattr(sc, "_market_guard_active", lambda now=None: True)
    with app.test_client() as client:
        _login(client)
        r = client.post(
            "/system/api/shutdown",
            json={"confirm": "SHUTDOWN", "override_market_hours": True},
        )
    assert r.status_code == 200
    assert r.get_json()["status"] == "shutting_down"
    assert fired.wait(2.0)
    assert calls == ["test_user"]


def test_off_hours_shutdown_proceeds(app, exit_stub, monkeypatch):
    fired, _calls = exit_stub
    monkeypatch.setattr(sc, "_market_guard_active", lambda now=None: False)
    with app.test_client() as client:
        _login(client)
        r = client.post("/system/api/shutdown", json={"confirm": "SHUTDOWN"})
    assert r.status_code == 200
    assert fired.wait(2.0)


# ---------------------------------------------------------------------------
# the guard clock
# ---------------------------------------------------------------------------


def _at(hhmm: str, date: str) -> dt.datetime:
    y, m, d = (int(x) for x in date.split("-"))
    h, mi = (int(x) for x in hhmm.split(":"))
    return IST.localize(dt.datetime(y, m, d, h, mi))


def test_guard_windows():
    monday, saturday = "2026-09-07", "2026-09-05"
    # weekday inside the window -> guarded (empty market calendar fails open to
    # the weekday rule inside is_trading_day)
    assert sc._market_guard_active(_at("10:00", monday)) is True
    assert sc._market_guard_active(_at("09:00", monday)) is True
    # outside the window -> free, both sides
    assert sc._market_guard_active(_at("08:59", monday)) is False
    assert sc._market_guard_active(_at("15:35", monday)) is False
    # weekend -> free even mid-window
    assert sc._market_guard_active(_at("10:00", saturday)) is False
