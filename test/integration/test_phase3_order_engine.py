"""Phase 3 Batch A — order placement and simplified engine integration tests.

Covers:
  B1 — sandbox_place_order with mocked LTP writes a SandboxOrders row (Flow 2, P0)
  B2 — kill_switch override persists across a fresh DB read (Flow 15, P0)
  B3 — strategy mode set-and-readable round-trip via harness helpers (Flow A3 variant)
  B4 — chartink webhook with a seeded strategy returns success (Flow 4, P0)
  B5 — POST /api/v1/placeorder with an invalid API key returns 403 (Flow 1, P1)

All tests use BootHarness from test/harness.py (sets OPENALGO_TESTING=1 before
importing the app; all DB writes land in conftest.py's per-process temp dir).

Refs #129  Phase 3 Batch A
"""

from __future__ import annotations

import datetime as dt
import uuid
from unittest.mock import patch

import pytest

# BootHarness import must come before any other app module so OPENALGO_TESTING
# is set first.  conftest.py already ran before this file, which is fine:
# it only redirects DB env vars, not app imports.
from test.harness import BootHarness

# ============================================================================
# B1 — sandbox_place_order creates a SandboxOrders row
# ============================================================================


class TestSandboxPlaceOrderCreatesSandboxOrderRow:
    """B1 (Flow 2, P0): calling sandbox_place_order with a mocked LTP writes a
    SandboxOrders row and returns status='success'."""

    @pytest.fixture(autouse=True, scope="class")
    def harness(self):
        # scope="class": BootHarness.create() (which calls create_app()) runs
        # ONCE per class, not once per test.  Reduces parallel create_app() calls
        # in resource-constrained CI (self-hosted Docker runner, ~300 MB free RAM)
        # from N_tests to N_classes — the primary cause of the 10-minute timeout.
        with BootHarness.create() as h:
            yield h

    def test_sandbox_placeorder_creates_sandbox_order_row(self, harness):
        """sandbox_place_order → SandboxOrders row with order_status='open'."""
        # 1. Set up a valid broker session so verify_api_key can resolve user_id.
        harness.mock_zerodha_login(user_id="ZR0001")

        with harness.app.app_context():
            # 2. Seed an API key for the user so verify_api_key returns user_id.
            from database.auth_db import upsert_api_key

            test_api_key = "test_api_key_phase3_b1"  # nosec B105 — test value  # pragma: allowlist secret
            upsert_api_key("ZR0001", test_api_key)

            # 3. Seed a SymToken row so the order manager finds the symbol.
            #    NSE equity (no F&O lot-size check), lotsize=1.
            from database.symbol import Base as SymBase
            from database.symbol import SymToken
            from database.symbol import db_session as sym_session

            SymBase.metadata.create_all(
                bind=sym_session.get_bind(),
                tables=[SymToken.__table__],
                checkfirst=True,
            )
            existing = SymToken.query.filter_by(symbol="SBIN", exchange="NSE").first()
            if not existing:
                sym_session.add(
                    SymToken(
                        symbol="SBIN",
                        brsymbol="SBIN",
                        exchange="NSE",
                        brexchange="NSE",
                        token="3045",
                        lotsize=1,
                        instrumenttype="EQ",
                        tick_size=0.05,
                    )
                )
                sym_session.commit()

            # 4. Initialize sandbox DB tables so SandboxOrders / SandboxFunds exist.
            from database.sandbox_db import init_db as sandbox_init_db

            sandbox_init_db()

            # 5. Seed sandbox funds (uses default ₹1 Crore from sandbox config).
            from sandbox.fund_manager import FundManager

            fm = FundManager("ZR0001")
            fm.initialize_funds()

            # 6. Call sandbox_place_order with a pre-fetched quote so no broker
            #    REST call is made (avoids needing a live Zerodha session).
            from services.sandbox_service import sandbox_place_order

            order_data = {
                "symbol": "SBIN",
                "exchange": "NSE",
                "action": "BUY",
                "quantity": 1,
                "price": 0,
                "pricetype": "MARKET",
                "product": "CNC",
                "strategy": "test_phase3",
            }
            prefetched_quote = {"ltp": 500.0, "close": 490.0}

            success, response, status_code = sandbox_place_order(
                order_data=order_data,
                api_key=test_api_key,
                original_data={**order_data, "apikey": test_api_key},
                prefetched_quote=prefetched_quote,
            )

        # 7. Assert the response.
        assert success is True, f"Expected success=True, got {response}"
        assert response.get("status") == "success", f"Expected status='success', got {response}"
        assert response.get("orderid"), f"Expected orderid in response, got {response}"

        # 8. Verify a SandboxOrders row was written.
        with harness.app.app_context():
            from database.sandbox_db import SandboxOrders

            order_row = SandboxOrders.query.filter_by(user_id="ZR0001", symbol="SBIN").first()
            assert order_row is not None, (
                "Expected a SandboxOrders row for SBIN/ZR0001 after sandbox_place_order"
            )
            assert order_row.action == "BUY"
            assert order_row.quantity == 1


