"""Tests for the daily post-market review (issue #511, Phase 1).

Covers the three modules that make up the foundation: log compaction
(``postmarket_log_digest``), the day digest (``postmarket_day_digest``), and the
orchestration/scheduling layer (``postmarket_review_service``).

Coverage:

* message normalisation collapses per-occurrence variance into stable templates,
* error bucketing counts by ``(logger, template)`` and keeps one exemplar,
* the durable snapshot survives a truncation of ``errors.jsonl`` — the whole
  point of writing it, since the file is capped at 1000 lines and truncated on
  every app startup,
* re-running the digest for a date is idempotent (counts must not inflate),
* the text-log scan filters by line date and never returns raw log text,
* a section that raises degrades to ``None`` and is named in ``sources_failed``
  instead of killing the digest,
* the futures carry walk models exits as separate SELL rows (the #497 shape:
  open lots accumulate while ``exits_today`` stays 0),
* a non-trading day is skipped without persisting an empty row,
* ``run_review_for_date`` persists idempotently (one row per date) and dispatches
  through ``notification_service.notify`` with the ``postmarket_review`` event,
* a Telegram failure does not abort persistence,
* the per-fire ``POSTMARKET_REVIEW_ENABLED`` flag gates the job body,
* ``register_jobs`` is idempotent and honours ``POSTMARKET_REVIEW_TIME``.
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import pytz
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

IST = pytz.timezone("Asia/Kolkata")

DATE = "2026-07-30"  # a Thursday inside the #497 window
WEEKEND = "2026-08-01"  # a Saturday


def _mk(module, db_path):
    eng = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    module.Base.metadata.create_all(bind=eng)
    return eng, sess


# --------------------------------------------------------------------------
# Log compaction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "Error placing order for 'SBIN': qty 25 rejected",
            "Error placing order for <v>: qty <n> rejected",
        ),
        (
            "Error placing order for 'INFY': qty 100 rejected",
            "Error placing order for <v>: qty <n> rejected",
        ),
        ("failed at 2026-07-30 15:20:01", "failed at <ts>"),
        ("cannot open C:\\workspace\\db\\openalgo.db", "cannot open <path>"),
    ],
)
def test_normalize_message_collapses_variance(raw, expected):
    from services.postmarket_log_digest import normalize_message

    assert normalize_message(raw) == expected


def test_two_instances_of_one_error_share_a_template():
    """The point of normalisation: 1000 raw errors must collapse to few buckets."""
    from services.postmarket_log_digest import normalize_message

    a = normalize_message("Error verifying API key for user 42: timeout after 3.5s")
    b = normalize_message("Error verifying API key for user 99: timeout after 7.1s")
    assert a == b


def _write_errors(path, records):
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_collect_error_templates_buckets_and_filters_by_date(tmp_path):
    from services.postmarket_log_digest import collect_error_templates

    _write_errors(
        tmp_path / "errors.jsonl",
        [
            {
                "ts": f"{DATE} 10:00:00",
                "level": "ERROR",
                "logger": "database.auth_db",
                "message": "Error verifying API key for user 1",
                "exception": ["Traceback...\n", "ValueError: nope\n"],
            },
            {
                "ts": f"{DATE} 11:00:00",
                "level": "ERROR",
                "logger": "database.auth_db",
                "message": "Error verifying API key for user 2",
            },
            {
                "ts": "2026-07-29 11:00:00",  # different day — excluded
                "level": "ERROR",
                "logger": "database.auth_db",
                "message": "Error verifying API key for user 3",
            },
        ],
    )

    result = collect_error_templates(DATE, tmp_path)

    assert result["total"] == 2
    assert len(result["templates"]) == 1
    entry = next(iter(result["templates"].values()))
    assert entry["count"] == 2
    assert entry["logger"] == "database.auth_db"
    assert "ValueError: nope" in (entry["traceback"] or "")
    assert result["by_logger"] == {"database.auth_db": 2}


def test_snapshot_survives_errors_jsonl_truncation(tmp_path):
    """The durable snapshot is why a restart cannot erase the morning's errors."""
    from services.postmarket_log_digest import build_log_digest

    errors = tmp_path / "errors.jsonl"
    _write_errors(
        errors,
        [
            {"ts": f"{DATE} 09:30:00", "level": "ERROR", "logger": "svc.a", "message": "boom 1"},
            {"ts": f"{DATE} 09:31:00", "level": "ERROR", "logger": "svc.a", "message": "boom 2"},
        ],
    )
    first = build_log_digest(DATE, log_dir=tmp_path)
    assert first["errors"]["total"] == 2
    assert first["snapshot_written"] is True

    # Simulate the startup truncation wiping the file.
    _write_errors(errors, [])

    second = build_log_digest(DATE, log_dir=tmp_path)
    assert second["errors"]["total"] == 2, "snapshot must preserve pre-truncation errors"


