"""Fail-safe trade journal writes.

Every public helper here wraps its DB work in a try/except that logs but
never raises. Trade execution must not break because a journal write
failed — audit loss is recoverable, a missed order or a half-filled
position with no exit is not.

Sentinel return values:

* ``record_entry`` returns the new row id, or ``0`` on DB failure. Callers
  can pass the returned id unconditionally to ``update_entry_fill`` /
  ``record_exit``; both no-op silently on id ``<= 0``.
* Read helpers return ``[]`` / ``{}`` on DB failure rather than raising.
  Reflection / inspection callers prefer empty results to a crash.
"""

import datetime as dt
import json
from typing import Any

import pytz

from database.trade_journal_db import TradeJournal, _now_iso, _row_to_dict
from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")


def _session():
    """Resolve the live session from the DB module on each call.

    Tests monkeypatch the module-level ``db_session``; binding at import time
    would freeze the original session and skip the patch.
    """
    from database import trade_journal_db as tjdb

    return tjdb.db_session


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError) as e:
        logger.warning("trade_journal: failed to JSON-encode field: %s", e)
        return None


# ---------------------------------------------------------------------------
# Write path — every function below MUST be fail-safe.
# ---------------------------------------------------------------------------


def record_entry(
    *,
    symbol: str,
    direction: str,
    quantity: int,
    strategy_name: str,
    signal_source: str,
    entry_price: float | None = None,
    entry_order_id: str | None = None,
    signal_decision_id: int | None = None,
    scan_cycle_id: int | None = None,
    regime_snapshot: dict[str, Any] | None = None,
    nifty_pct: float | None = None,
    india_vix: float | None = None,
    notes: str | dict[str, Any] | None = None,
) -> int:
    """Insert a partial row at order placement. Returns the new row id, or 0
    on DB failure. ``placed_at``, ``created_at``, ``updated_at`` are all set
    to "now" in IST.
    """
    sess = _session()
    try:
        now_iso = _now_iso()
        notes_json: str | None
        if isinstance(notes, dict):
            notes_json = _json_or_none(notes)
        else:
            notes_json = notes if isinstance(notes, str) else None

        row = TradeJournal(
            placed_at=now_iso,
            symbol=symbol,
            direction=direction,
            quantity=int(quantity),
            strategy_name=strategy_name,
            signal_source=signal_source,
            signal_decision_id=signal_decision_id,
            scan_cycle_id=scan_cycle_id,
            entry_price=entry_price,
            entry_order_id=entry_order_id,
            regime_snapshot=_json_or_none(regime_snapshot),
            nifty_pct_at_entry=nifty_pct,
            india_vix_at_entry=india_vix,
            notes=notes_json,
            created_at=now_iso,
            updated_at=now_iso,
        )
        sess.add(row)
        sess.commit()
        return int(row.id)
    except Exception as e:
        logger.warning("trade_journal.record_entry failed: %s", e)
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


