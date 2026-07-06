"""Unit tests for ``database.auth_db.invalidate_auth``.

The helper is called by the login-flow freshness probe when it discovers the
stored token is stale. It must:

- Set ``is_revoked=True`` on the existing row.
- Clear ``auth_cache``, ``feed_token_cache``, ``broker_cache``.
- Be a no-op (returns False) when no row exists — safe to repeat.
- Never raise on best-effort downstream failures (ZMQ publish, WS pool cleanup).
- Make ``get_auth_token`` return None for the user afterwards.
"""

from __future__ import annotations

from unittest.mock import patch

from database import auth_db


def _seed_auth(name: str = "tester", broker: str = "zerodha", token: str = "tok-1") -> None:
    auth_db.upsert_auth(name=name, auth_token=token, broker=broker, feed_token=None, user_id=name)


def test_invalidate_returns_false_when_row_missing():
    assert auth_db.invalidate_auth("nobody-here") is False


def test_invalidate_marks_row_revoked():
    _seed_auth("test_inv_1")
    assert auth_db.invalidate_auth("test_inv_1") is True

    row = auth_db.Auth.query.filter_by(name="test_inv_1").first()
    assert row is not None
    assert row.is_revoked is True


def test_get_auth_token_returns_none_after_invalidate():
    _seed_auth("test_inv_2", token="some-encrypted-token")
    assert auth_db.get_auth_token("test_inv_2") is not None

    auth_db.invalidate_auth("test_inv_2")

    # After invalidation, get_auth_token must NOT return the stale token —
    # this is the load-bearing contract that prevents background subsystems
    # from picking up yesterday's session.
    assert auth_db.get_auth_token("test_inv_2") is None


def test_invalidate_clears_caches():
    _seed_auth("test_inv_3")
    # Prime the caches.
    auth_db.get_auth_token("test_inv_3")

    # Confirm at least one cache populated.
    assert len(auth_db.auth_cache) >= 1 or len(auth_db.broker_cache) >= 1

    auth_db.invalidate_auth("test_inv_3")

    # All three caches cleared.
    assert len(auth_db.auth_cache) == 0
    assert len(auth_db.feed_token_cache) == 0
    assert len(auth_db.broker_cache) == 0


def test_invalidate_is_idempotent():
    _seed_auth("test_inv_4")
    assert auth_db.invalidate_auth("test_inv_4") is True
    # Second call sees the row already revoked but still returns True (it
    # exists) and re-clears the caches — safe to call repeatedly.
    assert auth_db.invalidate_auth("test_inv_4") is True


def test_invalidate_tolerates_zmq_failure():
    """A ZMQ publish failure must not raise — invalidation completes locally."""
    _seed_auth("test_inv_5")

    with patch(
        "database.cache_invalidation.publish_all_cache_invalidation",
        side_effect=RuntimeError("zmq dead"),
    ):
        # Must not raise. Should still mark revoked + clear caches.
        result = auth_db.invalidate_auth("test_inv_5")
        assert result is True

    row = auth_db.Auth.query.filter_by(name="test_inv_5").first()
    assert row.is_revoked is True


def test_invalidate_tolerates_pool_cleanup_failure():
    """A WS pool cleanup failure must not raise."""
    _seed_auth("test_inv_6")

    with patch(
        "websocket_proxy.broker_factory.cleanup_pools_for_user",
        side_effect=RuntimeError("pool dead"),
    ):
        result = auth_db.invalidate_auth("test_inv_6")
        assert result is True
