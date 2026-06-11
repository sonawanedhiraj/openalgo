"""Mocked E2E for the scanner-vs-Chartink EOD comparison job.

Exercises ``services.scanner_comparison_eod_service.run_comparison_for_date``
end-to-end against in-memory SQLite copies of the three tables it touches
(``scan_cycle``, ``scan_results`` + ``scan_definitions``, ``scanner_comparison``)
and a mocked notification service. Asserts:

* a ``scanner_comparison`` row per side is written with the correct Jaccard,
* ``telegram_sent`` is True and ``notify()`` was actually called,
* re-running the same date is idempotent (no duplicate rows, overwrite in place).

Hermetic — no app boot, no broker session, no real Telegram. The global
``test/conftest.py`` guard keeps every engine off the live DBs even though we
rebind to ``:memory:`` here.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

DATE = "2026-06-11"


def _mk(module):
    """Rebind a DB module to a fresh in-memory engine and create its tables."""
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    module.Base.metadata.create_all(bind=eng)
    return eng, sess


@pytest.fixture
def wired(monkeypatch):
    """Rebind all three DB modules + mock the notification service.

    Seeds a known scenario:
      BUY  — chartink={X,Y}, inhouse={Y,Z}  → ∩={Y}, jaccard 1/3, ratio 1/2
      SELL — chartink={A,B,C,D}, inhouse={C,D,E} → ∩={C,D}, jaccard 2/5, ratio 1/2
    """
    from database import scan_cycle_db, scanner_comparison_db, scanner_db

    cmp_eng, cmp_sess = _mk(scanner_comparison_db)
    scan_eng, scan_sess = _mk(scanner_db)
    cyc_eng, cyc_sess = _mk(scan_cycle_db)

    monkeypatch.setattr(scanner_comparison_db, "engine", cmp_eng)
    monkeypatch.setattr(scanner_comparison_db, "db_session", cmp_sess)
    monkeypatch.setattr(scanner_db, "engine", scan_eng)
    monkeypatch.setattr(scanner_db, "db_session", scan_sess)
    monkeypatch.setattr(scan_cycle_db, "engine", cyc_eng)
    monkeypatch.setattr(scan_cycle_db, "db_session", cyc_sess)

    # --- Chartink side: scan_cycle rows posted via webhook (two cycles). ---
    cyc_sess.add_all(
        [
            scan_cycle_db.ScanCycle(
                started_at=f"{DATE}T13:19:02.018722+05:30",
                cycle_kind="chartink",
                screener_buy='["X"]',
                screener_sell='["A", "B"]',
                post_status="ok",
            ),
            scan_cycle_db.ScanCycle(
                started_at=f"{DATE}T14:19:06.858338+05:30",
                cycle_kind="chartink",
                screener_buy='["Y"]',
                screener_sell='["B", "C", "D"]',
                post_status="ok",
            ),
            # Noise that must be excluded: a non-chartink cycle + a prior day.
            scan_cycle_db.ScanCycle(
                started_at=f"{DATE}T14:30:00+05:30",
                cycle_kind="test",
                screener_buy='["NOISE"]',
                screener_sell='["NOISE"]',
                post_status="ok",
            ),
            scan_cycle_db.ScanCycle(
                started_at="2026-06-10T14:19:06+05:30",
                cycle_kind="chartink",
                screener_buy='["OLD"]',
                screener_sell='["OLD"]',
                post_status="ok",
            ),
        ]
    )
    cyc_sess.commit()

    # --- In-house side: scan_definitions + scan_results (source='inhouse'). ---
    buy_def = scanner_db.ScanDefinition(
        name="fno_intraday_buy_20",
        screener_type="buy",
        expression_json="{}",
        rule_module="fno_intraday_buy_chartink",
        enabled=1,
        created_at=DATE,
        updated_at=DATE,
    )
    sell_def = scanner_db.ScanDefinition(
        name="fno_intraday_sell_20",
        screener_type="sell",
        expression_json="{}",
        rule_module="fno_intraday_sell_chartink",
        enabled=1,
        created_at=DATE,
        updated_at=DATE,
    )
    scan_sess.add_all([buy_def, sell_def])
    scan_sess.commit()
    buy_id, sell_id = buy_def.id, sell_def.id

    scan_sess.add_all(
        [
            scanner_db.ScanResult(
                scan_definition_id=buy_id,
                run_at=f"{DATE}T12:20:00+05:30",
                symbols='["Y"]',
                source="inhouse",
            ),
            scanner_db.ScanResult(
                scan_definition_id=buy_id,
                run_at=f"{DATE}T12:25:00+05:30",
                symbols='["Z"]',
                source="inhouse",
            ),
            scanner_db.ScanResult(
                scan_definition_id=sell_id,
                run_at=f"{DATE}T12:20:00+05:30",
                symbols='["C", "D"]',
                source="inhouse",
            ),
            scanner_db.ScanResult(
                scan_definition_id=sell_id,
                run_at=f"{DATE}T12:30:00+05:30",
                symbols='["E"]',
                source="inhouse",
            ),
            # Noise: a chartink-sourced result + a prior day, both must be excluded.
            scanner_db.ScanResult(
                scan_definition_id=sell_id,
                run_at=f"{DATE}T12:35:00+05:30",
                symbols='["NOISE"]',
                source="chartink",
            ),
            scanner_db.ScanResult(
                scan_definition_id=sell_id,
                run_at="2026-06-10T12:35:00+05:30",
                symbols='["OLD"]',
                source="inhouse",
            ),
        ]
    )
    scan_sess.commit()

    # --- Mock notification service so notify() is observable + never sends. ---
    mock_notif = MagicMock()
    monkeypatch.setattr(
        "services.notification_service.get_notification_service",
        lambda: mock_notif,
    )

    yield {
        "cmp": scanner_comparison_db,
        "mock_notif": mock_notif,
    }

    for s in (cmp_sess, scan_sess, cyc_sess):
        s.remove()
    for e in (cmp_eng, scan_eng, cyc_eng):
        e.dispose()


def test_run_comparison_writes_rows_and_notifies(wired):
    from services import scanner_comparison_eod_service as svc

    result = svc.run_comparison_for_date(date=DATE, dispatch_telegram=True)

    # --- BUY metrics: chartink={X,Y}, inhouse={Y,Z}, ∩={Y}. ---
    buy = result["BUY"]
    assert buy["inhouse_count"] == 2
    assert buy["chartink_count"] == 2
    assert buy["intersection_count"] == 1
    assert buy["jaccard"] == pytest.approx(1 / 3)
    assert buy["ratio"] == pytest.approx(0.5)
    assert buy["false_positives"] == ["Z"]  # inhouse-only
    assert buy["false_negatives"] == ["X"]  # chartink-only

    # --- SELL metrics: chartink={A,B,C,D}, inhouse={C,D,E}, ∩={C,D}. ---
    sell = result["SELL"]
    assert sell["inhouse_count"] == 3
    assert sell["chartink_count"] == 4
    assert sell["intersection_count"] == 2
    assert sell["jaccard"] == pytest.approx(2 / 5)
    assert sell["ratio"] == pytest.approx(0.5)
    assert sell["false_positives"] == ["E"]
    assert sell["false_negatives"] == ["A", "B"]

    # --- Telegram dispatched + notify actually called. ---
    assert result["telegram_sent"] is True
    wired["mock_notif"].notify.assert_called_once()
    args, _ = wired["mock_notif"].notify.call_args
    assert args[0] == "scanner_comparison"
    assert DATE in args[1]

    # --- Persisted rows: one per side, correct Jaccard + telegram flag. ---
    rows = wired["cmp"].get_comparisons_for_date(DATE)
    assert len(rows) == 2
    by_side = {r["screener_side"]: r for r in rows}
    assert set(by_side) == {"BUY", "SELL"}
    assert by_side["SELL"]["jaccard"] == pytest.approx(2 / 5)
    assert by_side["SELL"]["intersection_count"] == 2
    assert by_side["BUY"]["jaccard"] == pytest.approx(1 / 3)
    assert all(r["telegram_sent"] for r in rows)
    assert by_side["SELL"]["false_negatives"] == ["A", "B"]


def test_run_comparison_is_idempotent(wired):
    from services import scanner_comparison_eod_service as svc

    svc.run_comparison_for_date(date=DATE, dispatch_telegram=True)
    first = wired["cmp"].get_comparisons_for_date(DATE)
    assert len(first) == 2

    # Re-run for the same date — delete-then-insert must NOT duplicate.
    svc.run_comparison_for_date(date=DATE, dispatch_telegram=True)
    second = wired["cmp"].get_comparisons_for_date(DATE)
    assert len(second) == 2

    # A third run via the recent-comparisons view confirms global row count too.
    svc.run_comparison_for_date(date=DATE, dispatch_telegram=True)
    recent = wired["cmp"].get_recent_comparisons(limit=50)
    assert len([r for r in recent if r["date"] == DATE]) == 2


def test_empty_day_yields_parity_and_null_jaccard(wired, monkeypatch):
    """A day with no hits on either side: jaccard None, parity suggestion."""
    from services import scanner_comparison_eod_service as svc

    empty_date = "2026-06-09"  # nothing seeded for this date
    result = svc.run_comparison_for_date(date=empty_date, dispatch_telegram=False)

    for side in ("BUY", "SELL"):
        m = result[side]
        assert m["inhouse_count"] == 0
        assert m["chartink_count"] == 0
        assert m["jaccard"] is None
        assert m["ratio"] is None
        assert "parity" in m["tuning_suggestion"]

    assert result["telegram_sent"] is False
    wired["mock_notif"].notify.assert_not_called()

    rows = wired["cmp"].get_comparisons_for_date(empty_date)
    assert len(rows) == 2
    assert all(not r["telegram_sent"] for r in rows)
