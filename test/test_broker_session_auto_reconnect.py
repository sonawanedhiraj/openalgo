"""Mocked E2E tests for event-driven broker-session WS reinitialization.

When a Zerodha (or any broker) session is refreshed, ``upsert_auth()`` publishes a
ZMQ ``CACHE_INVALIDATE`` event. The WebSocket proxy's ``_handle_cache_invalidation``
consumes it. With ``BROKER_SESSION_AUTO_RECONNECT_ENABLED=true`` it proactively
reconnects the live broker adapter with the fresh token and re-subscribes the held
symbol set — so the feed resumes WITHOUT an OpenAlgo restart. With the flag off
(default) it preserves the pre-existing behavior: disconnect + drop, lazy rebuild
on the next client auth.

These tests are fully hermetic — no real Zerodha connection, no ZMQ socket, no WS
server. They bypass ``WebSocketProxy.__init__`` (which binds port 8765 / ZMQ) via
``__new__`` and exercise the handler against a fake adapter that records calls.
"""

import json

from websocket_proxy.server import WebSocketProxy

USER_ID = "testuser"
TOPIC = f"CACHE_INVALIDATE_ALL_{USER_ID}"
MESSAGE = json.dumps({"action": "invalidate", "user_id": USER_ID, "cache_type": "ALL"})


class FakeAdapter:
    """Mock broker WS adapter recording its lifecycle calls.

    Mirrors the real adapter contract: ``disconnect()`` wipes ``subscribed_symbols``
    (as ``ZerodhaWebSocketAdapter.disconnect`` does), so a correct reconnect MUST
    snapshot the subscription set *before* calling disconnect.
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
        return {"status": "success"}


def _make_proxy(adapter, broker="zerodha"):
    """Build a WebSocketProxy without running __init__ (no port bind / ZMQ)."""
    proxy = WebSocketProxy.__new__(WebSocketProxy)
    proxy.broker_adapters = {USER_ID: adapter}
    proxy.user_broker_mapping = {USER_ID: broker}
    return proxy


def test_auto_reconnect_resubscribes_when_flag_on(monkeypatch):
    """Flag ON: disconnect → initialize(broker,user) → connect → re-subscribe all,
    and the adapter stays in the registry (not dropped)."""
    monkeypatch.setenv("BROKER_SESSION_AUTO_RECONNECT_ENABLED", "true")
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


def test_default_disconnects_and_drops_when_flag_off(monkeypatch):
    """Flag OFF (default): pre-existing behavior — disconnect + drop, no reconnect."""
    monkeypatch.delenv("BROKER_SESSION_AUTO_RECONNECT_ENABLED", raising=False)
    adapter = FakeAdapter()
    proxy = _make_proxy(adapter)

    proxy._handle_cache_invalidation(TOPIC, MESSAGE)

    assert [c[0] for c in adapter.calls] == ["disconnect"]
    assert USER_ID not in proxy.broker_adapters  # dropped for lazy rebuild


def test_explicit_false_is_off(monkeypatch):
    """An explicit falsey value keeps the default behavior."""
    monkeypatch.setenv("BROKER_SESSION_AUTO_RECONNECT_ENABLED", "false")
    adapter = FakeAdapter()
    proxy = _make_proxy(adapter)

    proxy._handle_cache_invalidation(TOPIC, MESSAGE)

    assert [c[0] for c in adapter.calls] == ["disconnect"]
    assert USER_ID not in proxy.broker_adapters


def test_reconnect_failure_removes_adapter(monkeypatch):
    """Flag ON but the broker WS refuses: adapter is removed so the next client
    auth rebuilds it from scratch (no half-dead adapter left in the registry)."""
    monkeypatch.setenv("BROKER_SESSION_AUTO_RECONNECT_ENABLED", "true")

    class FailingAdapter(FakeAdapter):
        def connect(self):
            self.calls.append(("connect",))
            raise RuntimeError("zerodha WS refused new token")

    adapter = FailingAdapter()
    proxy = _make_proxy(adapter)

    proxy._handle_cache_invalidation(TOPIC, MESSAGE)

    assert USER_ID not in proxy.broker_adapters


def test_missing_broker_mapping_falls_back_to_drop(monkeypatch):
    """Flag ON but no broker known for the user: cannot re-initialize, so fall
    back to disconnect-and-drop rather than crashing the listener."""
    monkeypatch.setenv("BROKER_SESSION_AUTO_RECONNECT_ENABLED", "true")
    adapter = FakeAdapter()
    proxy = _make_proxy(adapter)
    proxy.user_broker_mapping = {}  # broker unknown

    proxy._handle_cache_invalidation(TOPIC, MESSAGE)

    assert [c[0] for c in adapter.calls] == ["disconnect"]
    assert USER_ID not in proxy.broker_adapters


def test_no_adapter_is_noop(monkeypatch):
    """Flag ON but no live adapter for the user: handler is a clean no-op."""
    monkeypatch.setenv("BROKER_SESSION_AUTO_RECONNECT_ENABLED", "true")
    proxy = WebSocketProxy.__new__(WebSocketProxy)
    proxy.broker_adapters = {}
    proxy.user_broker_mapping = {}

    # Should not raise.
    proxy._handle_cache_invalidation(TOPIC, MESSAGE)
    assert proxy.broker_adapters == {}