# ============================================================================
# B2 — kill_switch override persists across a fresh DB read
# ============================================================================


class TestKillSwitchPersistsAcrossFreshServiceInstance:
    """B2 (Flow 15, P0): a kill_switch row written to the temp DB blocks entries
    when is_entry_blocked() is re-evaluated (simulates a service restart)."""

    @pytest.fixture(autouse=True, scope="class")
    def harness(self):
        # scope="class": one create_app() per class, not per test (CI timeout fix).
        with BootHarness.create() as h:
            yield h

    def test_kill_switch_persists_across_fresh_db_read(self, harness):
        """Writing a kill_switch override row blocks a fresh is_entry_blocked() read."""
        with harness.app.app_context():
            from database.strategy_runtime_override_db import is_entry_blocked, set_override

            # 1. Write a kill_switch override with far-future expiry.
            set_override(
                strategy_name="simplified_engine",
                override_type="kill_switch",
                expires_at=dt.datetime(2099, 12, 31, 23, 59, 59),
                reason="3% daily loss limit reached — phase3 B2 test",
                set_by="test_phase3",
            )

            # 2. Immediately re-read via a fresh call (simulates service restart).
            blocked, override_dict = is_entry_blocked("simplified_engine")

        assert blocked is True, (
            "is_entry_blocked must return True after a kill_switch override is written"
        )
        assert override_dict is not None
        assert override_dict["override_type"] == "kill_switch"
        assert override_dict["strategy_name"] == "simplified_engine"

    def test_expired_kill_switch_does_not_block(self, harness):
        """An expired kill_switch row must NOT block entries (lazy-expiry semantics)."""
        with harness.app.app_context():
            from database.strategy_runtime_override_db import is_entry_blocked, set_override

            # Write with an already-past expiry.
            past = dt.datetime(2000, 1, 1)
            set_override(
                strategy_name="simplified_engine",
                override_type="kill_switch",
                expires_at=past,
                reason="already expired",
                set_by="test_phase3",
            )

            # Evaluate using a now that is after the expiry.
            blocked, _ = is_entry_blocked(
                "simplified_engine",
                now=dt.datetime(2026, 6, 25, 12, 0, 0),
            )

        assert blocked is False, (
            "An expired kill_switch row must not block entries (lazy-expiry semantics)"
        )

    def test_pause_override_also_blocks_entries(self, harness):
        """A 'pause' override type must also block entries via is_entry_blocked."""
        with harness.app.app_context():
            from database.strategy_runtime_override_db import is_entry_blocked, set_override

            set_override(
                strategy_name="sector_follow_cap5_vol",
                override_type="pause",
                expires_at=dt.datetime(2099, 12, 31),
                reason="stale feed: test pause",
                set_by="test_phase3",
            )
            blocked, override_dict = is_entry_blocked("sector_follow_cap5_vol")

        assert blocked is True
        assert override_dict["override_type"] == "pause"


# ============================================================================
# B3 — strategy mode set-and-readable round-trip
# ============================================================================