def update_entry_fill(
    journal_id: int,
    entry_price: float | None = None,
    entry_fill_at: str | None = None,
) -> None:
    """Patch the entry-fill columns after the broker reports a fill.

    Silently no-ops on ``journal_id <= 0`` (the record_entry sentinel) or on
    DB failure.
    """
    if not journal_id or journal_id <= 0:
        return

    sess = _session()
    try:
        row = sess.query(TradeJournal).filter_by(id=journal_id).first()
        if row is None:
            logger.warning(
                "trade_journal.update_entry_fill: journal_id=%s not found", journal_id
            )
            return
        if entry_price is not None:
            row.entry_price = float(entry_price)
        row.entry_fill_at = entry_fill_at or _now_iso()
        row.updated_at = _now_iso()
        sess.commit()
    except Exception as e:
        logger.warning(
            "trade_journal.update_entry_fill failed (id=%s): %s", journal_id, e
        )
        try:
            sess.rollback()
        except Exception:
            pass
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def record_exit(
    journal_id: int,
    *,
    exit_price: float | None = None,
    exit_order_id: str | None = None,
    exit_reason: str | None = None,
    exited_at: str | None = None,
    pnl: float | None = None,
    pnl_pct: float | None = None,
    hold_duration_seconds: int | None = None,
) -> None:
    """Finalise the row with exit fill + outcome columns.

    If ``pnl`` / ``pnl_pct`` / ``hold_duration_seconds`` are not supplied,
    they are derived from the existing entry columns on the row when both
    sides are present. Silently no-ops on ``journal_id <= 0`` or DB failure.
    """
    if not journal_id or journal_id <= 0:
        return

    sess = _session()
    try:
        row = sess.query(TradeJournal).filter_by(id=journal_id).first()
        if row is None:
            logger.warning(
                "trade_journal.record_exit: journal_id=%s not found", journal_id
            )
            return

        now_iso = _now_iso()
        row.exited_at = exited_at or now_iso
        if exit_price is not None:
            row.exit_price = float(exit_price)
        if exit_order_id is not None:
            row.exit_order_id = exit_order_id
        if exit_reason is not None:
            row.exit_reason = exit_reason

        # Derive outcome columns when the caller didn't pass them.
        if (
            pnl is None
            and row.entry_price is not None
            and row.exit_price is not None
            and row.quantity
        ):
            if (row.direction or "").upper() == "SHORT":
                pnl = (float(row.entry_price) - float(row.exit_price)) * int(row.quantity)
            else:
                pnl = (float(row.exit_price) - float(row.entry_price)) * int(row.quantity)
        if pnl is not None:
            row.pnl = float(pnl)

        if (
            pnl_pct is None
            and pnl is not None
            and row.entry_price
            and row.quantity
        ):
            denom = float(row.entry_price) * abs(int(row.quantity))
            if denom > 0:
                pnl_pct = float(pnl) / denom
        if pnl_pct is not None:
            row.pnl_pct = float(pnl_pct)

        if hold_duration_seconds is None and row.placed_at and row.exited_at:
            try:
                placed = dt.datetime.fromisoformat(row.placed_at)
                exited = dt.datetime.fromisoformat(row.exited_at)
                hold_duration_seconds = int((exited - placed).total_seconds())
            except (TypeError, ValueError):
                hold_duration_seconds = None
        if hold_duration_seconds is not None:
            row.hold_duration_seconds = int(hold_duration_seconds)

        row.updated_at = now_iso
        sess.commit()
    except Exception as e:
        logger.warning("trade_journal.record_exit failed (id=%s): %s", journal_id, e)
        try:
            sess.rollback()
        except Exception:
            pass
    finally:
        try:
            sess.remove()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Read path — fail-safe to empty containers so callers can render an empty
# dashboard rather than crash.
# ---------------------------------------------------------------------------


