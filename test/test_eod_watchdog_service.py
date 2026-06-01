"""Tests for the P0 EOD watchdog + journal-based position rehydrate.

Covers:

* :func:`strategies.list_intraday_strategies` filters out positional
  (``intraday = False``) strategies.
* :func:`services.eod_watchdog_service.start_eod_watchdog` schedules one
  cron job per intraday strategy at its declared ``eod_exit_time``.
* The watchdog job body dispatches to
  :func:`services.simplified_stock_engine_service.flatten_strategy_positions`
  with the correct strategy name.
* :func:`flatten_strategy_positions` picks up only today's open rows for
  the named strategy — ignoring already-exited rows, other strategies'
  rows, and yesterday's rows.
* A ``place_order`` failure leaves the row open + escalates to the
  notification service.
* :meth:`SimplifiedStockEngineService.rehydrate_positions_from_journal`
  rebuilds the in-memory ``positions`` dict from today's open journal
  rows; engine-owned positions take priority.

These tests do NOT call the real broker. Every place_order /
notification path is mocked.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest
import pytz
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

# Pre-resolve the restx_api / services.place_order_service circular import.
# Mirrors the workaround in test_simplified_stock_engine_service.py — without
# this, an inner import of services.place_order_service trips a partial-init
# ImportError when the test module loads under pytest.
import restx_api  # noqa: F401, E402
import services.place_order_service  # noqa: F401, E402

IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_journal_db(monkeypatch):
    """Point trade_journal_db at an in-memory SQLite. Mirrors the fixture in
    test_trade_journal_service.py."""
    from database import trade_journal_db as tjdb

    test_engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    test_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    )
    monkeypatch.setattr(tjdb, "engine", test_engine)
    monkeypatch.setattr(tjdb, "db_session", test_session)
    tjdb.Base.metadata.create_all(test_engine)
    yield tjdb
    test_session.remove()
    test_engine.dispose()


@pytest.fixture
def stopped_watchdog():
    """Ensure no scheduler leaks across tests."""
    from services import eod_watchdog_service

    eod_watchdog_service.stop_eod_watchdog()
    yield
    eod_watchdog_service.stop_eod_watchdog()


def _seed_open_row(
    tjdb,
    *,
    symbol: str,
    strategy: str,
    qty: int = 100,
    entry_price: float = 100.0,
    direction: str = "LONG",
    days_ago: int = 0,
    exited: bool = False,
) -> int:
    """Insert a journal row directly (bypassing the service) so we can pin
    placed_at and exited_at deterministically."""
    now = dt.datetime.now(IST) - dt.timedelta(days=days_ago)
    row = tjdb.TradeJournal(
        placed_at=now.isoformat(),
        symbol=symbol,
        direction=direction,
        quantity=qty,
        strategy_name=strategy,
        signal_source="chartink",
        entry_price=entry_price,
        entry_order_id=f"ord-{symbol}-{days_ago}",
        exited_at=now.isoformat() if exited else None,
        exit_price=(entry_price + 1) if exited else None,
        exit_reason=("manual" if exited else None),
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    tjdb.db_session.add(row)
    tjdb.db_session.commit()
    return int(row.id)


# ---------------------------------------------------------------------------
# Registry helper
# ---------------------------------------------------------------------------


def test_list_intraday_strategies_includes_trending_equity():
    """The shipped strategy must be classified intraday with a parseable cut-off."""
    from strategies import list_intraday_strategies

    result = dict(list_intraday_strategies())
    assert "trending_equity_intraday" in result
    # HH:MM format — the watchdog will split on ':' so this matters.
    assert ":" in result["trending_equity_intraday"]


def test_list_intraday_strategies_excludes_positional_strategy():
    """A strategy declaring ``intraday = False`` must NOT appear."""
    from strategies import _registry, list_intraday_strategies
    from strategies.base import BaseStrategy

    class _Positional(BaseStrategy):
        name = "positional_test_only"
        intraday = False
        eod_exit_time = "15:20"

        def on_scan_hit(self, symbol, direction): ...
        def seed_history(self, symbol, candles): ...
        def on_bar(self, symbol, candle): return None
        def on_tick(self, symbol, price): return []
        def confirm_entry(self, symbol, executed_price=None): return None
        def confirm_exit(self, symbol, exit_price=None, reason=None): return None
        def clear_pending_entry(self, symbol): ...
        def clear_pending_exit(self, symbol): ...

    _registry["positional_test_only"] = _Positional
    try:
        names = {n for n, _ in list_intraday_strategies()}
        assert "positional_test_only" not in names
        assert "trending_equity_intraday" in names
    finally:
        _registry.pop("positional_test_only", None)


# ---------------------------------------------------------------------------
# Watchdog scheduler
# ---------------------------------------------------------------------------


def test_start_eod_watchdog_schedules_one_job_per_intraday_strategy(stopped_watchdog):
    from services import eod_watchdog_service

    with patch(
        "services.eod_watchdog_service.list_intraday_strategies",
        return_value=[("strat_a", "15:20"), ("strat_b", "15:25")],
    ), patch(
        "services.eod_watchdog_service.registered_strategies",
        return_value={"strat_a": object(), "strat_b": object(), "positional_x": object()},
    ):
        result = eod_watchdog_service.start_eod_watchdog()

    assert result["started"] is True
    job_names = {j["strategy"] for j in result["jobs"]}
    assert job_names == {"strat_a", "strat_b"}
    assert {"strategy": "positional_x", "reason": "positional"} in result["skipped"]

    sched = eod_watchdog_service.get_scheduler()
    assert sched is not None
    assert sched.running
    # APScheduler exposes the scheduled jobs — confirm one per strategy.
    job_ids = {j.id for j in sched.get_jobs()}
    assert "eod_watchdog_strat_a" in job_ids
    assert "eod_watchdog_strat_b" in job_ids
    assert len(sched.get_jobs()) == 2


def test_start_eod_watchdog_is_idempotent(stopped_watchdog):
    """Calling start twice must not stack jobs or raise."""
    from services import eod_watchdog_service

    with patch(
        "services.eod_watchdog_service.list_intraday_strategies",
        return_value=[("strat_a", "15:20")],
    ), patch(
        "services.eod_watchdog_service.registered_strategies",
        return_value={"strat_a": object()},
    ):
        eod_watchdog_service.start_eod_watchdog()
        second = eod_watchdog_service.start_eod_watchdog()

    assert second == {"started": False, "jobs": [], "skipped": []}
    # The original scheduler must still be running with one job.
    sched = eod_watchdog_service.get_scheduler()
    assert sched is not None
    assert sched.running
    assert len(sched.get_jobs()) == 1


def test_start_eod_watchdog_skips_bad_time_format(stopped_watchdog):
    from services import eod_watchdog_service

    with patch(
        "services.eod_watchdog_service.list_intraday_strategies",
        return_value=[("strat_good", "15:20"), ("strat_bad", "not-a-time")],
    ), patch(
        "services.eod_watchdog_service.registered_strategies",
        return_value={"strat_good": object(), "strat_bad": object()},
    ):
        result = eod_watchdog_service.start_eod_watchdog()

    assert {"strategy": "strat_bad", "reason": "bad_time"} in result["skipped"]
    assert [j["strategy"] for j in result["jobs"]] == ["strat_good"]


def test_run_strategy_eod_flatten_dispatches_to_flatten():
    """The cron-job body must call flatten_strategy_positions with the
    strategy name and publish the summary via the notification service."""
    from services import eod_watchdog_service

    flatten_result = {
        "strategy": "trending_equity_intraday",
        "reason": "eod_watchdog",
        "attempted": 1,
        "succeeded": 1,
        "failed": [],
        "skipped": [],
    }
    with patch(
        "services.simplified_stock_engine_service.flatten_strategy_positions",
        return_value=flatten_result,
    ) as mock_flatten, patch(
        "services.notification_service.get_notification_service"
    ) as mock_ns:
        eod_watchdog_service._run_strategy_eod_flatten("trending_equity_intraday")

    mock_flatten.assert_called_once_with(
        "trending_equity_intraday", reason="eod_watchdog"
    )
    mock_ns.return_value.publish_eod_watchdog_summary.assert_called_once_with(
        strategy_name="trending_equity_intraday", result=flatten_result
    )


def test_run_strategy_eod_flatten_crash_escalates_failure_alert():
    """If flatten itself raises, the watchdog must emit a failure alert and
    NOT propagate the exception (which would crash the APScheduler thread)."""
    from services import eod_watchdog_service

    with patch(
        "services.simplified_stock_engine_service.flatten_strategy_positions",
        side_effect=RuntimeError("kaboom"),
    ), patch(
        "services.notification_service.get_notification_service"
    ) as mock_ns:
        # Must not raise.
        eod_watchdog_service._run_strategy_eod_flatten("strat_x")

    mock_ns.return_value.publish_eod_watchdog_failure.assert_called_once()
    call_kwargs = mock_ns.return_value.publish_eod_watchdog_failure.call_args.kwargs
    assert call_kwargs["strategy_name"] == "strat_x"
    assert "kaboom" in call_kwargs["error"]


# ---------------------------------------------------------------------------
# flatten_strategy_positions
# ---------------------------------------------------------------------------


def _patched_engine_service(svc_mock):
    """Patch get_simplified_stock_engine_service to return svc_mock everywhere it's used."""
    return patch(
        "services.simplified_stock_engine_service.get_simplified_stock_engine_service",
        return_value=svc_mock,
    )