def test_rerunning_digest_is_idempotent(tmp_path):
    """Counts must not inflate on re-run — the merge takes max, not sum."""
    from services.postmarket_log_digest import build_log_digest

    _write_errors(
        tmp_path / "errors.jsonl",
        [{"ts": f"{DATE} 09:30:00", "level": "ERROR", "logger": "svc.a", "message": "boom"}] * 3,
    )

    totals = {build_log_digest(DATE, log_dir=tmp_path)["errors"]["total"] for _ in range(3)}

    assert totals == {3}


def test_text_log_scan_counts_without_returning_raw_text(tmp_path):
    from services.postmarket_log_digest import scan_text_log

    (tmp_path / f"openalgo_{DATE}.log").write_text(
        f"[{DATE} 09:20:00,001] INFO in scanner_service: scanner PASS TCS\n"
        f"[{DATE} 09:21:00,001] WARNING in sector_follow: aggregator had no today bars\n"
        f"[{DATE} 09:22:00,001] ERROR in health_db: database is locked\n"
        "Traceback (most recent call last):\n"
        "  File 'x.py', line 1, in <module>\n"
        "2026-07-29 09:23:00 INFO in other: previous day line\n",
        encoding="utf-8",
    )

    result = scan_text_log(DATE, tmp_path)

    assert result["by_level"] == {"INFO": 1, "WARNING": 1, "ERROR": 1}
    assert result["markers"]["scanner_pass"] == 1
    assert result["markers"]["aggregator_miss"] == 1
    assert result["warning_by_module"] == {"sector_follow": 1, "health_db": 1}
    # No raw log line may appear anywhere in the returned structure.
    assert "database is locked" not in json.dumps(result)


# --------------------------------------------------------------------------
# Day digest
# --------------------------------------------------------------------------


def test_failing_section_degrades_without_killing_the_digest():
    from services import postmarket_day_digest as pdd

    with patch.object(pdd, "_collect_signals", side_effect=RuntimeError("table gone")):
        digest = pdd.build_day_digest(DATE)

    assert digest["signals"] is None
    assert "signals" in digest["sources_failed"]
    # Everything else still built.
    assert digest["date"] == DATE
    assert "logs" in digest


def test_futures_carry_treats_exits_as_separate_sell_rows(tmp_path, monkeypatch):
    """Exits are SELL rows; ``exit_price`` is never back-filled onto the BUY.

    Reproduces the #497 shape: BUY legs accumulate across days with no SELL, so
    open lots climb while ``exits_today`` stays 0.
    """
    from database import futures_follow_db as ffdb
    from services import postmarket_day_digest as pdd

    eng, sess = _mk(ffdb, tmp_path / "ff.db")
    monkeypatch.setattr(ffdb, "engine", eng)
    monkeypatch.setattr(ffdb, "db_session", sess)

    sess.add_all(
        [
            ffdb.FuturesFollowTrade(
                strategy_id="futures_follow_cap50",
                mode="sandbox",
                side="BUY",
                nifty_symbol="NIFTY28JUL26FUT",
                lots=1,
                quantity=75,
                entry_date="2026-07-27",
                status="placed",
            ),
            ffdb.FuturesFollowTrade(
                strategy_id="futures_follow_cap50",
                mode="sandbox",
                side="BUY",
                nifty_symbol="NIFTY28JUL26FUT",
                lots=2,
                quantity=150,
                entry_date="2026-07-28",
                status="placed",
            ),
            ffdb.FuturesFollowTrade(
                strategy_id="futures_follow_cap50",
                mode="sandbox",
                side="BUY",
                nifty_symbol="NIFTY28JUL26FUT",
                lots=2,
                quantity=150,
                entry_date=DATE,
                status="placed",
            ),
        ]
    )
    sess.commit()

    result = pdd._collect_futures_carry(DATE)

    assert result["entries_today"] == 1
    assert result["exits_today"] == 0
    assert result["open_lots_carried"] == 5
    assert result["oldest_open_entry_date"] == "2026-07-27"
    assert result["carry_age_days"] == 3


