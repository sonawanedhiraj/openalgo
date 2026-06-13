"""Tests for the sector_follow_cap5_vol universe-stock 1m backfill.

Covers the durable fix for the manual-backfill gap that held all entries on
2026-06-12 (every universe stock 2 business days stale):

  * the service iterates the full locked-static-30 universe and hands the historify
    job pipeline ``exchange='NSE'``, ``interval='1m'``, ``incremental=True``;
  * the scheduled job is registered on the ``10 16 * * mon-fri`` (16:10 IST) cron.

Mocked end-to-end — ``create_and_start_job`` is patched so no real broker download
or DuckDB write happens.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from services import sector_follow_stock_backfill as sfsb
from services.sector_follow_service import load_config


def test_symbols_match_locked_static_30_universe():
    """The backfill symbol set is exactly the config universe (deduped, sorted)."""
    syms = sfsb.sector_follow_stock_symbols()
    expected = sorted(set(load_config().universe))
    assert syms == expected
    assert len(syms) == 30


def test_backfill_iterates_all_stocks_with_correct_pipeline_args():
    """Every universe stock is sent through create_and_start_job as NSE 1m incremental."""
    captured = {}

    def fake_create_and_start_job(**kwargs):
        captured.update(kwargs)
        return True, {"job_id": "job-123"}, 200

    with (
        patch.object(sfsb, "sector_follow_stock_symbols", wraps=sfsb.sector_follow_stock_symbols),
        patch(
            "database.auth_db.get_first_available_api_key",
            return_value="test-api-key",
        ),
        patch(
            "services.historify_service.create_and_start_job",
            side_effect=fake_create_and_start_job,
        ),
    ):
        result = sfsb.backfill_sector_follow_stocks("2026-06-10", "2026-06-13")

    assert result["status"] == "success"
    assert result["job_id"] == "job-123"

    # Pipeline args mirror the index backfill exactly.
    assert captured["interval"] == "1m"
    assert captured["incremental"] is True
    assert captured["start_date"] == "2026-06-10"
    assert captured["end_date"] == "2026-06-13"
    assert captured["config"] == {"source": "sector_follow_stock_backfill"}

    # Every symbol carried, each on the NSE exchange.
    sent_symbols = {d["symbol"] for d in captured["symbols"]}
    assert sent_symbols == set(sfsb.sector_follow_stock_symbols())
    assert len(captured["symbols"]) == 30
    assert all(d["exchange"] == "NSE" for d in captured["symbols"])


def test_backfill_no_api_key_returns_error_without_calling_pipeline():
    """A missing API key short-circuits — never reaches the download pipeline."""
    with (
        patch("database.auth_db.get_first_available_api_key", return_value=None),
        patch("services.historify_service.create_and_start_job") as m_job,
    ):
        result = sfsb.backfill_sector_follow_stocks("2026-06-10", "2026-06-13")

    assert result["status"] == "error"
    assert "no api key" in result["message"].lower()
    m_job.assert_not_called()


def test_refresh_uses_lookback_window():
    """The scheduled body backfills a small lookback window ending today."""
    with patch.object(
        sfsb, "backfill_sector_follow_stocks", return_value={"status": "success"}
    ) as m:
        sfsb.refresh_sector_follow_stocks()
    assert m.call_count == 1
    start, end = m.call_args.args[0], m.call_args.args[1]
    # start strictly precedes end; both are YYYY-MM-DD.
    assert len(start) == 10 and len(end) == 10
    assert start < end


def test_scheduled_job_registered_on_16_10_mon_fri_cron():
    """_register_sector_follow_stock_job adds a 16:10 IST mon-fri CronTrigger."""
    from services.historify_scheduler_service import HistorifyScheduler

    sched = HistorifyScheduler()
    mock_scheduler = MagicMock()
    with patch.object(type(sched), "scheduler", new=mock_scheduler):
        sched._register_sector_follow_stock_job()

    assert mock_scheduler.add_job.call_count == 1
    _, kwargs = mock_scheduler.add_job.call_args
    assert kwargs["id"] == "sector_follow_stock_backfill"
    assert kwargs["replace_existing"] is True

    trigger = kwargs["trigger"]
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields["hour"] == "16"
    assert fields["minute"] == "10"
    # day_of_week mon-fri == 0-4 in APScheduler's field repr
    assert fields["day_of_week"] in ("mon-fri", "0-4")
    assert str(trigger.timezone) == "Asia/Kolkata"