def _make_svc_mock(api_key="test-key"):
    svc = MagicMock()
    svc.config.exchange = "NSE"
    svc.config.product = "MIS"
    svc._lock = MagicMock()
    svc._lock.__enter__ = MagicMock(return_value=None)
    svc._lock.__exit__ = MagicMock(return_value=None)
    svc._api_key_by_symbol = {"SOMESYM": api_key} if api_key else {}
    svc._user_api_keys = {}
    svc.engine = MagicMock()
    svc.engine.positions = {}
    return svc


def test_flatten_strategy_positions_no_open_rows_is_noop(fresh_journal_db):
    """When there are no open rows, no order is sent."""
    from services import simplified_stock_engine_service as ses

    svc = _make_svc_mock()
    with _patched_engine_service(svc), patch(
        "services.place_order_service.place_order"
    ) as mock_po:
        result = ses.flatten_strategy_positions("trending_equity_intraday")

    mock_po.assert_not_called()
    assert result["attempted"] == 0
    assert result["succeeded"] == 0
    assert result["failed"] == []


def test_flatten_strategy_positions_flattens_todays_open_long(fresh_journal_db):
    """A LONG row entered today gets a SELL MARKET order; row is closed."""
    from services import simplified_stock_engine_service as ses

    jid = _seed_open_row(
        fresh_journal_db,
        symbol="NBCC",
        strategy="trending_equity_intraday",
        qty=500,
        entry_price=104.94,
        direction="LONG",
    )

    svc = _make_svc_mock()
    with _patched_engine_service(svc), patch(
        "services.place_order_service.place_order",
        return_value=(True, {"orderid": "WD-1", "status": "success"}, 200),
    ) as mock_po:
        result = ses.flatten_strategy_positions(
            "trending_equity_intraday", reason="eod_watchdog"
        )

    assert mock_po.called
    sent_payload = mock_po.call_args.args[0]
    assert sent_payload["symbol"] == "NBCC"
    assert sent_payload["action"] == "SELL"  # LONG flatten -> SELL
    assert sent_payload["quantity"] == 500
    assert sent_payload["pricetype"] == "MARKET"
    assert sent_payload["product"] == "MIS"
    assert sent_payload["exchange"] == "NSE"
    assert sent_payload["strategy"] == "trending_equity_intraday"

    assert result["attempted"] == 1
    assert result["succeeded"] == 1
    assert result["failed"] == []

    # Row is now closed.
    row = (
        fresh_journal_db.db_session.query(fresh_journal_db.TradeJournal)
        .filter_by(id=jid)
        .first()
    )
    assert row.exited_at is not None
    assert row.exit_reason == "eod_watchdog"
    assert row.exit_order_id == "WD-1"


