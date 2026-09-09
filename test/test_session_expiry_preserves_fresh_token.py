"""Session expiry must not destroy a broker token that outlived the cookie (#719).

Every morning since the headless auto-login shipped (#654) the operator's
first browser visit with yesterday's cookie tripped ``check_session_validity``,
whose ``revoke_user_tokens()`` wiped the 40-second-old token the auto-login had
just written — a second Kite login per day plus a burst of "auth token revoked"
errors in between. The cookie's age was being read as the token's age; the
``auth.token_updated_at`` stamp is what now tells the two apart.

Hermetic: the global ``test/conftest.py`` redirect points every DB env var at a
throwaway temp DB; broker probes are monkeypatched.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytz
from flask import Flask, session
from sqlalchemy import text

import database.auth_db as auth_db
import utils.session as sess

IST = pytz.timezone("Asia/Kolkata")
UTC = pytz.utc


def _ist(y, m, d, hh, mm) -> datetime:
    return IST.localize(datetime(y, m, d, hh, mm))


def _naive_utc(dt_ist: datetime) -> datetime:
    """The repo's stored-timestamp contract: naive UTC."""
    return dt_ist.astimezone(UTC).replace(tzinfo=None)


NOW = _ist(2026, 9, 9, 8, 20)  # the incident: first browser visit at 08:20 IST


@pytest.fixture
def app():
    app = Flask(__name__)
    app.secret_key = "test"  # pragma: allowlist secret
    app.config["TESTING"] = True
    return app


@pytest.fixture(autouse=True)
def _fixed_env(monkeypatch):
    monkeypatch.setenv("SESSION_EXPIRY_TIME", "03:00")
    monkeypatch.setenv("AUTO_LOGIN_EARLIEST_TIME", "07:30")
    monkeypatch.delenv("DISABLE_SESSION_EXPIRY", raising=False)


# --------------------------------------------------------------------------- #
# classify_stored_token — pure given now_ist
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "stamp_ist, expected",
    [
        (None, "stale"),  # legacy row from before the column existed
        (_ist(2026, 9, 8, 22, 0), "stale"),  # yesterday's token
        (_ist(2026, 9, 9, 2, 59), "stale"),  # before the 03:00 boundary
        (_ist(2026, 9, 9, 8, 19), "trusted"),  # the incident's token: 08:19 boot auto-login
        (_ist(2026, 9, 9, 7, 30), "trusted"),  # exactly at the safe-after time
        (_ist(2026, 9, 9, 6, 0), "probe"),  # inside Kite's flush window
        (_ist(2026, 9, 9, 3, 0), "probe"),  # at the boundary, before the flush
    ],
)
def test_classify_stored_token(monkeypatch, stamp_ist, expected):
    stamp = _naive_utc(stamp_ist) if stamp_ist is not None else None
    monkeypatch.setattr(auth_db, "get_token_updated_at", lambda name: stamp)
    assert sess.classify_stored_token("dheeraj", now_ist=NOW) == expected


def test_classify_accepts_tz_aware_stamp(monkeypatch):
    monkeypatch.setattr(auth_db, "get_token_updated_at", lambda name: _ist(2026, 9, 9, 8, 19))
    assert sess.classify_stored_token("dheeraj", now_ist=NOW) == "trusted"


def test_classify_before_todays_boundary_uses_yesterdays_morning(monkeypatch):
    """At 02:00 the relevant boundary is YESTERDAY 03:00 (and its 07:30)."""
    monkeypatch.setattr(
        auth_db, "get_token_updated_at", lambda name: _naive_utc(_ist(2026, 9, 9, 8, 19))
    )
    assert sess.classify_stored_token("dheeraj", now_ist=_ist(2026, 9, 10, 2, 0)) == "trusted"


def test_classify_read_failure_is_stale(monkeypatch):
    def boom(name):
        raise RuntimeError("db locked")

    monkeypatch.setattr(auth_db, "get_token_updated_at", boom)
    assert sess.classify_stored_token("dheeraj", now_ist=NOW) == "stale"


# --------------------------------------------------------------------------- #
# revoke_user_tokens — what actually happens to the DB row
# --------------------------------------------------------------------------- #
class _Spy:
    def __init__(self):
        self.upserts = []
        self.invalidations = []
        self.published = []
        self.symbol_cache_cleared = 0


@pytest.fixture
def spy(monkeypatch):
    s = _Spy()
    monkeypatch.setattr(
        auth_db,
        "upsert_auth",
        lambda *a, **kw: s.upserts.append((a, kw)) or 1,
    )
    monkeypatch.setattr(auth_db, "invalidate_auth", lambda name: s.invalidations.append(name))
    import database.cache_invalidation as ci
    import database.master_contract_cache_hook as mc

    monkeypatch.setattr(ci, "publish_all_cache_invalidation", lambda u: s.published.append(u))
    monkeypatch.setattr(mc, "clear_cache_on_logout", lambda: setattr(s, "symbol_cache_cleared", 1))
    return s


def _stamp(monkeypatch, stamp_ist):
    stamp = _naive_utc(stamp_ist) if stamp_ist is not None else None
    monkeypatch.setattr(auth_db, "get_token_updated_at", lambda name: stamp)


def _now(monkeypatch, now_ist):
    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now_ist.astimezone(tz) if tz else now_ist.replace(tzinfo=None)

    monkeypatch.setattr(sess, "datetime", _FrozenDatetime)


