"""Tests for the MVP backtest data layer (backtest_runs, backtest_trades).

Uses an in-memory SQLite engine and monkeypatches the backtest_db module's
``engine`` and ``db_session`` so each test starts from a clean slate.
"""

import json
import time

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_backtest_db(monkeypatch):
    """Point database.backtest_db at a fresh in-memory SQLite for one test."""
    from database import backtest_db as bdb

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=test_engine))

    monkeypatch.setattr(bdb, "engine", test_engine)
    monkeypatch.setattr(bdb, "db_session", test_session)

    # Tables are bound to bdb.Base.metadata; create them on the patched engine.
    bdb.Base.metadata.create_all(bind=test_engine)

    yield bdb

    test_session.remove()
    test_engine.dispose()


def test_init_creates_tables(fresh_backtest_db):
    from services import backtest_service

    backtest_service.init_backtest_db()

    inspector = inspect(fresh_backtest_db.engine)
    tables = set(inspector.get_table_names())
    assert "backtest_runs" in tables
    assert "backtest_trades" in tables

    # Idempotent.
    backtest_service.init_backtest_db()
    tables_after = set(inspect(fresh_backtest_db.engine).get_table_names())
    assert tables == tables_after


def test_create_run_and_get(fresh_backtest_db):
    from services import backtest_service

    run_id = backtest_service.create_run(
        strategy_name="trending_equity_intraday",
        rule_names=["fno_intraday_buy_chartink"],
        symbols=["SBIN", "INFY"],
        from_date="2026-01-01",
        to_date="2026-01-15",
        interval="5m",
        config={"atr_sl_mult": 1.5, "position_size": 500},
    )
    assert run_id > 0

    row = backtest_service.get_run(run_id)
    assert row["strategy_name"] == "trending_equity_intraday"
    assert row["status"] == "running"
    assert row["from_date"] == "2026-01-01"
    assert row["to_date"] == "2026-01-15"
    assert row["interval"] == "5m"
    assert json.loads(row["rule_names"]) == ["fno_intraday_buy_chartink"]
    assert json.loads(row["symbols"]) == ["SBIN", "INFY"]
    cfg = json.loads(row["config"])
    assert cfg["atr_sl_mult"] == 1.5
    assert cfg["position_size"] == 500


def test_record_and_close_trade_then_finalize(fresh_backtest_db):
    from services import backtest_service

    run_id = backtest_service.create_run(
        strategy_name="test_strat",
        rule_names=["r1"],
        symbols=["SBIN"],
        from_date="2026-01-01",
        to_date="2026-01-02",
        interval="5m",
        config={},
    )

    # Three trades: +100, -50, +75 → gross 125, 2 winners, 1 loser.
    trade_specs = [
        ("LONG", 600.0, 602.0, 50, "target"),  # +100
        ("LONG", 605.0, 604.0, 50, "stop_loss"),  # -50
        ("LONG", 610.0, 611.5, 50, "target"),  # +75
    ]
    for direction, entry, exit_p, qty, reason in trade_specs:
        tid = backtest_service.record_trade(
            run_id=run_id,
            symbol="SBIN",
            direction=direction,
            entry_at="2026-01-01T09:30:00+05:30",
            entry_price=entry,
            entry_reason="r1",
            quantity=qty,
            atr_at_entry=1.0,
            sl_price=entry - 1.0,
            target_price=entry + 1.5,
        )
        assert tid > 0
        pnl = (exit_p - entry) * qty
        backtest_service.close_trade(
            tid,
            exit_at="2026-01-01T10:00:00+05:30",
            exit_price=exit_p,
            exit_reason=reason,
            pnl=pnl,
            pnl_pct=pnl / (entry * qty),
            hold_duration_seconds=1800,
        )

    metrics = backtest_service.finalize_run(run_id)
    assert metrics["total_trades"] == 3
    assert metrics["winners"] == 2
    assert metrics["losers"] == 1
    assert metrics["gross_pnl"] == pytest.approx(125.0)
    assert metrics["win_rate"] == pytest.approx(2 / 3, rel=1e-4)

    # Row should be marked completed with the same metrics.
    row = backtest_service.get_run(run_id)
    assert row["status"] == "completed"
    assert row["total_trades"] == 3
    assert row["winners"] == 2
    assert row["losers"] == 1
    assert row["gross_pnl"] == pytest.approx(125.0)
    assert row["completed_at"] is not None