def get_recent_trades(hours: int = 24) -> list[dict]:
    """Return journal rows with ``placed_at`` within the last ``hours``,
    newest first. Returns ``[]`` on DB failure.
    """
    cutoff = (dt.datetime.now(IST) - dt.timedelta(hours=hours)).isoformat()

    sess = _session()
    try:
        rows = (
            sess.query(TradeJournal)
            .filter(TradeJournal.placed_at >= cutoff)
            .order_by(TradeJournal.placed_at.desc())
            .all()
        )
        return [_row_to_dict(r) for r in rows]
    except Exception as e:
        logger.warning("trade_journal.get_recent_trades failed: %s", e)
        return []
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def get_trades_for_symbol(symbol: str, days: int = 7) -> list[dict]:
    """Return journal rows for ``symbol`` placed in the last ``days``. Newest first."""
    cutoff = (dt.datetime.now(IST) - dt.timedelta(days=days)).isoformat()

    sess = _session()
    try:
        rows = (
            sess.query(TradeJournal)
            .filter(TradeJournal.symbol == symbol)
            .filter(TradeJournal.placed_at >= cutoff)
            .order_by(TradeJournal.placed_at.desc())
            .all()
        )
        return [_row_to_dict(r) for r in rows]
    except Exception as e:
        logger.warning("trade_journal.get_trades_for_symbol failed: %s", e)
        return []
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def get_open_trades_today(strategy_name: str | None = None) -> list[dict]:
    """Return open journal rows entered today (IST), optionally filtered by
    ``strategy_name``.

    "Open" means ``exited_at IS NULL``; "today" is the calendar date in IST.
    Newest first. Returns ``[]`` on DB failure.

    Used by:

    * :func:`services.eod_watchdog_service` — to flatten orphaned intraday
      positions at the strategy's EOD cut-off, even when the broker tick
      stream is dead and the engine's tick-driven exit can't fire.
    * :meth:`SimplifiedStockEngineService.rehydrate_positions_from_journal` —
      to restore the in-memory ``positions`` dict on engine startup so a
      mid-day restart doesn't make the engine forget what the broker holds.
    """
    today_iso = dt.datetime.now(IST).date().isoformat()

    sess = _session()
    try:
        q = (
            sess.query(TradeJournal)
            .filter(TradeJournal.exited_at.is_(None))
            .filter(TradeJournal.placed_at >= today_iso)
        )
        if strategy_name:
            q = q.filter(TradeJournal.strategy_name == strategy_name)
        rows = q.order_by(TradeJournal.placed_at.desc()).all()
        return [_row_to_dict(r) for r in rows]
    except Exception as e:
        logger.warning("trade_journal.get_open_trades_today failed: %s", e)
        return []
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def get_open_journal_id_for_symbol(symbol: str) -> int | None:
    """Returns the journal id of the most recent open entry on ``symbol``
    (i.e. ``exited_at IS NULL``). Used by the engine at exit time to find
    the row to close out.

    Returns ``None`` when there is no open entry, or on DB failure (so the
    engine treats it the same as "nothing to update" — fail-safe).
    """
    sess = _session()
    try:
        row = (
            sess.query(TradeJournal)
            .filter(TradeJournal.symbol == symbol)
            .filter(TradeJournal.exited_at.is_(None))
            .order_by(TradeJournal.placed_at.desc())
            .first()
        )
        return int(row.id) if row else None
    except Exception as e:
        logger.warning(
            "trade_journal.get_open_journal_id_for_symbol failed: %s", e
        )
        return None
    finally:
        try:
            sess.remove()
        except Exception:
            pass


def get_today_summary() -> dict:
    """Aggregate today's closed-out journal rows.

    Returns a dict shaped::

        {
            "count": <int>,                # total trades closed today
            "total_pnl": <float>,
            "winners": <int>,              # rows with pnl > 0
            "losers": <int>,               # rows with pnl < 0
            "by_strategy": {strat: {"count": n, "pnl": x}, ...},
            "by_exit_reason": {reason: {"count": n, "pnl": x}, ...},
        }

    Returns the empty shape on DB failure.
    """
    empty = {
        "count": 0,
        "total_pnl": 0.0,
        "winners": 0,
        "losers": 0,
        "by_strategy": {},
        "by_exit_reason": {},
    }

    today = dt.datetime.now(IST).date().isoformat()

    sess = _session()
    try:
        rows = (
            sess.query(TradeJournal)
            .filter(TradeJournal.exited_at.isnot(None))
            .filter(TradeJournal.exited_at >= today)
            .all()
        )
    except Exception as e:
        logger.warning("trade_journal.get_today_summary failed: %s", e)
        return empty
    finally:
        try:
            sess.remove()
        except Exception:
            pass

    out = {
        "count": 0,
        "total_pnl": 0.0,
        "winners": 0,
        "losers": 0,
        "by_strategy": {},
        "by_exit_reason": {},
    }
    for row in rows:
        out["count"] += 1
        pnl = float(row.pnl or 0.0)
        out["total_pnl"] += pnl
        if pnl > 0:
            out["winners"] += 1
        elif pnl < 0:
            out["losers"] += 1

        strat = row.strategy_name or "unknown"
        bucket = out["by_strategy"].setdefault(strat, {"count": 0, "pnl": 0.0})
        bucket["count"] += 1
        bucket["pnl"] += pnl

        reason = row.exit_reason or "unknown"
        rbucket = out["by_exit_reason"].setdefault(reason, {"count": 0, "pnl": 0.0})
        rbucket["count"] += 1
        rbucket["pnl"] += pnl

    out["total_pnl"] = round(out["total_pnl"], 2)
    for bucket in out["by_strategy"].values():
        bucket["pnl"] = round(bucket["pnl"], 2)
    for bucket in out["by_exit_reason"].values():
        bucket["pnl"] = round(bucket["pnl"], 2)
    return out
