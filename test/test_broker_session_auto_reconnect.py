"""Mocked E2E tests for event-driven broker-session WS reinitialization.

When a Zerodha (or any broker) session is refreshed, ``upsert_auth()`` publishes a
ZMQ ``CACHE_INVALIDATE`` event. The WebSocket proxy's ``_handle_cache_invalidation``
consumes it and **unconditionally** (no feature flag) reconnects the live broker
adapter with the fresh token and re-subscribes the held symbol set — so the feed
resumes WITHOUT an OpenAlgo restart. The safety guarantee is carried by these
hermetic tests, not by a flag.

Coverage:
  - subscriptions preserved across the disconnect→reconnect cycle (+ resubscribe);
  - failure-graceful: a rejected token logs ``logger.exception``, does not crash
    the proxy, and does not lose the previous good session state;
  - idempotent: repeated session-refresh events do not pile up duplicate
    connections or duplicate subscriptions;
  - the Flask-side login completion emits the ``broker_session_refreshed``
    SocketIO UI notification.

All hermetic — no real Zerodha connection, no ZMQ socket, no WS server. The proxy
is built via ``WebSocketProxy.__new__`` to bypass ``__init__`` (which binds port
8765 / ZMQ).
"""

import json
from unittest.mock import MagicMock

from websocket_proxy.server import WebSocketProxy

USER_ID = "testuser"
TOPIC = f"CACHE_INVALIDATE_ALL_{USER_ID}"
MESSAGE = json.dumps({"action": "invalidate", "user_id": USER_ID, "cache_type": "ALL"})


class FakeAdapter:
    """Mock broker WS adapter recording its lifecycle calls.

    Mirrors the real adapter contract: ``disconnect()`` wipes ``subscribed_symbols``
    (as ``ZerodhaWebSocketAdapter.disconnect`` does) and ``subscribe()`` repopulates
    it (as the real adapter tracks subscriptions), so a correct reconnect MUST
    snapshot the set *before* disconnect.
    """

    def __init__(self):
        self.subscribed_symbols = {
            "NSE:RELIANCE": {"symbol": "RELIANCE", "exchange": "NSE", "token": 738561, "mode": 2},
            "NSE:INFY": {"symbol": "INFY", "exchange": "NSE", "token": 408065, "mode": 1},
        }
        self.calls = []

    def disconnect(self):
        self.calls.append(("disconnect",))
        self.subscribed_symbols.clear()  # real adapter wipes state on disconnect
        return {"status": "success"}

    def initialize(self, broker_name, user_id, auth_data=None):
        self.calls.append(("initialize", broker_name, user_id))
        return {"status": "success"}

    def connect(self):
        self.calls.append(("connect",))
        return {"status": "success"}

    def subscribe(self, symbol, exchange, mode=2, depth_level=5):
        self.calls.append(("subscribe", symbol, exchange, mode))
        # Real adapter repopulates its tracking dict on subscribe (keyed → de-dupes).
        self.subscribed_symbols[f"{exchange}:{symbol}"] = {
            "symbol": symbol,
            "exchange": exchange,
            "token": 0,
            "mode": mode,
        }
        return {"status": "success"}


def _make_proxy(adapter, broker="zerodha"):
    """Build a WebSocketProxy without running __init__ (no port bind / ZMQ)."""
    proxy = WebSocketProxy.__new__(WebSocketProxy)
    proxy.broker_adapters = {USER_ID: adapter}
    proxy.user_broker_mapping = {USER_ID: broker}
    proxy._last_known_subscriptions = {}
    return proxy


def test_reconnect_resubscribes_by_default():
    """No flag: a cache-invalidation event reconnects and re-subscribes the held
    symbol set, and the live adapter stays in the registry."""
    adapter = FakeAdapter()
    proxy = _make_proxy(adapter)

    proxy._handle_cache_invalidation(TOPIC, MESSAGE)

    names = [c[0] for c in adapter.calls]
    # Ordering is load-bearing: snapshot happens before disconnect wipes state.
    assert names == ["disconnect", "initialize", "connect", "subscribe", "subscribe"]
    assert ("initialize", "zerodha", USER_ID) in adapter.calls

    resubscribed = {(c[1], c[2], c[3]) for c in adapter.calls if c[0] == "subscribe"}
    assert resubscribed == {("RELIANCE", "NSE", 2), ("INFY", "NSE", 1)}

    # Live adapter preserved — the feed resumes without a client reconnect.
    assert proxy.broker_adapters[USER_ID] is adapter


