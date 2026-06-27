"""Regression tests for issue #154 — backfill check_and_refresh_if_stale
must propagate the submitted job_id to the caller.

Without this propagation, boot_convergence's wait_for_jobs (PR #152) sees
job_id=None for every arm, filters them, and returns {} immediately — the
lock then releases while the 5-worker pool is still mid-download (the exact
bug PR #152 was meant to fix).

``compute_stale_symbols`` is imported lazily inside each
``check_and_refresh_if_stale`` so we patch it at the source module
(``services.data_freshness_service``), not the importer.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from services import (
    scanner_universe_backfill,
    sector_follow_index_backfill,
    sector_follow_stock_backfill,
)


def test_sector_follow_index_propagates_job_id():
    """When backfill submits a job, check_and_refresh_if_stale must include
    its job_id in the returned dict so the caller can wait on it."""
    with (
        patch.object(
            sector_follow_index_backfill,
            "compute_stale_symbols",
            return_value=(["NIFTY", "BANKNIFTY"], [], {}),
        ),
        patch.object(
            sector_follow_index_backfill,
            "backfill_sector_indices",
            return_value={"status": "success", "job_id": "test-job-abc", "symbols": []},
        ),
    ):
        result = sector_follow_index_backfill.check_and_refresh_if_stale(date(2026, 6, 26))

    assert result.get("job_id") == "test-job-abc"
    assert result["status"] == "ok"
    assert result["refreshed"] == ["NIFTY", "BANKNIFTY"]


def test_sector_follow_index_no_job_id_when_fresh():
    """A fresh feed doesn't submit a job — result must NOT carry a stale
    job_id key (the caller would attempt to wait on a non-existent job)."""
    with patch.object(
        sector_follow_index_backfill,
        "compute_stale_symbols",
        return_value=([], ["NIFTY"], {}),
    ):
        result = sector_follow_index_backfill.check_and_refresh_if_stale(date(2026, 6, 26))

    assert "job_id" not in result
    assert result["stale_symbols"] == []


def test_sector_follow_stock_propagates_job_id():
    with (
        patch.object(
            sector_follow_stock_backfill,
            "compute_stale_symbols",
            return_value=(["INFY", "TCS"], [], {}),
        ),
        patch.object(
            sector_follow_stock_backfill,
            "backfill_sector_follow_stocks",
            return_value={"status": "success", "job_id": "stock-job-xyz", "symbols": []},
        ),
    ):
        result = sector_follow_stock_backfill.check_and_refresh_if_stale(date(2026, 6, 26))

    assert result.get("job_id") == "stock-job-xyz"
    assert result["refreshed"] == ["INFY", "TCS"]


def test_sector_follow_stock_no_job_id_when_fresh():
    with patch.object(
        sector_follow_stock_backfill,
        "compute_stale_symbols",
        return_value=([], ["INFY"], {}),
    ):
        result = sector_follow_stock_backfill.check_and_refresh_if_stale(date(2026, 6, 26))
    assert "job_id" not in result


def test_scanner_universe_propagates_job_id_per_interval():
    # Patch scanner_universe_symbols so the test doesn't depend on SCANNER_SYMBOLS
    # env being set (CI does not load .env; locally it does). Without this the
    # impl early-returns 'empty universe' before reaching the patched backfill.
    with (
        patch.object(
            scanner_universe_backfill, "scanner_universe_symbols", return_value=["RELIANCE"]
        ),
        patch.object(
            scanner_universe_backfill,
            "compute_stale_symbols",
            return_value=(["RELIANCE"], [], {}),
        ),
        patch.object(
            scanner_universe_backfill,
            "backfill_scanner_universe",
            return_value={"status": "success", "job_id": "scanner-job-1m", "symbols": []},
        ),
    ):
        result = scanner_universe_backfill.check_and_refresh_if_stale(
            date(2026, 6, 26), interval="1m"
        )

    assert result.get("job_id") == "scanner-job-1m"


def test_scanner_universe_no_job_id_when_fresh():
    with (
        patch.object(
            scanner_universe_backfill, "scanner_universe_symbols", return_value=["RELIANCE"]
        ),
        patch.object(
            scanner_universe_backfill,
            "compute_stale_symbols",
            return_value=([], ["RELIANCE"], {}),
        ),
    ):
        result = scanner_universe_backfill.check_and_refresh_if_stale(
            date(2026, 6, 26), interval="1m"
        )
    assert "job_id" not in result


def test_scanner_universe_no_job_id_when_backfill_returns_ok_noop():
    """The empty-universe `status="ok"` path doesn't submit a job — no job_id
    in result."""
    with (
        patch.object(scanner_universe_backfill, "scanner_universe_symbols", return_value=["X"]),
        patch.object(
            scanner_universe_backfill,
            "compute_stale_symbols",
            return_value=(["X"], [], {}),
        ),
        patch.object(
            scanner_universe_backfill,
            "backfill_scanner_universe",
            return_value={"status": "ok", "symbols": []},
        ),
    ):
        result = scanner_universe_backfill.check_and_refresh_if_stale(
            date(2026, 6, 26), interval="1m"
        )
    assert "job_id" not in result


def test_scanner_universe_no_job_id_on_backfill_error():
    """A failed backfill carries no job_id — wait_for_jobs would have nothing
    real to poll."""
    with (
        patch.object(scanner_universe_backfill, "scanner_universe_symbols", return_value=["X"]),
        patch.object(
            scanner_universe_backfill,
            "compute_stale_symbols",
            return_value=(["X"], [], {}),
        ),
        patch.object(
            scanner_universe_backfill,
            "backfill_scanner_universe",
            return_value={"status": "error", "message": "broker token expired"},
        ),
    ):
        result = scanner_universe_backfill.check_and_refresh_if_stale(
            date(2026, 6, 26), interval="1m"
        )
    assert "job_id" not in result
    assert result["status"] == "error"