def test_futures_carry_fifo_consumes_oldest_buy_first(tmp_path, monkeypatch):
    from database import futures_follow_db as ffdb
    from services import postmarket_day_digest as pdd

    eng, sess = _mk(ffdb, tmp_path / "ff2.db")
    monkeypatch.setattr(ffdb, "engine", eng)
    monkeypatch.setattr(ffdb, "db_session", sess)

    sess.add_all(
        [
            ffdb.FuturesFollowTrade(
                strategy_id="s",
                mode="sandbox",
                side="BUY",
                nifty_symbol="N",
                lots=2,
                quantity=150,
                entry_date="2026-07-27",
                status="placed",
            ),
            ffdb.FuturesFollowTrade(
                strategy_id="s",
                mode="sandbox",
                side="BUY",
                nifty_symbol="N",
                lots=1,
                quantity=75,
                entry_date="2026-07-29",
                status="placed",
            ),
            ffdb.FuturesFollowTrade(
                strategy_id="s",
                mode="sandbox",
                side="SELL",
                nifty_symbol="N",
                lots=2,
                quantity=150,
                # created_at is what dates an exit — entry_date carries the entry
                # session even on a SELL row. See
                # test_exits_are_counted_by_write_date_not_entry_date.
                created_at=datetime(2026, 7, 30, 15, 25),
                entry_date=DATE,
                status="placed",
            ),
        ]
    )
    sess.commit()

    result = pdd._collect_futures_carry(DATE)

    assert result["exits_today"] == 1
    assert result["open_lots_carried"] == 1
    # The 2-lot SELL consumed the whole 27th BUY, leaving the 29th open.
    assert result["oldest_open_entry_date"] == "2026-07-29"


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


@pytest.fixture
def review_table(monkeypatch, tmp_path):
    from database import postmarket_review_db as prdb

    eng, sess = _mk(prdb, tmp_path / "review.db")
    monkeypatch.setattr(prdb, "engine", eng)
    monkeypatch.setattr(prdb, "db_session", sess)
    return prdb


def test_non_trading_day_is_skipped_and_persists_nothing(review_table):
    from services import postmarket_review_service as prs

    result = prs.run_review_for_date(WEEKEND, dispatch_telegram=False, persist=True)

    assert result["skipped"] == "non_trading_day"
    assert result["persisted"] is False
    assert review_table.get_review(WEEKEND) is None


def test_run_review_persists_idempotently_and_notifies(review_table):
    from services import postmarket_review_service as prs

    fake = MagicMock()
    with (
        patch.object(prs, "__name__", prs.__name__),
        patch("services.notification_service.get_notification_service", return_value=fake),
    ):
        first = prs.run_review_for_date(DATE, dispatch_telegram=True, persist=True)
        second = prs.run_review_for_date(DATE, dispatch_telegram=True, persist=True)

    assert first["persisted"] and second["persisted"]
    assert first["telegram_sent"] is True

    event_type = fake.notify.call_args_list[0][0][0]
    assert event_type == "postmarket_review"

    stored = review_table.get_review(DATE)
    assert stored is not None
    assert stored["review_date"] == DATE
    assert stored["summary_text"]


def test_telegram_failure_does_not_abort_persistence(review_table):
    from services import postmarket_review_service as prs

    fake = MagicMock()
    fake.notify.side_effect = RuntimeError("telegram down")
    with patch("services.notification_service.get_notification_service", return_value=fake):
        result = prs.run_review_for_date(DATE, dispatch_telegram=True, persist=True)

    assert result["telegram_sent"] is False
    assert result["persisted"] is True
    assert review_table.get_review(DATE) is not None


def test_job_respects_per_fire_enable_flag(monkeypatch):
    from services import postmarket_review_service as prs

    monkeypatch.setenv("POSTMARKET_REVIEW_ENABLED", "false")
    with patch.object(prs, "run_review_for_date") as runner:
        prs._postmarket_review_job()

    runner.assert_not_called()


def test_render_summary_marks_degraded_sections():
    from services.postmarket_review_service import render_summary

    text = render_summary(
        {
            "date": DATE,
            "is_trading_day": True,
            "jobs": None,
            "trade_journal": None,
            "logs": None,
            "sources_failed": ["jobs", "trade_journal", "logs"],
        }
    )

    # A failed section must read as "?" — never as a zero, which would look like
    # a genuinely quiet day.
    assert "Jobs: ? (section failed)" in text
    assert "Trades: ? (section failed)" in text
    assert "Degraded sections" in text
    assert "0 jobs" not in text


