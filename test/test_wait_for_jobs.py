"""Tests for ``services.historify_service.wait_for_jobs`` (issue #151).

The helper is the load-bearing piece of the boot_convergence design fix:
without it, the lock releases the millisecond ``create_and_start_job``
submits to the ThreadPoolExecutor, sibling schedulers' writes overlap,
and the in-process DuckDB lock errors recur.

These tests pin down:
- Empty/None inputs return {} (no work to wait for).
- All-terminal jobs return immediately.
- A pending-then-completed job blocks until completion.
- Timeout returns partial state instead of hanging forever.
- A raising ``get_job_status`` is treated as transient — retried, not fatal.
- A missing job_id (404 from get_job_status) is treated as terminal "unknown".
"""

from __future__ import annotations

import time as _time
from unittest.mock import patch

from services import historify_service


def _job_status_response(status: str) -> tuple[bool, dict, int]:
    """Build the tuple shape returned by ``get_job_status`` for a given status."""
    return True, {"status": "success", "job": {"status": status}, "items": []}, 200


def test_empty_input_is_noop():
    assert historify_service.wait_for_jobs([]) == {}
    assert historify_service.wait_for_jobs([None, None]) == {}
    assert historify_service.wait_for_jobs(["", None]) == {}


def test_all_terminal_returns_immediately():
    with patch.object(
        historify_service,
        "get_job_status",
        side_effect=lambda jid: _job_status_response("completed"),
    ):
        started = _time.monotonic()
        result = historify_service.wait_for_jobs(["job-1", "job-2"], poll_sec=0.5)
        elapsed = _time.monotonic() - started

    assert result == {"job-1": "completed", "job-2": "completed"}
    assert elapsed < 0.5, f"should not have slept on already-terminal jobs (took {elapsed:.2f}s)"


def test_pending_then_completed_blocks_until_done():
    """First poll reports running, second reports completed → returns after the
    poll interval. Proves the wait actually blocks until job completion."""
    calls = {"count": 0}

    def fake_status(_jid):
        calls["count"] += 1
        if calls["count"] <= 2:
            return _job_status_response("running")
        return _job_status_response("completed")

    with patch.object(historify_service, "get_job_status", side_effect=fake_status):
        started = _time.monotonic()
        result = historify_service.wait_for_jobs(["job-x"], poll_sec=0.1)
        elapsed = _time.monotonic() - started

    assert result == {"job-x": "completed"}
    # Slept through at least 2 poll intervals (2 * 0.1s = 0.2s) before completing.
    assert elapsed >= 0.2
    assert calls["count"] >= 3


def test_completed_with_errors_is_terminal():
    """``completed_with_errors`` is the partial-failure terminal state;
    don't keep polling on it."""
    with patch.object(
        historify_service,
        "get_job_status",
        side_effect=lambda jid: _job_status_response("completed_with_errors"),
    ):
        result = historify_service.wait_for_jobs(["job-1"], poll_sec=0.5)
    assert result == {"job-1": "completed_with_errors"}


def test_failed_and_cancelled_are_terminal():
    statuses = iter(["failed", "cancelled"])

    def fake_status(_jid):
        return _job_status_response(next(statuses))

    with patch.object(historify_service, "get_job_status", side_effect=fake_status):
        result = historify_service.wait_for_jobs(["job-1", "job-2"], poll_sec=0.5)

    assert set(result.values()) == {"failed", "cancelled"}


def test_timeout_returns_partial_state():
    """A job that never reaches a terminal state must NOT hang the lock-holder
    forever. After timeout we return the live status and let the caller
    proceed."""
    with patch.object(
        historify_service,
        "get_job_status",
        side_effect=lambda jid: _job_status_response("running"),
    ):
        started = _time.monotonic()
        result = historify_service.wait_for_jobs(["forever-job"], timeout_sec=1, poll_sec=0.2)
        elapsed = _time.monotonic() - started

    assert result == {"forever-job": "running"}
    # Spent ~timeout_sec, did not hang.
    assert 0.8 <= elapsed <= 2.5


def test_missing_job_id_is_terminal_unknown():
    """get_job_status returns (False, {"status":"error"}, 404) for an unknown
    job_id — treat as terminal so we don't poll forever on a job that was
    never persisted."""

    def fake_status(_jid):
        return False, {"status": "error", "message": "Job not found"}, 404

    with patch.object(historify_service, "get_job_status", side_effect=fake_status):
        result = historify_service.wait_for_jobs(["ghost-job"], poll_sec=0.5)

    assert result == {"ghost-job": "unknown"}


def test_status_exception_is_transient_retried():
    """A raising get_job_status (DB blip) is logged + retried — not fatal."""
    calls = {"count": 0}

    def fake_status(_jid):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("db blip")
        return _job_status_response("completed")

    with patch.object(historify_service, "get_job_status", side_effect=fake_status):
        result = historify_service.wait_for_jobs(["job-x"], poll_sec=0.1)

    assert result == {"job-x": "completed"}
    assert calls["count"] >= 2


def test_filters_out_none_and_empty():
    """The schedulers pass ``[res['index'].get('job_id'), res['stock'].get('job_id')]``
    where an arm with no work yields None. The helper must drop those and
    still wait on the real job_ids."""
    with patch.object(
        historify_service,
        "get_job_status",
        side_effect=lambda jid: _job_status_response("completed"),
    ):
        result = historify_service.wait_for_jobs([None, "real-job", "", None])

    assert result == {"real-job": "completed"}
