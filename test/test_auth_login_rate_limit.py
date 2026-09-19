"""GET /auth/login must not share the brute-force rate limit with POST (#742).

``GET /auth/login`` (and the sibling ``/auth/broker`` and
``/auth/reset-password`` GET branches) only inspect Flask session state and
redirect — no credential is ever checked. Before this fix all three shared
their ``@limiter.limit(...)`` decorators with the POST branch on the same
view function that actually verifies a password / broker token / reset step.

``utils.session.check_session_validity`` redirects any non-AJAX request with
an invalid session to ``redirect(url_for("auth.login"))`` — a GET. Once the
daily 03:00 IST session-expiry boundary invalidates a cookie, a background
poll (a stale browser tab's ``setInterval``/plain ``fetch()`` against a
``check_session_validity``-protected route) keeps hitting that GET redirect,
and every hit consumed one unit of ``LOGIN_RATE_LIMIT_HOUR`` — exhausting the
25/hour budget meant for actual password attempts and permanently locking the
operator out of the login form (2026-09-19 incident: a stray ~60s poll
tripped the limit at 03:25 IST; because a new hit lands every minute the
60-minute moving window never dropped back below the threshold, so 429s
persisted for hours until the process was restarted).

Hermetic: bare Flask app + the real ``auth_bp`` and the real module-level
``limiter`` singleton (its decorators are bound to that instance at import
time, so a test-local ``Limiter()`` swapped in via monkeypatch would never be
consulted) with a hard ``limiter.reset()`` before and after every test so
counts never leak across tests or across a stale in-process singleton state.
The global ``test/conftest.py`` redirect points ``DATABASE_URL`` at a
throwaway temp DB before any ``database.*`` import binds its engine.
"""

from __future__ import annotations

import re

import pytest
from flask import Flask

import blueprints.auth as auth_module
import database.user_db as user_db
from limiter import limiter


def _per_period_count(rate_limit_str: str) -> int:
    """Parse the leading count out of a flask-limiter string like '5 per minute'."""
    match = re.match(r"\s*(\d+)\s+per\s+", rate_limit_str)
    assert match, f"unexpected rate limit format: {rate_limit_str!r}"
    return int(match.group(1))


@pytest.fixture
def client():
    user_db.init_db()

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test"  # pragma: allowlist secret
    limiter.init_app(app)
    app.register_blueprint(auth_module.auth_bp)

    limiter.reset()
    yield app.test_client()
    limiter.reset()

    try:
        user_db.db_session.query(user_db.User).delete()
        user_db.db_session.commit()
    finally:
        user_db.db_session.remove()


def test_get_login_never_hits_429_past_the_post_limit(client):
    """GET is a pure session-state redirect and must be rate-limit-exempt."""
    per_minute = _per_period_count(auth_module.LOGIN_RATE_LIMIT_MIN)

    statuses = [client.get("/auth/login").status_code for _ in range(per_minute + 10)]

    assert 429 not in statuses, (
        f"GET /auth/login hit 429 after {statuses.count(429)} of "
        f"{len(statuses)} requests — GET must be exempt from LOGIN_RATE_LIMIT"
    )


def test_post_login_is_still_rate_limited(client):
    """The fix must not accidentally remove POST's brute-force protection."""
    per_minute = _per_period_count(auth_module.LOGIN_RATE_LIMIT_MIN)

    creds = {"username": "nope", "password": "nope"}  # pragma: allowlist secret
    statuses = [client.post("/auth/login", data=creds).status_code for _ in range(per_minute + 5)]

    assert 429 in statuses, (
        "POST /auth/login was never rate-limited — the fix must only exempt "
        "GET, not remove POST's brute-force protection"
    )


def test_get_and_post_login_share_no_budget(client):
    """A burst of GETs must not eat into the POST budget the fix must protect."""
    per_minute = _per_period_count(auth_module.LOGIN_RATE_LIMIT_MIN)

    # Simulate the incident: many GET redirects in a row (a stale poll loop).
    for _ in range(per_minute * 3):
        client.get("/auth/login")

    # POST attempts up to the limit must still succeed (not be pre-consumed
    # by the GET burst above).
    creds = {"username": "nope", "password": "nope"}  # pragma: allowlist secret
    statuses = [client.post("/auth/login", data=creds).status_code for _ in range(per_minute)]

    assert 429 not in statuses, (
        "GET traffic consumed budget meant for POST — GET and POST must use "
        "independent counters once GET is exempted"
    )


def test_get_broker_and_reset_password_are_also_exempt(client):
    """The same GET-vs-POST split applies to /auth/broker and /auth/reset-password."""
    per_minute = _per_period_count(auth_module.LOGIN_RATE_LIMIT_MIN)

    broker_statuses = [client.get("/auth/broker").status_code for _ in range(per_minute + 5)]
    assert 429 not in broker_statuses

    reset_statuses = [client.get("/auth/reset-password").status_code for _ in range(per_minute + 5)]
    assert 429 not in reset_statuses