def test_register_jobs_is_idempotent_and_honours_time_env(monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler

    from services import postmarket_review_service as prs

    monkeypatch.setenv("POSTMARKET_REVIEW_TIME", "18:05")
    sched = BackgroundScheduler(timezone=IST)
    sched.start(paused=True)
    try:
        prs.register_jobs(sched)
        prs.register_jobs(sched)

        jobs = [j for j in sched.get_jobs() if j.id == "postmarket_review"]
        assert len(jobs) == 1
        assert "18:05" in jobs[0].name
        assert jobs[0].trigger.fields[jobs[0].trigger.FIELD_NAMES.index("hour")].expressions
    finally:
        sched.shutdown(wait=False)


def test_parse_hh_mm_falls_back_on_garbage():
    from services.postmarket_review_service import _parse_hh_mm

    assert _parse_hh_mm("09:45") == (9, 45)
    assert _parse_hh_mm("not-a-time") == (17, 15)
    assert _parse_hh_mm("99:99") == (17, 15)


# --------------------------------------------------------------------------
# Phase 2 (#532) — verdict rendering and violation persistence
# --------------------------------------------------------------------------


def _contracts(violations, evaluated=True, unknown=()):
    return {
        "violations": list(violations),
        "counts": {"pass": 0, "fail": len(violations), "unknown": len(unknown), "skipped": 0},
        "unknown_contracts": list(unknown),
        "evaluated": evaluated,
    }


def test_summary_leads_with_violations_and_severity():
    from services.postmarket_review_service import render_summary

    text = render_summary(
        {"date": DATE, "is_trading_day": True, "sources_failed": []},
        _contracts(
            [
                {
                    "severity": "P0",
                    "strategy": "futures_follow_cap50",
                    "contract_id": "t1_exit_for_carry",
                    "summary": "8 lots open, 0 exits today",
                }
            ]
        ),
    )

    lines = text.splitlines()
    # The verdict must precede the counts — the operator reads the first lines.
    assert "1 expectation(s) violated" in lines[1]
    assert "[P0]" in lines[2]
    assert "futures_follow_cap50/t1_exit_for_carry" in lines[2]


def test_summary_states_a_clean_day_explicitly():
    """Silence must never be how a healthy day is communicated."""
    from services.postmarket_review_service import render_summary

    text = render_summary(
        {"date": DATE, "is_trading_day": True, "sources_failed": []}, _contracts([])
    )

    assert "All expectations passed" in text


def test_summary_omits_verdict_when_contracts_did_not_run():
    from services.postmarket_review_service import render_summary

    text = render_summary(
        {"date": DATE, "is_trading_day": True, "sources_failed": []},
        _contracts([], evaluated=False),
    )

    assert "expectation" not in text.lower()


def test_violations_are_persisted_and_read_back(review_table):
    from services import postmarket_review_service as prs

    with patch("services.notification_service.get_notification_service", return_value=MagicMock()):
        result = prs.run_review_for_date(DATE, dispatch_telegram=False, persist=True)

    stored = review_table.get_review(DATE)
    assert stored is not None
    assert stored["n_violations"] == len(result["contracts"]["violations"])
    assert stored["contracts"]["evaluated"] is True
    for violation in stored["violations"]:
        assert violation["fingerprint"]
        assert violation["severity"] in ("P0", "P1", "P2")


def test_contract_failure_does_not_abort_the_review(review_table):
    """A broken contract layer must still leave a persisted digest behind."""
    from services import postmarket_review_service as prs

    with patch(
        "services.strategy_expectations.evaluate_expectations",
        side_effect=RuntimeError("contracts exploded"),
    ):
        result = prs.run_review_for_date(DATE, dispatch_telegram=False, persist=True)

    assert result["persisted"] is True
    assert result["contracts"]["evaluated"] is False
    assert review_table.get_review(DATE) is not None


def test_exits_are_counted_by_write_date_not_entry_date(tmp_path, monkeypatch):
    """A SELL row's `entry_date` is the ENTRY session, not the day it was written.

    Found by the investigating agent (#536) reading the code: `place_exit` records
    the SELL leg with `entry_date=position.entry_date`, and the column's own
    docstring says so — a real row shows `entry_date=2026-07-17` created on
    2026-07-20. Counting exits by `entry_date` made `exits_today` structurally
    always 0 for a T+1 strategy, so `t1_exit_for_carry` could never observe a
    successful exit and fired on healthy days that still carried older lots.
    """
    from datetime import datetime

    from database import futures_follow_db as ffdb
    from services import postmarket_day_digest as pdd

    eng, sess = _mk(ffdb, tmp_path / "ff_exitdate.db")
    monkeypatch.setattr(ffdb, "engine", eng)
    monkeypatch.setattr(ffdb, "db_session", sess)

    sess.add_all(
        [
            ffdb.FuturesFollowTrade(
                strategy_id="s",
                mode="sandbox",
                side="BUY",
                nifty_symbol="N",
                lots=1,
                quantity=75,
                entry_date="2026-07-29",
                status="placed",
                created_at=datetime(2026, 7, 29, 9, 50),
            ),
            # The exit happened on the 30th but carries the 29th as entry_date.
            ffdb.FuturesFollowTrade(
                strategy_id="s",
                mode="sandbox",
                side="SELL",
                nifty_symbol="N",
                lots=1,
                quantity=75,
                entry_date="2026-07-29",
                status="placed",
                created_at=datetime(2026, 7, 30, 9, 55),
            ),
        ]
    )
    sess.commit()

    result = pdd._collect_futures_carry("2026-07-30")

    assert result["exits_today"] == 1, "the exit was written on the 30th"
    assert result["lots_sold_today"] == 1
    assert result["open_lots_carried"] == 0
