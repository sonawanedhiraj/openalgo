"""Tier 2 of #58 — boot broker-session lifecycle tests for backfill schedulers.

PR #56 fixed the #55 root cause (boot backfill firing against a dead Zerodha daily
token → 200+ 401s in errors.jsonl). The fix is ``is_live_broker_session()`` in
``services.broker_session_health``, consumed by ``_wait_for_broker_session()`` in both
``scanner_backfill_scheduler`` and ``sector_follow_backfill_scheduler``.

These 8 tests (4 per scheduler) confirm the fix holds and cannot silently regress.

Mock seams
----------
- ``services.broker_session_health.is_live_broker_session`` — controls what the probe
  returns without touching the DB or a real broker.  The function is locally imported
  inside ``_wait_for_broker_session`` at call time, so patching the attribute on the
  source module is the correct target.
- For cases where the probe returns False or raises, the side-effect also sets the
  module-level ``_stop_event`` so the polling loop exits on the first iteration
  instead of sleeping 15 s between retries.
- ``run_boot_backfill_checks`` is patched to a no-op MagicMock so we can assert
  call/no-call without triggering any real DuckDB or broker work.
- ``start_periodic_backfill_check`` is patched to a no-op so no background daemon
  threads are started during tests.

``test/conftest.py`` handles DB isolation globally — no extra fixtures needed here.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Module-path constants
# ---------------------------------------------------------------------------

_MOD_SCAN = "services.scanner_backfill_scheduler"
_MOD_SF = "services.sector_follow_backfill_scheduler"
_MOD_HEALTH = "services.broker_session_health"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _dead_session_side_effect(stop_event):
    """Side effect for is_live_broker_session that returns False and exits the loop.

    Sets the module-level _stop_event before returning so the polling loop in
    _wait_for_broker_session exits on the first iteration (avoids the 15 s poll
    sleep in unit tests).
    """

    def _se():
        stop_event.set()
        return False

    return _se


def _raising_side_effect(stop_event):
    """Side effect that raises RuntimeError and sets the stop_event.

    The exception is caught by _wait_for_broker_session's try/except.  Setting
    the event ensures the loop also exits fast (no 15 s wait between retries).
    """

    def _se():
        stop_event.set()
        raise RuntimeError("broker probe failed — network blip")

    return _se


# ---------------------------------------------------------------------------
# Scanner backfill scheduler — 4 cases
# ---------------------------------------------------------------------------


class TestScannerBackfillBootSession:
    """Boot-path tests for services.scanner_backfill_scheduler._boot_worker."""

    def setup_method(self):
        import services.scanner_backfill_scheduler as sbs

        sbs._stop_event.clear()

    def teardown_method(self):
        import services.scanner_backfill_scheduler as sbs

        sbs._stop_event.clear()

    def test_boot_with_valid_token_runs_backfill(self):
        """Live broker session → _boot_worker must call run_boot_backfill_checks."""
        import services.scanner_backfill_scheduler as sbs

        with (
            patch(f"{_MOD_HEALTH}.is_live_broker_session", return_value=True),
            patch(f"{_MOD_SCAN}.run_boot_backfill_checks") as mock_boot,
            patch(f"{_MOD_SCAN}.start_periodic_backfill_check"),
        ):
            sbs._stop_event.clear()
            sbs._boot_worker()

        assert mock_boot.call_count == 1, (
            "run_boot_backfill_checks must be called when the broker session is live"
        )

    def test_boot_with_dead_daily_token_skips_backfill(self):
        """#55 case: auth row exists but daily token is dead → MUST NOT call backfill."""
        import services.scanner_backfill_scheduler as sbs

        sbs._stop_event.clear()
        with (
            patch(
                f"{_MOD_HEALTH}.is_live_broker_session",
                side_effect=_dead_session_side_effect(sbs._stop_event),
            ),
            patch(f"{_MOD_SCAN}.run_boot_backfill_checks") as mock_boot,
            patch(f"{_MOD_SCAN}.start_periodic_backfill_check"),
        ):
            sbs._boot_worker()

        assert mock_boot.call_count == 0, (
            "MUST NOT run backfill when the daily token is dead (the #55 class)"
        )

    def test_boot_with_no_auth_row_skips_backfill(self):
        """No auth row at all → is_live_broker_session returns False → backfill skipped."""
        import services.scanner_backfill_scheduler as sbs

        sbs._stop_event.clear()
        with (
            patch(
                f"{_MOD_HEALTH}.is_live_broker_session",
                side_effect=_dead_session_side_effect(sbs._stop_event),
            ),
            patch(f"{_MOD_SCAN}.run_boot_backfill_checks") as mock_boot,
            patch(f"{_MOD_SCAN}.start_periodic_backfill_check"),
        ):
            sbs._boot_worker()

        assert mock_boot.call_count == 0, "MUST NOT run backfill when there is no auth row"

    def test_boot_with_probe_exception_skips_backfill(self):
        """Probe raises (network blip) → no propagation up, no backfill."""
        import services.scanner_backfill_scheduler as sbs

        sbs._stop_event.clear()
        with (
            patch(
                f"{_MOD_HEALTH}.is_live_broker_session",
                side_effect=_raising_side_effect(sbs._stop_event),
            ),
            patch(f"{_MOD_SCAN}.run_boot_backfill_checks") as mock_boot,
            patch(f"{_MOD_SCAN}.start_periodic_backfill_check"),
        ):
            sbs._boot_worker()  # MUST NOT raise

        assert mock_boot.call_count == 0, "MUST NOT run backfill when the session probe raises"