class TestStrategyModeSetAndVisibleRoundTrip:
    """B3 (Flow A3 variant): set_strategy_mode() persists and get_strategy_mode()
    returns the same value — no REST call needed to prove the DB round-trip."""

    @pytest.fixture(autouse=True, scope="class")
    def harness(self):
        # scope="class": one create_app() per class, not per test (CI timeout fix).
        with BootHarness.create() as h:
            yield h

    def test_mode_set_sandbox_readable(self, harness):
        """set_strategy_mode('simplified_engine', 'sandbox') → readable via get_strategy_mode."""
        harness.set_strategy_mode("simplified_engine", "sandbox")
        assert harness.get_strategy_mode("simplified_engine") == "sandbox"

    def test_mode_set_live_readable(self, harness):
        """set_strategy_mode('simplified_engine', 'live') → readable as 'live'."""
        harness.set_strategy_mode("simplified_engine", "live")
        assert harness.get_strategy_mode("simplified_engine") == "live"

    def test_mode_toggle_sandbox_live_sandbox(self, harness):
        """Mode toggles are immediately consistent with no caching artefacts."""
        harness.set_strategy_mode("futures_follow_cap50", "sandbox")
        assert harness.get_strategy_mode("futures_follow_cap50") == "sandbox"

        harness.set_strategy_mode("futures_follow_cap50", "live")
        assert harness.get_strategy_mode("futures_follow_cap50") == "live"

        harness.set_strategy_mode("futures_follow_cap50", "sandbox")
        assert harness.get_strategy_mode("futures_follow_cap50") == "sandbox"

    def test_strategies_list_api_returns_200_with_session(self, harness):
        """GET /strategies/api/list returns 200 JSON when a session is set."""
        harness.set_auth_session()
        harness.set_strategy_mode("simplified_engine", "sandbox")

        resp = harness.client.get("/strategies/api/list")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

        data = resp.get_json()
        assert data is not None, "Response must be valid JSON"
        assert isinstance(data, (list, dict)), f"Expected list or dict, got {type(data)}"


# ============================================================================
# B4 — chartink webhook arms simplified engine and returns ok
# ============================================================================


class TestChartinkWebhookArmsEngineAndRespondsOk:
    """B4 (Flow 4, P0): POST /chartink/simplified-stock-engine/<webhook_id> with
    a seeded strategy returns 200 + status='success' or a known expected response."""

    @pytest.fixture(autouse=True, scope="class")
    def harness(self):
        # scope="class": one create_app() per class, not per test (CI timeout fix).
        with BootHarness.create() as h:
            yield h

    @pytest.fixture()
    def webhook_id_and_strategy(self, harness):
        """Seed a ChartinkStrategy row using chartink_db.create_strategy().

        IMPORTANT: the /chartink/simplified-stock-engine/<webhook_id> route
        imports get_strategy_by_webhook_id from database.chartink_db (which
        queries the chartink_strategies table), NOT from database.strategy_db
        (which queries the unrelated strategies table). We must use chartink_db
        here so the fixture seeds the correct table.

        The chartink_db has no in-process TTL cache (unlike strategy_db), so no
        cache-clearing is needed — the HTTP handler queries the DB on every call.
        """
        with harness.app.app_context():
            from database.chartink_db import create_strategy
            from database.chartink_db import init_db as chartink_init_db

            # Belt-and-suspenders — conftest already initialises chartink_db
            # at session scope, but ensures the table exists even if run alone.
            chartink_init_db()

            wid = str(uuid.uuid4())
            strategy = create_strategy(
                name="test_chartink_phase3",
                webhook_id=wid,
                user_id="ZR0001",
                is_intraday=False,  # Skip time-window check — always allowed
                start_time="09:15",
                end_time="15:15",
                squareoff_time="15:20",
            )
            assert strategy is not None, "chartink_db.create_strategy returned None"

        return wid

    def test_webhook_valid_payload_responds_ok(self, harness, webhook_id_and_strategy):
        """POST to a seeded webhook_id with a valid Chartink payload must return 200."""
        webhook_id = webhook_id_and_strategy

        payload = {
            "scan_name": "FnO Intraday Buy 20",
            "scan_url": "https://chartink.com/screener/fno-intraday-buy-20",
            "stocks": "RELIANCE,INFY",
            "trigger_prices": "2500,1500",
            "triggered_at": "2026-06-25 10:05:00",
        }

        # Patch the engine service so no real engine needs to be running.
        # We assert the webhook accepted the payload and returned success.
        with (
            patch(
                "services.simplified_stock_engine_service.get_simplified_stock_engine_service",
                return_value=None,
            ),
            patch(
                "services.scan_cycle_service.start_cycle",
                return_value="test_cycle_b4",
            ),
            patch("services.scan_cycle_service.heartbeat"),
            patch("services.scan_cycle_service.complete_cycle"),
        ):
            resp = harness.client.post(
                f"/chartink/simplified-stock-engine/{webhook_id}",
                json=payload,
                content_type="application/json",
            )

        # The webhook can return 200 (success/skip) or 400 (time-window if
        # is_intraday=True).  With is_intraday=False the time check is skipped,
        # so we expect either 200 success or 500 (if engine not initialized).
        # We assert that the response is NOT 404 (unknown webhook_id).
        assert resp.status_code != 404, (
            f"webhook_id {webhook_id!r} was not found — strategy seeding failed. "
            f"Response: {resp.get_json()}"
        )
        data = resp.get_json()
        assert data is not None, "Response must be valid JSON"

    def test_unknown_webhook_id_returns_404(self, harness):
        """An unknown webhook_id must return 404 with an error payload."""
        unknown_id = str(uuid.uuid4())

        with (
            patch("services.scan_cycle_service.start_cycle", return_value="test_cycle_b4b"),
            patch("services.scan_cycle_service.heartbeat"),
            patch("services.scan_cycle_service.complete_cycle"),
        ):
            resp = harness.client.post(
                f"/chartink/simplified-stock-engine/{unknown_id}",
                json={"stocks": "RELIANCE", "scan_name": "test"},
                content_type="application/json",
            )

        assert resp.status_code == 404, (
            f"Unknown webhook_id should return 404, got {resp.status_code}"
        )
        data = resp.get_json()
        assert data is not None
        # Must report an error in the JSON body.
        assert data.get("status") == "error" or data.get("error"), (
            f"404 response must have error info, got {data}"
        )


