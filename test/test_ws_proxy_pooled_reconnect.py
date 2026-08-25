"""Pooled-adapter reconnect regression tests (issue #673).

On 2026-08-25 the WS proxy's event-driven reconnect wiped every live
subscription: ``_snapshot_subscriptions`` read ``adapter.subscribed_symbols``,
an attribute only RAW broker adapters have, while the production configuration
(``ENABLE_CONNECTION_POOLING`` on) registers ``_PooledAdapterWrapper`` — which
exposes ``subscriptions`` instead. The snapshot failed, the reconnect
"preserved 0 subscription(s)", and the tick feed died from market open. The
existing suite (test_broker_session_auto_reconnect.py) stayed green because its
FakeAdapter models only the raw shape — the drift between test fake and
production shape IS the bug class, so these tests use the REAL
``_PooledAdapterWrapper`` backed by a fake pool, not a hand-rolled adapter fake.

Coverage:
  - the real pooled wrapper survives the reconnect with all subscriptions;
  - snapshot precedence: ``subscribed_symbols`` wins over an (empty, unmaintained)
    ``subscriptions`` dict — raw adapters carry BOTH, so flipping the order
    would regress every raw adapter;
  - both pooled wrapper classes satisfy the snapshot contract;
  - layer 2: a shortfall against the proxy's own client ``subscription_index``
    is re-subscribed from the index (loud, additive, per-symbol isolated),
    and stays completely idle when the snapshot restore was complete;
  - pooled failure-gracefulness and idempotency mirrors of the raw-path tests.

All hermetic — no ZMQ, no ports, proxy built via ``WebSocketProxy.__new__``.
"""

import json
from unittest.mock import MagicMock

from websocket_proxy.broker_factory import _PooledAdapterWrapper
from websocket_proxy.server import WebSocketProxy

USER_ID = "testuser"
TOPIC = f"CACHE_INVALIDATE_ALL_{USER_ID}"
MESSAGE = json.dumps({"action": "invalidate", "user_id": USER_ID, "cache_type": "ALL"})


class FakeConnectionPool:
    """Mirrors the real ConnectionPool surface the wrapper delegates to.

    ``subscriptions`` is a property over an internal map (as
    ``ConnectionPool.subscriptions`` is over ``subscription_map``), and
    ``disconnect()`` clears it — so a correct reconnect MUST snapshot before
    disconnecting, exactly like the raw-adapter tests enforce.
    """

    def __init__(self, initial=None):
        self._subs = dict(initial or {})
        self.calls = []

    @property
    def subscriptions(self):
        # Same value shape as connection_manager.ConnectionPool.subscriptions
        return {f"{s}_{e}_{m}": {"symbol": s, "exchange": e, "mode": m} for (s, e, m) in self._subs}

    def initialize(self, broker_name, user_id, auth_data=None, force=False):
        self.calls.append(("initialize", broker_name, user_id))
        return {"status": "success"}

    def connect(self):
        self.calls.append(("connect",))
        return {"success": True}

    def disconnect(self):
        self.calls.append(("disconnect",))
        self._subs.clear()

    def subscribe(self, symbol, exchange, mode=2, depth_level=5):
        self.calls.append(("subscribe", symbol, exchange, mode))
        self._subs[(symbol, exchange, mode)] = True
        return {"status": "success"}


def _make_pooled_wrapper(initial_subs):
    wrapper = _PooledAdapterWrapper(adapter_class=object, broker_name="zerodha")
    wrapper._pool = FakeConnectionPool(initial_subs)
    wrapper._user_id = USER_ID
    return wrapper


def _make_proxy(adapter, broker="zerodha"):
    proxy = WebSocketProxy.__new__(WebSocketProxy)
    proxy.broker_adapters = {USER_ID: adapter}
    proxy.user_broker_mapping = {USER_ID: broker}
    proxy._last_known_subscriptions = {}
    return proxy


INITIAL = {("RELIANCE", "NSE", 2): True, ("INFY", "NSE", 1): True}


# ---------------------------------------------------------------------------
# Layer 1 — the snapshot works on the REAL pooled wrapper
# ---------------------------------------------------------------------------