# ---------------------------------------------------------------------------
# Sector-follow backfill scheduler — 4 cases (mirrors scanner tests above)
# ---------------------------------------------------------------------------


class TestSectorFollowBackfillBootSession:
    """Boot-path tests for services.sector_follow_backfill_scheduler._boot_worker."""

    def setup_method(self):
        import services.sector_follow_backfill_scheduler as sfbs

        sfbs._stop_event.clear()

    def teardown_method(self):
        import services.sector_follow_backfill_scheduler as sfbs

        sfbs._stop_event.clear()

    def test_boot_with_valid_token_runs_backfill(self):
        """Live broker session → _boot_worker must call run_boot_backfill_checks."""
        import services.sector_follow_backfill_scheduler as sfbs

        with (
            patch(f"{_MOD_HEALTH}.is_live_broker_session", return_value=True),
            patch(f"{_MOD_SF}.run_boot_backfill_checks") as mock_boot,
            patch(f"{_MOD_SF}.start_periodic_backfill_check"),
        ):
            sfbs._stop_event.clear()
            sfbs._boot_worker()

        assert mock_boot.call_count == 1, (
            "run_boot_backfill_checks must be called when the broker session is live"
        )

    def test_boot_with_dead_daily_token_skips_backfill(self):
        """#55 case: auth row exists but daily token is dead → MUST NOT call backfill."""
        import services.sector_follow_backfill_scheduler as sfbs

        sfbs._stop_event.clear()
        with (
            patch(
                f"{_MOD_HEALTH}.is_live_broker_session",
                side_effect=_dead_session_side_effect(sfbs._stop_event),
            ),
            patch(f"{_MOD_SF}.run_boot_backfill_checks") as mock_boot,
            patch(f"{_MOD_SF}.start_periodic_backfill_check"),
        ):
            sfbs._boot_worker()

        assert mock_boot.call_count == 0, (
            "MUST NOT run backfill when the daily token is dead (the #55 class)"
        )

    def test_boot_with_no_auth_row_skips_backfill(self):
        """No auth row at all → is_live_broker_session returns False → backfill skipped."""
        import services.sector_follow_backfill_scheduler as sfbs

        sfbs._stop_event.clear()
        with (
            patch(
                f"{_MOD_HEALTH}.is_live_broker_session",
                side_effect=_dead_session_side_effect(sfbs._stop_event),
            ),
            patch(f"{_MOD_SF}.run_boot_backfill_checks") as mock_boot,
            patch(f"{_MOD_SF}.start_periodic_backfill_check"),
        ):
            sfbs._boot_worker()

        assert mock_boot.call_count == 0, "MUST NOT run backfill when there is no auth row"

    def test_boot_with_probe_exception_skips_backfill(self):
        """Probe raises (network blip) → no propagation up, no backfill."""
        import services.sector_follow_backfill_scheduler as sfbs

        sfbs._stop_event.clear()
        with (
            patch(
                f"{_MOD_HEALTH}.is_live_broker_session",
                side_effect=_raising_side_effect(sfbs._stop_event),
            ),
            patch(f"{_MOD_SF}.run_boot_backfill_checks") as mock_boot,
            patch(f"{_MOD_SF}.start_periodic_backfill_check"),
        ):
            sfbs._boot_worker()  # MUST NOT raise

        assert mock_boot.call_count == 0, "MUST NOT run backfill when the session probe raises"
