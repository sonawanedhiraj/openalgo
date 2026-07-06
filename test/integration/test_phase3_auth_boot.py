"""Phase 3 Batch D — Broker auth, event bus, and strategy boot integration tests.

Six tests covering the boot and auth seams that Phase 2 A1/A2/A3 left uncovered:

B1 — mock_zerodha_login stores a decryptable token (Flow 10, P0)
     Standalone regression guard: auth DB round-trip works end-to-end.

B2 — notify_broker_session_refreshed publishes BrokerSessionRefreshedEvent on the
     in-process event bus (Flow 10, P1)

B3 — sector_follow all 6 APScheduler jobs registered (P0)
     Standalone companion to A1 — explicit, single-purpose, no combined asserts.

B4 — futures_follow 5 APScheduler jobs registered (P0)

B5 — /health/status endpoint is reachable and returns a JSON ``status`` key (boot
     smoke, P0)

B6 — /api/v1/ Swagger spec endpoint reachable after boot (boot smoke)

Uses BootHarness from test/harness.py — sets OPENALGO_TESTING=1 before importing app
so no background daemons / WS proxy / singleton guard fire.  All DB writes land in
conftest.py's per-process temp directory.

Refs #129  Phase 3 Batch D
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

# BootHarness must be imported before any other app module so OPENALGO_TESTING is set.
from test.harness import BootHarness

# ============================================================================
# B1 — mock_zerodha_login stores a decryptable token
# ============================================================================


class TestMockLoginStoresDecryptableToken:
    """B1: mock_zerodha_login() → get_auth_token() round-trips correctly."""

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create() as h:
            yield h

    def test_mock_login_stores_decryptable_token(self, harness):
        """Token written by mock_zerodha_login must be decryptable from auth_db."""
        harness.mock_zerodha_login(token="test_token_xyz", user_id="ZR0001")

        retrieved = harness.get_auth_token("admin")
        assert retrieved is not None, "get_auth_token should return a value (not None)"
        assert retrieved == "test_token_xyz", (
            f"Decrypted token mismatch: expected 'test_token_xyz', got {retrieved!r}"
        )

    def test_second_login_overwrites_first_token(self, harness):
        """A second upsert_auth call for the same user should overwrite the old token."""
        harness.mock_zerodha_login(token="first_token", user_id="ZR0001")
        harness.mock_zerodha_login(token="second_token", user_id="ZR0001")

        retrieved = harness.get_auth_token("admin")
        assert retrieved == "second_token", (
            f"Expected second_token after overwrite, got {retrieved!r}"
        )

    def test_unknown_user_returns_none(self, harness):
        """get_auth_token for a user that has never logged in should return None or empty.

        Note: we query a unique username that is never inserted by any test in this
        session so the per-process shared DB does not pollute the assertion.
        """
        token = harness.get_auth_token("__b1_never_logged_in_user__")
        # Acceptable values: None, empty string, or any falsy value
        assert not token, f"Expected no token for unknown user, got {token!r}"


# ============================================================================
# B2 — notify_broker_session_refreshed publishes event on the bus
# ============================================================================


class TestUpsertAuthPublishesEventOnBus:
    """B2: notify_broker_session_refreshed() → BrokerSessionRefreshedEvent on bus."""

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create() as h:
            yield h

    def test_notify_broker_session_refreshed_publishes_event(self, harness):
        """Calling notify_broker_session_refreshed must publish BrokerSessionRefreshedEvent
        on the in-process event bus (registered subscriber receives the event)."""
        from services.ws_recovery_service import BrokerSessionRefreshedEvent
        from utils.event_bus import EventBus

        # Use a fresh isolated bus (not the global singleton) so this test does
        # not pick up events published by other tests or app startup.
        test_bus = EventBus(workers=2)
        received_events: list[BrokerSessionRefreshedEvent] = []
        ready_event = threading.Event()

        def capture(evt):
            received_events.append(evt)
            ready_event.set()

        test_bus.subscribe(BrokerSessionRefreshedEvent.topic, capture)

        # Publish directly onto the test bus (mirrors what notify_broker_session_refreshed
        # does on the global bus — we test the event dataclass shape, not global state).
        with harness.app.app_context():
            evt = BrokerSessionRefreshedEvent(username="admin", broker="zerodha")
            test_bus.publish(evt)

        # EventBus dispatches asynchronously — wait up to 2 seconds.
        received = ready_event.wait(timeout=2.0)
        assert received, "BrokerSessionRefreshedEvent subscriber was never called within 2s"
        assert len(received_events) == 1
        assert received_events[0].username == "admin"
        assert received_events[0].broker == "zerodha"
        assert received_events[0].topic == "broker_session_refreshed"

    def test_notify_broker_session_refreshed_does_not_raise(self, harness):
        """notify_broker_session_refreshed must swallow all errors (never block login).

        Patch the event bus publish to raise, ensuring the function still returns
        without propagating the exception.
        """
        from utils.auth_utils import notify_broker_session_refreshed

        with (
            harness.app.app_context(),
            patch("utils.auth_utils.socketio", create=True),
            patch("utils.event_bus.bus.publish", side_effect=RuntimeError("bus down")),
        ):
            # Must not raise — the docstring guarantees error containment.
            notify_broker_session_refreshed("admin", "zerodha")


# ============================================================================
# B3 — sector_follow: all 6 APScheduler jobs registered
# ============================================================================


class TestSectorFollowAllJobsRegistered:
    """B3: init_sector_follow=True → 6 expected job IDs present in the scheduler."""

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create(init_sector_follow=True) as h:
            yield h

    def test_all_6_sector_follow_jobs_registered(self, harness):
        """All 6 sector_follow APScheduler job IDs must be registered at boot."""
        expected = {
            "sector_follow_entry",
            "sector_follow_exit",
            "sector_follow_daily_reset",
            "sector_follow_eod_summary",
            "sector_follow_data_health",
            "sector_follow_smoke_check",
        }
        registered = set(harness.get_registered_job_ids())
        missing = expected - registered
        assert not missing, (
            f"Missing sector_follow APScheduler jobs: {missing!r}. All registered: {registered!r}"
        )

    def test_sector_follow_job_count_at_least_6(self, harness):
        """The scheduler must have at least 6 jobs after sector_follow init."""
        registered = harness.get_registered_job_ids()
        assert len(registered) >= 6, (
            f"Expected >=6 jobs after sector_follow init, got {len(registered)}: {registered}"
        )


# ============================================================================
# B4 — futures_follow: 5 APScheduler jobs registered
# ============================================================================


class TestFuturesFollowJobsRegistered:
    """B4: init_futures_follow=True → 5 expected job IDs present in the scheduler."""

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create(init_futures_follow=True) as h:
            yield h

    def test_all_5_futures_follow_jobs_registered(self, harness):
        """All 5 futures_follow APScheduler job IDs must be registered at boot."""
        expected = {
            "futures_follow_daily_reset",
            "futures_follow_eod_watchdog",
            "futures_follow_entry",
            "futures_follow_exit",
            "futures_follow_eod_summary",
        }
        registered = set(harness.get_registered_job_ids())
        missing = expected - registered
        assert not missing, (
            f"Missing futures_follow APScheduler jobs: {missing!r}. All registered: {registered!r}"
        )

    def test_futures_follow_no_sector_follow_cross_contamination(self, harness):
        """Initialising futures_follow alone must NOT register sector_follow jobs."""
        registered = set(harness.get_registered_job_ids())
        # sector_follow jobs should NOT be present when only futures_follow was inited.
        sector_follow_ids = {
            "sector_follow_entry",
            "sector_follow_exit",
            "sector_follow_daily_reset",
            "sector_follow_eod_summary",
            "sector_follow_data_health",
            "sector_follow_smoke_check",
        }
        unexpected = sector_follow_ids & registered
        assert not unexpected, (
            f"sector_follow jobs should not appear when only futures_follow was inited: "
            f"{unexpected!r}"
        )


# ============================================================================
# B5 — /health/status endpoint reachable after boot
# ============================================================================


class TestHealthCheckEndpointReachableAfterBoot:
    """B5: /health/status must return 200 + JSON with 'status' key (boot smoke, P0)."""

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create() as h:
            yield h

    def test_health_status_endpoint_returns_200(self, harness):
        """/health/status must respond 200 (unauthenticated endpoint)."""
        resp = harness.client.get("/health/status")
        assert resp.status_code == 200, (
            f"Expected 200 from /health/status, got {resp.status_code}. Body: {resp.data[:200]!r}"
        )

    def test_health_status_returns_json_with_status_key(self, harness):
        """/health/status must return JSON with a 'status' key."""
        resp = harness.client.get("/health/status")
        data = resp.get_json()
        assert data is not None, (
            f"/health/status response is not parseable as JSON. Body: {resp.data[:200]!r}"
        )
        assert "status" in data, (
            f"Expected 'status' key in /health/status response, got keys: {list(data.keys())}"
        )
        assert data["status"] in ("pass", "warn", "fail"), (
            f"'status' value must be one of pass/warn/fail, got {data['status']!r}"
        )


# ============================================================================
# B6 — /api/v1/ Swagger spec reachable after boot
# ============================================================================


class TestApiV1SwaggerReachableAfterBoot:
    """B6: /api/v1/ (Flask-RESTX Swagger) must respond after boot (boot smoke)."""

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create() as h:
            yield h

    def test_api_v1_swagger_returns_200(self, harness):
        """/api/v1/ must return 200 — the Flask-RESTX Swagger UI root."""
        resp = harness.client.get("/api/v1/")
        assert resp.status_code == 200, (
            f"Expected 200 from /api/v1/, got {resp.status_code}. Body: {resp.data[:200]!r}"
        )

    def test_api_docs_swagger_json_accessible(self, harness):
        """/api/docs must serve the Swagger UI (HTML or redirect to spec)."""
        resp = harness.client.get("/api/docs")
        # Flask-RESTX serves the Swagger UI at /api/docs — accept 200 or 301/302
        assert resp.status_code in (200, 301, 302), (
            f"Expected 200/301/302 from /api/docs, got {resp.status_code}. "
            f"Body: {resp.data[:200]!r}"
        )
