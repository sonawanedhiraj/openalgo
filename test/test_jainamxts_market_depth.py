"""Regression test for the F823 UnboundLocalError in JainamXTS ``get_market_depth``.

The bug: inside ``BrokerData.get_market_depth`` a later ``with db_session() as
session:`` block rebound ``session`` as a function-local, which shadowed the
module-level ``from flask import session`` import for the *whole* function. The
earlier flask-session reads (``hasattr(session, "marketdata_userid")`` etc.)
therefore referenced an unbound local — ``UnboundLocalError`` at runtime, F823
statically — whenever no instance ``user_id`` was set.

The fix renamed the DB context-manager binding to ``db_sess`` so the flask
``session`` import is used as intended on the lookup path.

These tests are hermetic: no DB, no broker network, no flask request context.
``session`` is monkeypatched to a plain dict so the lookup short-circuits and
the method returns ``None`` (its documented "no user ID available" failure path)
instead of crashing.
"""

from __future__ import annotations

import broker.jainamxts.api.data as jx_data
from broker.jainamxts.api.data import BrokerData


def test_get_market_depth_no_user_id_returns_none_without_unbound_local(monkeypatch):
    """The previously-crashing failure path now returns None cleanly.

    With no instance ``user_id`` and an empty session, the method must reach its
    "No user ID available" return — exercising the flask-session read at the line
    that used to raise UnboundLocalError — and return None rather than raise.
    """
    # Empty session → hasattr(session, "marketdata_userid") is False, so the
    # session-lookup branch is skipped deterministically (no request context).
    monkeypatch.setattr(jx_data, "session", {}, raising=False)

    bd = BrokerData(auth_token="dummy-auth", feed_token=None, user_id=None)

    # Must NOT raise UnboundLocalError (the F823 bug); returns None per the
    # "No user ID available for market depth request" guard.
    result = bd.get_market_depth("SBIN", "NSE")

    assert result is None


def test_get_market_depth_session_user_id_branch_is_reachable(monkeypatch):
    """The flask-session read itself works once the import is no longer shadowed.

    A session exposing ``marketdata_userid`` lets the lookup branch run; with no
    feed token available and a stubbed get_feed_token failure the method raises a
    plain Exception (not UnboundLocalError), proving the session read executed.
    """

    class _Sess:
        marketdata_userid = "U123"

        def get(self, key, default=None):
            return {"marketdata_userid": "U123"}.get(key, default)

    monkeypatch.setattr(jx_data, "session", _Sess(), raising=False)
    # No feed token anywhere → forces the get_feed_token path; stub it to fail
    # so we stop before any network call.
    monkeypatch.setattr(
        "database.auth_db.get_feed_token",
        lambda: (None, None, "no feed token"),
    )

    bd = BrokerData(auth_token="dummy-auth", feed_token=None, user_id=None)

    raised = None
    try:
        bd.get_market_depth("SBIN", "NSE")
    except Exception as exc:  # noqa: BLE001 — we only assert it's not UnboundLocalError
        raised = exc

    assert not isinstance(raised, UnboundLocalError)
