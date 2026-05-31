"""MVP backtester service.

This module owns three concerns:

* DB helpers over ``backtest_runs`` / ``backtest_trades`` — every helper is
  fail-safe (logs on error, returns a sentinel rather than raising).
* Summary metrics (``finalize_run``) — total trades, winners/losers, gross
  P&L, win rate, peak-to-trough drawdown on the cumulative P&L curve.
* The replay loop and simulated execution (``run_backtest``) — added in a
  later commit.

The simulator runs in PARALLEL to the live engine and writes to
``backtest_*`` tables only. It must never touch ``trade_journal``,
``daily_intent``, or any live state.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

import pytz

from database.backtest_db import (
    BacktestRun,
    BacktestTrade,
    _now_iso,
    _run_to_dict,
    _trade_to_dict,
)
from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")


def _session():
    """Resolve the live session from the DB module on each call.

    Tests monkeypatch the module-level ``db_session``; binding at import time
    would freeze the original session and skip the patch.
    """
    from database import backtest_db as bdb

    return bdb.db_session


def init_backtest_db() -> None:
    """Idempotent table creation. Thin wrapper around the DB module's init."""
    from database import backtest_db as bdb

    bdb.init_db()


# ---------------------------------------------------------------------------
# Write path — fail-safe helpers.
# ---------------------------------------------------------------------------


def create_run(
    strategy_name: str,
    rule_names: list[str],
    symbols: list[str],
    from_date: str,
    to_date: str,
    interval: str,
    config: dict[str, Any],
) -> int:
    """Insert a fresh ``backtest_runs`` row with ``status='running'``.

    Returns the new row id, or ``0`` on DB failure.
    """
    sess = _session()
    try:
        row = BacktestRun(
            started_at=_now_iso(),
            strategy_name=strategy_name,
            rule_names=json.dumps(list(rule_names or [])),
            symbols=json.dumps(list(symbols or [])),
            from_date=from_date,
            to_date=to_date,
            interval=interval,
            config=json.dumps(config or {}, default=str),
            status="running",
        )
        sess.add(row)
        sess.commit()
        return int(row.id)
    except Exception as e:
        logger.warning("backtest_service.create_run failed: %s", e)
        try:
            sess.rollback()
        except Exception:
            pass
        return 0
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def update_run_status(
    run_id: int,
    status: str,
    error_message: str | None = None,
) -> None:
    """Set ``status`` (and optional ``error_message``) on a run row. Silent no-op on failure."""
    if not run_id or run_id <= 0:
        return
    sess = _session()
    try:
        row = sess.query(BacktestRun).filter_by(id=run_id).first()
        if row is None:
            logger.warning("backtest_service.update_run_status: run_id=%s not found", run_id)
            return
        row.status = status
        if error_message is not None:
            row.error_message = error_message
        if status in ("completed", "error"):
            row.completed_at = _now_iso()
        sess.commit()
    except Exception as e:
        logger.warning("backtest_service.update_run_status failed (id=%s): %s", run_id, e)
        try:
            sess.rollback()
        except Exception:
            pass
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def record_trade(
    run_id: int,
    symbol: str,
    direction: str,
    entry_at: str,
    entry_price: float,
    entry_reason: str,
    quantity: int,
    atr_at_entry: float | None,
    sl_price: float | None,
    target_price: float | None = None,
) -> int:
    """Insert a ``backtest_trades`` row representing an open position.

    ``target_price`` is optional — trailing-stop strategies leave it None.
    Returns the new row id, or ``0`` on DB failure.
    """
    sess = _session()
    try:
        row = BacktestTrade(
            run_id=int(run_id),
            symbol=symbol,
            direction=direction,
            entry_at=entry_at,
            entry_price=float(entry_price),
            entry_reason=entry_reason,
            quantity=int(quantity),
            atr_at_entry=float(atr_at_entry) if atr_at_entry is not None else None,
            sl_price=float(sl_price) if sl_price is not None else None,
            target_price=float(target_price) if target_price is not None else None,
        )
        sess.add(row)
        sess.commit()
        return int(row.id)
    except Exception as e:
        logger.warning("backtest_service.record_trade failed: %s", e)
        try:
            sess.rollback()
        except Exception:
            pass
        return 0
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def close_trade(
    trade_id: int,
    exit_at: str,
    exit_price: float,
    exit_reason: str,
    pnl: float,
    pnl_pct: float,
    hold_duration_seconds: int,
) -> None:
    """Finalise a trade row with exit details + outcome. Silent no-op on failure."""
    if not trade_id or trade_id <= 0:
        return
    sess = _session()
    try:
        row = sess.query(BacktestTrade).filter_by(id=trade_id).first()
        if row is None:
            logger.warning("backtest_service.close_trade: trade_id=%s not found", trade_id)
            return
        row.exit_at = exit_at
        row.exit_price = float(exit_price)
        row.exit_reason = exit_reason
        row.pnl = float(pnl)
        row.pnl_pct = float(pnl_pct)
        row.hold_duration_seconds = int(hold_duration_seconds)
        sess.commit()
    except Exception as e:
        logger.warning("backtest_service.close_trade failed (id=%s): %s", trade_id, e)
        try:
            sess.rollback()
        except Exception:
            pass
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def finalize_run(run_id: int) -> dict[str, Any]:
    """Compute summary metrics from ``backtest_trades`` and stamp the run row.

    Returns ``{total_trades, winners, losers, gross_pnl, win_rate, max_drawdown}``.
    Trades that never closed (``pnl IS NULL``) are excluded from the metrics
    but still counted in ``total_trades``. Status is bumped to ``completed``.
    Returns the empty-shape dict on DB failure (and does not raise).
    """
    empty = {
        "total_trades": 0,
        "winners": 0,
        "losers": 0,
        "gross_pnl": 0.0,
        "win_rate": 0.0,
        "max_drawdown": 0.0,
    }
    if not run_id or run_id <= 0:
        return empty

    sess = _session()
    try:
        trades = (
            sess.query(BacktestTrade)
            .filter_by(run_id=int(run_id))
            .order_by(BacktestTrade.id.asc())
            .all()
        )

        total = len(trades)
        winners = sum(1 for t in trades if (t.pnl or 0.0) > 0)
        losers = sum(1 for t in trades if (t.pnl or 0.0) < 0)
        gross = sum(float(t.pnl or 0.0) for t in trades)

        # Win rate over CLOSED trades; an open trade with NULL pnl shouldn't
        # be counted as a loser. Falls back to 0.0 when nothing has closed.
        closed = winners + losers
        win_rate = (winners / closed) if closed > 0 else 0.0

        # Max peak-to-trough drawdown on the cumulative P&L curve.
        # Drawdown is the magnitude of the worst peak-trough fall; reported
        # as a positive number (0.0 when the curve never retreats).
        running = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            running += float(t.pnl or 0.0)
            if running > peak:
                peak = running
            dd = peak - running
            if dd > max_dd:
                max_dd = dd

        row = sess.query(BacktestRun).filter_by(id=int(run_id)).first()
        if row is not None:
            row.total_trades = total
            row.winners = winners
            row.losers = losers
            row.gross_pnl = round(gross, 4)
            row.win_rate = round(win_rate, 6)
            row.max_drawdown = round(max_dd, 4)
            row.status = "completed"
            row.completed_at = _now_iso()
            sess.commit()

        return {
            "total_trades": total,
            "winners": winners,
            "losers": losers,
            "gross_pnl": round(gross, 4),
            "win_rate": round(win_rate, 6),
            "max_drawdown": round(max_dd, 4),
        }
    except Exception as e:
        logger.warning("backtest_service.finalize_run failed (id=%s): %s", run_id, e)
        try:
            sess.rollback()
        except Exception:
            pass
        return empty
    finally:
        try:
            sess.remove()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Read path — fail-safe to empty containers.
