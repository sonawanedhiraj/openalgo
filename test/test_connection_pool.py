"""Integration tests for ConnectionPool.initialize() response-shape handling.

These tests exercise ConnectionPool.initialize() DIRECTLY — NOT via
WebSocketProxy.broker_adapters injection — which is the gap that allowed the
line-443 predicate bug (#76) to ship undetected. Every response shape the
predicate must handle is covered, plus exception safety, idempotency, force-
reinit, and a thread-leak guard.

Regression guard: test_zerodha_status_success_is_not_failure MUST FAIL on the
pre-#79 commit (39e5f8f99) where the predicate was `not result.get("success")`
— None is falsy, so {"status": "success"} triggered is_error=True. It passes on
the fixed predicate (`result.get("success") is False`).
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

from websocket_proxy.connection_manager import ConnectionPool

# ---------------------------------------------------------------------------
# Helpers — adapter classes (not instances; ConnectionPool calls adapter_class())
# ---------------------------------------------------------------------------


def _make_adapter_class(initialize_return):
    """Return a class whose initialize() always returns initialize_return."""

    class FakeAdapter:
        def __init__(self):
            self.subscribed_symbols = {}
            self.disconnect_calls = 0

        def initialize(self, broker_name, user_id, auth_data=None):
            return initialize_return

        def disconnect(self):
            self.disconnect_calls += 1
            return {"status": "success"}

    return FakeAdapter


def _make_raising_adapter_class(exc_class=RuntimeError, msg="boom"):
    """Return a class whose initialize() raises exc_class(msg)."""

    class RaisingAdapter:
        def __init__(self):
            self.subscribed_symbols = {}

        def initialize(self, broker_name, user_id, auth_data=None):
            raise exc_class(msg)

    return RaisingAdapter


# ---------------------------------------------------------------------------
# Fixture: stub SharedZmqPublisher so no real ZMQ socket is opened
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_shared_publisher():
    """Patch SharedZmqPublisher in connection_manager for every test.

    ConnectionPool.__init__ does self.shared_publisher = SharedZmqPublisher()
    and _create_adapter() calls self.shared_publisher.bind().  With the real
    class those open ZMQ sockets; the mock keeps tests hermetic.
    """
    mock_pub = MagicMock()
    mock_pub.bind.return_value = 5555
    with patch(
        "websocket_proxy.connection_manager.SharedZmqPublisher",
        return_value=mock_pub,
    ):
        yield mock_pub


def _make_pool(adapter_class):
    return ConnectionPool(
        adapter_class=adapter_class,
        broker_name="zerodha",
        user_id="testuser",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConnectionPoolInitialize:
    """Class-level integration tests for ConnectionPool.initialize().

    Each test creates a fresh pool, calls initialize(), and asserts both the
    return value and the resulting pool state.  All run against the real
    initialize() logic with only SharedZmqPublisher stubbed out.
    """

    # --- Response-shape coverage ---

    def test_success_bool_true(self):
        """{"success": True} → success, adapter appended, pool marked initialized."""
        pool = _make_pool(_make_adapter_class({"success": True}))
        result = pool.initialize()

        assert result["success"] is True
        assert pool.initialized is True
        assert len(pool.adapters) == 1

    def test_failure_bool_false(self):
        """{"success": False} → failure, adapters list stays empty, not initialized."""
        pool = _make_pool(_make_adapter_class({"success": False, "error": "bad token"}))
        result = pool.initialize()

        assert result["success"] is False
        assert "error" in result
        assert pool.initialized is False
        assert len(pool.adapters) == 0

    def test_zerodha_status_success_is_not_failure(self):
        """{"status": "success"} (Zerodha adapter shape) must succeed.

        This is the REGRESSION GUARD for issue #76.  Pre-fix the predicate was
        effectively `not result.get("success")` — None is falsy, so this dict
        (which has no "success" key) was treated as an error and initialize()
        returned failure.  Post-fix the predicate is `is False`, so None != False
        and the shape is correctly treated as success.

        If this test fails it means the line-443 predicate has regressed.
        """
        pool = _make_pool(_make_adapter_class({"status": "success"}))
        result = pool.initialize()

        assert result["success"] is True, (
            '{"status": "success"} (Zerodha shape) must NOT be treated as an error '
            "— this indicates a regression of issue #76"
        )
        assert pool.initialized is True
        assert len(pool.adapters) == 1

    def test_status_error_is_failure(self):
        """{"status": "error", "message": "..."} from adapter → pool init fails."""
        pool = _make_pool(_make_adapter_class({"status": "error", "message": "auth failed"}))
        result = pool.initialize()

        assert result["success"] is False
        assert pool.initialized is False
        assert len(pool.adapters) == 0

    def test_empty_dict_is_success(self):
        """Empty {} → no "success" / "status" key → treated as success (defensive)."""
        pool = _make_pool(_make_adapter_class({}))
        result = pool.initialize()

        assert result["success"] is True
        assert pool.initialized is True

    def test_none_return_is_success(self):
        """None return → falsy short-circuit on both predicates → treated as success."""
        pool = _make_pool(_make_adapter_class(None))
        result = pool.initialize()

        assert result["success"] is True
        assert pool.initialized is True

    # --- Exception safety ---

    def test_adapter_raises_exception_is_caught(self):
        """adapter.initialize() raising is caught; pool returns failure without crashing."""
        pool = _make_pool(_make_raising_adapter_class(RuntimeError, "network timeout"))
        result = pool.initialize()

        assert result["success"] is False
        assert "network timeout" in result.get("error", "")
        assert pool.initialized is False
        assert len(pool.adapters) == 0

    # --- Idempotency ---

    def test_already_initialized_is_idempotent(self):
        """Calling initialize() twice without force is a no-op on the second call."""
        AdapterClass = _make_adapter_class({"success": True})
        call_log = []
        original_init = AdapterClass.initialize

        def counting_init(self, *a, **kw):
            call_log.append(1)
            return original_init(self, *a, **kw)

        AdapterClass.initialize = counting_init

        pool = _make_pool(AdapterClass)
        r1 = pool.initialize()
        r2 = pool.initialize()

        assert r1["success"] is True
        assert r2["success"] is True
        assert len(call_log) == 1, "adapter.initialize must be called only once"
        assert pool.initialized is True

    # --- Force re-init ---

    def test_force_reinitialize_disconnects_old_adapter(self):
        """force=True tears down the existing adapter before re-initializing."""
        AdapterClass = _make_adapter_class({"success": True})
        pool = _make_pool(AdapterClass)
        pool.initialize()
        first_adapter = pool.adapters[0]

        result = pool.initialize(force=True)

        assert result["success"] is True
        assert pool.initialized is True
        assert first_adapter.disconnect_calls == 1, (
            "force=True must disconnect the old adapter before rebuilding"
        )

    # --- Thread-leak guard ---

    def test_no_threads_spawned_by_initialize(self):
        """ConnectionPool.initialize() must not start background threads."""
        pool = _make_pool(_make_adapter_class({"success": True}))
        before = threading.active_count()
        pool.initialize()
        after = threading.active_count()

        assert after <= before + 1, (
            f"initialize() spawned unexpected threads: before={before} after={after}"
        )