def test_flatten_strategy_positions_flattens_short_with_buy(fresh_journal_db):
    """A SHORT row gets a BUY MARKET order."""
    from services import simplified_stock_engine_service as ses

    _seed_open_row(
        fresh_journal_db,
        symbol="HCLTECH",
        strategy="trending_equity_intraday",
        qty=50,
        entry_price=1500.0,
        direction="SHORT",
    )

    svc = _make_svc_mock()
    with _patched_engine_service(svc), patch(
        "services.place_order_service.place_order",
        return_value=(True, {"orderid": "WD-2"}, 200),
    ) as mock_po:
        ses.flatten_strategy_positions("trending_equity_intraday")

    sent_payload = mock_po.call_args.args[0]
    assert sent_payload["action"] == "BUY"  # SHORT flatten -> BUY


def test_flatten_strategy_positions_ignores_other_strategies(fresh_journal_db):
    """Open rows belonging to other strategies must not be touched."""
    from services import simplified_stock_engine_service as ses

    _seed_open_row(
        fresh_journal_db,
        symbol="MINE",
        strategy="trending_equity_intraday",
    )
    other_id = _seed_open_row(
        fresh_journal_db,
        symbol="THEIRS",
        strategy="some_other_strategy",
    )

    svc = _make_svc_mock()
    with _patched_engine_service(svc), patch(
        "services.place_order_service.place_order",
        return_value=(True, {"orderid": "X"}, 200),
    ) as mock_po:
        result = ses.flatten_strategy_positions("trending_equity_intraday")

    assert mock_po.call_count == 1
    assert mock_po.call_args.args[0]["symbol"] == "MINE"
    assert result["attempted"] == 1

    # The other-strategy row must still be open.
    other_row = (
        fresh_journal_db.db_session.query(fresh_journal_db.TradeJournal)
        .filter_by(id=other_id)
        .first()
    )
    assert other_row.exited_at is None