# ---------------------------------------------------------------------------


def get_run(run_id: int) -> dict[str, Any]:
    """Return the run row as a dict, or ``{}`` if not found / on DB failure."""
    if not run_id or run_id <= 0:
        return {}
    sess = _session()
    try:
        row = sess.query(BacktestRun).filter_by(id=int(run_id)).first()
        return _run_to_dict(row) if row else {}
    except Exception as e:
        logger.warning("backtest_service.get_run failed (id=%s): %s", run_id, e)
        return {}
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def get_run_trades(run_id: int) -> list[dict[str, Any]]:
    """Return all trades for a run, ordered by id. ``[]`` on failure."""
    if not run_id or run_id <= 0:
        return []
    sess = _session()
    try:
        rows = (
            sess.query(BacktestTrade)
            .filter_by(run_id=int(run_id))
            .order_by(BacktestTrade.id.asc())
            .all()
        )
        return [_trade_to_dict(r) for r in rows]
    except Exception as e:
        logger.warning("backtest_service.get_run_trades failed (id=%s): %s", run_id, e)
        return []
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def get_recent_runs(limit: int = 10) -> list[dict[str, Any]]:
    """Return recent runs ordered by ``started_at DESC``. ``[]`` on failure."""
    sess = _session()
    try:
        rows = (
            sess.query(BacktestRun)
            .order_by(BacktestRun.started_at.desc())
            .limit(max(int(limit), 1))
            .all()
        )
        return [_run_to_dict(r) for r in rows]
    except Exception as e:
        logger.warning("backtest_service.get_recent_runs failed: %s", e)
        return []
    finally:
        try:
            sess.remove()
        except Exception:
            pass
