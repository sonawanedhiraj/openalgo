"""Control/observability API for the open15_vol_breakout strategy (issue #425).

GET /open15_vol_breakout/api/status
    Live state: mode, day status (armed / skipped_late_boot / done), today's
    selection with gaps, entries, and positions. Session auth (same as the
    other strategy blueprints); read-only.

GET /open15_vol_breakout/api/trades?limit=N&date=YYYY-MM-DD
    Journal rows (research fields included: level / trigger second / trigger
    price / entry-minute close) — the captured-drift measurement data.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

open15_bp = Blueprint("open15_bp", __name__, url_prefix="/open15_vol_breakout")


@open15_bp.route("/api/status", methods=["GET"])
@check_session_validity
def status():
    from services.open15_breakout_service import get_open15_service

    svc = get_open15_service()
    if svc is None:
        return jsonify(
            {
                "strategy": "open15_vol_breakout",
                "enabled": False,
                "message": "service not initialized (OPEN15_ENABLED=false?)",
            }
        )
    return jsonify(svc.get_status())


@open15_bp.route("/api/trades", methods=["GET"])
@check_session_validity
def trades():
    from database.open15_breakout_db import Open15Trade, db_session

    limit = min(int(request.args.get("limit", 100)), 500)
    date = request.args.get("date")
    try:
        q = db_session.query(Open15Trade)
        if date:
            q = q.filter(Open15Trade.trade_date == date)
        rows = q.order_by(Open15Trade.id.desc()).limit(limit).all()
        return jsonify(
            [
                {
                    "id": r.id,
                    "trade_date": r.trade_date,
                    "symbol": r.symbol,
                    "side": r.side,
                    "mode": r.mode,
                    "gap_pct": r.gap_pct,
                    "level": r.level,
                    "baseline_vol": r.baseline_vol,
                    "cum_vol_at_trigger": r.cum_vol_at_trigger,
                    "trigger_minute": r.trigger_minute,
                    "trigger_second": r.trigger_second,
                    "trigger_price": r.trigger_price,
                    "entry_minute_close": r.entry_minute_close,
                    "quantity": r.quantity,
                    "entry_status": r.entry_status,
                    "exit_ts": r.exit_ts,
                    "exit_price": r.exit_price,
                    "exit_status": r.exit_status,
                    "pnl": r.pnl,
                    "status": r.status,
                    "reason": r.reason,
                }
                for r in rows
            ]
        )
    except Exception:
        logger.exception("open15: trades query failed")
        return jsonify([]), 500
    finally:
        db_session.remove()