def test_fresh_token_is_preserved_untouched(app, spy, monkeypatch):
    """The incident: auto-login wrote the token at 08:19, cookie expired at 08:20."""
    _stamp(monkeypatch, _ist(2026, 9, 9, 8, 19))
    _now(monkeypatch, NOW)
    with app.test_request_context("/"):
        session["user"] = "dheeraj"
        sess.revoke_user_tokens()
    assert spy.upserts == []
    assert spy.invalidations == []
    assert spy.published == []  # no ZMQ CACHE_INVALIDATE → no WS-proxy reconnect
    assert spy.symbol_cache_cleared == 0


def test_stale_token_is_revoked_as_before(app, spy, monkeypatch):
    _stamp(monkeypatch, _ist(2026, 9, 8, 9, 0))
    _now(monkeypatch, NOW)
    with app.test_request_context("/"):
        session["user"] = "dheeraj"
        sess.revoke_user_tokens()
    assert len(spy.upserts) == 1
    args, kwargs = spy.upserts[0]
    assert args == ("dheeraj", "", "") and kwargs == {"revoke": True}
    assert spy.published == ["dheeraj"]
    assert spy.symbol_cache_cleared == 1


def test_legacy_row_without_stamp_is_revoked(app, spy, monkeypatch):
    _stamp(monkeypatch, None)
    _now(monkeypatch, NOW)
    with app.test_request_context("/"):
        session["user"] = "dheeraj"
        sess.revoke_user_tokens()
    assert len(spy.upserts) == 1


def test_flush_window_token_alive_is_preserved(app, spy, monkeypatch):
    """Stamped 06:00 (inside the flush window) — only the broker knows; alive → keep."""
    import services.broker_session_health as health

    _stamp(monkeypatch, _ist(2026, 9, 9, 6, 0))
    _now(monkeypatch, NOW)
    probes = []
    monkeypatch.setattr(health, "is_live_session_for", lambda n: probes.append(n) or True)
    with app.test_request_context("/"):
        session["user"] = "dheeraj"
        sess.revoke_user_tokens()
    assert probes == ["dheeraj"]
    assert spy.upserts == [] and spy.invalidations == [] and spy.published == []


def test_flush_window_token_dead_is_invalidated(app, spy, monkeypatch):
    """Stamped 06:00, broker rejects it (flushed) → invalidate + legacy revoke."""
    import services.broker_session_health as health

    _stamp(monkeypatch, _ist(2026, 9, 9, 6, 0))
    _now(monkeypatch, NOW)
    monkeypatch.setattr(health, "is_live_session_for", lambda n: False)
    with app.test_request_context("/"):
        session["user"] = "dheeraj"
        sess.revoke_user_tokens()
    assert spy.invalidations == ["dheeraj"]
    assert len(spy.upserts) == 1


def test_revoke_db_tokens_false_still_skips_everything_for_fresh_token(app, spy, monkeypatch):
    """The app.py before_request variant must not publish an invalidation either."""
    _stamp(monkeypatch, _ist(2026, 9, 9, 8, 19))
    _now(monkeypatch, NOW)
    with app.test_request_context("/"):
        session["user"] = "dheeraj"
        sess.revoke_user_tokens(revoke_db_tokens=False)
    assert spy.published == [] and spy.symbol_cache_cleared == 0


def test_check_session_validity_clears_cookie_but_keeps_fresh_token(app, spy, monkeypatch):
    """End to end through the decorator: 302 to login, cookie gone, row untouched."""
    _stamp(monkeypatch, _ist(2026, 9, 9, 8, 19))
    _now(monkeypatch, NOW)

    @app.route("/capabilities")
    @sess.check_session_validity
    def capabilities():
        return "ok"

    @app.route("/auth/login")
    def login():  # url_for("auth.login") target
        return "login"

    app.view_functions["auth.login"] = login
    app.add_url_rule("/auth/login", endpoint="auth.login", view_func=login)

    client = app.test_client()
    with client.session_transaction() as s:
        s["user"] = "dheeraj"
        s["logged_in"] = True
        s["login_time"] = _ist(2026, 9, 8, 8, 0).isoformat()  # yesterday's cookie
    resp = client.get("/capabilities")
    assert resp.status_code == 302
    assert spy.upserts == [] and spy.published == []
    with client.session_transaction() as s:
        assert "user" not in s and "logged_in" not in s


# --------------------------------------------------------------------------- #
# The stamp itself: upsert_auth writes it, revoke clears it, migration adds it
# --------------------------------------------------------------------------- #
def test_upsert_auth_stamps_and_revoke_clears(monkeypatch):
    import database.cache_invalidation as ci

    monkeypatch.setattr(ci, "publish_all_cache_invalidation", lambda u: None)
    auth_db.init_db()
    name = "stamp-user-719"
    before = datetime.utcnow() - timedelta(seconds=5)
    try:
        auth_db.upsert_auth(name, "key:tok", "zerodha")
        stamp = auth_db.get_token_updated_at(name)
        assert stamp is not None and stamp >= before
        auth_db.upsert_auth(name, "", "", revoke=True)
        assert auth_db.get_token_updated_at(name) is None  # revoked → no stamp served
    finally:
        auth_db.Auth.query.filter_by(name=name).delete()
        auth_db.db_session.commit()
        auth_db.db_session.remove()


def test_migration_adds_column_to_pre_719_table():
    auth_db.init_db()
    with auth_db.engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE auth DROP COLUMN token_updated_at"))
            conn.commit()
        except Exception:
            pytest.skip("sqlite without DROP COLUMN support")
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(auth)")).fetchall()]
        assert "token_updated_at" not in cols
    auth_db._migrate_auth_columns()
    auth_db._migrate_auth_columns()  # idempotent
    with auth_db.engine.connect() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(auth)")).fetchall()]
    assert "token_updated_at" in cols
