"""Replay endpoints + the single-run lock (issue #604).

The endpoints are the ONLY newly reachable path to the replay engine, so what
matters here is not that they work but that they cannot be talked into doing
something the engine's guards forbid: replaying a traded day, running during
market hours, or starting two ~250-call broker fetches at once.
"""

from __future__ import annotations

from datetime import datetime

import pytest
import pytz
from flask import Flask

import blueprints.open15_breakout as bp


@pytest.fixture
def app_client():
    """Minimal app carrying the blueprint, with a primed (valid) session.

    ``check_session_validity`` is DESTRUCTIVE on its failure path — it revokes
    broker tokens and clears the session — so the session is primed rather than
    the decorator patched out.
    """
    app = Flask(__name__)
    app.secret_key = "test"  # pragma: allowlist secret — throwaway test app
    app.register_blueprint(bp.open15_bp)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["logged_in"] = True
        sess["user"] = "testuser"
        sess["login_time"] = datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    return c


def test_start_refuses_an_ineligible_day(monkeypatch, app_client):
    """The button is a hint; THIS is the gate. A crafted POST must be refused."""
    monkeypatch.setattr(
        bp,
        "check_eligibility",
        lambda *a, **k: {
            "eligible": False,
            "reason": "day_was_traded",
            "detail": "has a real fill",
        },
        raising=False,
    )
    import services.open15_replay as R

    monkeypatch.setattr(
        R,
        "check_eligibility",
        lambda *a, **k: {
            "eligible": False,
            "reason": "day_was_traded",
            "detail": "has a real fill",
        },
    )

    resp = app_client.post("/open15_vol_breakout/api/replay", json={"date": "2026-08-14"})
    assert resp.status_code == 403
    assert resp.get_json()["reason"] == "day_was_traded"
    # and nothing was started
    assert not bp._REPLAY_LOCK.locked()


def test_start_refuses_during_market_hours(monkeypatch, app_client):
    import services.open15_replay as R

    monkeypatch.setattr(
        R, "check_eligibility", lambda *a, **k: {"eligible": False, "reason": "market_hours"}
    )
    resp = app_client.post("/open15_vol_breakout/api/replay", json={"date": "2026-08-12"})
    assert resp.status_code == 403
    assert resp.get_json()["reason"] == "market_hours"


def test_second_run_is_rejected_with_409(monkeypatch, app_client):
    """One ~250-call fetch at a time, process-wide — a double-click must not
    put two of them on the live strategy's broker quota."""
    import services.open15_replay as R

    monkeypatch.setattr(R, "check_eligibility", lambda *a, **k: {"eligible": True, "reason": "x"})
    assert bp._REPLAY_LOCK.acquire(blocking=False)
    try:
        resp = app_client.post("/open15_vol_breakout/api/replay", json={"date": "2026-08-12"})
        assert resp.status_code == 409
        assert resp.get_json()["reason"] == "busy"
    finally:
        bp._REPLAY_LOCK.release()


def test_date_is_required(app_client):
    assert app_client.post("/open15_vol_breakout/api/replay", json={}).status_code == 400
    assert app_client.get("/open15_vol_breakout/api/replay/eligibility").status_code == 400
    assert app_client.get("/open15_vol_breakout/api/replay/status").status_code == 400


def test_status_reports_idle_for_an_unknown_date(app_client):
    r = app_client.get("/open15_vol_breakout/api/replay/status?date=2020-01-01")
    assert r.get_json()["status"] == "idle"


def test_worker_releases_the_lock_on_failure(monkeypatch):
    """A crashed run must not wedge the lock and block every later replay."""
    import services.open15_replay as R

    monkeypatch.setattr(
        R, "replay_session", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert bp._REPLAY_LOCK.acquire(blocking=False)
    bp._replay_worker("2026-08-12", False)  # releases in its finally

    assert not bp._REPLAY_LOCK.locked()
    assert bp._replay_get("2026-08-12")["status"] == "failed"
    assert "boom" in bp._replay_get("2026-08-12")["error"]


def test_worker_reports_ineligible_as_a_reason_not_a_crash(monkeypatch):
    """The day can stop qualifying between the click and the write."""
    import services.open15_replay as R

    def refuse(*a, **k):
        raise R.ReplayIneligible("day_was_traded", "refusing to overwrite")

    monkeypatch.setattr(R, "replay_session", refuse)
    assert bp._REPLAY_LOCK.acquire(blocking=False)
    bp._replay_worker("2026-08-13", False)

    assert not bp._REPLAY_LOCK.locked()
    state = bp._replay_get("2026-08-13")
    assert state["status"] == "failed"
    assert "day_was_traded" in state["error"]


def test_every_page_post_sends_the_csrf_header():
    """CSRFProtect is global — a POST without the header is a silent 400 (#613).

    The replay button shipped with a hand-rolled POST that omitted
    ``X-CSRFToken``, so clicking it did nothing at all:

        WARNING in app: CSRF Error on /open15_vol_breakout/api/replay:
        400 Bad Request: The CSRF token is missing.

    Asserted over the page source rather than one call site, because the defect
    is "someone hand-rolled another POST", not "this POST is wrong".
    """
    import re

    from blueprints.open15_breakout import _LOGS_PAGE

    posts = [m.start() for m in re.finditer(r"method:\s*'POST'", _LOGS_PAGE)]
    assert posts, "no POST found — did the page change shape?"
    for at in posts:
        window = _LOGS_PAGE[at : at + 400]
        assert "X-CSRFToken" in window, f"a POST at offset {at} omits X-CSRFToken; use csrfToken()"


def test_csrf_helper_failure_does_not_throw():
    """A failed token fetch must degrade to an empty string, not an exception —
    otherwise the click handler dies before it can report anything."""
    from blueprints.open15_breakout import _LOGS_PAGE

    at = _LOGS_PAGE.index("async function csrfToken()")
    body = _LOGS_PAGE[at : _LOGS_PAGE.index("}", _LOGS_PAGE.index("catch", at))]
    assert "catch" in body and "return ''" in body