def test_record_trade_allows_null_target_price(fresh_backtest_db):
    """Trailing-stop strategies may leave target_price NULL."""
    from services import backtest_service

    run_id = backtest_service.create_run(
        strategy_name="trail",
        rule_names=["r"],
        symbols=["X"],
        from_date="2026-01-01",
        to_date="2026-01-01",
        interval="5m",
        config={},
    )
    tid = backtest_service.record_trade(
        run_id=run_id,
        symbol="X",
        direction="LONG",
        entry_at="2026-01-01T09:30:00+05:30",
        entry_price=100.0,
        entry_reason="r",
        quantity=10,
        atr_at_entry=2.0,
        sl_price=98.0,
        target_price=None,
    )
    assert tid > 0
    trades = backtest_service.get_run_trades(run_id)
    assert len(trades) == 1
    assert trades[0]["target_price"] is None
    assert trades[0]["sl_price"] == pytest.approx(98.0)


def test_get_recent_runs_orders_by_started_desc(fresh_backtest_db):
    from services import backtest_service

    ids = []
    for name in ("first", "second", "third"):
        ids.append(
            backtest_service.create_run(
                strategy_name=name,
                rule_names=[],
                symbols=[],
                from_date="2026-01-01",
                to_date="2026-01-01",
                interval="5m",
                config={},
            )
        )
        time.sleep(0.01)

    recent = backtest_service.get_recent_runs(limit=5)
    assert [r["strategy_name"] for r in recent] == ["third", "second", "first"]


def test_finalize_run_zero_trades(fresh_backtest_db):
    """Zero-trade run must produce 0 win_rate (no division by zero)."""
    from services import backtest_service

    run_id = backtest_service.create_run(
        strategy_name="empty",
        rule_names=[],
        symbols=[],
        from_date="2026-01-01",
        to_date="2026-01-01",
        interval="5m",
        config={},
    )
    metrics = backtest_service.finalize_run(run_id)
    assert metrics == {
        "total_trades": 0,
        "winners": 0,
        "losers": 0,
        "gross_pnl": 0.0,
        "win_rate": 0.0,
        "max_drawdown": 0.0,
    }
    row = backtest_service.get_run(run_id)
    assert row["status"] == "completed"


def test_finalize_run_max_drawdown(fresh_backtest_db):
    """Trades [+100, -50, -75, +200] → peak 100, trough -25 → drawdown 125."""
    from services import backtest_service

    run_id = backtest_service.create_run(
        strategy_name="dd",
        rule_names=[],
        symbols=["X"],
        from_date="2026-01-01",
        to_date="2026-01-01",
        interval="5m",
        config={},
    )

    pnls = [100.0, -50.0, -75.0, 200.0]
    for p in pnls:
        tid = backtest_service.record_trade(
            run_id=run_id,
            symbol="X",
            direction="LONG",
            entry_at="2026-01-01T09:30:00+05:30",
            entry_price=100.0,
            entry_reason="r",
            quantity=1,
            atr_at_entry=None,
            sl_price=None,
            target_price=None,
        )
        backtest_service.close_trade(
            tid,
            exit_at="2026-01-01T10:00:00+05:30",
            exit_price=100.0 + p,
            exit_reason="target" if p > 0 else "stop_loss",
            pnl=p,
            pnl_pct=p / 100.0,
            hold_duration_seconds=1800,
        )

    metrics = backtest_service.finalize_run(run_id)
    assert metrics["total_trades"] == 4
    assert metrics["winners"] == 2
    assert metrics["losers"] == 2
    assert metrics["gross_pnl"] == pytest.approx(175.0)
    # Peak after trade 1 = 100, running min after trade 3 = -25, drawdown = 125.
    assert metrics["max_drawdown"] == pytest.approx(125.0)


def test_update_run_status_records_error(fresh_backtest_db):
    from services import backtest_service

    run_id = backtest_service.create_run(
        strategy_name="will_error",
        rule_names=[],
        symbols=[],
        from_date="2026-01-01",
        to_date="2026-01-01",
        interval="5m",
        config={},
    )
    backtest_service.update_run_status(run_id, "error", error_message="get_history blew up")
    row = backtest_service.get_run(run_id)
    assert row["status"] == "error"
    assert row["error_message"] == "get_history blew up"
    assert row["completed_at"] is not None


def test_get_run_unknown_id_returns_empty(fresh_backtest_db):
    from services import backtest_service

    assert backtest_service.get_run(0) == {}
    assert backtest_service.get_run(99999) == {}
    assert backtest_service.get_run_trades(99999) == []


def test_close_trade_unknown_id_is_silent(fresh_backtest_db):
    from services import backtest_service

    # Should not raise.
    backtest_service.close_trade(
        0,
        exit_at="2026-01-01T10:00:00+05:30",
        exit_price=100.0,
        exit_reason="target",
        pnl=10.0,
        pnl_pct=0.1,
        hold_duration_seconds=60,
    )
    backtest_service.close_trade(
        99999,
        exit_at="2026-01-01T10:00:00+05:30",
        exit_price=100.0,
        exit_reason="target",
        pnl=10.0,
        pnl_pct=0.1,
        hold_duration_seconds=60,
    )