def test_flatten_strategy_positions_ignores_already_exited_rows(fresh_journal_db):
    """A row whose exited_at is already set must be skipped (idempotency)."""
    from services import simplified_stock_engine_service as ses

    _seed_open_row(
        fresh_journal_db,
        symbol="CLOSED",
        strategy="trending_equity_intraday",
        exited=True,
    )

    svc = _make_svc_mock()
    with _patched_engine_service(svc), patch(
        "services.place_order_service.place_order"
    ) as mock_po:
        result = ses.flatten_strategy_positions("trending_equity_intraday")

    mock_po.assert_not_called()
    assert result["attempted"] == 0


def test_flatten_strategy_positions_ignores_yesterdays_rows(fresh_journal_db):
    """A row entered yesterday must not be picked up by today's watchdog."""
    from services import simplified_stock_engine_service as ses

    _seed_open_row(
        fresh_journal_db,
        symbol="YESTERDAY",
        strategy="trending_equity_intraday",
        days_ago=1,
    )

    svc = _make_svc_mock()
    with _patched_engine_service(svc), patch(
        "services.place_order_service.place_order"
    ) as mock_po:
        result = ses.flatten_strategy_positions("trending_equity_intraday")

    mock_po.assert_not_called()
    assert result["attempted"] == 0


def test_flatten_strategy_positions_handles_place_order_failure(fresh_journal_db):
    """A broker rejection must leave the row open + escalate via the notification service."""
    from services import simplified_stock_engine_service as ses

    jid = _seed_open_row(
        fresh_journal_db,
        symbol="REJECTED",
        strategy="trending_equity_intraday",
    )

    svc = _make_svc_mock()
    with _patched_engine_service(svc), patch(
        "services.place_order_service.place_order",
        return_value=(False, {"message": "insufficient_margin"}, 400),
    ), patch(
        "services.notification_service.get_notification_service"
    ) as mock_ns:
        result = ses.flatten_strategy_positions("trending_equity_intraday")

    assert result["attempted"] == 1
    assert result["succeeded"] == 0
    assert len(result["failed"]) == 1
    assert result["failed"][0]["symbol"] == "REJECTED"

    # Row must remain open so the next watchdog pass (or operator) can retry.
    row = (
        fresh_journal_db.db_session.query(fresh_journal_db.TradeJournal)
        .filter_by(id=jid)
        .first()
    )
    assert row.exited_at is None

    mock_ns.return_value.publish_eod_watchdog_failure.assert_called_once()


def test_flatten_strategy_positions_handles_no_api_key(fresh_journal_db):
    """When no api_key can be resolved, all open positions are reported as failed."""
    from services import simplified_stock_engine_service as ses

    _seed_open_row(
        fresh_journal_db,
        symbol="STRANDED",
        strategy="trending_equity_intraday",
    )

    # Empty per-symbol map AND empty user_api_keys, AND auth_db helper returns None.
    svc = _make_svc_mock(api_key=None)
    with _patched_engine_service(svc), patch(
        "database.auth_db.get_first_available_api_key", return_value=None
    ), patch("services.place_order_service.place_order") as mock_po, patch(
        "services.notification_service.get_notification_service"
    ) as mock_ns:
        result = ses.flatten_strategy_positions("trending_equity_intraday")

    mock_po.assert_not_called()
    assert result["succeeded"] == 0
    assert len(result["failed"]) == 1
    assert result["failed"][0]["error"] == "no_api_key"
    mock_ns.return_value.publish_eod_watchdog_failure.assert_called_once()