def test_pooled_wrapper_reconnect_preserves_subscriptions():
    """THE 2026-08-25 regression: with pooling on, a cache-invalidation event
    must preserve and re-subscribe the full symbol set (was 0/0)."""
    wrapper = _make_pooled_wrapper(INITIAL)
    proxy = _make_proxy(wrapper)

    proxy._handle_cache_invalidation(TOPIC, MESSAGE)

    pool = wrapper._pool
    resubscribed = {(c[1], c[2], c[3]) for c in pool.calls if c[0] == "subscribe"}
    assert resubscribed == {("RELIANCE", "NSE", 2), ("INFY", "NSE", 1)}
    # The wrapper's live subscription set is fully restored.
    assert len(wrapper.subscriptions) == 2
    # And the wrapper stays registered (feed resumes without a client reconnect).
    assert proxy.broker_adapters[USER_ID] is wrapper


def test_snapshot_prefers_subscribed_symbols_over_empty_subscriptions():
    """Raw adapters carry BOTH attributes: per-broker code maintains
    ``subscribed_symbols`` while base_adapter initializes ``subscriptions = {}``
    and may never touch it. The snapshot must keep reading the maintained one."""

    class RawShapedAdapter:
        subscribed_symbols = {
            "NSE:SBIN": {"symbol": "SBIN", "exchange": "NSE", "token": 1, "mode": 2}
        }
        subscriptions = {}  # base_adapter's unmaintained default

    snap = WebSocketProxy._snapshot_subscriptions(RawShapedAdapter())
    assert snap == [("SBIN", "NSE", 2)]


def test_both_pooled_wrapper_classes_satisfy_snapshot_contract():
    """There are TWO pooled wrapper classes (broker_factory and the
    connection_manager factory twin). The snapshot must read both."""
    # broker_factory wrapper
    wrapper = _make_pooled_wrapper({("TCS", "NSE", 2): True})
    assert WebSocketProxy._snapshot_subscriptions(wrapper) == [("TCS", "NSE", 2)]

    # connection_manager's nested wrapper shape: no subscribed_symbols, a
    # ``subscriptions`` property over the pool.
    class CmShapedWrapper:
        @property
        def subscriptions(self):
            return {"TCS_NSE_2": {"symbol": "TCS", "exchange": "NSE", "mode": 2}}

    assert WebSocketProxy._snapshot_subscriptions(CmShapedWrapper()) == [("TCS", "NSE", 2)]


def test_pooled_reconnect_failure_is_graceful_and_preserves_state(monkeypatch):
    """Pooled mirror of the raw-path graceful-failure test: a rejected token
    drops the adapter but KEEPS the subscription snapshot for the next attempt."""
    mock_logger = MagicMock()
    monkeypatch.setattr("websocket_proxy.server.logger", mock_logger)

    wrapper = _make_pooled_wrapper(INITIAL)

    def failing_connect():
        raise ConnectionError("token rejected")

    wrapper._pool.connect = failing_connect
    proxy = _make_proxy(wrapper)

    proxy._handle_cache_invalidation(TOPIC, MESSAGE)

    assert USER_ID not in proxy.broker_adapters  # dead adapter dropped
    assert set(proxy._last_known_subscriptions[USER_ID]) == {
        ("RELIANCE", "NSE", 2),
        ("INFY", "NSE", 1),
    }
    assert mock_logger.exception.called


def test_pooled_repeated_events_are_idempotent():
    """Two refresh events must not pile up duplicate subscriptions."""
    wrapper = _make_pooled_wrapper(INITIAL)
    proxy = _make_proxy(wrapper)

    proxy._handle_cache_invalidation(TOPIC, MESSAGE)
    proxy._handle_cache_invalidation(TOPIC, MESSAGE)

    assert len(wrapper.subscriptions) == 2
    assert {(s["symbol"], s["exchange"], s["mode"]) for s in wrapper.subscriptions.values()} == {
        ("RELIANCE", "NSE", 2),
        ("INFY", "NSE", 1),
    }


# ---------------------------------------------------------------------------
# Layer 2 — post-reconnect verification against the client subscription_index
# ---------------------------------------------------------------------------


class ShapeDriftedAdapter:
    """An adapter exposing NEITHER subscription attribute — the future-drift
    case layer 2 exists for. Lifecycle succeeds; only the snapshot is blind."""

    def __init__(self):
        self.calls = []

    def disconnect(self):
        self.calls.append(("disconnect",))

    def initialize(self, broker_name, user_id, auth_data=None):
        self.calls.append(("initialize", broker_name, user_id))
        return {"status": "success"}

    def connect(self):
        self.calls.append(("connect",))
        return {"status": "success"}

    def subscribe(self, symbol, exchange, mode=2, depth_level=5):
        self.calls.append(("subscribe", symbol, exchange, mode))
        return {"status": "success"}


