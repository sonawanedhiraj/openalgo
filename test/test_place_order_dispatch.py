"""Tests for Stage-0 resolver wired into ``place_order_with_auth``.

Each test rebinds ``database.daily_intent_db`` to a fresh in-memory SQLite so
nothing touches ``db/openalgo.db``. The broker side-call
(``broker.<name>.api.order_api.place_order_api``) and the sandbox side-call
(``services.sandbox_service.sandbox_place_order``) are both monkeypatched so
tests assert routing without any broker network calls or sandbox DB writes.

The critical case is ``test_place_order_routes_to_sandbox_when_live_but_analyze_on``
— that exercises the bug being fixed: declaring ``daily_intent=live`` but
leaving the global ``analyze_mode`` flag on must still resolve to SANDBOX, not
fire the live broker path.

P0-T1 integration tests (Issue #94)
------------------------------------
The second block of tests exercises ``place_order_service`` in *live* mode
against the FastAPI mock Zerodha broker (``test/fixtures/mock_broker/app.py``)
started in a daemon thread. ``BROKER_API_URL`` is redirected to the mock and
the global httpx client is cleared so each test connects to the mock, not the
real Zerodha endpoint. No live DB access — ``resolve_effective_mode`` is
monkeypatched to ``EffectiveMode.LIVE`` and events are suppressed.
"""

import asyncio
import concurrent.futures
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

# Pre-resolve the restx_api / services.place_order_service circular import
# before any test does ``from services import place_order_service``. The
# project-root conftest no longer eagerly loads restx_api (see conftest.py
# for why), so the tests that participate in the cycle now take care of it
# themselves. ``import sandbox`` in conftest already pinned the project-root
# sandbox package, so this import still resolves submodules correctly even
# after pytest has added test/ to sys.path.
import restx_api  # noqa: E402, F401


@pytest.fixture
def fresh_intent_db(monkeypatch):
    """Point daily_intent_db at a fresh in-memory SQLite for one test."""
    from database import daily_intent_db as dim

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=test_engine))

    monkeypatch.setattr(dim, "engine", test_engine)
    monkeypatch.setattr(dim, "db_session", test_session)
    dim.Base.metadata.create_all(test_engine)

    yield dim

    test_session.remove()
    test_engine.dispose()


def _order_payload():
    """Minimal validated order_data passed to place_order_with_auth."""
    return {
        "apikey": "test-api-key",
        "strategy": "unit_test",
        "symbol": "INFY",
        "exchange": "NSE",
        "action": "BUY",
        "quantity": 1,
        "pricetype": "MARKET",
        "product": "MIS",
    }


# ---------------------------------------------------------------------------
# LIVE path
# ---------------------------------------------------------------------------