# ---------------------------------------------------------------------------
# Rehydrate
# ---------------------------------------------------------------------------


def _make_real_service():
    """Build a real SimplifiedStockEngineService for the rehydrate tests."""
    from services.simplified_stock_engine_core import MODE_DISABLED, SimplifiedEngineConfig
    from services.simplified_stock_engine_service import SimplifiedStockEngineService

    return SimplifiedStockEngineService(config=SimplifiedEngineConfig(mode=MODE_DISABLED))


def test_rehydrate_restores_long_position_from_today_row(fresh_journal_db):
    svc = _make_real_service()
    _seed_open_row(
        fresh_journal_db,
        symbol="NBCC",
        strategy="trending_equity_intraday",
        qty=500,
        entry_price=104.94,
        direction="LONG",
    )

    added = svc.rehydrate_positions_from_journal()
    assert added == 1
    pos = svc.engine.positions["NBCC"]
    assert pos.qty == 500  # LONG -> positive
    assert pos.entry_price == 104.94


def test_rehydrate_restores_short_with_negative_qty(fresh_journal_db):
    svc = _make_real_service()
    _seed_open_row(
        fresh_journal_db,
        symbol="HCLTECH",
        strategy="trending_equity_intraday",
        qty=50,
        entry_price=1500.0,
        direction="SHORT",
    )

    svc.rehydrate_positions_from_journal()
    pos = svc.engine.positions["HCLTECH"]
    assert pos.qty == -50  # SHORT -> negative


def test_rehydrate_skips_already_known_symbol(fresh_journal_db):
    """The engine's in-memory state wins over the journal — a position
    already loaded must not be overwritten."""
    from services.simplified_stock_engine_core import Position

    svc = _make_real_service()
    svc.engine.positions["NBCC"] = Position(
        symbol="NBCC",
        entry_price=100.0,
        qty=300,
        stop_loss=98.0,
        entry_time=dt.datetime.now(),
        risk_per_share=2.0,
    )
    _seed_open_row(
        fresh_journal_db,
        symbol="NBCC",
        strategy="trending_equity_intraday",
        qty=500,
        entry_price=104.94,
    )

    added = svc.rehydrate_positions_from_journal()
    assert added == 0
    # Untouched.
    assert svc.engine.positions["NBCC"].qty == 300
    assert svc.engine.positions["NBCC"].entry_price == 100.0


def test_rehydrate_skips_rows_without_entry_price(fresh_journal_db):
    """A row whose entry fill never confirmed (entry_price IS NULL) is
    unusable — the engine has no reference price to size a stop against."""
    svc = _make_real_service()
    now = dt.datetime.now(IST)
    row = fresh_journal_db.TradeJournal(
        placed_at=now.isoformat(),
        symbol="GHOST",
        direction="LONG",
        quantity=100,
        strategy_name="trending_equity_intraday",
        signal_source="chartink",
        entry_price=None,
        entry_order_id="never-filled",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    fresh_journal_db.db_session.add(row)
    fresh_journal_db.db_session.commit()

    added = svc.rehydrate_positions_from_journal()
    assert added == 0
    assert "GHOST" not in svc.engine.positions


def test_rehydrate_ignores_other_strategies(fresh_journal_db):
    svc = _make_real_service()
    _seed_open_row(
        fresh_journal_db,
        symbol="MINE",
        strategy="trending_equity_intraday",
    )
    _seed_open_row(
        fresh_journal_db,
        symbol="THEIRS",
        strategy="some_other_strategy",
    )

    svc.rehydrate_positions_from_journal()
    assert "MINE" in svc.engine.positions
    assert "THEIRS" not in svc.engine.positions


def test_rehydrate_ignores_yesterdays_rows(fresh_journal_db):
    svc = _make_real_service()
    _seed_open_row(
        fresh_journal_db,
        symbol="YESTERDAY",
        strategy="trending_equity_intraday",
        days_ago=1,
    )

    added = svc.rehydrate_positions_from_journal()
    assert added == 0
    assert "YESTERDAY" not in svc.engine.positions