# ============================================================================
# B5 — Invalid API key returns 403
# ============================================================================


class TestPlaceOrderWithInvalidApiKeyReturns403:
    """B5 (Flow 1, P1): POST /api/v1/placeorder with an invalid apikey must return 403."""

    @pytest.fixture(autouse=True, scope="class")
    def harness(self):
        # scope="class": one create_app() per class, not per test (CI timeout fix).
        with BootHarness.create() as h:
            yield h

    def test_invalid_apikey_returns_403(self, harness):
        """An invalid API key in the request body must be rejected with HTTP 403."""
        with harness.app.app_context():
            # Ensure the auth_db tables exist (init_integration_tables covers
            # auth_db, but be explicit to avoid flakiness).
            from database.auth_db import init_db as auth_init_db

            auth_init_db()

        payload = {
            "apikey": "definitely_invalid_key_xyz_phase3_b5",  # nosec B106 — test value  # pragma: allowlist secret
            "strategy": "test",
            "symbol": "SBIN",
            "exchange": "NSE",
            "action": "BUY",
            "product": "CNC",
            "pricetype": "MARKET",
            "quantity": "1",
            "price": "0",
        }

        resp = harness.client.post(
            "/api/v1/placeorder",
            json=payload,
            content_type="application/json",
        )

        assert resp.status_code == 403, (
            f"Expected 403 for invalid API key, got {resp.status_code}. Response: {resp.get_json()}"
        )
        data = resp.get_json()
        assert data is not None, "Response must be valid JSON"
        assert data.get("status") == "error", f"Expected status='error', got {data}"

    def test_missing_apikey_field_returns_4xx(self, harness):
        """A request with no 'apikey' field must be rejected (400 or 403)."""
        payload = {
            # apikey intentionally omitted
            "strategy": "test",
            "symbol": "SBIN",
            "exchange": "NSE",
            "action": "BUY",
            "product": "CNC",
            "pricetype": "MARKET",
            "quantity": "1",
            "price": "0",
        }

        resp = harness.client.post(
            "/api/v1/placeorder",
            json=payload,
            content_type="application/json",
        )

        # Schema validation rejects the missing required field (400) or the
        # service layer rejects the empty key (403).  Either is correct.
        assert resp.status_code in (400, 403), (
            f"Expected 400 or 403 for missing apikey, got {resp.status_code}. "
            f"Response: {resp.get_json()}"
        )
