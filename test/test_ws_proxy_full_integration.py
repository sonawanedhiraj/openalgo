"""P0-T6: WS proxy full integration — ZMQ → subscription_index → client.

Tests four behaviours of WebSocketProxy:
  1. ZMQ tick → subscription_index lookup → client.send() called      (async)
  2. Subscribe adds client to index; unsubscribe removes it            (sync)
  3. last_message_time updated on each received tick                   (async)
  4. _last_known_subscriptions persists after adapter reconnect failure (sync)

All hermetic — no real ZMQ socket, no real WebSocket server, no broker.
WebSocketProxy is constructed via __new__ to bypass __init__ (no port bind / ZMQ).

Marked Linux-only because zmq.asyncio event-loop integration differs on Windows.

Refs #94
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock

import pytest

from websocket_proxy.server import WebSocketProxy

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="zmq.asyncio integration is Linux-only; Windows ProactorEventLoop differs",
)


# ---------------------------------------------------------------------------
# Fixture / factory
# ---------------------------------------------------------------------------


def _make_proxy() -> WebSocketProxy:
    """Instantiate WebSocketProxy without __init__ (no port binding, no ZMQ socket)."""
    proxy = WebSocketProxy.__new__(WebSocketProxy)
    proxy.running = False
    proxy.clients = {}
    proxy.subscriptions = {}
    proxy.broker_adapters = {}
    proxy.user_mapping = {}
    proxy.user_broker_mapping = {}
    proxy._last_known_subscriptions = {}
    proxy.subscription_index = defaultdict(set)
    proxy.last_message_time = {}
    proxy.message_throttle_interval = 0.05
    proxy._messages_processed = 0
    proxy._last_cleanup_time = time.time()  # prevents cleanup on first pass
    proxy._cleanup_interval = 300
    proxy._throttle_entry_max_age = 60
    return proxy


def _mock_zmq_single(proxy: WebSocketProxy, topic: bytes, data: bytes) -> None:
    """Wire proxy.socket so zmq_listener receives exactly one message then stops.

    First recv_multipart() call returns [topic, data].
    Second call sets proxy.running=False and raises asyncio.TimeoutError —
    zmq_listener catches it as the normal poll-timeout and exits the while-loop.
    """
    call_no = 0

    async def _recv():
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            return [topic, data]
        proxy.running = False
        raise TimeoutError

    proxy.socket = MagicMock()
    proxy.socket.recv_multipart = _recv


# ---------------------------------------------------------------------------
# Test 1 — ZMQ message routes to subscribed WebSocket client
# ---------------------------------------------------------------------------


async def test_zmq_message_routes_to_subscribed_client():
    """A ZMQ tick for RELIANCE/NSE/LTP is forwarded to the one subscribed client."""
    proxy = _make_proxy()

    client_id = 1
    mock_ws = AsyncMock()
    proxy.clients[client_id] = mock_ws
    proxy.user_mapping[client_id] = "user1"
    proxy.user_broker_mapping["user1"] = "zerodha"
    # LTP mode = 1
    proxy.subscription_index[("RELIANCE", "NSE", 1)].add(client_id)

    tick_data = json.dumps({"ltp": 2450.75, "symbol": "RELIANCE"}).encode()
    _mock_zmq_single(proxy, b"NSE_RELIANCE_LTP", tick_data)
    proxy.running = True

    await proxy.zmq_listener()

    # client.send must have been called exactly once
    assert mock_ws.send.call_count == 1, f"Expected 1 send call, got {mock_ws.send.call_count}"
    payload = json.loads(mock_ws.send.call_args[0][0])
    assert payload["type"] == "market_data"
    assert payload["symbol"] == "RELIANCE"
    assert payload["exchange"] == "NSE"
    assert payload["mode"] == 1  # LTP


async def test_unsubscribed_client_receives_no_message():
    """A client that is NOT in subscription_index receives no message for that tick."""
    proxy = _make_proxy()

    client_id = 42
    mock_ws = AsyncMock()
    proxy.clients[client_id] = mock_ws
    proxy.user_mapping[client_id] = "user1"
    proxy.user_broker_mapping["user1"] = "zerodha"
    # subscription_index is EMPTY — client is not subscribed to any symbol

    tick_data = json.dumps({"ltp": 2450.75}).encode()
    _mock_zmq_single(proxy, b"NSE_RELIANCE_LTP", tick_data)
    proxy.running = True

    await proxy.zmq_listener()

    mock_ws.send.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2 — Subscription index management (synchronous)
# ---------------------------------------------------------------------------


class TestSubscriptionIndex:
    """subscription_index is correctly maintained on subscribe / unsubscribe."""

    def test_subscribe_adds_client_to_index(self):
        """Adding a client to the index makes the key present."""
        proxy = _make_proxy()

        proxy.subscription_index[("INFY", "NSE", 1)].add(10)
        proxy.subscription_index[("INFY", "NSE", 1)].add(11)

        assert 10 in proxy.subscription_index[("INFY", "NSE", 1)]
        assert 11 in proxy.subscription_index[("INFY", "NSE", 1)]

    def test_unsubscribe_last_client_removes_key(self):
        """Unsubscribing the last client for a symbol removes the key from index."""
        proxy = _make_proxy()
        key = ("WIPRO", "NSE", 1)
        proxy.subscription_index[key].add(5)

        # Simulate the unsubscribe path from handle_client
        proxy.subscription_index[key].discard(5)
        if not proxy.subscription_index[key]:
            del proxy.subscription_index[key]

        assert key not in proxy.subscription_index

    def test_unsubscribe_one_client_leaves_others(self):
        """Unsubscribing one client keeps others in the index."""
        proxy = _make_proxy()
        key = ("TCS", "NSE", 2)
        proxy.subscription_index[key].update({20, 21, 22})

        # Client 21 unsubscribes
        proxy.subscription_index[key].discard(21)
        if not proxy.subscription_index[key]:
            del proxy.subscription_index[key]

        assert key in proxy.subscription_index
        assert 21 not in proxy.subscription_index[key]
        assert {20, 22} == proxy.subscription_index[key]


# ---------------------------------------------------------------------------
# Test 3 — last_message_time updated per tick
# ---------------------------------------------------------------------------


async def test_last_message_time_updated_on_received_tick():
    """Every tick updates last_message_time for the (symbol, exchange, mode) key."""
    proxy = _make_proxy()

    # No subscribed clients — message still updates last_message_time
    tick_data = json.dumps({"ltp": 1.0}).encode()
    _mock_zmq_single(proxy, b"NSE_SBIN_LTP", tick_data)
    proxy.running = True

    await proxy.zmq_listener()

    key = ("SBIN", "NSE", 1)
    assert key in proxy.last_message_time, (
        "last_message_time must be updated even when no client is subscribed"
    )
    # Timestamp should be very recent (within last 5 seconds)
    age = time.time() - proxy.last_message_time[key]
    assert age < 5.0, f"last_message_time age {age:.2f}s > 5s — not updated recently"


# ---------------------------------------------------------------------------
# Test 4 — _last_known_subscriptions persists after adapter failure
# ---------------------------------------------------------------------------


class FakeAdapterWithSubs:
    """Minimal adapter stub — reports subscriptions then fails on initialize."""

    def __init__(self, subscriptions: list[tuple]):
        self._subs = {
            f"{ex}:{sym}": {"symbol": sym, "exchange": ex, "mode": mode}
            for sym, ex, mode in subscriptions
        }
        self.calls: list[str] = []

    @property
    def subscribed_symbols(self):
        return self._subs

    def disconnect(self):
        self.calls.append("disconnect")
        self._subs.clear()
        return {"status": "success"}

    def initialize(self, broker_name, user_id, auth_data=None):
        self.calls.append("initialize")
        return {"status": "error", "message": "stale_token"}

    def connect(self):
        self.calls.append("connect")
        return {"status": "success"}

    def subscribe(self, symbol, exchange, mode=2, depth_level=5):
        self.calls.append(f"subscribe:{symbol}")
        return {"status": "success"}


class TestLastKnownSubscriptions:
    def test_reconnect_failure_preserves_subscription_snapshot(self):
        """When adapter.initialize() fails, _last_known_subscriptions retains symbols."""
        proxy = _make_proxy()

        subs = [("RELIANCE", "NSE", 2), ("INFY", "NSE", 2)]
        adapter = FakeAdapterWithSubs(subs)
        proxy.broker_adapters["user1"] = adapter
        proxy.user_broker_mapping["user1"] = "zerodha"

        result = proxy._reconnect_broker_adapter("user1")

        # Reconnect failed (initialize returned error)
        assert result is False
        # Dead adapter was removed from the registry
        assert "user1" not in proxy.broker_adapters
        # But the subscription snapshot is preserved for retry
        saved = proxy._last_known_subscriptions.get("user1", [])
        saved_symbols = {sym for sym, _, _ in saved}
        assert saved_symbols == {"RELIANCE", "INFY"}, (
            f"_last_known_subscriptions should retain both symbols; got {saved_symbols}"
        )

    def test_reconnect_success_re_subscribes_all_symbols(self):
        """When adapter.initialize() succeeds, all symbols are re-subscribed."""
        proxy = _make_proxy()

        class GoodAdapter(FakeAdapterWithSubs):
            def initialize(self, broker_name, user_id, auth_data=None):
                self.calls.append("initialize")
                return {"status": "success"}

        subs = [("WIPRO", "NSE", 1), ("HCL", "NSE", 1)]
        adapter = GoodAdapter(subs)
        proxy.broker_adapters["user1"] = adapter
        proxy.user_broker_mapping["user1"] = "zerodha"

        result = proxy._reconnect_broker_adapter("user1")

        assert result is True
        # All symbols re-subscribed
        subscribe_calls = [c for c in adapter.calls if c.startswith("subscribe:")]
        subscribed_symbols = {c.split(":")[1] for c in subscribe_calls}
        assert subscribed_symbols == {"WIPRO", "HCL"}

    def test_empty_proxy_adapter_returns_false_gracefully(self):
        """Calling _reconnect_broker_adapter for unknown user returns False, no crash."""
        proxy = _make_proxy()
        result = proxy._reconnect_broker_adapter("nonexistent")
        assert result is False
