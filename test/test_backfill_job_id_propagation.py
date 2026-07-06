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
    its job_id in the returned dict so the caller can wait on it.

    Issue #313: compute_stale_symbols is now read twice — the initial
    stale-check plus the post-job verification — so the second read must show
    the symbols advanced for them to count as refreshed."""
    with (
        patch.object(
            sector_follow_index_backfill,
            "compute_stale_symbols",
            side_effect=[
                (["NIFTY", "BANKNIFTY"], [], {}),  # initial stale-check
                ([], ["NIFTY", "BANKNIFTY"], {}),  # post-job verification: advanced
            ],
        ),
        patch.object(
            sector_follow_index_backfill,
            "backfill_sector_indices",
            return_value={"status": "success", "job_id": "test-job-abc", "symbols": []},
        ),
        patch(
            "services.historify_service.wait_for_jobs",
            return_value={"test-job-abc": "completed"},
        ),
    ):
        result = sector_follow_index_backfill.check_and_refresh_if_stale(date(2026, 6, 26))

    assert result.get("job_id") == "test-job-abc"
    assert result["status"] == "ok"
    assert result["refreshed"] == ["NIFTY", "BANKNIFTY"]
    assert result["still_stale"] == []


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
            side_effect=[
                (["INFY", "TCS"], [], {}),  # initial stale-check
                ([], ["INFY", "TCS"], {}),  # post-job verification: advanced (#313)
            ],
        ),
        patch.object(
            sector_follow_stock_backfill,
            "backfill_sector_follow_stocks",
            return_value={"status": "success", "job_id": "stock-job-xyz", "symbols": []},
        ),
        patch(
            "services.historify_service.wait_for_jobs",
            return_value={"stock-job-xyz": "completed"},
        ),
    ):
        result = sector_follow_stock_backfill.check_and_refresh_if_stale(date(2026, 6, 26))

    assert result.get("job_id") == "stock-job-xyz"
    assert result["refreshed"] == ["INFY", "TCS"]
    assert result["still_stale"] == []


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


# --------------------------------------------------------------------------- #
# Issue #193 — convergence schedulers must pass the INCREMENTAL window to
# the backfill helper, not a fixed [today - LOOKBACK, today]. These tests
# would have failed on the pre-#193 ``start = ref - timedelta(days=LOOKBACK)``
# line; they pass with the ``compute_incremental_start_date`` helper.
# --------------------------------------------------------------------------- #


def test_sector_follow_stock_passes_incremental_window_when_friday_data_present():
    """Sunday boot, every stale stock holds Friday's bars → catch-up window is
    Sat..Sun (start = Fri + 1 day), not the pre-fix Wed..Sun (today - 4 days)."""
    captured: dict = {}

    def fake_backfill(start, end, symbols=None):
        captured["start"] = start
        captured["end"] = end
        return {"status": "success", "job_id": "test-job", "symbols": symbols}

    details = {
        "INFY": {"last_date": "2026-06-26"},  # Friday
        "TCS": {"last_date": "2026-06-26"},  # Friday
    }
    with (
        patch.object(
            sector_follow_stock_backfill,
            "compute_stale_symbols",
            side_effect=[
                (["INFY", "TCS"], [], details),  # initial stale-check
                ([], ["INFY", "TCS"], {}),  # post-job verification (#313)
            ],
        ),
        patch.object(
            sector_follow_stock_backfill,
            "backfill_sector_follow_stocks",
            side_effect=fake_backfill,
        ),
        patch("services.historify_service.wait_for_jobs", return_value={"test-job": "completed"}),
    ):
        sector_follow_stock_backfill.check_and_refresh_if_stale(date(2026, 6, 28))

    assert captured["start"] == "2026-06-27", (
        f"expected incremental start = Fri + 1day = 2026-06-27, got {captured['start']}"
    )
    assert captured["end"] == "2026-06-28"


def test_sector_follow_stock_falls_back_to_full_lookback_when_no_stored_data():
    """A symbol with no stored bars MUST trigger the LOOKBACK floor so the
    first-time fetch is wide enough. (Helper docstring: per-symbol mixed
    windows would need an API change to the backfill helpers.)"""
    captured: dict = {}

    def fake_backfill(start, end, symbols=None):
        captured["start"] = start
        captured["end"] = end
        return {"status": "success", "job_id": "test-job", "symbols": symbols}

    details = {"NEW_STOCK": {"last_date": None}}
    with (
        patch.object(
            sector_follow_stock_backfill,
            "compute_stale_symbols",
            side_effect=[
                (["NEW_STOCK"], [], details),  # initial stale-check
                ([], ["NEW_STOCK"], {}),  # post-job verification (#313)
            ],
        ),
        patch.object(
            sector_follow_stock_backfill,
            "backfill_sector_follow_stocks",
            side_effect=fake_backfill,
        ),
        patch("services.historify_service.wait_for_jobs", return_value={"test-job": "completed"}),
    ):
        sector_follow_stock_backfill.check_and_refresh_if_stale(date(2026, 6, 28))

    # 2026-06-28 minus 4-day lookback = 2026-06-24
    assert captured["start"] == "2026-06-24"


def test_sector_follow_index_passes_incremental_window_when_friday_data_present():
    captured: dict = {}

    def fake_backfill(start, end, symbols=None):
        captured["start"] = start
        captured["end"] = end
        return {"status": "success", "job_id": "test-job", "symbols": symbols}

    details = {"NIFTY": {"last_date": "2026-06-26"}}
    with (
        patch.object(
            sector_follow_index_backfill,
            "compute_stale_symbols",
            side_effect=[
                (["NIFTY"], [], details),  # initial stale-check
                ([], ["NIFTY"], {}),  # post-job verification (#313)
            ],
        ),
        patch.object(
            sector_follow_index_backfill,
            "backfill_sector_indices",
            side_effect=fake_backfill,
        ),
        patch("services.historify_service.wait_for_jobs", return_value={"test-job": "completed"}),
    ):
        sector_follow_index_backfill.check_and_refresh_if_stale(date(2026, 6, 28))

    assert captured["start"] == "2026-06-27"
    assert captured["end"] == "2026-06-28"


def test_scanner_universe_passes_incremental_window_per_interval():
    """The scanner helper's lookback varies by interval (``_LOOKBACK_DAYS``);
    the incremental computation must still honor it as the cap."""
    captured: dict = {}

    def fake_backfill(start, end, interval=None, symbols=None):
        captured["start"] = start
        captured["end"] = end
        captured["interval"] = interval
        return {"status": "success", "job_id": "scanner-1m", "symbols": symbols}

    details = {"RELIANCE": {"last_date": "2026-06-26"}}
    with (
        patch.object(
            scanner_universe_backfill, "scanner_universe_symbols", return_value=["RELIANCE"]
        ),
        patch.object(
            scanner_universe_backfill,
            "compute_stale_symbols",
            return_value=(["RELIANCE"], [], details),
        ),
        patch.object(
            scanner_universe_backfill,
            "backfill_scanner_universe",
            side_effect=fake_backfill,
        ),
    ):
        scanner_universe_backfill.check_and_refresh_if_stale(date(2026, 6, 28), interval="1m")

    assert captured["start"] == "2026-06-27"
    assert captured["end"] == "2026-06-28"
    assert captured["interval"] == "1m"