def _with_index(proxy, index, user_mapping):
    proxy.subscription_index = index
    proxy.user_mapping = user_mapping
    return proxy


def test_index_restores_when_snapshot_is_blind(monkeypatch):
    """Snapshot yields nothing but the proxy's own clients hold symbols: the
    reconnect must restore them from subscription_index. This is the PRIMARY
    path on login-driven reconnects (the login flow disconnects the pool —
    clearing subscription_map — before the CACHE_INVALIDATE is consumed, live
    finding 2026-08-25 10:12:33), so a full successful restore is a WARNING,
    never an ERROR — a daily expected ERROR trains the operator to ignore
    errors."""
    mock_logger = MagicMock()
    monkeypatch.setattr("websocket_proxy.server.logger", mock_logger)

    adapter = ShapeDriftedAdapter()
    proxy = _with_index(
        _make_proxy(adapter),
        index={
            ("RELIANCE", "NSE", 2): {1},
            ("INFY", "NSE", 1): {1, 2},
            ("HDFCBANK", "NSE", 2): {99},  # another user's client — must NOT be restored
        },
        user_mapping={1: USER_ID, 2: USER_ID, 99: "otheruser"},
    )

    proxy._handle_cache_invalidation(TOPIC, MESSAGE)

    restored = {(c[1], c[2], c[3]) for c in adapter.calls if c[0] == "subscribe"}
    assert restored == {("RELIANCE", "NSE", 2), ("INFY", "NSE", 1)}
    assert mock_logger.warning.called
    assert not mock_logger.error.called  # full restore succeeded — expected path


def test_index_layer_idle_when_snapshot_restore_complete(monkeypatch):
    """When layer 1 restored everything, layer 2 must not add churn: exactly
    one subscribe per held symbol, and no ERROR log."""
    mock_logger = MagicMock()
    monkeypatch.setattr("websocket_proxy.server.logger", mock_logger)

    wrapper = _make_pooled_wrapper(INITIAL)
    proxy = _with_index(
        _make_proxy(wrapper),
        index={("RELIANCE", "NSE", 2): {1}, ("INFY", "NSE", 1): {1}},
        user_mapping={1: USER_ID},
    )

    proxy._handle_cache_invalidation(TOPIC, MESSAGE)

    subscribe_calls = [c for c in wrapper._pool.calls if c[0] == "subscribe"]
    assert len(subscribe_calls) == 2
    assert not mock_logger.error.called
    assert not mock_logger.warning.called


def test_index_layer_noop_on_empty_index(monkeypatch):
    """No clients, nothing to verify against — behavior identical to today."""
    mock_logger = MagicMock()
    monkeypatch.setattr("websocket_proxy.server.logger", mock_logger)

    adapter = ShapeDriftedAdapter()
    proxy = _with_index(_make_proxy(adapter), index={}, user_mapping={})

    proxy._handle_cache_invalidation(TOPIC, MESSAGE)

    assert [c for c in adapter.calls if c[0] == "subscribe"] == []
    assert not mock_logger.error.called


def test_index_restore_is_per_symbol_isolated(monkeypatch):
    """One failing re-subscribe must not abort the rest (matches the existing
    per-symbol isolation convention in the reconnect resubscribe loop), and an
    INCOMPLETE restore — unlike the expected full one — escalates to ERROR:
    those symbols have no live feed until the next heal."""
    mock_logger = MagicMock()
    monkeypatch.setattr("websocket_proxy.server.logger", mock_logger)

    class PartiallyFailingAdapter(ShapeDriftedAdapter):
        def subscribe(self, symbol, exchange, mode=2, depth_level=5):
            if symbol == "RELIANCE":
                raise RuntimeError("boom")
            return super().subscribe(symbol, exchange, mode, depth_level)

    adapter = PartiallyFailingAdapter()
    proxy = _with_index(
        _make_proxy(adapter),
        index={("RELIANCE", "NSE", 2): {1}, ("INFY", "NSE", 1): {1}},
        user_mapping={1: USER_ID},
    )

    proxy._handle_cache_invalidation(TOPIC, MESSAGE)

    restored = {(c[1], c[2], c[3]) for c in adapter.calls if c[0] == "subscribe"}
    assert restored == {("INFY", "NSE", 1)}
    assert mock_logger.error.called  # incomplete restore is the ERROR case