def test_place_order_routes_to_broker_when_live(fresh_intent_db, monkeypatch):
    """daily_intent='live' + analyze_mode=False → broker.place_order_api fires."""
    from services import place_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")

    broker_place_order = MagicMock(
        return_value=(SimpleNamespace(status=200), {"status": "ok"}, "OID-LIVE-1")
    )
    fake_broker_module = SimpleNamespace(place_order_api=broker_place_order)
    monkeypatch.setattr(place_order_service, "import_broker_module", lambda _b: fake_broker_module)

    sandbox_called = MagicMock()
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_called)

    payload = _order_payload()
    success, response, status = place_order_service.place_order_with_auth(
        payload,
        auth_token="dummy-token",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    assert success is True
    assert response == {"status": "success", "orderid": "OID-LIVE-1"}
    assert status == 200
    broker_place_order.assert_called_once()
    sandbox_called.assert_not_called()


# ---------------------------------------------------------------------------
# SANDBOX path (explicit intent)
# ---------------------------------------------------------------------------


def test_place_order_routes_to_sandbox_when_sandbox_intent(fresh_intent_db, monkeypatch):
    """daily_intent='sandbox' → sandbox_place_order fires, broker NOT called."""
    from services import place_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")

    sandbox_mock = MagicMock(
        return_value=(True, {"status": "success", "orderid": "SBX-1", "mode": "analyze"}, 200)
    )
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)

    broker_called = MagicMock()
    monkeypatch.setattr(
        place_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_called),
    )

    payload = _order_payload()
    success, response, status = place_order_service.place_order_with_auth(
        payload,
        auth_token="dummy-token",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    assert success is True
    assert response["status"] == "success"
    assert response["orderid"] == "SBX-1"
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_called.assert_not_called()


# ---------------------------------------------------------------------------
# SANDBOX path (the bug being fixed: live + analyze_on → sandbox)
# ---------------------------------------------------------------------------


def test_place_order_routes_to_sandbox_when_live_but_analyze_on(fresh_intent_db, monkeypatch):
    """THE BUG: daily_intent='live' + analyze_mode=True must conservative-down
    to sandbox, not silently fire on the live broker.
    """
    from services import place_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: True)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")

    sandbox_mock = MagicMock(
        return_value=(True, {"status": "success", "orderid": "SBX-BUG", "mode": "analyze"}, 200)
    )
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)

    broker_called = MagicMock()
    monkeypatch.setattr(
        place_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_called),
    )

    payload = _order_payload()
    success, response, status = place_order_service.place_order_with_auth(
        payload,
        auth_token="dummy-token",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    assert success is True
    sandbox_mock.assert_called_once()
    broker_called.assert_not_called(), "Live broker fired despite analyze_mode=True!"


# ---------------------------------------------------------------------------
# SKIP path (mode-only: 'skip' is retired — legacy intent collapses to SANDBOX)
# ---------------------------------------------------------------------------


def test_place_order_routes_to_sandbox_when_skip_legacy_intent(fresh_intent_db, monkeypatch):
    """Mode-only (B2, 2026-06-12): a legacy daily_intent='skip' no longer rejects
    — it collapses to SANDBOX. External callers are never refused for config."""
    from services import place_order_service
    from services.mode_service import set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")

    sandbox_mock = MagicMock(
        return_value=(True, {"status": "success", "orderid": "SBX-SKIP", "mode": "analyze"}, 200)
    )
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)
    broker_mock = MagicMock()
    monkeypatch.setattr(
        place_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_mock),
    )

    payload = _order_payload()
    success, response, status = place_order_service.place_order_with_auth(
        payload,
        auth_token="dummy-token",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    assert success is True
    assert response["status"] == "success"
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_mock.assert_not_called()


# ---------------------------------------------------------------------------
# No-config path (mode-only: no row → SANDBOX default, never a DISABLED reject)
# ---------------------------------------------------------------------------


def test_place_order_routes_to_sandbox_when_no_intent(fresh_intent_db, monkeypatch):
    """Mode-only (B2): no daily_intent row → SANDBOX default (was DISABLED reject)."""
    from services import place_order_service

    # NO set_daily_intent call — table is empty.
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)
    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")

    sandbox_mock = MagicMock(
        return_value=(True, {"status": "success", "orderid": "SBX-DFLT", "mode": "analyze"}, 200)
    )
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)
    broker_mock = MagicMock()
    monkeypatch.setattr(
        place_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_mock),
    )

    payload = _order_payload()
    success, response, status = place_order_service.place_order_with_auth(
        payload,
        auth_token="dummy-token",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    assert success is True
    assert status == 200
    sandbox_mock.assert_called_once()
    broker_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Response shape: sandbox and live returns share the 3-tuple convention so
# callers don't need to special-case either path.
# ---------------------------------------------------------------------------


def test_place_order_reject_response_shape_matches_existing_convention(
    fresh_intent_db, monkeypatch
):
    """Both sandbox and live returns are (bool, dict, int) — same outer shape."""
    from services import place_order_service
    from services.mode_service import set_daily_intent

    monkeypatch.setattr("services.mode_service._today_ist_str", lambda: "2026-05-28")
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)

    # ---- shape for the SANDBOX path (skip collapses to sandbox in mode-only) ----
    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    sandbox_mock = MagicMock(
        return_value=(True, {"status": "success", "orderid": "SBX", "mode": "analyze"}, 200)
    )
    monkeypatch.setattr("services.sandbox_service.sandbox_place_order", sandbox_mock)
    monkeypatch.setattr(
        place_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=MagicMock()),
    )

    payload = _order_payload()
    sandbox_result = place_order_service.place_order_with_auth(
        payload,
        auth_token="dummy-token",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    # ---- shape for a successful LIVE order, same call shape ----
    # Re-bind: flip intent to live, ensure broker fires
    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    broker_place_order = MagicMock(
        return_value=(SimpleNamespace(status=200), {"status": "ok"}, "OID-OK")
    )
    monkeypatch.setattr(
        place_order_service,
        "import_broker_module",
        lambda _b: SimpleNamespace(place_order_api=broker_place_order),
    )
    success_result = place_order_service.place_order_with_auth(
        payload,
        auth_token="dummy-token",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    # Same outer shape: tuple of length 3, (bool, dict, int).
    for result in (sandbox_result, success_result):
        assert isinstance(result, tuple) and len(result) == 3
        assert isinstance(result[0], bool)
        assert isinstance(result[1], dict)
        assert isinstance(result[2], int)


# ===========================================================================
# P0-T1 Integration tests — live-mode dispatch via mock Zerodha broker
# Issue #94 — test plan §Flow 1 / P0
# ===========================================================================


def _free_port() -> int:
    """Ask the OS for an available port, then release it before returning."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mock_broker_server():
    """Start the FastAPI mock Zerodha broker in a daemon thread.

    Yields ``(base_url, state)`` where ``state`` is the live in-memory state
    object that tests can inspect or mutate (e.g. ``state.fail_next_order``).
    """
    import uvicorn

    from test.fixtures.mock_broker.app import app, state

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    def _run() -> None:
        # A non-main thread needs its own event loop (Windows compat).
        asyncio.set_event_loop(asyncio.new_event_loop())
        server.run()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/_mock/healthz", timeout=0.3).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        server.should_exit = True
        pytest.fail(f"Mock broker did not become ready on {base_url}")

    state.reset()
    yield base_url, state

    server.should_exit = True
    t.join(timeout=5)


@pytest.fixture
def _live_patches(mock_broker_server, monkeypatch):
    """Apply per-test patches for live-mode integration tests.

    * Redirects ``BROKER_API_URL`` to the mock server.
    * Clears the global httpx client so it reconnects to the mock.
    * Patches ``resolve_effective_mode`` → LIVE.
    * Patches ``get_br_symbol`` so SBIN/NSE → SBIN-EQ.
    """
    base_url, state = mock_broker_server
    state.reset()

    import broker.zerodha.api.order_api as zk_api

    monkeypatch.setattr(zk_api, "BROKER_API_URL", base_url)

    import utils.httpx_client as hx

    saved_client = hx._httpx_client
    hx._httpx_client = None

    from services.mode_service import EffectiveMode

    monkeypatch.setattr(
        "services.place_order_service.resolve_effective_mode",
        lambda: EffectiveMode.LIVE,
    )

    import broker.zerodha.mapping.transform_data as td

    monkeypatch.setattr(
        td,
        "get_br_symbol",
        lambda sym, exch: "SBIN-EQ" if (sym == "SBIN" and exch == "NSE") else sym,
    )

    yield base_url, state

    hx._httpx_client = saved_client


def _live_payload() -> dict:
    """Minimal order payload for the P0-T1 integration tests."""
    return {
        "apikey": "p0-t1-test-key",  # pragma: allowlist secret
        "strategy": "p0_t1_integration",
        "symbol": "SBIN",
        "exchange": "NSE",
        "action": "BUY",
        "quantity": 1,
        "pricetype": "MARKET",
        "product": "MIS",
        "price": "0",
        "trigger_price": "0",
        "disclosed_quantity": "0",
    }


# ---------------------------------------------------------------------------
# Test 1 — Happy path
# ---------------------------------------------------------------------------


def test_p0t1_live_happy_path(mock_broker_server, _live_patches):
    """BUY MARKET NSE/SBIN in live mode → mock broker returns success.

    Verifies:
    * ``success=True`` and HTTP 200.
    * Response conforms to ``/api/v1/placeorder`` contract: ``{"status":"success","orderid":"MOCK…"}``.
    * Mock broker ``/orders/regular`` was called exactly once.
    """
    from services import place_order_service

    _, state = _live_patches
    payload = _live_payload()

    success, response, status = place_order_service.place_order_with_auth(
        payload,
        auth_token="mock-token-live",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    assert success is True, f"Expected success=True, got: {response}"
    assert status == 200
    assert response.get("status") == "success", f"Unexpected response: {response}"
    assert "orderid" in response, f"Missing 'orderid' key: {response}"
    assert response["orderid"].startswith("MOCK"), f"Unexpected orderid: {response['orderid']}"
    assert len(state.orders) == 1


# ---------------------------------------------------------------------------
# Test 2 — Broker failure path (500 without "status" key → exception surface)
# ---------------------------------------------------------------------------


def test_p0t1_live_broker_failure(mock_broker_server, _live_patches):
    """Mock broker returns non-200 → service surfaces ``{"status":"error", …}``.

    Sets ``state.token_valid = False`` so the mock broker replies 401 with a
    ``{"detail": ...}`` body that lacks a top-level ``"status"`` key.
    ``place_order_api`` raises ``KeyError`` on ``response_data["status"]``;
    ``place_order_with_auth``'s except block catches it (``logger.exception``
    path) and returns a structured error dict instead of propagating.
    """
    from services import place_order_service

    _, state = _live_patches
    # Make the mock broker reject with a non-standard body (no "status" key).
    state.token_valid = False
    payload = _live_payload()

    success, response, status = place_order_service.place_order_with_auth(
        payload,
        auth_token="mock-token-live",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    assert success is False, f"Expected success=False on broker error, got: {response}"
    assert response.get("status") == "error", f"Expected error status: {response}"
    assert "message" in response, f"Missing 'message' key: {response}"
    assert "internal error" in response["message"].lower(), (
        f"Unexpected message: {response['message']}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Rate-limit: concurrent orders, httpx pooling holds
# ---------------------------------------------------------------------------


def test_p0t1_live_rate_limit_pooling(mock_broker_server, _live_patches):
    """N concurrent BUY orders succeed and all reach ``/orders/regular``.

    Submits 5 orders from separate threads sharing the same httpx connection
    pool. Every order must succeed and the mock broker must record all 5.
    """
    from services import place_order_service

    _, state = _live_patches
    N = 5

    def _place() -> tuple:
        payload = _live_payload()
        return place_order_service.place_order_with_auth(
            payload,
            auth_token="mock-token-live",
            broker="zerodha",
            original_data=payload,
            emit_event=False,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=N) as pool:
        futures = [pool.submit(_place) for _ in range(N)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    successes = [r for r in results if r[0] is True]
    assert len(successes) == N, (
        f"Expected {N} successes, got {len(successes)}; "
        f"failures: {[r for r in results if r[0] is False]}"
    )
    assert len(state.orders) == N, f"Mock broker recorded {len(state.orders)} orders, expected {N}"


# ---------------------------------------------------------------------------
# Test 4 — Symbol mapping: SBIN → SBIN-EQ in broker tradingsymbol
# ---------------------------------------------------------------------------


def test_p0t1_live_symbol_mapping(mock_broker_server, _live_patches):
    """OpenAlgo 'SBIN' translates to Zerodha broker symbol 'SBIN-EQ'.

    Verifies that ``transform_data`` → ``get_br_symbol`` mapping is exercised
    and the mock broker receives ``tradingsymbol=SBIN-EQ`` in the POST body.
    """
    from services import place_order_service

    _, state = _live_patches
    payload = _live_payload()  # symbol="SBIN", exchange="NSE"

    success, response, status = place_order_service.place_order_with_auth(
        payload,
        auth_token="mock-token-live",
        broker="zerodha",
        original_data=payload,
        emit_event=False,
    )

    assert success is True, f"Order placement failed: {response}"
    assert len(state.orders) == 1
    assert state.orders[0]["tradingsymbol"] == "SBIN-EQ", (
        f"Expected tradingsymbol='SBIN-EQ', got: {state.orders[0]['tradingsymbol']!r}"
    )