def test_reconnect_failure_is_graceful_and_preserves_state(monkeypatch):
    """A rejected token logs logger.exception, does not crash the proxy, drops the
    dead adapter, and KEEPS the previous good subscription set (state not lost)."""
    mock_logger = MagicMock()
    monkeypatch.setattr("websocket_proxy.server.logger", mock_logger)

    class FailingAdapter(FakeAdapter):
        def connect(self):
            self.calls.append(("connect",))
            raise RuntimeError("zerodha WS rejected new token (401)")

    adapter = FailingAdapter()
    proxy = _make_proxy(adapter)

    # Must not raise — the proxy stays alive.
    proxy._handle_cache_invalidation(TOPIC, MESSAGE)

    # Silent-drop discipline: failure logged with full traceback via exception().
    assert mock_logger.exception.called

    # Dead adapter dropped so the next client auth rebuilds it cleanly.
    assert USER_ID not in proxy.broker_adapters

    # Previous good session state retained for the next attempt.
    assert set(proxy._last_known_subscriptions[USER_ID]) == {
        ("RELIANCE", "NSE", 2),
        ("INFY", "NSE", 1),
    }


def test_repeated_events_are_idempotent():
    """Repeated session-refresh events never pile up duplicate connections or
    duplicate subscriptions — disconnect always precedes each reconnect."""
    adapter = FakeAdapter()
    proxy = _make_proxy(adapter)

    for _ in range(3):
        proxy._handle_cache_invalidation(TOPIC, MESSAGE)

    # Exactly one adapter for the user — registry never grows.
    assert list(proxy.broker_adapters.keys()) == [USER_ID]
    assert proxy.broker_adapters[USER_ID] is adapter

    # No overlapping live connections: every connect is balanced by a disconnect.
    n_disconnect = sum(1 for c in adapter.calls if c[0] == "disconnect")
    n_connect = sum(1 for c in adapter.calls if c[0] == "connect")
    assert n_disconnect == n_connect == 3

    # Subscriptions de-duped by the keyed dict — still exactly the two symbols.
    assert set(adapter.subscribed_symbols.keys()) == {"NSE:RELIANCE", "NSE:INFY"}


def test_missing_broker_mapping_falls_back_to_drop():
    """No broker known for the user: cannot re-initialize, so fall back to
    disconnect-and-drop rather than crashing the listener."""
    adapter = FakeAdapter()
    proxy = _make_proxy(adapter)
    proxy.user_broker_mapping = {}  # broker unknown

    proxy._handle_cache_invalidation(TOPIC, MESSAGE)

    assert [c[0] for c in adapter.calls] == ["disconnect"]
    assert USER_ID not in proxy.broker_adapters


def test_no_adapter_is_noop():
    """No live adapter for the user: handler is a clean no-op."""
    proxy = WebSocketProxy.__new__(WebSocketProxy)
    proxy.broker_adapters = {}
    proxy.user_broker_mapping = {}
    proxy._last_known_subscriptions = {}

    proxy._handle_cache_invalidation(TOPIC, MESSAGE)  # must not raise
    assert proxy.broker_adapters == {}


def test_login_completion_emits_broker_session_refreshed(monkeypatch):
    """The Flask-side login completion emits the broker_session_refreshed SocketIO
    UI notification (the WS proxy reconnect itself is ZMQ-driven, tested above)."""
    import extensions
    import utils.auth_utils as auth_utils

    mock_socketio = MagicMock()
    monkeypatch.setattr(extensions, "socketio", mock_socketio)

    auth_utils.notify_broker_session_refreshed("alice", "zerodha")

    mock_socketio.emit.assert_called_once()
    event_name, payload = mock_socketio.emit.call_args.args
    assert event_name == "broker_session_refreshed"
    assert payload == {"username": "alice", "broker": "zerodha"}


def test_notify_never_raises_on_emit_failure(monkeypatch):
    """A SocketIO emit failure must never propagate — login must not be blocked."""
    import extensions
    import utils.auth_utils as auth_utils

    mock_socketio = MagicMock()
    mock_socketio.emit.side_effect = RuntimeError("socketio down")
    monkeypatch.setattr(extensions, "socketio", mock_socketio)

    # Must not raise.
    auth_utils.notify_broker_session_refreshed("alice", "zerodha")
