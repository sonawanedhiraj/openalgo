"""Tests for the tick-liveness heartbeat hook in scanner_service (issue #376).

The heartbeat is a one-line side effect in ``_on_bar_close``: a live bar close
stamps a module-level wall-clock timestamp; a replayed bar must NOT. These tests
exercise the module-level accessors directly (no DB / aggregator needed).
"""

from __future__ import annotations

from services import scanner_service


def test_heartbeat_starts_none():
    scanner_service._reset_live_bar_close_for_tests()
    assert scanner_service.get_last_live_bar_close_wall() is None


def test_mark_live_bar_close_sets_wall_time():
    scanner_service._reset_live_bar_close_for_tests()
    scanner_service._mark_live_bar_close()
    ts = scanner_service.get_last_live_bar_close_wall()
    assert ts is not None
    import time

    assert abs(time.time() - ts) < 5.0


def test_replayed_bar_does_not_stamp(monkeypatch):
    """A bar carrying ``is_replay`` must return before the heartbeat stamp so a
    historical replay never makes a dead feed look alive."""
    scanner_service._reset_live_bar_close_for_tests()

    stamped = {"n": 0}
    monkeypatch.setattr(
        scanner_service, "_mark_live_bar_close", lambda: stamped.__setitem__("n", 1)
    )

    svc = scanner_service.ScannerService.__new__(scanner_service.ScannerService)
    # Minimal state for _append_bar / _on_bar_close early-return path.
    import threading

    svc._bar_history = {}
    svc._history_lock = threading.Lock()
    svc.history_size = 100

    svc._on_bar_close("RELIANCE", "5m", {"ts": None, "close": 1.0, "is_replay": True})
    assert stamped["n"] == 0  # replay bar did NOT stamp
